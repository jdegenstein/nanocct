"""Type stubs: nanobind's stubgen for every generated package module (run after the build, it imports the
extension), plus the generic NCollection container classes so that NCollection_Array1[gp_Pnt] type-checks.

    python -m generator.stubs
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .binders import BINDERS
from .parallel import jobs_from_env
from .parse import py_path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "nanocct"
GENERIC = ROOT / "generator" / "stubs"

_SCALARS = {"double": "float", "int": "int", "bool": "bool", "std::string": "str",
            # the C++ scalars without a Python type: the marker keys of nanocct/_templates.py, aliases of float/int in the
            # NCollection stub -- NCollection_HArray1[float32] is NCollection_HArray1[float] statically
            "float": "nanocct.NCollection.float32", "unsigned char": "nanocct.NCollection.uchar",
            "unsigned int": "nanocct.NCollection.uint", "unsigned long": "nanocct.NCollection.ulong",
            "unsigned long long": "nanocct.NCollection.ulonglong"}
_MANIFEST: dict = json.loads((ROOT / "src" / "cpp" / "manifest.json").read_text())   # read once (was re-read per call)
_PATHS: dict[str, str] = _MANIFEST.get("paths", {})
_CLASSES: dict[str, str] = _MANIFEST["classes"]


def _type_arg(arg: str, classes: dict[str, str], templates: dict[str, dict]) -> str | None:
    """Stub spelling of a template argument: float for double, nanocct.<pkg>.X for a class or handle<X>,
    the bound class for a nested instantiation."""
    if arg in _SCALARS:
        return _SCALARS[arg]
    m = re.match(r"(?:opencascade::)?handle<(.+)>$", arg)
    if m is not None:
        arg = m.group(1)
    inst = templates.get(arg)                    # template instantiations first: their Python name is the alias
    if inst is not None and not inst.get("skipped", False):
        return f"nanocct.{inst['package']}.{inst['name']}"
    if arg in classes and "<" not in arg:
        return f"nanocct.{classes[arg]}.{py_path(arg, classes[arg], _PATHS)}"
    return None


def _generic_spelling(concrete: str, templates: dict[str, dict]) -> str:
    """nanocct.NCollection.NCollection_Map__int -> NCollection_Map[int] (what NCollection_Map[int] denotes statically)."""
    name = concrete.rsplit(".", 1)[-1]
    for key, inst in templates.items():
        if inst.get("name") == name:
            kind, args = re.match(r"([\w:]+)<(.*)>$", key).groups()
            if kind not in BINDERS:            # a 7c instantiation (NCollection_EBTree<int, Bnd_Box2d>): no generic class, the concrete one is the type
                return concrete
            return f"{kind}[{', '.join(_stub_arg(a, templates) for a in _split_args(args))}]"
    return concrete


def _generic_or_none(name: str, templates: dict[str, dict]) -> str | None:
    """NCollection_Map[int] for a concrete instantiation name whose every argument has a stub spelling; None when an
    argument is not bound (NCollection_Array1<BRepGraph_NodeId::Typed<...>>: the concrete class stays)."""
    for key, inst in templates.items():
        if inst.get("name") == name:
            kind, args = re.match(r"([\w:]+)<(.*)>$", key).groups()
            spelled = [_type_arg(a, _CLASSES, templates) for a in _split_args(args)]
            if any(sp is None for sp in spelled):
                return None
            return f"{kind}[{', '.join(spelled)}]"    # type: ignore[arg-type]
    return None


def _stub_arg(arg: str, templates: dict[str, dict]) -> str:
    return _type_arg(arg, _CLASSES, templates) or arg


def _split_args(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "<"
        depth -= ch == ">"
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def _unhashable_ignore(text: str) -> str:
    """R-UNHASHABLE: stubgen writes `__hash__: None = None` for the nb::none() attribute. mypy rejects that as an
    incompatible override of object.__hash__ -- and rejects `ClassVar[None]` and a `-> None` method just the same --
    so the line needs the suppression typeshed itself puts on every unhashable class in builtins.pyi. ty accepts all
    the spellings; keeping stubgen's own avoids having to add a typing import to every stub that has one."""
    return re.sub(r"^(\s*__hash__: None = None)$", r"\1  # type: ignore[assignment]", text, flags=re.M)


# nanobind's StubGen.expr_str renders an enum default by repr() -- it tests `int` before `enum.Enum`
# (nanobind 3.1.0 stubgen.py:1211 and :1225, unchanged on master) and OCCT's enums are IntEnum -- so a default of an enum
# from another module is a bare name that does not resolve there: `Continuity: nanocct.GeomAbs.GeomAbs_Shape =
# GeomAbs_Shape.GeomAbs_C2` (215 mypy errors). The parameter's own annotation names the enum qualified.
_ENUM_DEFAULT = re.compile(r"(: ([\w.]+) = )(\w+)\.(\w+)(?=[,)])")


def _qualified_enum_defaults(text: str) -> str:
    """`X: nanocct.GeomAbs.GeomAbs_Shape = GeomAbs_Shape.GeomAbs_C2` -> `... = nanocct.GeomAbs.GeomAbs_Shape.GeomAbs_C2`:
    a default spelled `Enum.Member` goes through its parameter's annotation when that ends in the same enum name."""
    def spell(m: re.Match) -> str:
        annotation, enum, member = m.group(2), m.group(3), m.group(4)
        if annotation != enum and annotation.rsplit(".", 1)[-1] == enum:
            return f"{m.group(1)}{annotation}.{member}"
        return m.group(0)
    return _ENUM_DEFAULT.sub(spell, text)


def _unshadowed_class_names(text: str, module: str) -> str:
    """stubgen writes a class of the stub's own module by its bare name, and inside a class body that name
    resolves to a member of the class if it has one -- `def ChangeEdgeCurve3DRep(self, ...) -> EdgeCurve3DRep` in
    BRepGraphInc_Storage, which also has a method EdgeCurve3DRep, is the method, not the module's class (mypy: "Function
    ... is not valid as a type"). Such a name in an annotation of that class body is spelled `<module>.<Name>`. A nested
    class does not see the enclosing class's members (Python scoping), so each body is checked against its own.
    The same holds for a builtin type: LDOM_SBuffer (bound on Windows only) has a method `str`, so its `xsputn(self, s: str,
    ...)` named the method (2026-09-30); such an annotation is spelled `builtins.<name>`, and the stub imports builtins."""
    tree = ast.parse(text)
    top = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    edits: list[tuple[int, int, int, str]] = []      # (line, start col, end col, prefix) of a bare name, UTF-8 offsets as ast gives them

    def visit(cls: ast.ClassDef) -> None:
        members = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)} \
            | {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)} \
            | {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        shadowed = members & (top | _BUILTIN_TYPES)
        for n in cls.body:
            annotations: list[ast.expr | None] = []
            if isinstance(n, ast.ClassDef):
                visit(n)
            elif isinstance(n, ast.FunctionDef):
                a = n.args
                annotations = [x.annotation for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x is not None]
                annotations.append(n.returns)
            elif isinstance(n, ast.AnnAssign):
                annotations = [n.annotation]
            for ann in annotations:
                if ann is None:
                    continue
                for node in ast.walk(ann):
                    if isinstance(node, ast.Name) and node.id in shadowed:
                        prefix = module if node.id in top else "builtins"
                        edits.append((node.lineno, node.col_offset, node.end_col_offset, prefix))   # type: ignore[arg-type]

    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            visit(n)
    if len(edits) == 0:
        return text
    lines = text.splitlines(keepends=True)
    for line, start, end, prefix in sorted(edits, reverse=True):
        raw = lines[line - 1].encode()
        lines[line - 1] = (raw[:start] + f"{prefix}.".encode() + raw[start:end] + raw[end:]).decode()
    out = "".join(lines)
    if any(e[3] == "builtins" for e in edits) and re.search(r"^import builtins$", out, re.M) is None:
        first = next(i for i, n in enumerate(tree.body) if isinstance(n, (ast.Import, ast.ImportFrom)))
        at = tree.body[first].lineno - 1                   # before the first import: no edit above it moved a line
        out_lines = out.splitlines(keepends=True)
        out = "".join(out_lines[:at] + ["import builtins\n"] + out_lines[at:])
    return out


# builtin types a stub annotation names; a class member of the same name shadows them in its class body
_BUILTIN_TYPES = {"str", "int", "float", "bool", "bytes", "object", "list", "tuple", "dict", "set", "type"}


def _eq_accepts_object(text: str) -> str:
    """`__eq__`/`__ne__` accept any object at runtime: nanobind returns NotImplemented for an argument no overload takes,
    so `TopoDS_Shape() == 1` is False, not a TypeError. stubgen types them with the C++ operand, which mypy and ty flag as an
    incompatible override of object.__eq__ (177 [override] errors, final review 2026-09-30). A single definition gets
    `object` for its operand; an overload set keeps its overloads -- they say which types compare by value, e.g.
    TCollection_AsciiString == str -- and gains a last one taking `object`."""
    tree = ast.parse(text)
    retype: list[tuple[int, int, int]] = []           # (line, start col, end col) of an operand annotation -> object
    append: list[tuple[int, str, str]] = []           # (after line, indent, name): a catch-all overload
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for name in ("__eq__", "__ne__"):
            defs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(defs) == 0:
                continue
            def operands(d: ast.FunctionDef) -> list[ast.arg]:
                return d.args.posonlyargs + d.args.args            # stubgen writes `(self, arg: T, /)`: positional-only
            overloaded = any(isinstance(d, ast.Name) and d.id == "overload" for d in defs[0].decorator_list)
            if not overloaded:
                args = operands(defs[0])
                if len(args) == 2 and args[1].annotation is not None and ast.unparse(args[1].annotation) != "object":
                    a = args[1].annotation
                    retype.append((a.lineno, a.col_offset, a.end_col_offset))   # type: ignore[arg-type]
            elif not any(len(operands(d)) == 2 and operands(d)[1].annotation is not None
                         and ast.unparse(operands(d)[1].annotation) == "object" for d in defs):
                append.append((max(d.end_lineno for d in defs), " " * defs[0].col_offset, name))   # type: ignore[arg-type]
    if len(retype) == 0 and len(append) == 0:
        return text
    lines = text.splitlines(keepends=True)
    for line, start, end in sorted(retype, reverse=True):
        raw = lines[line - 1].encode()
        lines[line - 1] = (raw[:start] + b"object" + raw[end:]).decode()
    for after, indent, name in sorted(append, reverse=True):
        lines[after:after] = [f"\n{indent}@overload\n{indent}def {name}(self, other: object) -> bool: ...\n"]
    return "".join(lines)


def _capsule_for_every_python(text: str) -> str:
    """nanobind's stubgen, run on Python 3.13+, spells a capsule `types.CapsuleType` (stubgen.py:805), which Python 3.12
    does not have -- mypy on 3.12 reports it undefined (IntPatch_InterferencePolyhedron::SetBVHSets). The wheel supports
    3.12, so the stub uses typing_extensions.CapsuleType, which type checkers know on every version."""
    if "types.CapsuleType" not in text:
        return text
    text = re.sub(r"\btypes\.CapsuleType\b", "typing_extensions.CapsuleType", text)
    if re.search(r"^import typing_extensions$", text, flags=re.M) is not None:
        return text
    # in place of `import types` when nothing else uses the module, else next to it
    keep = re.search(r"(?<![\w.])types\.", text) is not None
    return re.sub(r"^import types\n", "import types\nimport typing_extensions\n" if keep else "import typing_extensions\n",
                  text, count=1, flags=re.M)


def _module_of(stub: Path) -> str:
    """src/nanocct/BRepGraphInc/__init__.pyi -> nanocct.BRepGraphInc"""
    return ".".join(stub.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")


def _with_imports(text: str) -> str:
    """Add `import nanocct.<pkg>` for every nanocct.<pkg>.X reference the generic spellings introduced."""
    used = set(re.findall(r"\bnanocct\.(\w+)\.", text))
    imported = set(re.findall(r"^import nanocct\.(\w+)$", text, re.M)) | set(re.findall(r"^from nanocct\.(\w+) import", text, re.M))
    missing = sorted(used - imported)
    if len(missing) == 0:
        return text
    lines = text.splitlines(keepends=True)
    anchors = [i for i, line in enumerate(lines) if line.startswith("import nanocct.")]
    if len(anchors) > 0:
        at = anchors[-1] + 1
    else:                                     # after the module docstring (first line) and a blank
        at = 1
        while at < len(lines) and lines[at].strip() != "":
            at += 1
        at += 1
    lines[at:at] = [f"import nanocct.{m}\n" for m in missing]
    return "".join(lines)


def _with_numpy_imports(text: str) -> str:
    """Add the numpy imports a stub's text names and stubgen did not write, and `Annotated` to its typing import.

    nanobind's stubgen writes `from numpy.typing import NDArray` itself for the `__array__` members (R-VIEW). The
    `numpy.` test also matches docstring text (`numpy.asarray(pixmap)` in Image.pyi), for which `import numpy` is
    added although no annotation needs it.
    """
    add = []
    if re.search(r"\bNDArray\b", text) and "from numpy.typing import NDArray" not in text:
        add.append("from numpy.typing import NDArray\n")
    if re.search(r"\bnumpy\.", text) and re.search(r"^import numpy$", text, re.M) is None:
        add.insert(0, "import numpy\n")
    typing_import = re.search(r"^from typing import (.+)$", text, re.M)
    if "Annotated[" in text and typing_import is not None and "Annotated" not in typing_import.group(1).split(", "):
        names = sorted({*typing_import.group(1).split(", "), "Annotated"}, key=str.lower)
        text = text[:typing_import.start()] + "from typing import " + ", ".join(names) + text[typing_import.end():]
    if len(add) == 0:
        return text
    lines = text.splitlines(keepends=True)
    at = next((i for i, line in enumerate(lines) if line.startswith("import nanocct.")), None)
    if at is None:                            # no nanocct imports: after the docstring and its blank line
        at = 1
        while at < len(lines) and lines[at].strip() != "":
            at += 1
        at += 1
    else:                                     # nanobind's own place, with a blank line before the block
        add.append("\n")
    lines[at:at] = add
    return "".join(lines)


# the hashed containers whose generic stub carries OCCT's hasher as an optional last type parameter (_H)
_HASHED = ("NCollection_Map", "NCollection_IndexedMap", "NCollection_DataMap", "NCollection_IndexedDataMap")


def _view_return_type(text: str, name: str) -> str | None:
    """The return annotation of stubgen's `__array__` on a concrete container class, if it has one.

    The zero-copy view (R-VIEW) exists only for element types that are a packed run of numpy scalars, and its
    dtype is that scalar -- so it cannot live on the generic `NCollection_Array1(Generic[_T])` stub, which would
    promise it for an array of TopoDS_Shape too. Extracting the return annotation (`NDArray[numpy.float64]`, or
    `Annotated[NDArray[numpy.float64], dict(writable=False)]` for gp_Dir) lets the concrete class inherit `__array__`
    from `_NCollection_ArrayView[...]` without repeating the method body and docstring on every instantiation.
    """
    m = re.search(rf"^class {re.escape(name)}\b.*?(?=^\S|\Z)", text, re.S | re.M)
    if m is None:
        return None
    ret = re.search(r"^    def __array__\([^\n]*?\) -> ([^\n:]+):", m.group(0), re.M)
    return None if ret is None else ret.group(1).strip()


# Members the binder binds for some element types only (nanocct_ncollection.h: `if constexpr (std::is_class_v<T>)` for the
# element references, `has_equal<T>` for List::Contains), so the generic stub cannot promise them: they are lifted per
# instantiation through a stub-only mixin base (`_NCollection_*`) when stubgen's concrete class has the probe member
# (final review 2026-09-30: the generic stubs promised ChangeValue for NCollection_Array1[float], 7 of 220 lacked it).
_GATED = {"NCollection_Array1": ("ChangeValue", "_NCollection_Change1d"),
          "NCollection_Array2": ("ChangeValue", "_NCollection_Change2d"),
          "NCollection_DynamicArray": ("ChangeValue", "_NCollection_Change0d"),
          "NCollection_LinearVector": ("ChangeValue", "_NCollection_Change0d"),
          "NCollection_Sequence": ("ChangeValue", "_NCollection_Change1d"),
          "NCollection_List": ("Contains", "_NCollection_ListContains")}
# an H class inherits them at runtime, and stubgen does not repeat inherited members: they come from the sibling
_GATED_FROM = {"NCollection_HArray1": "NCollection_Array1", "NCollection_HArray2": "NCollection_Array2",
               "NCollection_HSequence": "NCollection_Sequence"}


def _has_member(text: str, name: str, member: str) -> bool:
    """Whether stubgen's top-level `class name` defines `def member(`."""
    m = re.search(rf"^class {re.escape(name)}\b.*?(?=^\S|\Z)", text, re.S | re.M)
    return m is not None and re.search(rf"^    def {re.escape(member)}\(", m.group(0), re
