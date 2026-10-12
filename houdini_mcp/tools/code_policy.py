"""Static policy gates for ``execute_code``.

Every gate here reads the program's AST, which is the same grammar the server's
``exec`` will compile a moment later - so what the gate sees is what runs, and
text inside comments or string literals is not code.

Two kinds of rail live here, and they fail in opposite directions on purpose:

* ``read-only`` is an **allowlist** [LAW:parse-dont-validate]. The program may
  touch only names this module recognizes as inspection; anything else - a
  known mutator, a hou API added next release, a dynamic attribute lookup - is
  rejected because it is unrecognized, not because it was enumerated.
* ``normal`` is a **denylist** against accidents. It refuses the operations
  that destroy work (process exit, scene wipe, file deletion, shell and
  network access, dynamic execution) and admits everything else. It is a
  guard rail, not a sandbox; a caller who needs a guarantee asks for
  ``read-only``.

Callers parse the code once through :func:`parse_code` and reject what does not
parse; the detectors below assume a program that parses.
"""

from __future__ import annotations

import ast

__all__ = [
    "parse_code",
    "_detect_dangerous_code",
    "_detect_heavy_geometry_code",
    "_detect_import_hou",
    "_detect_non_inspection_code",
]


def parse_code(code: str) -> ast.Module:
    """Parse user code; raises ``SyntaxError`` so the caller can refuse loudly.

    [LAW:single-enforcer] The decision about unparseable code is made once, by
    the caller of this function. The detectors re-parse for convenience but
    never decide.
    """
    return ast.parse(code)


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


def _top_module(dotted: str) -> str:
    return dotted.partition(".")[0]


# =============================================================================
# normal policy: dangerous operations (denylist)
# =============================================================================

# Module rails: a module listed here admits only the members named. An empty set
# refuses the module outright, including its import. Access is judged by the
# root identifier (``os.remove``) whether or not an import statement is present,
# so a program that merely names the member is refused, not just one that imports it.
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
    # Refused outright.
    "subprocess": frozenset(),
    "shutil": frozenset(),
    "pathlib": frozenset(),
    "importlib": frozenset(),
    "builtins": frozenset(),
    "socket": frozenset(),
    "requests": frozenset(),
    "urllib": frozenset(),
    "http": frozenset(),
    "ftplib": frozenset(),
    "pty": frozenset(),
    "ctypes": frozenset(),
    "multiprocessing": frozenset(),
    "signal": frozenset(),
    "pickle": frozenset(),
    "marshal": frozenset(),
    "shelve": frozenset(),
    "code": frozenset(),
    "runpy": frozenset(),
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


def _rails_for_root(root: str, aliases: dict[str, str]) -> tuple[str, frozenset[str]] | None:
    """Resolve an identifier to the module it names, if that module has rails."""
    module = aliases.get(root, root)
    rails = MODULE_RAILS.get(module)
    return None if rails is None else (module, rails)


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local identifiers to the railed module they stand for.

    ``import os as o`` and the one-step ``o = os`` both bind ``o``; judging
    ``o.remove`` by its module keeps a rename from changing the verdict.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_module(alias.name)
                if top in MODULE_RAILS and alias.asname:
                    aliases[alias.asname] = top
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id in MODULE_RAILS
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = node.value.id
    return aliases


def _open_mode_is_writing(call: ast.Call) -> str | None:
    """Describe why an ``open()`` call writes, or None when it only reads."""
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


def _detect_dangerous_code(code: str) -> list[str]:
    """Operations the ``normal`` policy refuses without an explicit, configured bypass."""
    tree = parse_code(code)
    aliases = _module_aliases(tree)
    detected: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_module(alias.name)
                rails = MODULE_RAILS.get(top)
                if rails is None:
                    continue
                if not rails:
                    detected.append(f"{top} - blocked module")
                elif "." in alias.name and alias.name.split(".")[1] not in rails:
                    detected.append(f"{alias.name} - restricted member of {top}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = _top_module(node.module)
            rails = MODULE_RAILS.get(top)
            if rails is None:
                continue
            if not rails:
                detected.append(f"{top} - blocked module")
            elif "." in node.module:
                sub = node.module.split(".")[1]
                if sub not in rails:
                    detected.append(f"{top}.{sub} - restricted member of {top}")
            else:
                detected.extend(
                    f"{top}.{alias.name} - restricted member of {top}"
                    for alias in node.names
                    if alias.name not in rails
                )
        elif isinstance(node, ast.Attribute):
            chain = _dotted_chain(node)
            if chain is None:
                continue
            dotted = ".".join(chain)
            if dotted in DANGEROUS_HOU_PATHS:
                detected.append(DANGEROUS_HOU_PATHS[dotted])
            railed = _rails_for_root(chain[0], aliases)
            # Only the first hop below a railed module is judged: ``os.path.join``
            # is admitted by ``path``.
            if railed is not None and len(chain) == 2:
                module, rails = railed
                if chain[1] not in rails:
                    what = "blocked module" if not rails else f"restricted member of {module}"
                    detected.append(f"{module}.{chain[1]} - {what}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in DYNAMIC_EXECUTION_NAMES:
                detected.append(f"{node.id} - dynamic code execution")
            elif node.id in MODULE_RAILS and not MODULE_RAILS[node.id]:
                detected.append(f"{node.id} - blocked module")
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


def _detect_heavy_geometry_code(code: str) -> list[str]:
    """Method calls that can cook or iterate very large SOP data.

    Not destructive, but in production scenes they block the UI, drop the RPC
    connection, or crash Houdini. Dedicated summary tools run bounded analysis
    on the Houdini side instead.
    """
    detected = [
        HEAVY_GEOMETRY_METHODS[node.func.attr]
        for node in ast.walk(parse_code(code))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in HEAVY_GEOMETRY_METHODS
    ]
    return list(dict.fromkeys(detected))


def _detect_import_hou(code: str) -> bool:
    """``import hou`` / ``from hou import`` - hou is pre-injected, so this always fails."""
    for node in ast.walk(parse_code(code)):
        if isinstance(node, ast.Import) and any(a.name == "hou" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "hou":
            return True
    return False


# =============================================================================
# read-only policy: inspection allowlist
# =============================================================================

# Pure stdlib the inspection profile may import. ``operator`` is absent on
# purpose: attrgetter/methodcaller reach attributes by string.
READ_ONLY_MODULES: frozenset[str] = frozenset(
    {
        "json",
        "math",
        "re",
        "collections",
        "itertools",
        "functools",
        "statistics",
        "string",
        "textwrap",
        "pprint",
        "datetime",
        "time",
        "fnmatch",
        "typing",
        "dataclasses",
        "enum",
        "decimal",
        "fractions",
        "heapq",
        "bisect",
        "copy",
        "random",
        "abc",
        "numbers",
        "difflib",
        "base64",
    }
)

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
        "expandString",
        "expandStringAtFrame",
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


def _bound_names(tree: ast.Module) -> set[str]:
    """Identifiers the program binds itself: assignments, defs, args, handlers, patterns."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Local identifier -> top-level module, for every import statement."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or _top_module(alias.name)] = _top_module(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = _top_module(node.module)
    return aliases


def _attribute_admitted(attr: str, root_module: str | None) -> bool:
    if attr.startswith("__"):
        return False
    if root_module is not None and root_module in READ_ONLY_MODULES:
        return True
    return attr in READ_ONLY_ATTRIBUTES or attr[:1].isupper()


def _detect_non_inspection_code(code: str) -> list[str]:
    """Everything the ``read-only`` policy refuses: names it does not recognize as inspection.

    The program may bind its own names, import the pure modules in
    :data:`READ_ONLY_MODULES`, name the builtins in :data:`READ_ONLY_BUILTINS`,
    and read the attributes in :data:`READ_ONLY_ATTRIBUTES`. Anything else is
    reported, so a mutator this module has never heard of is refused the same
    way as one it has.
    """
    tree = parse_code(code)
    aliases = _import_aliases(tree)
    bound = _bound_names(tree)
    detected: list[str] = []

    # An imported module name that the program also rebinds no longer names
    # the module; refuse the ambiguity rather than guess.
    for name in sorted(set(aliases) & bound):
        detected.append(f"{name} - imported name is rebound")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            detected.extend(
                f"import {m} - module is not in the read-only allowlist"
                for m in modules
                if _top_module(m) not in READ_ONLY_MODULES
            )
        elif isinstance(node, ast.With | ast.AsyncWith):
            detected.append("with statement - context managers may mutate")
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.ctx, ast.Load):
                detected.append(f"{node.attr} - attribute assignment mutates")
                continue
            chain = _dotted_chain(node)
            root_module = aliases.get(chain[0]) if chain else None
            if not _attribute_admitted(node.attr, root_module):
                detected.append(f"{node.attr}() - not in the read-only inspection allowlist")
        elif isinstance(node, ast.MatchClass):
            detected.extend(
                f"{attr} - not in the read-only inspection allowlist"
                for attr in node.kwd_attrs
                if not _attribute_admitted(attr, None)
            )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            allowed = (
                node.id == "hou"
                or node.id in bound
                or node.id in aliases
                or node.id in READ_ONLY_BUILTINS
            )
            if not allowed or node.id.startswith("__"):
                detected.append(f"{node.id} - name is not in the read-only allowlist")

    return list(dict.fromkeys(detected))
