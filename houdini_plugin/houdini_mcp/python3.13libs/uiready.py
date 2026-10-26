"""Start the hrpyc listener once Houdini's UI is up, so an MCP server reaches this session.

Houdini runs every pythonX.Ylibs/uiready.py on its path, in graphical sessions only; the
package puts this one there. A hython session starts its listener itself.
"""

import hou
from houdini_mcp_plugin import start_hrpyc_server

result = start_hrpyc_server()
if result["status"] == "error":
    hou.ui.displayMessage(result["message"], severity=hou.severityType.Error)
