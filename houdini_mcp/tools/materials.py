"""Material and shader tools.

This module provides tools for creating, assigning, and inspecting
materials and shaders in Houdini.
"""

import logging
from typing import Any

from ._common import (
    InvalidInput,
    OperationFailed,
    _add_response_metadata,
    ensure_connected,
    handle_connection_errors,
    node_at,
    node_category,
)
from .parms import ParmNotFound, read_present, resolve, write_all

logger = logging.getLogger("houdini_mcp.tools.materials")


@handle_connection_errors("create_material")
def create_material(
    material_type: str = "principledshader",
    name: str | None = None,
    parent_path: str = "/mat",
    parameters: dict[str, Any] | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Create a new material/shader node.

    Creates a material node in the specified context (typically /mat or /shop).
    Supports common material types like Principled Shader, MaterialX, and classic shaders.

    Args:
        material_type: Type of material to create. Common types:
            - "principledshader": Houdini's standard PBR shader (recommended)
            - "mtlxstandard_surface": MaterialX Standard Surface
            - "classicshader": Classic Mantra shader
            - "arnold::standard_surface": Arnold Standard Surface (if Arnold installed)
        name: Optional name for the material. Auto-generated if not provided.
        parent_path: Parent context path (default: "/mat", alternative: "/shop")
        parameters: Optional dict of parameter values to set on the material.
            Common principledshader parameters:
            - basecolor: [r, g, b] base color
            - rough: float roughness (0-1)
            - metallic: float metallic (0-1)
            - ior: float index of refraction
            - basecolor_texture: string path to texture file

    Returns:
        Dict with:
        - status: "success", "partial" (a parameter failed) or "error"
        - material_path: Path to the created material node
        - material_name: Name of the material
        - material_type: Type of material created
        - updated: The parameters that were set, as {name, value}
        - failed: The parameters that were not, as {name, kind, reason}

    Examples:
        create_material()  # Create default principled shader
        create_material("principledshader", "red_metal",
                       parameters={"basecolor": [1, 0, 0], "metallic": 1.0})
        create_material("mtlxstandard_surface", "gold_mtlx")
    """
    hou = ensure_connected(host, port)

    # Find or create parent context
    parent = hou.node(parent_path)
    if parent is None:
        # Try to create /mat if it doesn't exist
        if parent_path == "/mat":
            try:
                parent = hou.node("/").createNode("matnet", "mat")
            except Exception as e:
                raise OperationFailed(
                    f"Cannot find or create material context: {parent_path}: {e}",
                    parent_path=parent_path,
                ) from e
        else:
            raise InvalidInput(f"Parent context not found: {parent_path}", path=parent_path)

    # Generate name if not provided
    if not name:
        name = f"{material_type}_1"

    # Create the material node
    try:
        mat_node = parent.createNode(material_type, name)
    except Exception as e:
        raise OperationFailed(
            f"Failed to create material of type '{material_type}': {str(e)}. "
            f"Check if this material type is available in your Houdini installation.",
            material_type=material_type,
        ) from e

    return write_all(mat_node, parameters or {}).fields(
        "create_material",
        created=[mat_node],
        material_path=mat_node.path(),
        material_name=mat_node.name(),
        material_type=material_type,
    )


@handle_connection_errors("assign_material")
def assign_material(
    geometry_path: str,
    material_path: str,
    group: str = "",
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Assign a material to geometry.

    Creates a Material SOP inside the geometry node to apply the material.
    If a Material SOP already exists, updates it instead.

    Args:
        geometry_path: Path to the geometry OBJ node (e.g., "/obj/geo1")
        material_path: Path to the material node (e.g., "/mat/principledshader1")
        group: Optional primitive group to apply material to (empty = all primitives)

    Returns:
        Dict with:
        - status: "success", "partial" (a write failed beside a write or a newly
          created Material SOP) or "error" (nothing changed; the fields ride on it)
        - geometry_path: Path to the geometry node
        - material_path: Path to the assigned material
        - material_sop_path: Path to the Material SOP that was created/modified
        - method: "material_sop" or "shop_materialpath"
        - updated / failed: each write, as {name, value} / {name, kind, reason}

    Examples:
        assign_material("/obj/geo1", "/mat/red_metal")
        assign_material("/obj/geo1", "/mat/gold", group="top_faces")
    """
    hou = ensure_connected(host, port)

    # Validate geometry node
    geo_node = node_at(hou, geometry_path, "Geometry node")

    # Check if it's an OBJ-level geo node
    node_type = geo_node.type().name()
    category = node_category(geo_node)

    if category != "Object":
        raise InvalidInput(
            f"Node {geometry_path} is not an Object-level node. "
            f"Expected geo node, got {category}/{node_type}",
            path=geometry_path,
        )

    # The material must exist before anything is written to the geometry.
    node_at(hou, material_path, "Material")

    # Method 1: Try setting shop_materialpath on the OBJ node directly
    if not group:
        try:
            resolve(geo_node, "shop_materialpath")
        except ParmNotFound:
            pass
        else:
            return write_all(geo_node, {"shop_materialpath": material_path}).fields(
                "assign_material",
                geometry_path=geometry_path,
                material_path=material_path,
                method="shop_materialpath",
            )

    # Method 2: Create/update Material SOP inside the geometry
    # Find the display node to connect to
    display_node = geo_node.displayNode()
    if display_node is None:
        # No display node, try to find any SOP
        children = geo_node.children()
        if not children:
            raise InvalidInput(
                f"Geometry node {geometry_path} has no SOP nodes inside", path=geometry_path
            )
        display_node = children[-1]

    # Check if a material SOP already exists and is connected
    existing_mat_sop = None
    for child in geo_node.children():
        if child.type().name() == "material":
            existing_mat_sop = child
            break

    created: list[Any] = []
    if existing_mat_sop:
        mat_sop = existing_mat_sop
    else:
        # Create new Material SOP
        mat_sop = geo_node.createNode("material", "material1")
        mat_sop.setFirstInput(display_node)
        mat_sop.setDisplayFlag(True)
        mat_sop.setRenderFlag(True)
        created.append(mat_sop)

    values = {"shop_materialpath1": material_path}
    if group:
        values["group1"] = group
    # [LAW:no-silent-failure] a SOP created here exists and is wired whatever the
    # writes did, so it counts as done; an existing one written nothing did nothing.
    return write_all(mat_sop, values).fields(
        "assign_material",
        created=created,
        geometry_path=geometry_path,
        material_path=material_path,
        material_sop_path=mat_sop.path(),
        method="material_sop",
        group=group if group else None,
    )


@handle_connection_errors("get_material_info")
def get_material_info(
    material_path: str,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Get detailed information about a material node.

    Returns material type, parameters, and texture references.

    Args:
        material_path: Path to the material node (e.g., "/mat/principledshader1")

    Returns:
        Dict with:
        - status: "success" or "error"
        - material_path: Path to the material
        - material_name: Name of the material
        - material_type: Type of material (e.g., "principledshader")
        - parameters: Dict of parameter names to current values
        - textures: List of texture file references found in parameters

    Examples:
        get_material_info("/mat/principledshader1")
        get_material_info("/mat/mtlxstandard_surface1")
    """
    hou = ensure_connected(host, port)

    mat_node = node_at(hou, material_path, "Material")

    # Get basic info
    result: dict[str, Any] = {
        "status": "success",
        "material_path": mat_node.path(),
        "material_name": mat_node.name(),
        "material_type": mat_node.type().name(),
        "parameters": {},
        "textures": [],
    }

    # Common material parameter names to include
    common_params = [
        # Principled Shader
        "basecolor",
        "basecolor_texture",
        "rough",
        "rough_texture",
        "metallic",
        "metallic_texture",
        "ior",
        "reflect",
        "reflecttint",
        "coat",
        "coatrough",
        "transparency",
        "transcolor",
        "dispersion",
        "sss",
        "sssdist",
        "ssscolor",
        "sheen",
        "sheentint",
        "emitcolor",
        "emitint",
        "opac",
        "opaccolor",
        # Normal/Bump
        "baseBumpAndNormal_enable",
        "baseNormal_texture",
        "baseBump_bumpTexture",
        # MaterialX Standard Surface
        "base",
        "base_color",
        "diffuse_roughness",
        "specular",
        "specular_color",
        "specular_roughness",
        "specular_IOR",
        "transmission",
        "transmission_color",
        "subsurface",
        "subsurface_color",
        "emission",
        "emission_color",
    ]

    result["parameters"] = read_present(hou, mat_node, common_params)
    result["textures"] = [
        {"parameter": parm_name, "path": value}
        for parm_name, value in result["parameters"].items()
        if isinstance(value, str)
        and any(ext in value.lower() for ext in [".jpg", ".png", ".exr", ".hdr", ".tif", ".tex"])
    ]
    result["parameter_count"] = len(result["parameters"])

    return _add_response_metadata(result)
