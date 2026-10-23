"""Houdini documentation fetching tool.

This module provides tools for fetching Houdini documentation from
the SideFX website. It does NOT require a Houdini connection.
"""

from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from pydantic import Field

from ._common import (
    InvalidInput,
    OperationFailed,
    TimedOut,
    handle_connection_errors,
)

# [LAW:types-are-the-program] the help types are the parameter's type, so the
# served schema lists them and the server refuses any other.
HelpType = Literal[
    "obj", "sop", "dop", "cop2", "chop", "vop", "lop", "top", "rop", "vex_function", "python_hou"
]

# Where each help type's pages live under the docs root.
_HELP_PATHS: dict[HelpType, str] = {
    "obj": "nodes/obj",
    "sop": "nodes/sop",
    "dop": "nodes/dop",
    "cop2": "nodes/cop2",
    "chop": "nodes/chop",
    "vop": "nodes/vop",
    "lop": "nodes/lop",
    "top": "nodes/top",
    "rop": "nodes/out",  # ROPs are under /out/
    "vex_function": "vex/functions",
    "python_hou": "hom/hou",
}

# [LAW:types-are-the-program] httpx refuses a zero or negative timeout, so the served
# schema refuses it first.
Seconds = Annotated[int, Field(ge=1)]


# [LAW:effects-at-boundaries] The one place the docs site's HTTP client is built. Tests
# replace it with a client over a fake transport, so the request put on the wire is
# what gets asserted.
def docs_client(timeout: Seconds) -> httpx.Client:
    """Build the client a docs page is fetched through, ``timeout`` seconds a phase."""
    # A page the site moves is still the page asked for; httpx follows only when told.
    return httpx.Client(timeout=timeout, follow_redirects=True)


@handle_connection_errors("get_houdini_help")
def get_houdini_help(
    help_type: HelpType,
    item_name: str,
    timeout: Seconds = 10,
) -> dict[str, Any]:
    """
    Fetch Houdini documentation from SideFX website.

    Retrieves and parses help documentation for nodes, VEX functions,
    and Python API. Helps AI understand Houdini concepts without
    hallucinating parameter names or functionality.

    NOTE: This tool does NOT require a Houdini connection - it fetches
    documentation directly from the SideFX website.

    Args:
        help_type: Type of documentation to fetch. Supported types:
            - "sop": SOP nodes (e.g., "box", "scatter", "vdbfrompolygons")
            - "obj": Object nodes (e.g., "geo", "cam", "light")
            - "dop": DOP nodes (e.g., "pyrosolver", "rbdpackedobject")
            - "cop2": COP nodes (e.g., "mosaic", "blur")
            - "chop": CHOP nodes (e.g., "math", "wave")
            - "vop": VOP nodes (e.g., "bind", "noise")
            - "lop": LOP/Solaris nodes (e.g., "usdimport", "materiallibrary")
            - "top": TOP/PDG nodes (e.g., "pythonscript", "wedge")
            - "rop": ROP nodes (e.g., "geometry", "karma")
            - "vex_function": VEX functions (e.g., "noise", "lerp", "chramp")
            - "python_hou": Python hou module classes (e.g., "Node", "Geometry")
        item_name: Name of the node or function (e.g., "box", "noise", "Node")
        timeout: Seconds the fetch may wait at each step - connecting, sending,
            reading - before it gives up (default: 10)

    Returns:
        Dict with:
        - status: "success" or "error"
        - title: Documentation title
        - url: Where the page was read from, after any redirect
        - description: Summary description
        - parameters: List of parameters with names, descriptions, and options
        - inputs: List of input connections (for nodes)
        - outputs: List of output connections (for nodes)
        - vex_info: Signature and return type (for VEX functions)
        - methods: Class methods (for Python hou module)

    Examples:
        get_houdini_help("sop", "box")  # Get box SOP documentation
        get_houdini_help("vex_function", "noise")  # Get VEX noise function docs
        get_houdini_help("python_hou", "Node")  # Get hou.Node class docs
        get_houdini_help("obj", "cam")  # Get camera object docs
    """
    # [LAW:parse-dont-validate] item_name is one page of its section: quoted whole, a
    # '/', '#' or '?' in it is part of the name, never a way to another page.
    page = quote(item_name, safe="")
    full_url = f"https://www.sidefx.com/docs/houdini/{_HELP_PATHS[help_type]}/{page}.html"

    try:
        with docs_client(timeout) as client:
            response = client.get(full_url)

        if response.status_code == 404:
            raise InvalidInput(
                f"Documentation not found for {help_type}/{item_name}. Check if the name is correct.",
                url=str(response.url),
            )

        if response.status_code != 200:
            raise OperationFailed(
                f"Failed to fetch help page. HTTP status: {response.status_code}",
                url=str(response.url),
            )

        soup = BeautifulSoup(response.text, "html.parser")

        # Extract title: the h1's text once the subtitle span is taken out, spaced as
        # the page spaces it, so a "hou." supertitle span abuts its class name
        # (hou.Node) and a Labs version span stays apart (Labs Align and Distribute 2.0).
        h1 = soup.find("h1", class_="title")
        if h1:
            subtitle = h1.find("span", class_="subtitle")
            subtitle_text = subtitle.extract().get_text(strip=True) if subtitle else ""
            title_text = " ".join(h1.get_text().split())
            title = f"{title_text} - {subtitle_text}" if subtitle_text else title_text
        else:
            title = item_name

        # Extract description/summary
        summary_p = soup.find("p", class_="summary")
        description = summary_p.get_text(strip=True) if summary_p else ""

        # Extract parameters
        parameters = []
        for param_div in soup.find_all("div", class_="parameter"):
            name_tag = param_div.find("p", class_="label")
            desc_tag = param_div.find("div", class_="content")

            if not name_tag:
                continue

            param_name = name_tag.get_text(strip=True)
            param_desc = ""
            if desc_tag:
                param_desc_p = desc_tag.find("p")
                param_desc = param_desc_p.get_text(strip=True) if param_desc_p else ""

            # Extract menu options if present
            options = []
            if desc_tag:
                defs = desc_tag.find("div", class_="defs")
                if defs:
                    for def_item in defs.find_all("div", class_="def"):
                        label = def_item.find("p", class_="label")
                        desc = def_item.find("div", class_="content")
                        if label:
                            option_name = label.get_text(strip=True)
                            option_desc = desc.get_text(strip=True) if desc else ""
                            options.append({"name": option_name, "description": option_desc})

            param_info: dict[str, Any] = {
                "name": param_name,
                "description": param_desc,
            }
            if options:
                param_info["options"] = options

            parameters.append(param_info)

        # Helper to extract inputs/outputs sections
        def extract_section(section_id: str) -> list[dict[str, str]]:
            items = []
            section_body = soup.find("div", id=f"{section_id}-body")
            if section_body:
                for def_item in section_body.find_all("div", class_="def"):
                    label = def_item.find("p", class_="label")
                    desc_div = def_item.find("div", class_="content")
                    if label:
                        items.append(
                            {
                                "name": label.get_text(strip=True),
                                "description": desc_div.get_text(strip=True) if desc_div else "",
                            }
                        )
            return items

        inputs = extract_section("inputs")
        outputs = extract_section("outputs")

        # For VEX functions, also extract signature and return type
        vex_info: dict[str, Any] = {}
        if help_type == "vex_function":
            # Look for function signature
            sig_div = soup.find("div", class_="signature")
            if sig_div:
                vex_info["signature"] = sig_div.get_text(strip=True)

            # Look for return type in the body
            returns_section = soup.find("div", id="returns-body")
            if returns_section:
                vex_info["returns"] = returns_section.get_text(strip=True)

        # For Python hou module, extract methods
        methods: list[dict[str, str]] = []
        if help_type == "python_hou":
            for method_div in soup.find_all("div", class_="method"):
                method_name_tag = method_div.find("p", class_="label")
                method_desc_tag = method_div.find("div", class_="content")
                if method_name_tag:
                    methods.append(
                        {
                            "name": method_name_tag.get_text(strip=True),
                            "description": (
                                method_desc_tag.get_text(strip=True)[:200] + "..."
                                if method_desc_tag
                                and len(method_desc_tag.get_text(strip=True)) > 200
                                else method_desc_tag.get_text(strip=True)
                                if method_desc_tag
                                else ""
                            ),
                        }
                    )

        result: dict[str, Any] = {
            "status": "success",
            "title": title,
            "url": str(response.url),
            "help_type": help_type,
            "item_name": item_name,
            "description": description,
        }

        if parameters:
            result["parameters"] = parameters
            result["parameter_count"] = len(parameters)

        if inputs:
            result["inputs"] = inputs

        if outputs:
            result["outputs"] = outputs

        if vex_info:
            result["vex_info"] = vex_info

        if methods:
            result["methods"] = methods[:50]  # Limit to first 50 methods
            result["method_count"] = len(methods)
            if len(methods) > 50:
                result["methods_truncated"] = True

        return result

    except httpx.TimeoutException as e:
        raise TimedOut(
            f"Request timed out: a step of the fetch took over {timeout} seconds", url=full_url
        ) from e
    # Only an item_name too long for any URL makes it unparseable; a bad redirect is a
    # RemoteProtocolError, an HTTPError.
    except httpx.InvalidURL as e:
        raise InvalidInput(f"No docs page can be named {item_name!r}: {e}", url=full_url) from e
    except httpx.HTTPError as e:
        raise OperationFailed(f"Network error: {str(e)}", url=full_url) from e
