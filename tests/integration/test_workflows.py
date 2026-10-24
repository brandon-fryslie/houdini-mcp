"""Multi-tool SOP workflows against a live Houdini, calling the tool functions directly.

1. Build from scratch (sphere → xform → color → OUT)
2. Augment an existing scene (insert mountain between grid → noise)
3. Parameters (discover → set → verify)
4. Errors (detect → fix → verify)
"""

from typing import Any

import pytest

from houdini_mcp.tools import (
    connect_nodes,
    create_node,
    disconnect_node_input,
    get_geo_summary,
    get_node_info,
    get_parameter_schema,
    list_children,
    set_node_flags,
    set_parameter,
)


class TestBuildFromScratch:
    """Building a complete SOP network from scratch."""

    async def test_build_sphere_xform_color_out_chain(
        self, at: dict[str, Any], summarizing: dict[str, Any]
    ) -> None:
        """Build and verify: sphere → xform → color → OUT."""

        # Create geo container
        geo_result = create_node("geo", "/obj", "test_build", **at)
        assert geo_result["status"] == "success"
        geo_path = geo_result["node_path"]

        # Create sphere
        sphere = create_node("sphere", geo_path, "sphere1", **at)
        assert sphere["status"] == "success"
        sphere_path = sphere["node_path"]

        # Set sphere radius
        set_result = set_parameter(sphere_path, "rad", [2.0, 2.0, 2.0], **at)
        assert set_result["status"] == "success"

        # Create xform
        xform = create_node("xform", geo_path, "xform1", **at)
        assert xform["status"] == "success"
        xform_path = xform["node_path"]

        # Set translation
        set_result = set_parameter(xform_path, "t", [0.0, 3.0, 0.0], **at)
        assert set_result["status"] == "success"

        # Create color
        color = create_node("color", geo_path, "color1", **at)
        assert color["status"] == "success"
        color_path = color["node_path"]

        # Set color to red
        set_result = set_parameter(color_path, "color", [1.0, 0.0, 0.0], **at)
        assert set_result["status"] == "success"

        # Create OUT null
        out = create_node("null", geo_path, "OUT", **at)
        assert out["status"] == "success"
        out_path = out["node_path"]

        # Wire: sphere → xform → color → OUT
        conn1 = connect_nodes(sphere_path, xform_path, **at)
        assert conn1["status"] == "success"

        conn2 = connect_nodes(xform_path, color_path, **at)
        assert conn2["status"] == "success"

        conn3 = connect_nodes(color_path, out_path, **at)
        assert conn3["status"] == "success"

        # Set display flag on OUT
        flags = set_node_flags(out_path, display=True, render=True, **at)
        assert flags["status"] == "success"

        # Verify with get_geo_summary
        summary = await get_geo_summary(out_path, max_sample_points=10, **summarizing)
        assert summary["status"] == "success"
        assert summary["point_count"] > 0

        # Verify bounding box reflects translation (Y center ≈ 3.0)
        if summary.get("bounding_box"):
            center_y = summary["bounding_box"]["center"][1]
            assert abs(center_y - 3.0) < 0.1, f"Expected center Y ≈ 3.0, got {center_y}"


class TestAugmentExistingScene:
    """Augmenting an existing network by inserting a node."""

    async def test_insert_mountain_between_grid_and_noise(
        self, at: dict[str, Any], summarizing: dict[str, Any]
    ) -> None:
        """Insert mountain between grid → noise."""

        # Create initial network: grid → noise
        geo_result = create_node("geo", "/obj", "test_augment", **at)
        geo_path = geo_result["node_path"]

        grid = create_node("grid", geo_path, "grid1", **at)
        grid_path = grid["node_path"]
        noise = create_node("attribnoise", geo_path, "noise1", **at)

        noise_path = noise["node_path"]

        # Wire grid → noise
        connect_nodes(grid_path, noise_path, **at)

        # Verify initial connection using list_children
        children = list_children(geo_path, **at)
        assert children["status"] == "success"
        assert children["returned"] == 2

        # Find noise node in children
        noise_child = next(c for c in children["children"] if c["name"] == "noise1")
        assert len(noise_child["inputs"]) == 1
        assert noise_child["inputs"][0]["source_node"] == grid_path

        # Get geometry BEFORE mountain
        geo_before = await get_geo_summary(noise_path, max_sample_points=0, **summarizing)
        assert geo_before["status"] == "success"

        # Create mountain node
        mountain = create_node("mountain", geo_path, "mountain1", **at)
        mountain_path = mountain["node_path"]

        # Set mountain height
        set_parameter(mountain_path, "height", 2.0, **at)

        # Insert mountain: grid → mountain → noise
        # 1. Disconnect noise from grid
        disconnect_result = disconnect_node_input(noise_path, 0, **at)
        assert disconnect_result["status"] == "success"
        assert disconnect_result["disconnected"]["source_node"] == grid_path

        # 2. Connect grid → mountain
        connect_nodes(grid_path, mountain_path, **at)

        # 3. Connect mountain → noise
        connect_nodes(mountain_path, noise_path, **at)

        # Verify new connections with get_node_info
        noise_info = get_node_info(noise_path, **at)
        assert noise_info["status"] == "success"
        assert len(noise_info["input_connections"]) == 1
        assert noise_info["input_connections"][0]["source_node"] == mountain_path

        # Get geometry AFTER mountain
        geo_after = await get_geo_summary(noise_path, max_sample_points=0, **summarizing)
        assert geo_after["status"] == "success"

        # Verify geometry changed (mountain should affect bounding box)
        if geo_before.get("bounding_box") and geo_after.get("bounding_box"):
            size_before = geo_before["bounding_box"]["size"]
            size_after = geo_after["bounding_box"]["size"]

            # Y size should increase due to mountain height
            assert size_after[1] > size_before[1], "Mountain should increase Y size"


class TestParameterWorkflow:
    """Parameter schema discovery and intelligent setting."""

    async def test_parameter_schema_discovery_and_setting(
        self, at: dict[str, Any], summarizing: dict[str, Any]
    ) -> None:
        """Discover parameter schema and set values intelligently."""

        # Create sphere
        geo_result = create_node("geo", "/obj", "test_params", **at)
        geo_path = geo_result["node_path"]

        sphere = create_node("sphere", geo_path, "sphere1", **at)
        sphere_path = sphere["node_path"]

        # Discover all parameters
        all_params = get_parameter_schema(sphere_path, max_parms=50, **at)
        assert all_params["status"] == "success"
        assert all_params["count"] > 0

        # Find specific parameter
        rad_params = [p for p in all_params["parameters"] if p["name"] == "rad"]
        assert len(rad_params) == 1, "Should find 'rad' parameter"

        rad_param = rad_params[0]
        assert rad_param["type"] == "vector"
        assert rad_param["tuple_size"] == 3

        # Query specific parameter
        rad_schema = get_parameter_schema(sphere_path, parm_name="rad", **at)
        assert rad_schema["status"] == "success"
        assert len(rad_schema["parameters"]) == 1

        # Set parameter based on schema
        new_radius = [3.0, 3.0, 3.0]
        set_result = set_parameter(sphere_path, "rad", new_radius, **at)
        assert set_result["status"] == "success"

        # Verify parameter was set
        node_info = get_node_info(sphere_path, **at)
        assert node_info["status"] == "success"

        # Check individual components
        assert abs(node_info["parameters"]["radx"] - 3.0) < 0.001
        assert abs(node_info["parameters"]["rady"] - 3.0) < 0.001
        assert abs(node_info["parameters"]["radz"] - 3.0) < 0.001

        # Verify geometry reflects parameter change
        geo_summary = await get_geo_summary(sphere_path, max_sample_points=0, **summarizing)
        assert geo_summary["status"] == "success"

        # Sphere diameter should be ~6.0 (radius 3.0)
        if geo_summary.get("bounding_box"):
            size = geo_summary["bounding_box"]["size"]
            expected_diameter = 6.0
            assert abs(size[0] - expected_diameter) < 0.5, (
                f"Expected diameter ≈ {expected_diameter}"
            )

    @pytest.mark.parametrize(
        ("container", "node_type", "menu"),
        [
            ("geo", "sphere", "type"),  # index menu
            ("geo", "fasciasolver", "femtristiffnessexp"),  # use-token menu
            ("geo", "agentaddmldeformer", "provider"),  # script-generated index menu
            ("topnet", "attributecreate", "pdg_workitemgeneration"),  # generated, use-token
            ("geo", "boolean", "booleanop"),  # index menu with separators
            ("topnet", "attributerandomize", "nameconflict"),  # use-token menu with separators
        ],
    )
    def test_every_offered_menu_value_is_one_set_parameter_selects(
        self, at: dict[str, Any], container: str, node_type: str, menu: str
    ) -> None:
        """A menu's offered values, current value and default share set_parameter's domain."""
        parent = create_node(container, "/obj", "test_menu", **at)["node_path"]
        node_path = create_node(node_type, parent, "menu_node", **at)["node_path"]

        def schema() -> dict[str, Any]:
            [param] = get_parameter_schema(node_path, parm_name=menu, **at)["parameters"]
            assert param["type"] == "menu"
            return param

        param = schema()
        offered = [item["value"] for item in param["menu_items"]]
        assert len(offered) > 1
        assert param["current_value"] in offered
        assert param["default"] in offered

        for value in offered:
            assert set_parameter(node_path, menu, value, **at)["status"] == "success"
            assert schema()["current_value"] == value

    def test_a_multiparm_lists_its_instances_as_parameters(self, at: dict[str, Any]) -> None:
        """attribcreate's type# is a template; type1 and type2 are the parameters it lists."""
        geo = create_node("geo", "/obj", "test_menu", **at)["node_path"]
        node_path = create_node("attribcreate", geo, "attribs", **at)["node_path"]
        assert set_parameter(node_path, "numattr", 2, **at)["status"] == "success"

        result = get_parameter_schema(node_path, max_parms=1000, **at)

        assert result["status"] == "success"
        listed = {p["name"]: p for p in result["parameters"]}
        assert not [name for name in listed if "#" in name]
        assert (listed["numattr"]["type"], listed["numattr"]["current_value"]) == ("multiparm", 2)
        for name in ("type1", "type2"):
            # An Unread value is a dict; a read menu value is its int.
            assert isinstance(listed[name]["current_value"], int)
            assert isinstance(listed[name]["menu_items"], list)
        # Every listed name is a parameter the node has, read as the listing read it.
        for name, entry in listed.items():
            assert get_parameter_schema(node_path, parm_name=name, **at)["parameters"] == [entry]

    def test_a_named_component_is_its_vector(self, at: dict[str, Any]) -> None:
        geo = create_node("geo", "/obj", "test_component", **at)["node_path"]
        sphere = create_node("sphere", geo, "s", **at)["node_path"]

        [rad] = get_parameter_schema(sphere, parm_name="rad", **at)["parameters"]

        assert rad["name"] == "rad"
        assert get_parameter_schema(sphere, parm_name="radx", **at)["parameters"] == [rad]


class TestErrorHandling:
    """Error detection, diagnosis, and fixing."""

    async def test_detect_missing_input_error(
        self, at: dict[str, Any], summarizing: dict[str, Any]
    ) -> None:
        """Detect when a node has no input (where one is expected)."""

        geo_result = create_node("geo", "/obj", "test_errors", **at)
        geo_path = geo_result["node_path"]

        # Create noise without input
        noise = create_node("attribnoise", geo_path, "noise1", **at)
        noise_path = noise["node_path"]

        # Force cook
        noise_info = get_node_info(noise_path, cook_info="force_cook", **at)
        assert noise_info["status"] == "success"
        assert noise_info["cook_info"]["errors"], noise_info

        # Noise without input cooks to no geometry at all
        geo = await get_geo_summary(noise_path, max_sample_points=0, **summarizing)
        assert geo["status"] == "error", geo
        assert geo["kind"] == "operation_failed", geo

        # Fix: Add grid input
        grid = create_node("grid", geo_path, "grid1", **at)
        grid_path = grid["node_path"]

        connect_nodes(grid_path, noise_path, **at)

        # Verify fix
        geo_after = await get_geo_summary(noise_path, max_sample_points=0, **summarizing)
        assert geo_after["status"] == "success"
        assert geo_after["point_count"] > 0, "Should have points after connecting input"

    def test_invalid_parameter_type_error(self, at: dict[str, Any]) -> None:
        """Test that invalid parameter types are rejected."""

        geo_result = create_node("geo", "/obj", "test_param_errors", **at)
        geo_path = geo_result["node_path"]

        sphere = create_node("sphere", geo_path, "sphere1", **at)
        sphere_path = sphere["node_path"]

        # Try to set vector parameter with scalar (should fail)
        bad_result = set_parameter(sphere_path, "rad", 5.0, **at)
        assert bad_result["status"] == "error"
        assert "tuple" in bad_result["message"].lower()

        # Use schema to find correct type
        schema = get_parameter_schema(sphere_path, parm_name="rad", **at)
        assert schema["status"] == "success"

        param = schema["parameters"][0]
        assert param["type"] == "vector"

        # Set with correct type
        good_result = set_parameter(sphere_path, "rad", [5.0, 5.0, 5.0], **at)
        assert good_result["status"] == "success"

    def test_incompatible_node_connection_error(self, at: dict[str, Any]) -> None:
        """Test that incompatible node types can't be connected."""

        # Create SOP node
        geo_result = create_node("geo", "/obj", "test_incompatible", **at)
        geo_path = geo_result["node_path"]

        grid = create_node("grid", geo_path, "grid1", **at)
        grid_path = grid["node_path"]

        # Create OBJ node
        cam = create_node("cam", "/obj", "cam1", **at)
        cam_path = cam["node_path"]

        # Try to connect SOP to OBJ (should fail)
        bad_conn = connect_nodes(grid_path, cam_path, **at)
        assert bad_conn["status"] == "error"
        assert (
            "incompatible" in bad_conn["message"].lower()
            or "category" in bad_conn["message"].lower()
        )

    async def test_safe_geometry_access_pattern(
        self, at: dict[str, Any], summarizing: dict[str, Any]
    ) -> None:
        """A force-cooked SOP needs no cook, and its geometry reads."""

        geo_result = create_node("geo", "/obj", "test_safe_access", **at)
        geo_path = geo_result["node_path"]

        box = create_node("box", geo_path, "box1", **at)
        box_path = box["node_path"]

        # Step 1: force cook
        node_info = get_node_info(box_path, cook_info="force_cook", **at)
        assert node_info["status"] == "success"
        assert "cook_info" in node_info

        assert node_info["cook_info"]["needs_to_cook"] is False

        # Step 2: a cooked node's geometry reads
        geo = await get_geo_summary(box_path, max_sample_points=0, **summarizing)
        assert geo["status"] == "success"
        assert geo["point_count"] > 0
