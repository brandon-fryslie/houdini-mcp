"""Tests for the get_parameter_schema function."""

import json
import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from tests.conftest import MockHouModule, MockHouNode, served_connection
from tests.fake_node_types import ParmTuple


class MockParmTemplate:
    """Mock parameter template for testing."""

    def __init__(
        self,
        name: str,
        label: str,
        parm_type: Any,
        num_components: int = 1,
        default_value: Any = None,
        min_val: Any = None,
        max_val: Any = None,
        menu_use_token: bool = False,
    ):
        self._name = name
        self._label = label
        self._type = parm_type
        self._num_components = num_components
        self._default_value = (
            default_value if default_value is not None else ([0.0] * num_components)
        )
        self._min_val = min_val
        self._max_val = max_val
        self._menu_use_token = menu_use_token

    def name(self) -> str:
        return self._name

    def label(self) -> str:
        return self._label

    def type(self) -> Any:
        return self._type

    def numComponents(self) -> int:
        return self._num_components

    def defaultValue(self) -> Any:
        # As in Houdini: Float, Int and String templates give a tuple of
        # per-component defaults; Toggle and Menu give one value.
        if self._type not in ("Float", "Int", "String"):
            return self._default_value
        if isinstance(self._default_value, list):
            return tuple(self._default_value)
        return (self._default_value,)

    def defaultExpression(self) -> list[str]:
        return [""] * self._num_components

    def minValue(self) -> Any:
        return self._min_val

    def maxValue(self) -> Any:
        return self._max_val

    # As in Houdini, a menu's items are read from its hou.Parm (see give_menu):
    # a script-generated menu's template lists none.
    def menuUseToken(self) -> bool:
        return self._menu_use_token


def give_menu(parm: Any, tokens: list[str], labels: list[str]) -> None:
    """Give a fake hou.Parm its menu, as Houdini evaluates it."""
    parm.menuItems = MagicMock(return_value=tuple(tokens))
    parm.menuLabels = MagicMock(return_value=tuple(labels))


def give_templates(node: MockHouNode, templates: list[MockParmTemplate]) -> None:
    """Give each of the fake node's parameters its template, by name: as in
    Houdini, a tuple's components share its template."""
    for template in templates:
        parm_tuple = node.parmTuple(template.name())
        for target in (parm_tuple, *parm_tuple):
            target.parmTemplate = MagicMock(return_value=template)


def create_mock_hou_with_parm_types():
    """Create a mock hou module with parmTemplateType enum."""
    mock_hou = MockHouModule()

    # Create parmTemplateType enum mock
    parm_template_type = MagicMock()
    parm_template_type.Float = "Float"
    parm_template_type.Int = "Int"
    parm_template_type.String = "String"
    parm_template_type.Toggle = "Toggle"
    parm_template_type.Menu = "Menu"
    parm_template_type.Button = "Button"
    parm_template_type.Ramp = "Ramp"
    parm_template_type.Data = "Data"
    parm_template_type.Folder = "Folder"
    parm_template_type.FolderSet = "FolderSet"
    parm_template_type.Separator = "Separator"
    parm_template_type.Label = "Label"

    mock_hou.parmTemplateType = parm_template_type

    return mock_hou


class TestGetParameterSchema:
    """Tests for get_parameter_schema function."""

    def test_get_parameter_schema_single_float(self, connected_to):
        """Test getting schema for a single float parameter."""
        from houdini_mcp.tools import get_parameter_schema

        # Setup mock hou with parmTemplateType
        mock_hou = create_mock_hou_with_parm_types()

        # Create a sphere node with radx parameter
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1", name="sphere1", node_type="sphere", params={"radx": 2.5}
        )
        mock_hou.add_node(sphere)

        # Create mock parameter template
        radx_template = MockParmTemplate(
            name="radx",
            label="Radius X",
            parm_type=mock_hou.parmTemplateType.Float,
            num_components=1,
            default_value=[1.0],
            min_val=0.0,
            max_val=None,
        )

        # Mock parm to return parmTemplate
        mock_parm = sphere.parm("radx")
        mock_parm.parmTemplate = MagicMock(return_value=radx_template)
        mock_parm.eval = MagicMock(return_value=2.5)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1", "radx")

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1/sphere1"
        assert result["count"] == 1

        param = result["parameters"][0]
        assert param["name"] == "radx"
        assert param["label"] == "Radius X"
        assert param["type"] == "float"
        assert param["default"] == 1.0
        assert param["min"] == 0.0
        assert param["max"] is None
        assert param["current_value"] == 2.5
        assert param["is_animatable"] is True

    def test_get_parameter_schema_vector(self, connected_to):
        """Test getting schema for a vector parameter (translate)."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        # Create a node with translate parameter
        geo = MockHouNode(
            path="/obj/geo1", name="geo1", node_type="geo", params={"t": [1.0, 2.0, 3.0]}
        )
        mock_hou.add_node(geo)

        # Create mock vector parameter template
        t_template = MockParmTemplate(
            name="t",
            label="Translate",
            parm_type=mock_hou.parmTemplateType.Float,
            num_components=3,
            default_value=[0.0, 0.0, 0.0],
            min_val=None,
            max_val=None,
        )

        # Mock parmTuple for vector parameter
        mock_parm_tuple = geo.parmTuple("t")
        mock_parm_tuple.parmTemplate = MagicMock(return_value=t_template)
        mock_parm_tuple.eval = MagicMock(return_value=(1.0, 2.0, 3.0))

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1", "t")

        assert result["status"] == "success"
        assert result["count"] == 1

        param = result["parameters"][0]
        assert param["name"] == "t"
        assert param["label"] == "Translate"
        assert param["type"] == "vector"
        assert param["tuple_size"] == 3
        assert param["default"] == [0.0, 0.0, 0.0]
        assert param["current_value"] == [1.0, 2.0, 3.0]
        assert param["is_animatable"] is True

    def test_a_named_component_is_described_as_itself_not_its_tuple(self, connected_to, caplog):
        """rady is the scalar set_parameter("rady", ...) writes: its own value and its
        own component's default, not rad's vector."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1",
            name="sphere1",
            node_type="sphere",
            params={"rad": ParmTuple(("radx", "rady", "radz"), (1.0, 5.0, 1.0))},
        )
        mock_hou.add_node(sphere)
        rad = MockParmTemplate(
            "rad", "Radius", mock_hou.parmTemplateType.Float, 3, [1.0, 2.0, 3.0], -1.0, 1.0
        )
        give_templates(sphere, [rad])
        connected_to(mock_hou)

        with caplog.at_level("INFO", logger="houdini_mcp.tools.parameters"):
            component = get_parameter_schema("/obj/geo1/sphere1", "rady")
        whole = get_parameter_schema("/obj/geo1/sphere1", "rad")

        assert component["parameters"] == [
            {
                "name": "rady",
                "label": "Radius",
                "current_value": 5.0,
                "type": "float",
                "is_animatable": True,
                "default": 2.0,
                "min": -1.0,
                "max": 1.0,
            }
        ]
        assert "parameter schema node=/obj/geo1/sphere1 parm=rady listed=1 of=1" in caplog.messages
        [vector] = whole["parameters"]
        assert (vector["name"], vector["type"], vector["tuple_size"], vector["default"]) == (
            "rad",
            "vector",
            3,
            [1.0, 2.0, 3.0],
        )

    def test_get_parameter_schema_menu(self, connected_to):
        """Test getting schema for a menu parameter."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        # Create a sphere node with type menu parameter
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1", name="sphere1", node_type="sphere", params={"type": 0}
        )
        mock_hou.add_node(sphere)

        # Create mock menu parameter template
        type_template = MockParmTemplate(
            name="type",
            label="Primitive Type",
            parm_type=mock_hou.parmTemplateType.Menu,
            num_components=1,
            default_value=0,
        )

        mock_parm = sphere.parm("type")
        mock_parm.parmTemplate = MagicMock(return_value=type_template)
        mock_parm.eval = MagicMock(return_value=0)
        give_menu(mock_parm, ["poly", "mesh", "polymesh"], ["Polygon", "Mesh", "Polygon Mesh"])

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1", "type")

        assert result["status"] == "success"
        param = result["parameters"][0]

        assert param["name"] == "type"
        assert param["label"] == "Primitive Type"
        assert param["type"] == "menu"
        assert param["default"] == 0
        assert param["is_animatable"] is False
        assert len(param["menu_items"]) == 3
        assert param["menu_items"] == [
            {"label": "Polygon", "value": 0},
            {"label": "Mesh", "value": 1},
            {"label": "Polygon Mesh", "value": 2},
        ]

    def test_get_parameter_schema_all_parameters(self, connected_to):
        """Test getting schema for all parameters on a node."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        # Create a node with multiple parameters
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1",
            name="sphere1",
            node_type="sphere",
            params={"radx": 1.0, "rady": 1.0, "radz": 1.0, "type": 0},
        )
        mock_hou.add_node(sphere)

        # Create multiple parameter templates
        templates = [
            MockParmTemplate(
                "radx",
                "Radius X",
                mock_hou.parmTemplateType.Float,
                default_value=[1.0],
                min_val=0.0,
            ),
            MockParmTemplate(
                "rady",
                "Radius Y",
                mock_hou.parmTemplateType.Float,
                default_value=[1.0],
                min_val=0.0,
            ),
            MockParmTemplate(
                "radz",
                "Radius Z",
                mock_hou.parmTemplateType.Float,
                default_value=[1.0],
                min_val=0.0,
            ),
            MockParmTemplate(
                "type",
                "Type",
                mock_hou.parmTemplateType.Menu,
                default_value=[0],
            ),
        ]

        give_templates(sphere, templates)
        give_menu(sphere.parm("type"), ["poly", "mesh"], ["Polygon", "Mesh"])

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1")

        assert result["status"] == "success"
        assert result["count"] == 4
        [menu] = [p for p in result["parameters"] if p["name"] == "type"]
        assert menu["menu_items"] == [
            {"label": "Polygon", "value": 0},
            {"label": "Mesh", "value": 1},
        ]
        assert len(result["parameters"]) == 4

        # Check that all parameters are present
        param_names = [p["name"] for p in result["parameters"]]
        assert "radx" in param_names
        assert "rady" in param_names
        assert "radz" in param_names
        assert "type" in param_names

    def test_get_parameter_schema_max_parms_limit(self, connected_to, caplog):
        """Test that max_parms limits the number of returned parameters."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        # Create a node with many parameters
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1",
            name="sphere1",
            node_type="sphere",
            params={f"parm{i}": 0.0 for i in range(20)},
        )
        mock_hou.add_node(sphere)

        # Create 20 parameter templates
        templates = [
            MockParmTemplate(
                f"parm{i}", f"Parameter {i}", mock_hou.parmTemplateType.Float, default_value=[0.0]
            )
            for i in range(20)
        ]

        give_templates(sphere, templates)

        connected_to(mock_hou)
        with caplog.at_level("INFO", logger="houdini_mcp.tools.parameters"):
            result = get_parameter_schema("/obj/geo1/sphere1", max_parms=5)

        assert result["status"] == "success"
        assert result["count"] == 5
        assert len(result["parameters"]) == 5
        assert result["warning"] == "Parameters limited to the first 5 of 20 parameters"
        assert "parameter schema node=/obj/geo1/sphere1 parm=None listed=5 of=20" in caplog.messages

        assert "warning" not in get_parameter_schema("/obj/geo1/sphere1", max_parms=20)

    def test_max_parms_counts_parameters_not_layout(self, connected_to):
        """Folders and separators neither use up max_parms nor make a listing look cut."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1",
            name="sphere1",
            node_type="sphere",
            # A separator is a parm tuple of no components, as in Houdini.
            params={"folder0": 0, "sep1": [], **{f"parm{i}": 0.0 for i in range(3)}, "sep2": []},
        )
        mock_hou.add_node(sphere)
        layout = mock_hou.parmTemplateType
        give_templates(
            sphere,
            [
                MockParmTemplate("folder0", "Tabs", layout.FolderSet),
                MockParmTemplate("sep1", "Separator", layout.Separator),
                *(
                    MockParmTemplate(f"parm{i}", f"P{i}", layout.Float, default_value=[0.0])
                    for i in range(3)
                ),
                MockParmTemplate("sep2", "Separator", layout.Separator),
            ],
        )

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1", max_parms=3)

        assert [p["name"] for p in result["parameters"]] == ["parm0", "parm1", "parm2"]
        assert "warning" not in result

    def test_get_parameter_schema_skip_folders(self, connected_to):
        """Test that folder/separator parameters are skipped."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1",
            name="sphere1",
            node_type="sphere",
            params={"folder0": 0, "radx": 1.0, "sep1": []},
        )
        mock_hou.add_node(sphere)

        # Mix of real parameters and folders
        templates = [
            MockParmTemplate("folder0", "Tabs", mock_hou.parmTemplateType.FolderSet),
            MockParmTemplate(
                "radx", "Radius X", mock_hou.parmTemplateType.Float, default_value=[1.0]
            ),
            MockParmTemplate("sep1", "Separator", mock_hou.parmTemplateType.Separator),
        ]

        give_templates(sphere, templates)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1")

        assert result["status"] == "success"
        # Should only return radx, not folder or separator
        assert result["count"] == 1
        assert result["parameters"][0]["name"] == "radx"

    def test_a_named_separator_is_refused_not_listed_as_nothing(self, connected_to):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        sphere = MockHouNode(path="/obj/geo1/s", name="s", node_type="sphere", params={"sep1": []})
        mock_hou.node("/obj").createNode("geo", "geo1").attach(sphere)
        give_templates(
            sphere, [MockParmTemplate("sep1", "Separator", mock_hou.parmTemplateType.Separator)]
        )

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/s", "sep1")

        assert (result["status"], result["kind"]) == ("error", "invalid_input")
        assert "sep1 on /obj/geo1/s is layout (Separator)" in result["message"]

    def test_get_parameter_schema_toggle_parameter(self, connected_to):
        """Test getting schema for a toggle parameter."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        mock_hou.node("/obj").createNode("geo", "geo1")
        node = MockHouNode(
            path="/obj/geo1/box1", name="box1", node_type="box", params={"consolidatepts": True}
        )
        mock_hou.add_node(node)

        toggle_template = MockParmTemplate(
            name="consolidatepts",
            label="Consolidate Points",
            parm_type=mock_hou.parmTemplateType.Toggle,
            default_value=[False],
        )

        mock_parm = node.parm("consolidatepts")
        mock_parm.parmTemplate = MagicMock(return_value=toggle_template)
        mock_parm.eval = MagicMock(return_value=True)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/box1", "consolidatepts")

        assert result["status"] == "success"
        param = result["parameters"][0]

        assert param["name"] == "consolidatepts"
        assert param["type"] == "toggle"
        assert param["is_animatable"] is False
        assert param["current_value"] is True

    def test_get_parameter_schema_node_not_found(self, connected_to):
        """Test error handling when node doesn't exist."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/nonexistent")

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_get_parameter_schema_parameter_not_found(self, connected_to):
        """Test error handling when specific parameter doesn't exist."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        mock_hou.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(
            path="/obj/geo1/sphere1", name="sphere1", node_type="sphere", params={"radx": 1.0}
        )
        mock_hou.add_node(sphere)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1", "nonexistent_param")

        assert result["status"] == "error"
        assert "Parameter not found" in result["message"]

    def test_get_parameter_schema_int_parameter(self, connected_to):
        """Test getting schema for an integer parameter."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        mock_hou.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(
            path="/obj/geo1/grid1", name="grid1", node_type="grid", params={"rows": 10}
        )
        mock_hou.add_node(grid)

        rows_template = MockParmTemplate(
            name="rows",
            label="Rows",
            parm_type=mock_hou.parmTemplateType.Int,
            default_value=[10],
            min_val=1,
            max_val=1000,
        )

        mock_parm = grid.parm("rows")
        mock_parm.parmTemplate = MagicMock(return_value=rows_template)
        mock_parm.eval = MagicMock(return_value=10)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/grid1", "rows")

        assert result["status"] == "success"
        param = result["parameters"][0]

        assert param["name"] == "rows"
        assert param["type"] == "int"
        assert param["default"] == 10
        assert param["min"] == 1
        assert param["max"] == 1000
        assert param["is_animatable"] is True

    def test_get_parameter_schema_string_parameter(self, connected_to):
        """Test getting schema for a string parameter."""
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()

        mock_hou.node("/obj").createNode("geo", "geo1")
        node = MockHouNode(
            path="/obj/geo1/file1",
            name="file1",
            node_type="file",
            params={"file": "/path/to/geo.bgeo"},
        )
        mock_hou.add_node(node)

        file_template = MockParmTemplate(
            name="file",
            label="Geometry File",
            parm_type=mock_hou.parmTemplateType.String,
            default_value=[""],
        )

        mock_parm = node.parm("file")
        mock_parm.parmTemplate = MagicMock(return_value=file_template)
        mock_parm.eval = MagicMock(return_value="/path/to/geo.bgeo")

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/file1", "file")

        assert result["status"] == "success"
        param = result["parameters"][0]

        assert param["name"] == "file"
        assert param["type"] == "string"
        assert param["is_animatable"] is False
        assert param["current_value"] == "/path/to/geo.bgeo"


class TestSchemaReadsOnlyWhatHoudiniGives:
    """[LAW:no-silent-failure] A part Houdini will not give is Unread, never a guess."""

    def _schema_of(
        self, connected_to, template: MockParmTemplate, value: Any, **parm_attrs: Any
    ) -> dict[str, Any]:
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = MockHouNode(path="/obj/n", name="n", node_type="null", params={"p": value})
        mock_hou.add_node(node)
        parm = node.parm("p")
        parm.parmTemplate = MagicMock(return_value=template)
        for attr, fake in parm_attrs.items():
            setattr(parm, attr, fake)
        connected_to(mock_hou)
        [param] = get_parameter_schema("/obj/n", "p")["parameters"]
        return param

    def test_a_menu_reports_its_real_default(self, connected_to):
        # Houdini gives a Menu's default as one int, not a tuple to index.
        menu = MockParmTemplate("p", "P", "Menu", default_value=2)
        assert self._schema_of(connected_to, menu, 2)["default"] == 2

    def test_a_toggle_reports_its_real_default(self, connected_to):
        toggle = MockParmTemplate("p", "P", "Toggle", default_value=True)
        assert self._schema_of(connected_to, toggle, 1)["default"] is True

    def test_a_ramp_has_no_default_value(self, connected_to):
        # Houdini gives a ramp template's defaultValue() as its count of points
        ramp = MockParmTemplate("p", "P", "Ramp", default_value=2)
        assert "default" not in self._schema_of(connected_to, ramp, None)

    def test_a_default_houdini_will_not_give_is_unread(self, connected_to):
        template = MockParmTemplate("p", "P", "Float", num_components=3)
        template.defaultValue = MagicMock(side_effect=RuntimeError("no default"))  # type: ignore[method-assign]
        param = self._schema_of(connected_to, template, 1.0)
        assert param["default"] == {"unread": "RuntimeError: no default"}

    def test_a_range_houdini_will_not_give_is_unread(self, connected_to):
        template = MockParmTemplate("p", "P", "Float", min_val=0.0, max_val=1.0)
        template.minValue = MagicMock(side_effect=RuntimeError("no range"))  # type: ignore[method-assign]
        param = self._schema_of(connected_to, template, 0.5)
        assert (param["min"], param["max"]) == ({"unread": "RuntimeError: no range"}, 1.0)

    def test_menu_items_houdini_will_not_give_are_unread_not_empty(self, connected_to):
        menu = MockParmTemplate("p", "P", "Menu", default_value=0)
        param = self._schema_of(
            connected_to, menu, 0, menuLabels=MagicMock(side_effect=RuntimeError("no menu"))
        )
        assert param["menu_items"] == {"unread": "RuntimeError: no menu"}


class TestMenuValueDomain:
    """A menu offers each item as the value set_parameter takes and eval reports."""

    def _menu(
        self, connected_to, tokens: list[str], value: int, default: int, use_token: bool = False
    ) -> dict[str, Any]:
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = MockHouNode(path="/obj/n", name="n", node_type="null", params={"m": value})
        mock_hou.add_node(node)
        template = MockParmTemplate(
            "m", "M", "Menu", default_value=default, menu_use_token=use_token
        )
        parm = node.parm("m")
        parm.parmTemplate = MagicMock(return_value=template)
        give_menu(parm, tokens, [t.upper() for t in tokens])
        connected_to(mock_hou)
        [param] = get_parameter_schema("/obj/n", "m")["parameters"]
        return param

    @staticmethod
    def _values(param: dict[str, Any]) -> list[Any]:
        return [item["value"] for item in param["menu_items"]]

    def test_an_index_menu_offers_each_item_by_index(self, connected_to):
        # The items come from the parm alone, as a script-generated menu's do.
        param = self._menu(connected_to, ["poly", "mesh", "polymesh"], value=1, default=0)
        assert self._values(param) == [0, 1, 2]
        assert param["current_value"] in self._values(param)
        assert param["default"] in self._values(param)

    def test_an_index_menu_with_numeric_tokens_still_offers_indexes(self, connected_to):
        # attribcreate's precision: '8' is the first item, not index 8
        param = self._menu(connected_to, ["8", "16", "32", "64", "auto"], value=4, default=4)
        assert self._values(param) == [0, 1, 2, 3, 4]

    def test_a_use_token_menu_offers_each_tokens_integer(self, connected_to):
        # fasciasolver's stiffness exponent: eval and the default are the token's integer
        param = self._menu(connected_to, ["10", "9", "8"], value=8, default=9, use_token=True)
        assert self._values(param) == [10, 9, 8]
        assert param["current_value"] in self._values(param)
        assert param["default"] in self._values(param)

    def test_a_separator_is_not_offered_but_keeps_its_index(self, connected_to):
        # boolean's operation: "seam" keeps index 2, after the separator at 1
        param = self._menu(connected_to, ["union", "_separator_", "seam"], value=2, default=0)
        assert param["menu_items"] == [
            {"label": "UNION", "value": 0},
            {"label": "SEAM", "value": 2},
        ]

    def test_a_use_token_menu_offers_no_separator(self, connected_to):
        # TOP nameconflict separates its integer tokens
        param = self._menu(
            connected_to, ["0", "_separator_", "6"], value=6, default=0, use_token=True
        )
        assert self._values(param) == [0, 6]

    def test_each_menu_read_logs_its_domain_and_what_it_offered(self, connected_to, caplog):
        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.tools.parameters"):
            self._menu(connected_to, ["0", "_separator_", "6"], value=6, default=0, use_token=True)
        assert "menu items parm=m use_token=True offered=2 separators=1" in caplog.messages

    def test_a_use_token_menu_with_a_non_integer_token_is_unread(self, connected_to):
        param = self._menu(connected_to, ["3", "b"], value=0, default=0, use_token=True)
        assert param["menu_items"] == {
            "unread": "ValueError: invalid literal for int() with base 10: 'b'"
        }


class TestMultiparmInstances:
    """A multiparm's instances are listed as the parameters the node has."""

    def _attribcreate(self) -> tuple[MockHouModule, MockHouNode]:
        # As Houdini 22 lists attribcreate with numattr=2: the count is a
        # multiparm folder, each instance's parms carry its number, and no
        # parm is named by its template's type#.
        mock_hou = create_mock_hou_with_parm_types()
        node = MockHouNode(
            path="/obj/geo1/n",
            name="n",
            node_type="attribcreate",
            params={
                "numattr": 2,
                "type1": 0,
                "value1v": ParmTuple(("value1v1", "value1v2"), (1.0, 0.0)),
                "type2": 2,
            },
        )
        mock_hou.node("/obj").createNode("geo", "geo1").attach(node)
        give_templates(
            node,
            [
                MockParmTemplate("numattr", "Attributes", "Folder", default_value=1),
                MockParmTemplate("type1", "Type", "Menu", default_value=0),
                MockParmTemplate("value1v", "Value", "Float", num_components=2),
                MockParmTemplate("type2", "Type", "Menu", default_value=0),
            ],
        )
        for name in ("type1", "type2"):
            give_menu(node.parm(name), ["float", "int", "string"], ["Float", "Integer", "String"])
        return mock_hou, node

    def test_the_listing_holds_each_instance_with_its_value(self, connected_to):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou, _ = self._attribcreate()
        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/n")

        assert [(p["name"], p["current_value"]) for p in result["parameters"]] == [
            ("numattr", 2),
            ("type1", 0),
            ("value1v", [1.0, 0.0]),
            ("type2", 2),
        ]
        assert result["parameters"][3]["menu_items"] == [
            {"label": "Float", "value": 0},
            {"label": "Integer", "value": 1},
            {"label": "String", "value": 2},
        ]

    def test_a_named_instance_is_the_entry_the_listing_holds(self, connected_to):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou, _ = self._attribcreate()
        connected_to(mock_hou)
        listed = get_parameter_schema("/obj/geo1/n")["parameters"]

        assert get_parameter_schema("/obj/geo1/n", "type2")["parameters"] == [listed[3]]
        assert get_parameter_schema("/obj/geo1/n", "value1v")["parameters"] == [listed[2]]

    def test_the_count_is_listed_as_the_parameter_that_makes_instances(self, connected_to):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou, _ = self._attribcreate()
        connected_to(mock_hou)
        [count] = get_parameter_schema("/obj/geo1/n", "numattr")["parameters"]

        assert count == {
            "name": "numattr",
            "label": "Attributes",
            "current_value": 2,
            "type": "multiparm",
            "is_animatable": False,
            "default": 1,
        }


class TestTheReadRunsInsideHoudini:
    """The schema is read where the parameters live: one call into Houdini, one
    JSON answer back, whatever the node's parameter count."""

    @staticmethod
    def _node_of(mock_hou: MockHouModule, name: str, count: int) -> MockHouNode:
        node = MockHouNode(
            path=f"/obj/{name}",
            name=name,
            node_type="null",
            params={f"p{i}": float(i) for i in range(count)},
        )
        mock_hou.add_node(node)
        give_templates(node, [MockParmTemplate(f"p{i}", f"P{i}", "Float") for i in range(count)])
        return node

    def test_the_server_asks_houdini_once_and_reads_no_parameter_itself(
        self, connected_to, monkeypatch
    ):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = self._node_of(mock_hou, "n", 3)
        for method in ("parmTuples", "parm", "parmTuple"):
            setattr(
                node, method, MagicMock(side_effect=AssertionError(f"{method} from the server"))
            )
        parameters = [{"name": "p0", "type": "float", "current_value": 0.0}]
        schema = SimpleNamespace(
            parameter_schema=MagicMock(
                return_value=json.dumps(
                    {"parameters": parameters, "listed": 3, "menus": [], "refused_reads": []}
                )
            )
        )
        monkeypatch.setitem(sys.modules, "houdini_mcp_schema", schema)
        connected_to(mock_hou)

        result = get_parameter_schema("/obj/n", "p0", max_parms=1)

        schema.parameter_schema.assert_called_once_with(mock_hou, node, "p0", 1)
        assert (result["parameters"], result["count"]) == (parameters, 1)
        assert result["warning"] == "Parameters limited to the first 1 of 3 parameters"

    def test_a_houdini_without_the_schema_read_is_told_to_install_the_plugin(
        self, connected_to, monkeypatch
    ):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        self._node_of(mock_hou, "n", 1)
        monkeypatch.setitem(sys.modules, "houdini_mcp_schema", None)
        connected_to(mock_hou)

        result = get_parameter_schema("/obj/n")

        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert (
            "Houdini cannot import houdini_mcp_schema (ModuleNotFoundError: " in result["message"]
        )
        assert "Install the plugin from the same release" in result["message"]

    def test_a_read_houdini_refused_is_an_event_on_the_server(self, connected_to, caplog):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = self._node_of(mock_hou, "n", 1)
        node.parm("p0").parmTemplate().defaultValue = MagicMock(
            side_effect=RuntimeError("no default")
        )
        connected_to(mock_hou)

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
            get_parameter_schema("/obj/n")

        assert "unread default of p0 reason=RuntimeError: no default" in caplog.messages

    def test_a_value_refused_is_logged_under_the_name_every_parameter_read_uses(
        self, connected_to, caplog
    ):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = self._node_of(mock_hou, "n", 1)
        node.parm("p0").eval = MagicMock(side_effect=RuntimeError("cook failed"))
        connected_to(mock_hou)

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
            result = get_parameter_schema("/obj/n")

        assert result["parameters"][0]["current_value"] == {"unread": "RuntimeError: cook failed"}
        assert caplog.messages == ["unread parameter p0 reason=RuntimeError: cook failed"]

    def test_a_value_shaped_like_an_unread_is_a_value_and_no_refusal(self, connected_to, caplog):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = self._node_of(mock_hou, "n", 1)
        node.parm("p0").set({"unread": "a key the user set"})
        connected_to(mock_hou)

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
            result = get_parameter_schema("/obj/n")

        assert result["parameters"][0]["current_value"] == {"unread": "a key the user set"}
        assert caplog.messages == []

    def test_a_template_value_with_no_json_form_is_converted_as_a_parm_value_is(self, connected_to):
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = self._node_of(mock_hou, "n", 1)
        template = node.parm("p0").parmTemplate()
        template.defaultValue = MagicMock(return_value=(SimpleNamespace(path=lambda: "/obj/d"),))
        template.minValue = MagicMock(return_value=SimpleNamespace(path=lambda: "/obj/lo"))
        connected_to(mock_hou)

        result = get_parameter_schema("/obj/n")

        assert result["status"] == "success", result
        assert (result["parameters"][0]["default"], result["parameters"][0]["min"]) == (
            "/obj/d",
            "/obj/lo",
        )

    def test_the_read_costs_the_link_the_same_requests_whatever_the_parameter_count(
        self, monkeypatch, reset_connection_state
    ):
        """Over a real rpyc link: a schema of 40 parameters sends exactly the
        requests one of 2 does, so no parameter or template crosses on its own."""
        from houdini_mcp.connection import houdini
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        for name, count in (("warm", 1), ("few", 2), ("many", 40)):
            self._node_of(mock_hou, name, count)
        # the served side is this interpreter: the hou it imports is the double
        monkeypatch.setitem(sys.modules, "hou", mock_hou)

        with served_connection("") as conn:
            with patch("houdini_mcp.connection.handshake") as handshake:
                handshake.connect.return_value = conn
                houdini().connect("localhost", 18811)
            # the plugin module's import and rpyc's first sight of each class
            # are paid once a link, not once a read
            get_parameter_schema("/obj/warm")
            send = conn.sync_request

            def requests_of(path: str) -> int:
                sent: list[Any] = []

                def counted(*request: Any) -> Any:
                    sent.append(request[0])
                    return send(*request)

                conn.sync_request = counted
                try:
                    result = get_parameter_schema(path, max_parms=100)
                finally:
                    conn.sync_request = send
                assert result["status"] == "success", result
                return len(sent)

            try:
                few, many = requests_of("/obj/few"), requests_of("/obj/many")
            finally:
                houdini().close()

        # [LAW:verifiable-goals] at least one request, or the count measured nothing
        assert 0 < few == many < 20
