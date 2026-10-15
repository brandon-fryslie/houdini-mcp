"""The one owner of named-parameter reads and writes (houdini-laws-etg.dq2).

A parameter name resolves the same way, a value converts the same way, and a
failure reads the same way whichever tool the caller picked.
"""

import logging
from unittest.mock import MagicMock

import pytest

from houdini_mcp.tools.parms import (
    UNEVALUABLE,
    ParmNotFound,
    ParmValueError,
    evaluated,
    read_present,
    resolve,
    write,
    write_all,
)
from tests.conftest import MockHouNode


def node_with(**params):
    return MockHouNode(path="/obj/n", name="n", node_type="geo", params=params)


class TestResolve:
    def test_scalar_resolves_as_a_parm(self):
        parm = resolve(node_with(radx=2.0), "radx")
        assert parm.is_tuple is False
        assert parm.name == "radx"

    def test_vector_resolves_as_a_tuple(self):
        parm = resolve(node_with(t=[1.0, 2.0, 3.0]), "t")
        assert parm.is_tuple is True

    def test_a_vector_name_is_not_a_parm_name(self):
        # [LAW:one-source-of-truth] the double keeps Houdini's rule: parm("t") is None
        assert node_with(t=[1.0, 2.0, 3.0]).parm("t") is None

    def test_missing_name_raises_with_node_and_name(self):
        with pytest.raises(ParmNotFound) as exc:
            resolve(node_with(radx=2.0), "nope")
        assert str(exc.value) == "Parameter not found: nope on /obj/n"


class TestReadAndWrite:
    def test_tuple_reads_as_a_list(self):
        node = node_with(t=[1.0, 2.0, 3.0])
        assert read_present(MagicMock(), node, ["t"]) == {"t": [1.0, 2.0, 3.0]}

    def test_scalar_reads_as_itself(self):
        node = node_with(radx=2.5)
        assert read_present(MagicMock(), node, ["radx"]) == {"radx": 2.5}

    def test_write_lands_on_the_node(self):
        node = node_with(radx=2.5, t=[0.0, 0.0, 0.0])
        write(resolve(node, "radx"), 4.0)
        write(resolve(node, "t"), [1.0, 2.0, 3.0])
        assert node._params["radx"] == 4.0
        assert list(node._params["t"]) == [1.0, 2.0, 3.0]

    def test_a_tuple_refuses_a_scalar_before_touching_houdini(self):
        node = node_with(t=[0.0, 0.0, 0.0])
        with pytest.raises(ParmValueError, match="t is a tuple"):
            write(resolve(node, "t"), 1.0)
        assert list(node._params["t"]) == [0.0, 0.0, 0.0]


class TestReadPresent:
    def test_only_names_the_node_has_are_answered(self):
        node = node_with(radx=2.5, t=[1.0, 2.0, 3.0])
        assert read_present(MagicMock(), node, ["radx", "t", "nope"]) == {
            "radx": 2.5,
            "t": [1.0, 2.0, 3.0],
        }

    def test_a_present_parm_that_cannot_evaluate_is_marked(self, caplog):
        node = node_with(radx=2.5)
        node.parm("radx").eval = MagicMock(side_effect=RuntimeError("no cook"))
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.parms"):
            assert read_present(MagicMock(), node, ["radx"]) == {"radx": UNEVALUABLE}
        assert "parameter unevaluable name=radx reason=no cook" in caplog.text


class TestEvaluated:
    def test_every_parm_with_its_json_value_or_the_marker(self):
        node = node_with(radx=2.5, file="/a.bgeo")
        node.parm("file").eval = MagicMock(side_effect=RuntimeError("no cook"))
        assert dict(evaluated(MagicMock(), node.parms())) == {"radx": 2.5, "file": UNEVALUABLE}

    def test_connection_loss_is_not_an_unevaluable_parm(self, mock_connection):
        node = node_with(radx=2.5)
        node.parm("radx").eval = MagicMock(side_effect=EOFError("stream has been closed"))
        with pytest.raises(EOFError):
            dict(evaluated(mock_connection, node.parms()))


class TestWriteAll:
    def test_each_write_is_reported_by_name(self, caplog):
        node = node_with(radx=2.5, t=[0.0, 0.0, 0.0], bad=1.0)
        node.parm("bad").set = MagicMock(side_effect=RuntimeError("read only"))

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.parms"):
            writes = write_all(node, {"radx": 3.0, "t": 1.0, "nope": 1, "bad": 2})

        assert writes.fields("op")["status"] == "partial"
        assert writes.updated == [{"name": "radx", "value": 3.0}]
        assert [f["name"] for f in writes.failed] == ["t", "nope", "bad"]
        assert writes.failed[0]["reason"] == "Parameter t is a tuple, provide a list/tuple value"
        assert writes.failed[1]["reason"] == "Parameter not found: nope on /obj/n"
        assert writes.failed[2]["reason"] == "read only"
        # A missing name or a wrong-shaped value is the caller's; a set Houdini
        # refused is Houdini's.
        assert [f["kind"] for f in writes.failed] == [
            "invalid_input",
            "invalid_input",
            "operation_failed",
        ]
        assert node._params["radx"] == 3.0
        assert (
            "updated=['radx'] failed=[{'name': 't', 'kind': 'invalid_input', 'reason': "
            in caplog.text
        )
        assert "{'name': 'bad', 'kind': 'operation_failed', 'reason': 'read only'}" in caplog.text

    def test_nothing_failed_is_success(self):
        writes = write_all(node_with(radx=2.5), {"radx": 1.0})
        assert writes.fields("op", rop_path="/out/k") == {
            "status": "success",
            "updated": [{"name": "radx", "value": 1.0}],
            "failed": [],
            "rop_path": "/out/k",
        }

    def test_connection_loss_propagates_instead_of_being_a_failed_parm(self, mock_connection):
        node = node_with(radx=2.5)
        node.parm("radx").set = MagicMock(side_effect=EOFError("stream has been closed"))
        with pytest.raises(EOFError):
            write_all(node, {"radx": 1.0})


class TestToolsShareTheOwner:
    """The same parm and value behave the same whichever tool the agent picks."""

    def test_set_parameter_refuses_a_scalar_for_a_tuple(self, mock_connection):
        from houdini_mcp.tools import set_parameter

        mock_connection.add_node(node_with(t=[0.0, 0.0, 0.0]))
        result = set_parameter("/obj/n", "t", 1.0, host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["kind"] == "invalid_input"
        assert result["message"] == "Parameter t is a tuple, provide a list/tuple value"
        assert result["failed"][0]["name"] == "t"

    def test_create_material_reports_the_parameter_it_could_not_set(self, mock_connection):
        from houdini_mcp.tools import create_material

        mock_connection.add_node(MockHouNode(path="/mat", name="mat", node_type="matnet"))
        result = create_material(
            "principledshader",
            "m",
            parameters={"metallic": 1.0, "nope": 1},
            host="localhost",
            port=18811,
        )
        assert result["status"] == "partial"
        assert result["updated"] == [{"name": "metallic", "value": 1.0}]
        assert result["failed"] == [
            {
                "name": "nope",
                "kind": "invalid_input",
                "reason": "Parameter not found: nope on /mat/m",
            }
        ]

    def test_create_render_node_reports_the_setting_it_could_not_set(self, mock_connection):
        from houdini_mcp.tools import create_render_node

        mock_connection.add_node(MockHouNode(path="/out", name="out", node_type="ropnet"))
        result = create_render_node(
            "karma", "k", {"tx": 1.0, "nope": 1}, host="localhost", port=18811
        )
        assert result["status"] == "partial"
        assert result["updated"] == [{"name": "tx", "value": 1.0}]
        assert result["failed"] == [
            {
                "name": "nope",
                "kind": "invalid_input",
                "reason": "Parameter not found: nope on /out/k",
            }
        ]

    def test_set_render_settings_reports_the_same_way(self, mock_connection):
        from houdini_mcp.tools import set_render_settings

        mock_connection.add_node(MockHouNode(path="/out", name="out", node_type="ropnet"))
        mock_connection.add_node(MockHouNode(path="/out/k", name="k", node_type="karma"))
        result = set_render_settings("/out/k", {"tx": 1.0, "nope": 1}, host="localhost", port=18811)
        assert result["status"] == "partial"
        assert result["updated"] == [{"name": "tx", "value": 1.0}]
        assert result["failed"] == [
            {
                "name": "nope",
                "kind": "invalid_input",
                "reason": "Parameter not found: nope on /out/k",
            }
        ]

    def test_set_render_settings_that_writes_nothing_is_an_error(self, mock_connection, caplog):
        """Every setting refused is not "partial": nothing was done, and the
        caller is told which settings and why, as its own input to fix."""
        from houdini_mcp.tools import set_render_settings
        from houdini_mcp.tools.failure import FAILURE, InvalidInputFailure

        mock_connection.add_node(MockHouNode(path="/out", name="out", node_type="ropnet"))
        mock_connection.add_node(MockHouNode(path="/out/k", name="k", node_type="karma"))
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools"):
            result = set_render_settings("/out/k", {"nope": 1}, host="localhost", port=18811)

        failure = FAILURE.validate_python(result)
        assert isinstance(failure, InvalidInputFailure), result
        assert failure.message == "Nothing was done: nope: Parameter not found: nope on /out/k"
        assert result["updated"] == []
        assert [f["name"] for f in result["failed"]] == ["nope"]
        # the error carries the report a success would have, ROP included
        assert result["rop_path"] == "/out/k"
        assert (
            "batch outcome operation=set_render_settings done=0 "
            "failed=[('nope', 'invalid_input')]" in caplog.text
        )

    def test_create_material_whose_every_parameter_fails_is_partial(self, mock_connection):
        """The material node was created, so the call did something: partial, not error."""
        from houdini_mcp.tools import create_material

        mock_connection.add_node(MockHouNode(path="/mat", name="mat", node_type="matnet"))
        result = create_material(
            "principledshader", "m", parameters={"nope": 1}, host="localhost", port=18811
        )

        assert result["status"] == "partial", result
        assert result["material_path"] == "/mat/m"
        assert result["updated"] == []

    def test_reads_return_one_shape_for_a_vector(self, mock_connection):
        from houdini_mcp.tools import get_material_info, get_render_settings

        mock_connection.add_node(MockHouNode(path="/mat", name="mat", node_type="matnet"))
        mock_connection.add_node(MockHouNode(path="/out", name="out", node_type="ropnet"))
        mock_connection.add_node(
            MockHouNode(
                path="/mat/m",
                name="m",
                node_type="principledshader",
                params={"basecolor": [1.0, 0.0, 0.0], "basecolor_texture": "/tex/a.png"},
            )
        )
        info = get_material_info("/mat/m", host="localhost", port=18811)
        assert info["parameters"] == {
            "basecolor": [1.0, 0.0, 0.0],
            "basecolor_texture": "/tex/a.png",
        }
        assert info["textures"] == [{"parameter": "basecolor_texture", "path": "/tex/a.png"}]

        mock_connection.add_node(
            MockHouNode(
                path="/out/k",
                name="k",
                node_type="karma",
                params={"f1": 1, "engine": "xpu", "resolution": [1920, 1080]},
            )
        )
        settings = get_render_settings("/out/k", host="localhost", port=18811)
        assert settings["settings"] == {"engine": "xpu", "f1": 1}


class TestJsonSafeHouValue:
    def test_a_repeated_scalar_is_not_a_cycle(self):
        from houdini_mcp.tools.parms import json_safe_hou_value

        assert json_safe_hou_value(MagicMock(), (0.0, 0.0, 0.0)) == [0.0, 0.0, 0.0]
        shared = [1, 2]
        assert json_safe_hou_value(MagicMock(), [shared, shared]) == [[1, 2], [1, 2]]

    def test_a_container_that_contains_itself_is(self):
        from houdini_mcp.tools.parms import json_safe_hou_value

        loop: list = [1]
        loop.append(loop)
        assert json_safe_hou_value(MagicMock(), loop) == [1, "<recursion>"]
