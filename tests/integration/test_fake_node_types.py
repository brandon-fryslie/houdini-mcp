"""The fake hou's node types are the ones this Houdini gives (houdini-laws-etg.0qg),
and a name reaches what Houdini gives it (houdini-fake-hou-efn.2vo)."""

from collections.abc import Callable
from typing import Any

from houdini_mcp_read import reason

from houdini_mcp.settings import Target
from houdini_mcp.tools._common import using
from tests.conftest import (
    NODE_TYPES,
    MockHouModule,
    _full_name,
    _name_components,
    _named,
    _resolved,
)
from tests.fake_node_types import load_table, read_node_types


def test_every_row_of_the_fake_table_is_what_houdini_reads(houdini: Target) -> None:
    """Each type the fake holds is one this Houdini makes, with the facts the table
    gives it, and createNode answers in each kind of network as the table says. On a
    mismatch, refresh the table: ``hython tests/refresh_fake_node_types.py``."""
    table = load_table()
    with using(houdini.host, houdini.port) as link:
        read = read_node_types(link.hou, table)

    def differing(category: str) -> dict[str, Any]:
        return {
            name: {"table": table[category][name], "houdini": facts}
            for name, facts in read[category].items()
            if facts != table[category][name]
        }

    assert {category: differing(category) for category in table} == dict.fromkeys(table, {})


NAMES = (
    "mountain",
    "mountain::2.0",
    "mountain::",
    "apex::controlextract",
    "apex::Sop/controlextract::2.0",
    "Sop/rbdmaterialfracture::rbdconcretefracture",
    "a::b::c::f",
    "Sop/grid",
    "",
)


def test_the_fake_splits_a_type_name_as_houdini_does(houdini: Target) -> None:
    """The fake's hou.hda.componentsFromFullNodeTypeName gives this Houdini's parts."""
    with using(houdini.host, houdini.port) as link:
        read = {name: tuple(link.hou.hda.componentsFromFullNodeTypeName(name)) for name in NAMES}

    assert {name: _name_components(name) for name in NAMES} == read


def _networks(hou: Any) -> dict[str, tuple[Any, str]]:
    """The nodes the name tests ask in, each as createNode leaves it in a fresh scene,
    with the category whose names each is asked: the contexts, a context root of each
    kind the table's types name, a subnet in each Vop and Sop root and one in that,
    for the depth the reader does not ask at, and a locked asset and a node that is
    not a network, which the table does not answer for."""
    obj = hou.node("/obj")
    geo = obj.createNode("geo")
    networks = {
        "/obj": obj,
        "/out": hou.node("/out"),
        "/mat": hou.node("/mat"),
        "/stage": hou.node("/stage"),
        "geo": geo,
        "dopnet": obj.createNode("dopnet"),
        "attribvop": geo.createNode("attribvop"),
        "geo/attribwrangle": geo.createNode("attribwrangle"),
    }
    for root in ("geo", "/mat", "attribvop"):
        subnet = networks[f"{root}/subnet"] = networks[root].createNode("subnet")
        networks[f"{root}/subnet/subnet"] = subnet.createNode("subnet")
    asked = {label: (n, n.childTypeCategory().name()) for label, n in networks.items()}
    return {**asked, "geo/box": (geo.createNode("box"), "Sop")}


def _forms(category: str, resolve: Callable[[str, str], object]) -> list[str]:
    """Every name form of every registered table type of ``category`` (its own name,
    without its version, with that and an empty version, its name alone, with an
    empty version, and its aliases) that ``resolve`` takes to a type the table holds,
    or to none. A name that reaches a type the table lacks (the unversioned
    geometryvopglobal) the fake answers by naming its refresh, not as Houdini does."""
    forms: dict[str, None] = {}
    for row in NODE_TYPES[category].values():
        if row.registered:
            scope, namespace, base, _ = _name_components(row.type)
            versionless = _full_name(scope, namespace, base, "")
            for form in (row.type, versionless, f"{versionless}::", base, f"{base}::"):
                forms.setdefault(form)
            forms.update(dict.fromkeys(row.aliases))
    return [form for form in forms if _reaches_the_table(resolve, category, form)]


def _reaches_the_table(resolve: Callable[[str, str], object], category: str, name: str) -> bool:
    try:
        resolve(category, name)
    except LookupError:
        return False
    return True


def _created(network: Any, name: str) -> tuple[str, str]:
    """What createNode does with ``name`` in ``network``: the type it made, or the
    reason it raised; what it made is destroyed again."""
    before = {child.path() for child in network.children()}
    try:
        node = network.createNode(name)
    except Exception as e:
        for child in network.children():
            if child.path() not in before:
                child.destroy()
        return ("raises", reason(e))
    made = node.type().name()
    node.destroy()
    return ("made", made)


def _create_node_answers(hou: Any) -> dict[str, dict[str, tuple[str, str]]]:
    """createNode's answer in each node for every form of the types of the category
    it is asked."""
    return {
        label: {name: _created(network, name) for name in _forms(category, _named)}
        for label, (network, category) in _networks(hou).items()
    }


def test_create_node_gives_what_houdini_gives_a_name_in_each_network(houdini: Target) -> None:
    """Every name form of every table type, in each network as createNode leaves it:
    the fake makes the type Houdini makes, or raises what Houdini raises."""
    with using(houdini.host, houdini.port) as link:
        asked = _create_node_answers(link.hou)

    fake = _create_node_answers(MockHouModule())

    assert {
        label: {
            name: {"fake": fake[label][name], "houdini": a}
            for name, a in answers.items()
            if fake[label][name] != a
        }
        for label, answers in asked.items()
    } == dict.fromkeys(asked, {})


def _rebuilt_and_resolved(category: str, name: str) -> object:
    """The type preferredNodeType resolves ``name`` to: rebuilt from its parts, which
    reads a trailing "::" as no version, and taking no alias."""
    return _resolved(category, _full_name(*_name_components(name)))


def test_preferred_node_type_gives_what_houdini_gives_a_name(houdini: Target) -> None:
    """Every name form of every table type of the categories the networks hold, in
    the category-qualified form preferredNodeType takes, rebuilt from its parts."""
    categories = ("Object", "Driver", "Vop", "Lop", "Sop", "Dop", "Director", "Manager")
    qualified = {
        category: {
            form: _full_name(scope, namespace, f"{category}/{base}", version)
            for form in _forms(category, _rebuilt_and_resolved)
            for scope, namespace, base, version in [_name_components(form)]
        }
        for category in categories
    }

    def preferred(hou: Any, category: str, name: str) -> str | None:
        found = hou.preferredNodeType(name)
        return None if found is None else found.name()

    with using(houdini.host, houdini.port) as link:
        asked = {
            category: {form: preferred(link.hou, category, q) for form, q in names.items()}
            for category, names in qualified.items()
        }
    fake_hou = MockHouModule()

    fake = {
        category: {form: preferred(fake_hou, category, q) for form, q in names.items()}
        for category, names in qualified.items()
    }

    assert {
        category: {
            form: {"fake": fake[category][form], "houdini": own}
            for form, own in answers.items()
            if fake[category][form] != own
        }
        for category, answers in asked.items()
    } == dict.fromkeys(asked, {})
