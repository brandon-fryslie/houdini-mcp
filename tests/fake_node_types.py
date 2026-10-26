"""The fake hou's node types: the table Houdini wrote, and the reader that writes it.

[LAW:one-source-of-truth] ``fake_node_types.json`` is the one record of the node
types the fake has and of what createNode does with each in a network that makes
nodes. Its keys are the fake's choice: for each category, the types it holds, under
their own names, and for each type the parameter tuples a fresh node carries. Every
fact under them is Houdini's answer, read by ``read_node_types``: the refresh script
writes the file from it under hython, and a live test checks the file against it, so
the table stays what Houdini 22.0.466 says. That a locked asset and a node that is not
a network refuse every child, whatever the name, is the fake's own rule (``_holding`` in
tests/conftest.py), which the live name test checks.

Imported by hython as well as by the tests, so it imports nothing beyond the
standard library and ``houdini_mcp_read``, the plugin tree's leaf that says how a
Houdini exception reads on either side of the link.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from houdini_mcp_read import reason

TABLE_PATH = Path(__file__).with_name("fake_node_types.json")

# category -> a type's own name -> its facts, as the file holds them
Table = dict[str, dict[str, dict[str, Any]]]


class FlagMethods(NamedTuple):
    """The methods a node flag is read and written through."""

    read: str
    write: str


# [LAW:one-source-of-truth] Houdini 22's flag methods as the fake models them, which
# the reader asks a node for and the fake's nodes answer. A copy of the server's
# houdini_mcp.tools._common.FLAGS on purpose: the fake is checked against Houdini,
# never built from the code it tests, so a wrong name there fails a test here.
FLAGS = {
    "display": FlagMethods("isDisplayFlagSet", "setDisplayFlag"),
    "render": FlagMethods("isRenderFlagSet", "setRenderFlag"),
    "bypass": FlagMethods("isBypassed", "bypass"),
}


@dataclass(frozen=True)
class ParmTuple:
    """A parameter tuple: the names of its parms, its components, and their values.

    ``menu`` is the tokens of a menu parm, whose value is the index of one; a parm
    that is not a menu offers none.
    """

    components: tuple[str, ...]
    values: tuple[Any, ...]
    menu: tuple[str, ...] = ()


@dataclass(frozen=True)
class Made:
    """createNode makes the node."""


@dataclass(frozen=True)
class Refused:
    """createNode raises hou.OperationFailed with ``message`` and makes no node."""

    message: str


@dataclass(frozen=True)
class Mismatched:
    """createNode makes the node and raises hou.MatchDefinitionError with ``message``:
    the asset's contents do not match its definition in that network, so the node is
    left unlocked, with ``errors``."""

    message: str
    errors: tuple[str, ...]


Answer = Made | Refused | Mismatched


@dataclass(frozen=True)
class Answers:
    """What createNode gives for one type in one kind of network: with none of that
    type in the network, and with one. A type refused with none held is refused with
    any held, since createNode makes no first one to hold."""

    with_none: Answer
    with_one: Answer

    def given(self, holds_one: bool) -> Answer:
        return self.with_one if holds_one else self.with_none


# what createNode gives for a type a network's answers leave out
MAKES = Answers(Made(), Made())


@dataclass(frozen=True)
class CreateNode:
    """createNode's answers in a context root of one type (``network``), and in a
    subnetwork of each type inside one, at any depth (``subnetworks``), for each type
    of the category it holds that is not simply made, however many are held (MAKES).

    A context root is a network that holds a category other than its own (/mat, an
    attribvop, a geo): Houdini answers createNode by the root a network is in, by
    whether it is that root or a subnetwork of some type inside it, and by whether it
    already holds a node of the type asked for, and by nothing else seen. A locked
    one refuses every child alike, so the table holds answers for the unlocked alone.
    """

    network: dict[str, Answers]
    subnetworks: dict[str, dict[str, Answers]]


@dataclass(frozen=True)
class NodeType:
    """One node type as Houdini gives it.

    ``type`` is its own name, the table's key. ``namespace_order`` is every type its
    name without scope, namespace or version could mean, as ``namespaceOrder`` gives
    them, the preferred first (``mountain::2.0`` and the older ``mountain``); none
    for the root and its contexts, which Houdini registers no type for. ``aliases``
    is the other names createNode takes for it. ``holds`` is the category of the
    nodes it holds, None for a node that is not a network. A ``locked`` asset holds a
    category and refuses every child at once. ``indirect_inputs`` is how many indirect
    inputs, named "1" on, a node of it has; None for a node Houdini says is not a
    subnetwork, whose indirectInputs() raises. A node that is neither has none: a Sop
    null, or a karma ROP. ``flags`` is the flags of FLAGS a node of it has, which
    follow its class: a SOP has all three, an object display alone, a ROP bypass
    alone, a context none. ``create_node`` is createNode's answers in a node of it, for
    an unlocked context root; None for any other type.
    """

    type: str
    description: str
    namespace_order: tuple[str, ...]
    aliases: tuple[str, ...]
    holds: str | None
    locked: bool
    indirect_inputs: int | None
    flags: frozenset[str]
    parms: dict[str, ParmTuple]
    create_node: CreateNode | None

    @property
    def registered(self) -> bool:
        """Whether Houdini's registry has it, and so whether any name reaches it."""
        return _registered(self.type, self.namespace_order)

    def __deepcopy__(self, memo: dict[int, Any]) -> "NodeType":
        # [LAW:one-source-of-truth] a type is the table's one record of it, so a copy
        # of a scene (an undo step) holds the same types, which the fake compares by
        # identity
        return self


def _has_answers(category: str, holds: str | None, locked: bool) -> bool:
    """Whether the table holds createNode's answers in a node of a ``category`` type
    that ``holds`` a category and is ``locked`` or not: an unlocked context root."""
    return holds not in (None, category) and not locked


def _registered(own: str, namespace_order: Any) -> bool:
    # [LAW:one-source-of-truth] the namespace order is Houdini's answer: a type it
    # lists is registered, and the root and its contexts are in none
    return own in namespace_order


def load_table() -> Table:
    with TABLE_PATH.open() as f:
        table: Table = json.load(f)
    return table


def node_types(table: Table) -> dict[str, dict[str, NodeType]]:
    """The table as records: category -> a type's own name -> the type."""
    return {
        category: {own: _node_type(category, own, facts) for own, facts in types.items()}
        for category, types in table.items()
    }


def _node_type(category: str, own: str, facts: dict[str, Any]) -> NodeType:
    # [LAW:parse-dont-validate] a context root's answers, parsed once here, on exactly
    # the types the table holds them for
    answered = "create_node" in facts
    if answered != _has_answers(category, facts["holds"], facts["locked"]):
        raise ValueError(
            f"{category}/{own} {'has' if answered else 'lacks'} createNode answers, "
            "which the table holds for an unlocked context root alone"
        )
    return NodeType(
        type=own,
        description=facts["description"],
        namespace_order=tuple(facts["namespace_order"]),
        aliases=tuple(facts["aliases"]),
        holds=facts["holds"],
        locked=facts["locked"],
        indirect_inputs=facts["indirect_inputs"],
        flags=frozenset(facts["flags"]),
        parms={
            tuple_name: ParmTuple(
                tuple(parm["components"]), tuple(parm["values"]), tuple(parm["menu"])
            )
            for tuple_name, parm in facts["parms"].items()
        },
        create_node=_create_node(facts["create_node"]) if answered else None,
    )


def _create_node(create_node: dict[str, Any]) -> CreateNode:
    return CreateNode(
        network=_answers_by_type(create_node["network"]),
        subnetworks={
            subnetwork: _answers_by_type(answers)
            for subnetwork, answers in create_node["subnetworks"].items()
        },
    )


def _answers_by_type(answers: dict[str, Any]) -> dict[str, Answers]:
    return {own: _answers(own, given) for own, given in answers.items()}


def _answers(own: str, given: dict[str, Any]) -> Answers:
    with_none = _answer(own, given["with_none"])
    # with_one is null exactly where createNode refused the first one
    if (given["with_one"] is None) != isinstance(with_none, Refused):
        raise ValueError(f"{own}'s answer with one held is {given['with_one']!r}")
    return Answers(
        with_none, with_none if given["with_one"] is None else _answer(own, given["with_one"])
    )


def _answer(own: str, answer: Any) -> Answer:
    match answer:
        case "made":
            return Made()
        case {"raises": "OperationFailed", "message": str(message)}:
            return Refused(message)
        case {"raises": "MatchDefinitionError", "message": str(message), "errors": list(errors)}:
            return Mismatched(message, tuple(errors))
    raise ValueError(f"the fake models no createNode answer for {own} like {answer!r}")


def read_node_types(hou: Any, table: Table) -> Table:
    """``table`` with every fact re-read from ``hou``, whose scene must be fresh.

    A type's facts are read off the first node of it the reader meets that Houdini
    made cleanly: the root and its contexts, which come with the scene, then each node
    createNode makes as the reader explores. Exploring a context root asks createNode
    there for every registered type of the category it holds; the first node of a
    context-root type it makes is kept, fresh, and explored in turn. Once every type's
    facts are read, each root is asked the same in a fresh subnetwork of each unlocked
    type of its category that holds it.
    """
    reader = _Reader(hou, table)
    root = hou.node("/")
    for node in (root, *root.children()):
        reader.meet(node, keep=True)
    # the list grows as each root explored keeps the roots it makes
    for network in reader.roots:
        reader.explore(network)
    # after every root, since a subnetwork type may be first met in a later one
    for network in reader.roots:
        reader.explore_subnetworks(network)
    return reader.read()


class _Reader:
    """What one exploration of a fresh scene has read so far."""

    def __init__(self, hou: Any, table: Table) -> None:
        self._components = hou.hda.componentsFromFullNodeTypeName
        self._table = table
        self._facts: dict[tuple[str, str], dict[str, Any]] = {}
        self._create_node: dict[tuple[str, str], dict[str, Any]] = {}
        self.roots: list[Any] = []

    def meet(self, node: Any, keep: bool) -> bool:
        """Read ``node``'s type's facts if it is the first clean node of it met; the
        first of a context-root type is kept to explore when ``keep``. Whether it is."""
        key = _type_key(node)
        if key in self._facts:
            return False
        category, own = key
        facts = self._facts[key] = _facts(node, self._table[category][own])
        is_root = _has_answers(category, facts["holds"], facts["locked"])
        if keep and is_root:
            self.roots.append(node)
        return keep and is_root

    def explore(self, network: Any) -> None:
        """createNode's answers in the context root ``network`` itself."""
        held = network.childTypeCategory().name()
        if held not in self._table:
            raise LookupError(
                f"{network.path()} holds {held}, which the table has no entry for: add "
                f'"{held}": {{}} to {TABLE_PATH.name}, or any of its types the fake needs '
                f"with hython tests/refresh_fake_node_types.py {held}/<type>"
            )
        self._create_node[_type_key(network)] = {
            "network": self._ask_all(network, held, keep=True),
            "subnetworks": {},
        }

    def explore_subnetworks(self, network: Any) -> None:
        """createNode's answers in a fresh subnetwork of each unlocked type the context
        root ``network`` makes that holds its category."""
        held = network.childTypeCategory().name()
        subnetworks = self._create_node[_type_key(network)]["subnetworks"]
        for own in self._table[held]:
            facts = self._facts.get((held, own))
            if facts is None or facts["holds"] != held or facts["locked"]:
                continue
            answer, subnetwork = self._ask(network, own)
            if answer == "made":
                subnetworks[own] = self._ask_all(subnetwork, held, keep=False)
            # a root first met in a subnetwork is not kept: the subnetwork goes, as does
            # one Houdini made and could not match
            if subnetwork is not None:
                subnetwork.destroy()

    def _ask_all(self, network: Any, category: str, keep: bool) -> dict[str, Any]:
        """createNode's answers in ``network`` for each registered type of
        ``category`` it does not simply make, asked with none of it held and again."""
        # the nodes Houdini builds with a fresh network, each cleared before its type is
        # asked for; no other node of a type is in the network until that type is asked
        built: dict[str, list[Any]] = {}
        for child in network.children():
            built.setdefault(child.type().name(), []).append(child)
        answers = {}
        for own in self._table[category]:
            facts = self._facts.get((category, own))
            if facts is not None and not _registered(own, facts["namespace_order"]):
                continue
            for held in built.get(own, []):
                held.destroy()
            with_none, first = self._ask(network, own)
            with_one = None
            if first is not None:
                with_one, again = self._ask(network, own)
                if again is not None:
                    again.destroy()
                if not (with_none == "made" and self.meet(first, keep)):
                    first.destroy()
            if (with_none, with_one) != ("made", "made"):
                answers[own] = {"with_none": with_none, "with_one": with_one}
        return answers

    def _ask(self, network: Any, own: str) -> tuple[Any, Any]:
        """createNode's answer in ``network`` for exactly ``own``, as the file holds
        it, and the node it made; None for a refusal."""
        # a trailing "::" asks for the type with no version, which own names alone
        # would not: createNode("mountain") gives mountain::2.0
        name = own if self._components(own)[3] else f"{own}::"
        before = {child.path() for child in network.children()}
        try:
            node = network.createNode(name)
        except Exception as e:
            left = [child for child in network.children() if child.path() not in before]
            raised, message = _raised(e)
            if raised == "OperationFailed" and not left:
                return {"raises": raised, "message": message}, None
            if raised == "MatchDefinitionError" and [c.type().name() for c in left] == [own]:
                errors = list(left[0].errors())
                return {"raises": raised, "message": message, "errors": errors}, left[0]
            raise
        if node.type().name() != own:
            raise ValueError(
                f"createNode({name!r}) in {network.path()} gives {node.type().name()}, not {own}"
            )
        return "made", node

    def read(self) -> Table:
        read: Table = {}
        for category, types in self._table.items():
            read[category] = {}
            for own in types:
                facts = self._facts.get((category, own))
                if facts is None:
                    raise LookupError(f"a fresh scene gives no clean node of {category}/{own}")
                create_node = self._create_node.get((category, own))
                read[category][own] = (
                    facts if create_node is None else {**facts, "create_node": create_node}
                )
        return read


def _type_key(node: Any) -> tuple[str, str]:
    node_type = node.type()
    return node_type.category().name(), node_type.name()


def _raised(e: BaseException) -> tuple[str, str]:
    """The Houdini exception ``e`` is, by class name, and its message.

    hython raises hou's class itself; over rpyc it arrives as rpyc's stand-in, which
    no except clause can name, so the class is known by the name its reason gives
    (``hou.OperationFailed: The attempted operation failed. ...``) in either form.
    """
    raised, _, message = reason(e).partition(": ")
    return raised.rpartition(".")[2], message


def _facts(node: Any, row: dict[str, Any]) -> dict[str, Any]:
    held = node.childTypeCategory()
    return {
        "description": node.type().description(),
        "namespace_order": list(node.type().namespaceOrder()),
        "aliases": list(node.type().aliases()),
        "holds": None if held is None else held.name(),
        "locked": node.isLockedHDA(),
        "indirect_inputs": _indirect_inputs(node),
        "flags": _flags(node),
        "parms": {name: _parm_tuple(node, name) for name in row["parms"]},
    }


def _indirect_inputs(node: Any) -> int | None:
    """How many indirect inputs ``node`` has; None when Houdini says it is not a
    subnetwork, the one answer it gives by raising."""
    try:
        indirect = node.indirectInputs()
    except Exception as e:
        # its message says which fault of the type it reports
        raised, message = _raised(e)
        if raised != "InvalidNodeType" or "Node is not a subnetwork" not in message:
            raise
        return None
    numbered = [(item.number(), item.name()) for item in indirect]
    if numbered != [(n, str(n + 1)) for n in range(len(numbered))]:
        # the fake numbers them from 0 and names each its number + 1, so a count
        # holds them only when Houdini does
        raise ValueError(f"{node.path()}'s indirect inputs are (number, name) {numbered}")
    return len(numbered)


def _flags(node: Any) -> list[str]:
    """The flags ``node``'s class has: those whose read and write it both has."""
    flags = []
    for flag, methods in FLAGS.items():
        has = [hasattr(node, method) for method in methods]
        if any(has) and not all(has):
            # the fake holds a flag as both its methods, so one alone it cannot hold
            raise ValueError(f"{node.path()} has {flag}'s {methods} as {has}")
        if all(has):
            flags.append(flag)
    return flags


def _parm_tuple(node: Any, name: str) -> dict[str, Any]:
    parm_tuple = node.parmTuple(name)
    if parm_tuple is None:
        raise LookupError(f"{node.path()} ({node.type().name()}) has no parm tuple {name!r}")
    components = [parm.name() for parm in parm_tuple]
    if any(parm.keyframes() for parm in parm_tuple):
        # the fake holds values; a parm Houdini computes has no one value to hold
        raise ValueError(f"{node.path()}'s {name!r} is an expression, not a value")
    template = parm_tuple.parmTemplate()
    if template.type().name() == "Int" and template.menuItems():
        # an int menu parm holds a value, not an index into its tokens, which the
        # fake's menu does not model
        raise ValueError(f"{node.path()}'s {name!r} is an int parm with a menu")
    return {
        "components": components,
        "values": [_held(parm) for parm in parm_tuple],
        # a menu parm holds an index and is set by a token, which names it
        "menu": list(template.menuItems()) if template.type().name() == "Menu" else [],
    }


def _held(parm: Any) -> Any:
    """What the parm holds: a string as written, since the fake expands no variables
    ($HIP stays $HIP); any other value as it evaluates."""
    value = parm.eval()
    return parm.unexpandedString() if isinstance(value, str) else value
