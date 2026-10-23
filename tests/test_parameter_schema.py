"""Tests for the get_parameter_schema function."""

from typing import Any
from unittest.mock import MagicMock

from tests.conftest import MockHouModule, MockHouNode


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
        menu_items: list[str] | None = None,
        menu_labels: list[str] | None = None,
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
        self._menu_items = menu_items or []
        self._menu_labels = menu_labels or []

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

    def menuItems(self) -> list[str]:
        return self._menu_items

    def menuLabels(self) -> list[str]:
        return self._menu_labels


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

        # Mock parmTemplates method
        sphere.parmTemplates = MagicMock(return_value=[radx_template])

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

        geo.parmTemplates = MagicMock(return_value=[t_template])

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
            menu_items=["poly", "mesh", "polymesh"],
            menu_labels=["Polygon", "Mesh", "Polygon Mesh"],
        )

        sphere.parmTemplates = MagicMock(return_value=[type_template])

        mock_parm = sphere.parm("type")
        mock_parm.parmTemplate = MagicMock(return_value=type_template)
        mock_parm.eval = MagicMock(return_value=0)

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
        assert param["menu_items"][0] == {"label": "Polygon", "value": "poly"}
        assert param["menu_items"][1] == {"label": "Mesh", "value": "mesh"}
        assert param["menu_items"][2] == {"label": "Polygon Mesh", "value": "polymesh"}

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
                menu_items=["poly", "mesh"],
                menu_labels=["Polygon", "Mesh"],
            ),
        ]

        sphere.parmTemplates = MagicMock(return_value=templates)

        # Mock individual parms
        for template in templates:
            mock_parm = sphere.parm(template.name())
            if mock_parm:
                mock_parm.parmTemplate = MagicMock(return_value=template)
                mock_parm.eval = MagicMock(return_value=sphere._params.get(template.name()))

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1")

        assert result["status"] == "success"
        assert result["count"] == 4
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

        sphere.parmTemplates = MagicMock(return_value=templates)

        connected_to(mock_hou)
        with caplog.at_level("INFO", logger="houdini_mcp.tools.parameters"):
            result = get_parameter_schema("/obj/geo1/sphere1", max_parms=5)

        assert result["status"] == "success"
        assert result["count"] == 5
        assert len(result["parameters"]) == 5
        assert result["warning"] == "Parameters limited to the first 5 of 20 parameters"
        assert "parameter schema node=/obj/geo1/sphere1 listed=5 of=20" in caplog.messages

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
            params={f"parm{i}": 0.0 for i in range(3)},
        )
        mock_hou.add_node(sphere)
        layout = mock_hou.parmTemplateType
        sphere.parmTemplates = MagicMock(
            return_value=[
                MockParmTemplate("folder1", "Folder", layout.Folder),
                MockParmTemplate("sep1", "Separator", layout.Separator),
                *(
                    MockParmTemplate(f"parm{i}", f"P{i}", layout.Float, default_value=[0.0])
                    for i in range(3)
                ),
                MockParmTemplate("sep2", "Separator", layout.Separator),
            ]
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
            path="/obj/geo1/sphere1", name="sphere1", node_type="sphere", params={"radx": 1.0}
        )
        mock_hou.add_node(sphere)

        # Mix of real parameters and folders
        templates = [
            MockParmTemplate("folder1", "Folder", mock_hou.parmTemplateType.Folder),
            MockParmTemplate(
                "radx", "Radius X", mock_hou.parmTemplateType.Float, default_value=[1.0]
            ),
            MockParmTemplate("sep1", "Separator", mock_hou.parmTemplateType.Separator),
        ]

        sphere.parmTemplates = MagicMock(return_value=templates)

        # Mock the radx parm
        mock_parm = sphere.parm("radx")
        mock_parm.parmTemplate = MagicMock(return_value=templates[1])
        mock_parm.eval = MagicMock(return_value=1.0)

        connected_to(mock_hou)
        result = get_parameter_schema("/obj/geo1/sphere1")

        assert result["status"] == "success"
        # Should only return radx, not folder or separator
        assert result["count"] == 1
        assert result["parameters"][0]["name"] == "radx"

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

        node.parmTemplates = MagicMock(return_value=[toggle_template])

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

        grid.parmTemplates = MagicMock(return_value=[rows_template])

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

        node.parmTemplates = MagicMock(return_value=[file_template])

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

    def _schema_of(self, connected_to, template: MockParmTemplate, value: Any) -> dict[str, Any]:
        from houdini_mcp.tools import get_parameter_schema

        mock_hou = create_mock_hou_with_parm_types()
        node = MockHouNode(path="/obj/n", name="n", node_type="null", params={"p": value})
        mock_hou.add_node(node)
        node.parm("p").parmTemplate = MagicMock(return_value=template)
        connected_to(mock_hou)
        [param] = get_parameter_schema("/obj/n", "p")["parameters"]
        return param

    def test_a_menu_reports_its_real_default(self, connected_to):
        # Houdini gives a Menu's default as one int, not a tuple to index.
        menu = MockParmTemplate("p", "P", "Menu", default_value=2, menu_labels=["a", "b", "c"])
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
        menu.menuLabels = MagicMock(side_effect=RuntimeError("no menu"))  # type: ignore[method-assign]
        param = self._schema_of(connected_to, menu, 0)
        assert param["menu_items"] == {"unread": "RuntimeError: no menu"}


class TestFlattenParmTemplates:
    def test_every_nested_template_is_reached_folder_first(self):
        from houdini_mcp.tools._common import _flatten_parm_templates

        mock_hou = create_mock_hou_with_parm_types()
        leaf = MockParmTemplate("bind#", "Bind", "String")
        multiparm = MockParmTemplate("bindings", "Bindings", "Folder")
        multiparm.parmTemplates = MagicMock(return_value=[leaf])  # type: ignore[attr-defined]
        tabs = MockParmTemplate("folder0", "Code", "Folder")
        tabs.parmTemplates = MagicMock(return_value=[multiparm])  # type: ignore[attr-defined]
        top = MockParmTemplate("top", "Top", "Float")

        flat = _flatten_parm_templates(mock_hou, [tabs, top])

        assert [t.name() for t in flat] == ["folder0", "bindings", "bind#", "top"]

    def test_a_folder_set_is_listed_and_not_descended(self):
        from houdini_mcp.tools._common import _flatten_parm_templates

        mock_hou = create_mock_hou_with_parm_types()
        # hou.FolderSetParmTemplate holds folder names only: it has no parmTemplates()
        folder_set = MockParmTemplate("fs", "Tabs", "FolderSet")
        top = MockParmTemplate("top", "Top", "Float")

        flat = _flatten_parm_templates(mock_hou, [folder_set, top])

        assert [t.name() for t in flat] == ["fs", "top"]
