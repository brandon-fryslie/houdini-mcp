"""Static policy gates for ``execute_code``.

Every gate here reads the program's AST, which is the same grammar the server's
``exec`` will compile a moment later - so what the gate sees is what runs, and
text inside comments or string literals is not code.

Two kinds of rail live here, and they fail in opposite directions on purpose:

* ``read-only`` is an **allowlist** [LAW:parse-dont-validate]. The program may
  touch only names this module recognizes as inspection; anything else - a
  known mutator, a hou API added next release, a dynamic attribute lookup, a
  private member of an admitted module - is rejected because it is
  unrecognized, not because it was enumerated.
* ``normal`` is a **denylist** against accidents. It refuses the operations
  that destroy work (process exit, scene wipe, file deletion, shell and
  network access, dynamic execution) and admits everything else. It is a
  guard rail, not a sandbox; a caller who needs a guarantee asks for
  ``read-only``.

The caller parses the program once through :func:`parse_code` and hands the
tree to every detector [LAW:single-enforcer]: the decision about unparseable
code is made in one place, and the detectors are pure functions of the tree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = [
    "parse_code",
    "_detect_dangerous_code",
    "_detect_heavy_geometry_code",
    "_detect_import_hou",
    "_detect_non_inspection_code",
]


def parse_code(code: str) -> ast.Module:
    """Parse user code; raises ``SyntaxError`` so the caller can refuse loudly."""
    try:
        return ast.parse(code)
    except ValueError as exc:
        # Early 3.11 releases report a null byte as ValueError; it is still
        # a program the gates cannot read [LAW:no-silent-failure].
        raise SyntaxError(str(exc)) from exc


def _dotted_chain(node: ast.expr) -> list[str] | None:
    """``hou.hipFile.clear`` -> ``["hou", "hipFile", "clear"]``; None if not rooted at a Name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


@dataclass(frozen=True)
class _Names:
    """What the program's identifiers stand for, read once from the tree.

    ``aliases`` maps an identifier to the dotted path it stands for:
    ``import os.path as p`` and ``from os import path as p`` both bind ``p`` to
    ``("os", "path")``, the pre-injected ``hou`` stands for itself, and the
    one-step ``q = p.x`` binds ``q`` to ``("os", "path", "x")``, so a rename
    never changes a verdict [LAW:one-source-of-truth]. ``bound``
    holds every other identifier the program binds itself - assignments, defs,
    args, handlers, patterns. A name in both is an import the program rebound.
    """

    aliases: dict[str, tuple[str, ...]]
    bound: frozenset[str]

    def resolve(self, chain: list[str]) -> tuple[tuple[str, ...], bool]:
        """Dotted path a chain names, and whether that path is rooted at an import."""
        alias = self.aliases.get(chain[0])
        if alias is None:
            return tuple(chain), False
        return alias + tuple(chain[1:]), True

    def is_local(self, identifier: str) -> bool:
        """A name the program binds and never imports is the program's own, not a module."""
        return identifier in self.bound and identifier not in self.aliases


def _names(tree: ast.Module) -> _Names:
    aliases: dict[str, tuple[str, ...]] = {"hou": ("hou",)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = tuple(alias.name.split("."))
                aliases[alias.asname or path[0]] = path if alias.asname else path[:1]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (*node.module.split("."), alias.name)

    # Assignments alias to a fixpoint: ``x = h`` may precede ``h = hou`` in
    # walk order, and neither source order nor nesting changes what x names.
    aliased_targets: set[int] = set()
    assigns = [
        (node, chain)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and (chain := _dotted_chain(node.value)) is not None
    ]
    grew = True
    while grew:
        grew = False
        for node, chain in assigns:
            if chain[0] not in aliases:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and id(target) not in aliased_targets:
                    aliases[target.id] = aliases[chain[0]] + tuple(chain[1:])
                    aliased_targets.add(id(target))
                    grew = True

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if id(node) not in aliased_targets:
                bound.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return _Names(aliases=aliases, bound=frozenset(bound))


# =============================================================================
# normal policy: dangerous operations (denylist)
# =============================================================================

# Module rails: a module listed here admits only the members named. An empty set
# refuses the module outright, including its import. Access is judged by the
# path an identifier resolves to (``os.remove``, ``osp.join`` -> ``os.path.join``)
# whether or not an import statement is present, so a program that merely names
# the member is refused, not just one that imports it. Only the first hop below
# the module is judged: ``os.path.join`` is admitted by ``path``.
MODULE_RAILS: dict[str, frozenset[str]] = {
    # File-system state changers live in os; the read side stays open.
    "os": frozenset(
        {
            "path",
            "environ",
            "getenv",
            "getcwd",
            "listdir",
            "scandir",
            "walk",
            "stat",
            "access",
            "sep",
            "pathsep",
            "linesep",
            "altsep",
            "extsep",
            "curdir",
            "pardir",
            "devnull",
            "name",
            "cpu_count",
            "getpid",
            "getlogin",
            "uname",
            "fspath",
            "fsencode",
            "fsdecode",
        }
    ),
    # sys exposes the interpreter's module table and exit; keep the read side.
    "sys": frozenset(
        {
            "version",
            "version_info",
            "hexversion",
            "api_version",
            "implementation",
            "platform",
            "byteorder",
            "maxsize",
            "float_info",
            "int_info",
            "flags",
            "path",
            "executable",
            "argv",
            "prefix",
            "exec_prefix",
            "stdout",
            "stderr",
            "getrecursionlimit",
            "getsizeof",
            "getdefaultencoding",
            "getfilesystemencoding",
        }
    ),
    "io": frozenset({"StringIO", "BytesIO"}),
    "codecs": frozenset({"encode", "decode", "lookup", "getencoder", "getdecoder"}),
    "tempfile": frozenset(
        {"gettempdir", "gettempdirb", "gettempprefix", "gettempprefixb", "tempdir"}
    ),
    # Refused outright: process and shell, file-system and archive writers,
    # databases, dynamic import, serialization that executes, network egress,
    # and the debugger (which parks the server on stdin).
    "subprocess": frozenset(),
    "shutil": frozenset(),
    "pathlib": frozenset(),
    "importlib": frozenset(),
    "builtins": frozenset(),
    "zipfile": frozenset(),
    "tarfile": frozenset(),
    "gzip": frozenset(),
    "bz2": frozenset(),
    "lzma": frozenset(),
    "sqlite3": frozenset(),
    "dbm": frozenset(),
    "socket": frozenset(),
    "requests": frozenset(),
    "urllib": frozenset(),
    "urllib3": frozenset(),
    "httpx": frozenset(),
    "aiohttp": frozenset(),
    "http": frozenset(),
    "ftplib": frozenset(),
    "smtplib": frozenset(),
    "xmlrpc": frozenset(),
    "telnetlib": frozenset(),
    "webbrowser": frozenset(),
    "pty": frozenset(),
    "ctypes": frozenset(),
    "multiprocessing": frozenset(),
    "signal": frozenset(),
    "pickle": frozenset(),
    "marshal": frozenset(),
    "shelve": frozenset(),
    "code": frozenset(),
    "runpy": frozenset(),
    "pdb": frozenset(),
}

# hou calls that destroy the session or the scene under any policy but privileged.
DANGEROUS_HOU_PATHS: dict[str, str] = {
    "hou.exit": "hou.exit() - will close Houdini",
    "hou.hipFile.clear": "hou.hipFile.clear() - scene wipe",
}

# Dynamic-execution builtins: a name the program evaluates or imports at runtime
# is a name no static gate can read.
DYNAMIC_EXECUTION_NAMES: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "__builtins__", "breakpoint"}
)

# open() modes that create or modify a file.
_WRITING_MODE_CHARS = frozenset("wax+")


def _rail_refusal(path: tuple[str, ...]) -> str | None:
    """Why the rails refuse a resolved dotted path, or None when they admit it."""
    rails = MODULE_RAILS.get(path[0])
    if rails is None:
        return None
    if not rails:
        return f"{'.'.join(path[:2])} - blocked module"
    if len(path) > 1 and path[1] not in rails:
        return f"{path[0]}.{path[1]} - restricted member of {path[0]}"
    # An admitted member may itself be a module (``os.path.os``) or expose its
    # internals through dunders; below the first hop, a railed name or a
    # private name is refused.
    for hop in path[2:]:
        if hop in MODULE_RAILS or hop.startswith("_"):
            return f"{'.'.join(path)} - reaches {hop} through {path[0]}.{path[1]}"
    return None


def _open_mode_is_writing(call: ast.Call) -> str | None:
    """Describe why an ``open()`` call writes, or None when it only reads."""
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        kw.arg is None for kw in call.keywords
    ):
        # Unpacked arguments can carry a mode the gate cannot read [LAW:no-silent-failure].
        return "open() with unpacked arguments - file writing cannot be ruled out"
    mode: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if _WRITING_MODE_CHARS & set(mode.value):
            return f"open() with mode {mode.value!r} - file writing"
        return None
    # A mode the gate cannot read is a mode the gate refuses [LAW:no-silent-failure].
    return "open() with non-literal mode - file writing cannot be ruled out"


def _detect_dangerous_code(tree: ast.Module) -> list[str]:
    """Operations the ``normal`` policy refuses without an explicit, configured bypass."""
    names = _names(tree)
    detected: list[str] = []
    # ``open`` is judged at its call, where the mode is; any other reference to
    # it (``o = open``, ``partial(open, ...)``) is a mode the gate cannot read.
    open_callees = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    def judge(chain: list[str]) -> None:
        # A name the program binds itself is its own variable, not the module
        # that happens to share its spelling (``code = parm.eval()``).
        if names.is_local(chain[0]):
            return
        path, _ = names.resolve(chain)
        dotted = ".".join(path)
        if dotted in DANGEROUS_HOU_PATHS:
            detected.append(DANGEROUS_HOU_PATHS[dotted])
        if why := _rail_refusal(path):
            detected.append(why)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if why := _rail_refusal(tuple(alias.name.split("."))):
                    detected.append(why)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if why := _rail_refusal((*node.module.split("."), alias.name)):
                    detected.append(why)
        elif isinstance(node, ast.Attribute):
            chain = _dotted_chain(node)
            if chain is not None:
                judge(chain)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if names.is_local(node.id):
                continue
            if node.id in DYNAMIC_EXECUTION_NAMES and node.id not in names.aliases:
                detected.append(f"{node.id} - dynamic code execution")
            elif node.id == "open" and id(node) not in open_callees:
                detected.append("open referenced without a call - file writing cannot be ruled out")
            else:
                judge([node.id])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and (why := _open_mode_is_writing(node))
        ):
            detected.append(why)

    return list(dict.fromkeys(detected))


# =============================================================================
# heavy geometry (all policies)
# =============================================================================

HEAVY_GEOMETRY_METHODS: dict[str, str] = {
    "geometry": "node.geometry() - can force a heavy SOP cook and destabilize Houdini",
    "points": "geo.points() - can materialize/iterate large point arrays",
    "prims": "geo.prims() - can materialize/iterate large primitive arrays",
    "vertices": "geo.vertices() - can materialize/iterate large vertex arrays",
    "iterPoints": "geo.iterPoints() - can iterate large point arrays",
    "iterPrims": "geo.iterPrims() - can iterate large primitive arrays",
}


def _detect_heavy_geometry_code(tree: ast.Module) -> list[str]:
    """Methods that can cook or iterate very large SOP data.

    Not destructive, but in production scenes they block the UI, drop the RPC
    connection, or crash Houdini. Dedicated summary tools run bounded analysis
    on the Houdini side instead. The attribute is judged, not the call, so
    binding the method or passing it to ``map`` is the same as calling it.
    """
    detected = [
        HEAVY_GEOMETRY_METHODS[node.attr]
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and node.attr in HEAVY_GEOMETRY_METHODS
    ]
    return list(dict.fromkeys(detected))


def _detect_import_hou(tree: ast.Module) -> bool:
    """``import hou`` / ``from hou import`` - hou is pre-injected, so this always fails."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "hou" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "hou":
            return True
    return False


# =============================================================================
# read-only policy: inspection allowlist
# =============================================================================

# Pure stdlib the inspection profile may import, each with the members it may
# name. A module's attributes are not admitted wholesale: ``random._os`` and
# ``typing.sys`` are real attributes of pure modules, so the first hop below a
# module is judged against its member list and every private name is refused.
# Capitalized members (classes, constants, flags) are admitted by shape and
# need not be listed. ``operator`` is absent on purpose: attrgetter and
# methodcaller reach attributes by string. ``time.sleep`` and ``random.seed``
# are absent: one parks the RPC thread, the other mutates interpreter state.
READ_ONLY_MODULES: dict[str, frozenset[str]] = {
    "json": frozenset({"dumps", "loads"}),
    "math": frozenset(
        {
            "sqrt",
            "floor",
            "ceil",
            "fabs",
            "pow",
            "exp",
            "log",
            "log2",
            "log10",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "atan2",
            "degrees",
            "radians",
            "hypot",
            "dist",
            "isclose",
            "isfinite",
            "isinf",
            "isnan",
            "fsum",
            "prod",
            "gcd",
            "lcm",
            "trunc",
            "copysign",
            "fmod",
            "modf",
            "comb",
            "perm",
            "factorial",
            "pi",
            "e",
            "tau",
            "inf",
            "nan",
        }
    ),
    "re": frozenset(
        {
            "compile",
            "match",
            "search",
            "fullmatch",
            "findall",
            "finditer",
            "sub",
            "subn",
            "split",
            "escape",
        }
    ),
    "collections": frozenset({"namedtuple", "defaultdict", "deque", "abc"}),
    "itertools": frozenset(
        {
            "chain",
            "count",
            "cycle",
            "repeat",
            "accumulate",
            "groupby",
            "islice",
            "starmap",
            "takewhile",
            "dropwhile",
            "zip_longest",
            "product",
            "permutations",
            "combinations",
            "combinations_with_replacement",
            "compress",
            "filterfalse",
            "tee",
            "pairwise",
            "batched",
        }
    ),
    "functools": frozenset(
        {
            "reduce",
            "partial",
            "partialmethod",
            "lru_cache",
            "cache",
            "cmp_to_key",
            "wraps",
            "total_ordering",
            "singledispatch",
        }
    ),
    "statistics": frozenset(
        {
            "mean",
            "fmean",
            "geometric_mean",
            "harmonic_mean",
            "median",
            "median_low",
            "median_high",
            "mode",
            "multimode",
            "stdev",
            "pstdev",
            "variance",
            "pvariance",
            "quantiles",
        }
    ),
    "string": frozenset(
        {
            "ascii_letters",
            "ascii_lowercase",
            "ascii_uppercase",
            "digits",
            "hexdigits",
            "octdigits",
            "punctuation",
            "printable",
            "whitespace",
            "capwords",
        }
    ),
    "textwrap": frozenset({"wrap", "fill", "dedent", "indent", "shorten"}),
    "pprint": frozenset({"pprint", "pformat", "pp", "isreadable", "isrecursive", "saferepr"}),
    "datetime": frozenset({"datetime", "date", "time", "timedelta", "timezone", "tzinfo"}),
    "time": frozenset(
        {
            "time",
            "time_ns",
            "monotonic",
            "monotonic_ns",
            "perf_counter",
            "perf_counter_ns",
            "process_time",
            "strftime",
            "strptime",
            "gmtime",
            "localtime",
            "mktime",
            "ctime",
            "asctime",
        }
    ),
    "fnmatch": frozenset({"fnmatch", "fnmatchcase", "filter", "translate"}),
    # get_type_hints is absent: it evals string annotations.
    "typing": frozenset({"cast", "overload", "final", "get_args", "get_origin"}),
    "dataclasses": frozenset(
        {
            "dataclass",
            "field",
            "fields",
            "asdict",
            "astuple",
            "is_dataclass",
            "replace",
            "make_dataclass",
        }
    ),
    "enum": frozenset({"auto", "unique"}),
    "decimal": frozenset({"getcontext"}),
    "fractions": frozenset(),
    "heapq": frozenset(
        {
            "heappush",
            "heappop",
            "heapify",
            "heapreplace",
            "heappushpop",
            "nlargest",
            "nsmallest",
            "merge",
        }
    ),
    "bisect": frozenset(
        {"bisect", "bisect_left", "bisect_right", "insort", "insort_left", "insort_right"}
    ),
    "copy": frozenset({"copy", "deepcopy"}),
    "random": frozenset(
        {
            "random",
            "randint",
            "randrange",
            "choice",
            "choices",
            "sample",
            "shuffle",
            "uniform",
            "gauss",
            "triangular",
            "normalvariate",
        }
    ),
    "abc": frozenset({"abstractmethod"}),
    "numbers": frozenset(),
    "difflib": frozenset({"unified_diff", "context_diff", "ndiff", "get_close_matches", "restore"}),
    "base64": frozenset(
        {
            "b64encode",
            "b64decode",
            "urlsafe_b64encode",
            "urlsafe_b64decode",
            "standard_b64encode",
            "standard_b64decode",
            "b16encode",
            "b16decode",
            "b32encode",
            "b32decode",
        }
    ),
}

# Builtins the inspection profile may name. Anything that opens files, reaches
# attributes by string, or evaluates text is absent.
READ_ONLY_BUILTINS: frozenset[str] = frozenset(
    {
        "print",
        "len",
        "range",
        "enumerate",
        "sorted",
        "reversed",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "repr",
        "format",
        "isinstance",
        "issubclass",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "zip",
        "map",
        "filter",
        "abs",
        "round",
        "divmod",
        "hash",
        "ord",
        "chr",
        "hex",
        "bin",
        "oct",
        "slice",
        "iter",
        "next",
        "callable",
        "hasattr",
        "type",
        "dir",
        "object",
        "super",
        "property",
        "staticmethod",
        "classmethod",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "LookupError",
        "ArithmeticError",
        "ZeroDivisionError",
        "NameError",
        "StopIteration",
        "NotImplementedError",
        "AssertionError",
        "OSError",
        "pow",
    }
)

# Attribute names an inspection program may read on any object. Grouped by
# what they inspect; every name here returns information and changes nothing.
# Capitalized attributes (types, enum members, constants) are admitted by
# shape and need not be listed.
READ_ONLY_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # --- navigation ---
        "node",
        "nodes",
        "item",
        "items",
        "root",
        "pwd",
        "parent",
        "children",
        "allSubChildren",
        "allItems",
        "glob",
        "recursiveGlob",
        "path",
        "name",
        "digitsInName",
        "selectedNodes",
        "selectedItems",
        "displayNode",
        "renderNode",
        "creatorNode",
        "networkBoxes",
        "iterNetworkBoxes",
        "stickyNotes",
        "iterStickyNotes",
        "nodeBundles",
        "nodeBundle",
        "indirectInputs",
        # --- node type / definition ---
        "type",
        "nodeType",
        "nodeTypes",
        "category",
        "categories",
        "nameWithCategory",
        "childTypeCategory",
        "nodeTypeCategory",
        "nodeTypeCategories",
        "objNodeTypeCategory",
        "sopNodeTypeCategory",
        "ropNodeTypeCategory",
        "dopNodeTypeCategory",
        "lopNodeTypeCategory",
        "vopNodeTypeCategory",
        "chopNodeTypeCategory",
        "copNodeTypeCategory",
        "shopNodeTypeCategory",
        "topNodeTypeCategory",
        "managerNodeTypeCategory",
        "rootNodeTypeCategory",
        "description",
        "label",
        "help",
        "icon",
        "instances",
        "definition",
        "sourcePath",
        "libraryFilePath",
        "sections",
        "contents",
        "matchesCurrentDefinition",
        "isInsideLockedHDA",
        "isEditableInsideLockedHDA",
        "isLockedHDA",
        "isEditable",
        "isNetwork",
        "isSubNetwork",
        "hda",
        "definitionsInFile",
        # --- parameters (read side) ---
        "parms",
        "parm",
        "parmTuple",
        "parmTuples",
        "spareParms",
        "parmsInFolder",
        "parmTemplate",
        "parmTemplates",
        "parmTemplateGroup",
        "entries",
        "eval",
        "evalAsString",
        "evalAsStrings",
        "evalAsInt",
        "evalAsInts",
        "evalAsFloat",
        "evalAsFloats",
        "evalAsNode",
        "evalAsNodes",
        "evalAsRamp",
        "evalAtFrame",
        "evalAtTime",
        "evalAsStringAtFrame",
        "evalAsFloatAtFrame",
        "evalAsIntAtFrame",
        "evalParm",
        "evalParmTuple",
        "ch",
        "chs",
        "rawValue",
        "unexpandedString",
        "expression",
        "expressionLanguage",
        "isTimeDependent",
        "isDisabled",
        "isHidden",
        "isLocked",
        "isAtDefault",
        "isSpare",
        "keyframes",
        "keyframesInRange",
        "getReferencedParm",
        "parmsReferencingThis",
        "componentIndex",
        "tuple",
        "defaultValue",
        "menuItems",
        "menuLabels",
        "dataType",
        "numComponents",
        "folderType",
        # --- flags, state, appearance (read side) ---
        "isDisplayFlagSet",
        "isRenderFlagSet",
        "isTemplateFlagSet",
        "isSelectableTemplateFlagSet",
        "isHighlightFlagSet",
        "isGenericFlagSet",
        "isBypassed",
        "isSelected",
        "isCurrent",
        "color",
        "position",
        "size",
        "comment",
        "userData",
        "userDataDict",
        "cookCount",
        "needsToCook",
        "errors",
        "warnings",
        "messages",
        "creationTime",
        "modificationTime",
        # --- wiring (read side) ---
        "inputs",
        "outputs",
        "input",
        "inputConnections",
        "outputConnections",
        "inputConnectors",
        "outputConnectors",
        "inputNames",
        "inputName",
        "outputName",
        "inputLabel",
        "outputLabel",
        "inputLabels",
        "outputNames",
        "outputLabels",
        "inputIndex",
        "outputIndex",
        "inputItem",
        "outputItem",
        "inputNode",
        "outputNode",
        "isIndirect",
        "outputForViewFlag",
        # --- scene, time, application ---
        "hipFile",
        "basename",
        "hasUnsavedChanges",
        "isLoadingHipFile",
        "isShuttingDown",
        "isNewFile",
        "frame",
        "time",
        "fps",
        "timeToFrame",
        "frameToTime",
        "playbar",
        "frameRange",
        "playbackRange",
        "isPlaying",
        "applicationVersion",
        "applicationVersionString",
        "applicationName",
        "applicationCompilationDate",
        "applicationPlatformInfo",
        "isUIAvailable",
        "licenseCategory",
        "getenv",
        "homeHoudiniDirectory",
        "houdiniPath",
        "findFile",
        "findFiles",
        "findFilesWithExtension",
        "findDirectory",
        "findDirectories",
        "text",
        "encode",
        "decode",
        # --- UI (read side) ---
        "ui",
        "curDesktop",
        "currentNode",
        "desktops",
        "panes",
        "paneTabs",
        "paneTabOfType",
        "currentPaneTabs",
        "floatingPanels",
        "isCurrentTab",
        "pane",
        # --- enums ---
        "paneTabType",
        "severityType",
        "parmTemplateType",
        "parmData",
        "exprLanguage",
        "nodeFlag",
        "nodeTypeFilter",
        "menuType",
        "stringParmType",
        "fileType",
        "rampBasis",
        "rampParmType",
        "scriptLanguage",
        "licenseCategoryType",
        "nodeTypeSource",
        "parmCondType",
        "parmLook",
        "parmNamingScheme",
        "dataParmType",
        # --- math values ---
        "hmath",
        "clamp",
        "fit",
        "fit01",
        "degToRad",
        "radToDeg",
        "smooth",
        "rand",
        "x",
        "y",
        "z",
        "w",
        "length",
        "lengthSquared",
        "normalized",
        "dot",
        "cross",
        "distanceTo",
        "angleTo",
        "inverted",
        "transposed",
        "determinant",
        "extractTranslates",
        "extractRotates",
        "extractScales",
        "explode",
        "at",
        "asTuple",
        "asTupleOfTuples",
        "worldTransform",
        "localTransform",
        "parmTransform",
        "preTransform",
        "origin",
        # --- geometry (read side; the heavy-geometry rail bounds the cost) ---
        "geometry",
        "points",
        "prims",
        "vertices",
        "iterPoints",
        "iterPrims",
        "iterVertices",
        "point",
        "prim",
        "vertex",
        "number",
        "normal",
        "attribs",
        "pointAttribs",
        "primAttribs",
        "vertexAttribs",
        "globalAttribs",
        "findPointAttrib",
        "findPrimAttrib",
        "findVertexAttrib",
        "findGlobalAttrib",
        "findPointGroup",
        "findPrimGroup",
        "findVertexGroup",
        "findEdgeGroup",
        "pointGroups",
        "primGroups",
        "vertexGroups",
        "edgeGroups",
        "attribValue",
        "attribType",
        "qualifier",
        "isArrayType",
        "strings",
        "floatAttribValue",
        "intAttribValue",
        "stringAttribValue",
        "floatListAttribValue",
        "intListAttribValue",
        "stringListAttribValue",
        "pointFloatAttribValues",
        "pointIntAttribValues",
        "pointStringAttribValues",
        "primFloatAttribValues",
        "primIntAttribValues",
        "primStringAttribValues",
        "intrinsicValue",
        "intrinsicNames",
        "intrinsicValueDict",
        "intrinsicReadOnly",
        "boundingBox",
        "minvec",
        "maxvec",
        "center",
        "sizevec",
        "isValid",
        "isClosed",
        "isAlmostEqual",
        "numVertices",
        "numEdges",
        # --- Python collections and strings (local data) ---
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "index",
        "count",
        "sort",
        "reverse",
        "copy",
        "keys",
        "values",
        "get",
        "setdefault",
        "update",
        "popitem",
        "fromkeys",
        "add",
        "discard",
        "union",
        "intersection",
        "difference",
        "symmetric_difference",
        "issubset",
        "issuperset",
        "most_common",
        "elements",
        "from_iterable",
        "split",
        "rsplit",
        "splitlines",
        "join",
        "strip",
        "lstrip",
        "rstrip",
        "startswith",
        "endswith",
        "lower",
        "upper",
        "title",
        "capitalize",
        "replace",
        "find",
        "rfind",
        "partition",
        "rpartition",
        "zfill",
        "ljust",
        "rjust",
        "isdigit",
        "isalpha",
        "isnumeric",
        "isalnum",
        "isspace",
        # --- re / datetime results ---
        "match",
        "search",
        "fullmatch",
        "findall",
        "finditer",
        "sub",
        "subn",
        "group",
        "groups",
        "groupdict",
        "span",
        "start",
        "end",
        "pattern",
        "isoformat",
        "strftime",
        "strptime",
        "timestamp",
        "now",
        "today",
        "date",
        "total_seconds",
        "real",
        "imag",
        "is_integer",
        "bit_length",
    }
)


def _inspection_refusal(path: tuple[str, ...], rooted_at_import: bool) -> str | None:
    """First hop of a dotted path the inspection profile does not recognize, or None.

    The hop directly below an imported module is judged against that module's
    member list; every other hop against :data:`READ_ONLY_ATTRIBUTES`. A private
    name is refused at any depth, and a capitalized name is admitted by shape.
    """
    for depth, hop in enumerate(path[1:], start=1):
        members = READ_ONLY_MODULES.get(path[0]) if rooted_at_import and depth == 1 else None
        admitted = READ_ONLY_ATTRIBUTES if members is None else members
        if hop.startswith("_") or not (hop in admitted or hop[:1].isupper()):
            return hop
    return None


def _detect_non_inspection_code(tree: ast.Module) -> list[str]:
    """Everything the ``read-only`` policy refuses: names it does not recognize as inspection.

    The program may bind its own names, import the modules and members in
    :data:`READ_ONLY_MODULES`, name the builtins in :data:`READ_ONLY_BUILTINS`,
    and read the attributes in :data:`READ_ONLY_ATTRIBUTES`. Anything else is
    reported, so a mutator this module has never heard of is refused the same
    way as one it has.
    """
    names = _names(tree)
    detected: list[str] = []

    # An aliased name (``hou``, an import, ``hf = hou.hipFile``) that the
    # program also rebinds no longer names that thing; refuse the ambiguity.
    for name in sorted(set(names.aliases) & names.bound):
        detected.append(f"{name} - aliased name is rebound")

    def judge_import(path: tuple[str, ...]) -> None:
        if path[0] not in READ_ONLY_MODULES:
            detected.append(f"import {path[0]} - module is not in the read-only allowlist")
        elif hop := _inspection_refusal(path, rooted_at_import=True):
            detected.append(f"{hop} - not in the read-only inspection allowlist")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                judge_import(tuple(alias.name.split(".")))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                judge_import((*(node.module or "").split("."), alias.name))
        elif isinstance(node, ast.With | ast.AsyncWith):
            detected.append("with statement - context managers may mutate")
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.ctx, ast.Load):
                detected.append(f"{node.attr} - attribute assignment mutates")
                continue
            chain = _dotted_chain(node)
            path, rooted = names.resolve(chain) if chain else (("", node.attr), False)
            if hop := _inspection_refusal(path, rooted):
                detected.append(f"{hop} - not in the read-only inspection allowlist")
        elif isinstance(node, ast.MatchClass):
            detected.extend(
                f"{attr} - not in the read-only inspection allowlist"
                for attr in node.kwd_attrs
                if _inspection_refusal(("", attr), rooted_at_import=False)
            )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            allowed = (
                node.id in names.bound or node.id in names.aliases or node.id in READ_ONLY_BUILTINS
            )
            if not allowed or node.id.startswith("__"):
                detected.append(f"{node.id} - name is not in the read-only allowlist")

    return list(dict.fromkeys(detected))
