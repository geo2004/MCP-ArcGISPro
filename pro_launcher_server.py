"""
ArcGIS Pro Launcher MCP Server
==============================
A deliberately single-purpose MCP server: it can do exactly ONE thing --
check whether ArcGIS Pro (and the companion C# add-in's MCP server on it) is
already running, and launch ArcGIS Pro if it isn't. Nothing else.

Why this exists: a cloud-hosted agent (e.g. Claude Cowork) has no direct shell
access to this machine -- its only reach here is through whichever local MCP
servers this config bridges to it. Giving it a general "run a shell command"
bridge would let it execute anything on this desktop; this server instead
exposes one narrow, safe action, so a cloud session can ensure Pro is up
before trying to use the other bridges, without a general remote-shell.

Configure in %APPDATA%\\Claude\\claude_desktop_config.json:
  {
    "mcpServers": {
      "arcgis-pro-launcher": {
        "command": "python",
        "args": ["C:/Users/User/Documents/MCP-ArcgisPro/pro_launcher_server.py"]
      }
    }
  }
"""

import subprocess
import sys
import time
import urllib.request

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

from mcp.server.fastmcp import FastMCP

ARCGIS_PRO_EXE = r"C:\Program Files\ArcGIS\Pro\bin\ArcGISPro.exe"
ADDIN_STATUS_URL = "http://localhost:5057/status"

mcp = FastMCP(
    "ArcGIS Pro Launcher",
    instructions="""
This server does exactly one thing: check whether ArcGIS Pro is running (via
the companion C# add-in's MCP server, which auto-starts as part of Pro's own
startup), and launch ArcGIS Pro if it isn't. It has no other capability -- no
project control, no geoprocessing, no shell access. Use the other bridges
(the Python arcpy bridge, the C# add-in) for everything else; call this one
first, only when you need Pro itself to be running and aren't sure it is.

Launching Pro does not open any project -- every fresh launch lands on the
Home screen with nothing open, same as always. ensure_arcgis_pro_running can
take a minute or more on a cold start; that's normal, not a failure.
"""
)


def _addin_is_up(timeout: float = 1.5) -> bool:
    try:
        urllib.request.urlopen(ADDIN_STATUS_URL, timeout=timeout)
        return True
    except Exception:
        return False


@mcp.tool()
def get_launcher_status() -> str:
    """Report whether ArcGIS Pro's add-in MCP server is currently reachable, without launching anything."""
    return '{"addinReachable": %s}' % ("true" if _addin_is_up() else "false")


@mcp.tool()
def ensure_arcgis_pro_running(wait_seconds: int = 90) -> str:
    """
    Check whether ArcGIS Pro is already running (via the add-in's MCP server).
    If not, launch ArcGIS Pro and wait up to wait_seconds for that server to
    come online. Does not open or touch any project -- Pro starts at its Home
    screen either way.

    Args:
        wait_seconds: Max seconds to wait for a fresh launch to become ready (default: 90)
    """
    if _addin_is_up():
        return '{"alreadyRunning": true, "started": false, "ready": true}'

    try:
        subprocess.Popen([ARCGIS_PRO_EXE], close_fds=True)
    except OSError as e:
        return '{"alreadyRunning": false, "started": false, "ready": false, "error": %r}' % str(e)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _addin_is_up():
            elapsed = round(wait_seconds - (deadline - time.time()), 1)
            return '{"alreadyRunning": false, "started": true, "ready": true, "elapsedSeconds": %s}' % elapsed
        time.sleep(2)

    return (
        '{"alreadyRunning": false, "started": true, "ready": false, '
        '"hint": "ArcGIS Pro was launched but the add-in is not responding yet after %ds -- '
        'it may still be starting, or the add-in may not be registered/enabled."}'
    ) % wait_seconds


if __name__ == "__main__":
    mcp.run()
