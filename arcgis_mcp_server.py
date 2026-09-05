"""
ArcGIS Pro MCP Server for Claude Desktop
=========================================
This is the MCP server that Claude Desktop connects to.
It relays tool calls to the ArcGIS Pro bridge via file-based IPC.

Prerequisites:
  pip install mcp

Configure in %APPDATA%\\Claude\\claude_desktop_config.json:
  {
    "mcpServers": {
      "arcgis-pro": {
        "command": "python",
        "args": ["C:/Users/User/Documents/MCP-ArcgisPro/arcgis_mcp_server.py"]
      }
    }
  }

Then restart Claude Desktop.
"""

import os
import json
import time
import sys

# On Windows, stdout/stdin default to the system ANSI code page when piped (not
# attached to a real console) rather than UTF-8 — exactly the case for an MCP
# stdio server. Without this, any non-ASCII character (em dashes, curly quotes,
# etc.) in tool docstrings or returned text gets mangled on the client side.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

from mcp.server.fastmcp import FastMCP

# ── IPC setup ────────────────────────────────────────────────────────────────
IPC_DIR = os.path.join(os.path.expanduser("~"), ".arcgis_mcp")
CMD_FILE = os.path.join(IPC_DIR, "command.json")
RESULT_FILE = os.path.join(IPC_DIR, "result.json")
LOCK_FILE = os.path.join(IPC_DIR, "lock")
os.makedirs(IPC_DIR, exist_ok=True)


def _load_timeout(default=120):
    """Timeout is configurable via <ipc_dir>/config.json (hardening layer).

    Default is 120s: a live publish_web_layer call took ~32s, and the old 15s
    timeout reported a false "Timeout" while the operation was quietly succeeding
    server-side. Override per-install via config.json's timeout_seconds.
    """
    try:
        with open(os.path.join(IPC_DIR, "config.json"), "r", encoding="utf-8") as f:
            return int(json.load(f).get("timeout_seconds", default))
    except Exception:
        return default


TIMEOUT = _load_timeout()  # seconds to wait for ArcGIS Pro to respond (was 15, now 120)

mcp = FastMCP(
    "ArcGIS Pro",
    instructions="""
You are connected to a live ArcGIS Pro session via the MCP bridge.
The user is assumed to have ArcGIS Pro open with a Map, Scene, or Globe already loaded.

## Always start with ping()
Call ping() first on every session to confirm the connection and read project status.

## Verify files before using them
- Use list_directory(path) to confirm exact filenames — never guess
- Use describe_data(path) to check the coordinate system before any distance-based operation

## Always check coordinate system before metric geoprocessing
- describe_data returns spatialReference.type as "Projected", "Geographic", or "Unknown".
- If "Geographic" (degrees) → reproject first:
  run_geoprocessing("management.Project", [input, output, WKID])
- If "Unknown" → the dataset has NO defined coordinate system. Do NOT reproject
  (there is no source CRS to project FROM — Project would fail or mislead). Instead
  tell the user the CRS is undefined; it must be defined with
  run_geoprocessing("management.DefineProjection", [input, WKID]) once the true CRS
  is known, or the data re-exported with a CRS.
- If "Projected" → distance/area geoprocessing can proceed directly.
- Common WKIDs: UTM Zone 49S = 32749, UTM Zone 50S = 32750, WGS 84 = 4326

## Standard workflow
ping → list_directory/describe_data → add layer → reproject if needed →
geoprocessing → add result to map → zoom_to_layer → create_layout → export_layout

## zoom_to_layer
Only works when a map view is open. If the tool returns a warning instead of zooming,
tell the user to open the map in ArcGIS Pro (double-click it in the Catalog pane).
To show a layer's extent without needing an open view at all, use create_layout()
+ export_layout() instead — layouts render headlessly and don't need an active view.

## update_features
Bulk-sets the same value(s) on every row matching where_clause — there is no undo.
Always state the where_clause and the exact field/value changes and get explicit
confirmation before calling this, especially with where_clause="" (every row).

## publish_web_layer
Creates or overwrites content on the user's ArcGIS Online/Enterprise portal. Always
confirm the service name and whether it should be public before calling this.

## When no specific tool covers the need
Use execute_python(code) to run any arcpy/arcpy.mp code directly.
Available variables: arcpy, os, proj (ArcGISProject), get_map().
Set result = <value> to return data.

## After every workflow
Summarize: layers added, tools run, output file paths, CRS used.
"""
)

# ── IPC helper ────────────────────────────────────────────────────────────────

def _call(op: str, args: dict = None) -> dict:
    """Prefer the socket transport (low latency, no poll); fall back to file IPC."""
    if args is None:
        args = {}
    try:
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from hardening.bridge_transport import read_port_file, send_request
        host, port, token = read_port_file(IPC_DIR)
        resp = send_request(port, op, args, host=host, timeout=TIMEOUT, token=token)
        err = str(resp.get("error", "") or "")
        if err.startswith("transport-inflight:"):
            # Request was already sent and may still be executing in ArcGIS Pro.
            # Do NOT retry over file IPC — that would duplicate side effects.
            return {"ok": False, "data": None,
                    "error": "Socket request did not return within the timeout and was NOT "
                             "retried (to avoid duplicate execution). The operation may still "
                             "be running in ArcGIS Pro; check the result before re-issuing."}
        if not err.startswith("transport-connect:"):
            return resp  # socket worked (success, or a normal handler error)
        # transport-connect: socket was unavailable before dispatch -> safe to fall back
    except Exception:
        pass  # no port file / transport unavailable -> fall back
    return _call_via_files(op, args)


_warned_file_fallback = False


def _call_via_files(op: str, args: dict = None) -> dict:
    """Send a command to ArcGIS Pro and wait for the result (file IPC fallback)."""
    global _warned_file_fallback
    if not _warned_file_fallback:
        # stderr only — stdout is reserved for the MCP stdio protocol
        print("[arcgis-mcp] Socket transport unavailable; using file IPC fallback.",
              file=sys.stderr)
        _warned_file_fallback = True
    if args is None:
        args = {}

    # Clean up any stale result
    for f in (RESULT_FILE, LOCK_FILE):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    # Write command atomically (tmp + replace) so the bridge's poll loop can
    # never read a partially written command.json.
    tmp = CMD_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"op": op, "args": args}, f)
        os.replace(tmp, CMD_FILE)
    except OSError as e:
        return {"ok": False, "data": None,
                "error": f"Could not write command file in {IPC_DIR}: {e}"}

    # Wait for result with adaptive backoff: start polling at 5 ms so a fast
    # operation returns almost immediately, then grow the sleep toward a 0.1 s cap
    # while waiting on a slow one. The old fixed 0.1 s sleep added up to 100 ms of
    # latency to every command, even trivial ones.
    _MIN_POLL = 0.005
    _MAX_POLL = 0.1
    poll = _MIN_POLL
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE) and not os.path.exists(LOCK_FILE):
            try:
                with open(RESULT_FILE, "r", encoding="utf-8") as f:
                    result = json.load(f)
                os.remove(RESULT_FILE)
                return result
            except (json.JSONDecodeError, OSError):
                time.sleep(_MIN_POLL)
                continue
        time.sleep(poll)
        poll = min(poll * 2, _MAX_POLL)

    # Timeout — clean up
    try:
        os.remove(CMD_FILE)
    except FileNotFoundError:
        pass
    return {"ok": False, "data": None, "error": (
        f"Timeout after {TIMEOUT}s: the ArcGIS Pro bridge did not respond. "
        "Checklist: (1) Is ArcGIS Pro open with a project loaded? "
        "(2) Is the bridge running? Start it with the 'MCP Bridge' toolbox (Start MCP Bridge) "
        "or by exec'ing pro_bridge.py in the Python window. "
        "(3) For long geoprocessing, raise 'timeout_seconds' in "
        f"{os.path.join(IPC_DIR, 'config.json')} and restart Claude Desktop."
    )}


def _result_text(result) -> str:
    # Defensive: a malformed/partial result (corrupt result.json, unexpected
    # transport payload) must surface as a readable error, not a KeyError crash.
    if not isinstance(result, dict):
        return f"Error: unexpected bridge response: {result!r}"
    if result.get("ok"):
        return json.dumps(result.get("data"), indent=2, default=str)
    return f"Error: {result.get('error') or 'unknown bridge error (no detail returned)'}"


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def ping() -> str:
    """
    Test the connection to ArcGIS Pro and get the full project status:
    version, project path, list of maps/scenes/globes, active map, and whether
    a view is currently open. Always call this first to understand the current state
    before running any other operations.
    """
    return _result_text(_call("ping"))


@mcp.tool()
def get_project_info() -> str:
    """Get information about the currently open ArcGIS Pro project: file path, geodatabase, and list of maps."""
    return _result_text(_call("get_project_info"))


@mcp.tool()
def get_active_map_name() -> str:
    """Get the name of the active map currently shown in ArcGIS Pro."""
    return _result_text(_call("get_active_map_name"))


@mcp.tool()
def list_layers(map_name: str = "") -> str:
    """
    List all layers in the active map (or a specific map by name).

    Args:
        map_name: Optional map name. If empty, uses the active map.
    """
    return _result_text(_call("list_layers", {"map_name": map_name}))


@mcp.tool()
def add_vector_layer(path: str) -> str:
    """
    Add a vector layer (shapefile, feature class, GeoJSON, etc.) to the active map.

    Args:
        path: Full path to the data source (e.g. C:/data/roads.shp or a feature class path)
    """
    return _result_text(_call("add_vector_layer", {"path": path}))


@mcp.tool()
def add_raster_layer(path: str) -> str:
    """
    Add a raster layer (GeoTIFF, IMG, etc.) to the active map.

    Args:
        path: Full path to the raster file (e.g. C:/data/dem.tif)
    """
    return _result_text(_call("add_raster_layer", {"path": path}))


@mcp.tool()
def remove_layer(name: str) -> str:
    """
    Remove a layer from the active map by name.

    Args:
        name: Exact layer name as shown in the Contents pane
    """
    return _result_text(_call("remove_layer", {"name": name}))


@mcp.tool()
def zoom_to_layer(name: str) -> str:
    """
    Zoom and pan the active map view to show the full extent of a layer.

    Args:
        name: Exact layer name as shown in the Contents pane
    """
    return _result_text(_call("zoom_to_layer", {"name": name}))


@mcp.tool()
def count_features(layer: str) -> str:
    """
    Count the total number of features (rows) in a layer.

    Args:
        layer: Exact layer name as shown in the Contents pane
    """
    return _result_text(_call("count_features", {"layer": layer}))


@mcp.tool()
def select_by_attribute(layer: str, where_clause: str, selection_type: str = "NEW_SELECTION") -> str:
    """
    Select features in a layer using a SQL WHERE clause.

    Args:
        layer: Exact layer name as shown in the Contents pane
        where_clause: SQL expression (e.g. "population > 100000" or "type = 'road'")
        selection_type: NEW_SELECTION, ADD_TO_SELECTION, REMOVE_FROM_SELECTION, SUBSET_SELECTION (default: NEW_SELECTION)
    """
    return _result_text(_call("select_by_attribute", {
        "layer": layer,
        "where_clause": where_clause,
        "selection_type": selection_type,
    }))


@mcp.tool()
def update_features(layer: str, where_clause: str, updates: dict) -> str:
    """
    Update attribute values on features matching a WHERE clause. Sets the same
    field value(s) on every matching row (bulk update). For per-row computed
    values (e.g. incrementing a counter), use execute_python with an UpdateCursor instead.

    Args:
        layer: Exact layer name as shown in the Contents pane
        where_clause: SQL expression selecting which rows to update (e.g. "STATUS = 'Pending'").
                      Pass "" to update every row in the layer — be deliberate about this.
        updates: Dict of {field_name: new_value} applied to every matching row,
                 e.g. {"STATUS": "Verified", "REVIEWED_BY": "GIS Team"}
    """
    return _result_text(_call("update_features", {
        "layer": layer,
        "where_clause": where_clause,
        "updates": updates,
    }))


@mcp.tool()
def save_project() -> str:
    """Save the current ArcGIS Pro project."""
    return _result_text(_call("save_project"))


@mcp.tool()
def run_geoprocessing(tool: str, params: list) -> str:
    """
    Run any ArcPy geoprocessing tool by its dotted name.

    Args:
        tool: Dotted tool name, e.g. "analysis.Buffer", "management.CopyFeatures", "conversion.FeatureClassToShapefile"
        params: List of positional parameters for the tool, e.g. ["roads", "output_buf", "100 Meters"]

    Example: run_geoprocessing("analysis.Buffer", ["C:/data/roads.shp", "C:/data/roads_buf.shp", "500 Meters"])

    Notes:
      - The FIRST parameter may be a layer name from the Contents pane (it is resolved
        to its data source path). Any other dataset inputs must be FULL paths.
      - Long-running tools may exceed the bridge timeout; the tool may still finish
        inside ArcGIS Pro — verify with describe_data before re-running.
      - In safe mode (hardening config) some destructive tools (e.g. management.Delete)
        are blocked by policy and return a 'Policy: ...' error.
    """
    return _result_text(_call("run_geoprocessing", {"tool": tool, "params": params}))


@mcp.tool()
def list_fields(dataset: str) -> str:
    """
    List all fields in a dataset (shapefile, feature class, table, layer name, etc.)
    with field type, length, and alias. Use this before building WHERE clauses or
    running attribute operations.

    Args:
        dataset: Full path to dataset, or layer name as shown in Contents pane
    """
    return _result_text(_call("list_fields", {"dataset": dataset}))


@mcp.tool()
def list_feature_classes(workspace: str = "", pattern: str = "*", feature_type: str = "") -> str:
    """
    List feature classes in a workspace (folder, GDB, or feature dataset).

    Args:
        workspace: Path to workspace/GDB (e.g. C:/data/mydb.gdb). Uses current workspace if empty.
        pattern: Wildcard filter, e.g. "road*" (default: "*")
        feature_type: Filter by geometry — Point, Line, Polygon, etc. (default: all)
    """
    return _result_text(_call("list_feature_classes", {
        "workspace": workspace, "pattern": pattern, "feature_type": feature_type}))


@mcp.tool()
def list_rasters(workspace: str = "", pattern: str = "*", raster_type: str = "") -> str:
    """
    List raster datasets in a workspace.

    Args:
        workspace: Path to workspace/folder. Uses current workspace if empty.
        pattern: Wildcard filter (default: "*")
        raster_type: Filter by type — TIF, IMG, GRID, etc. (default: all)
    """
    return _result_text(_call("list_rasters", {
        "workspace": workspace, "pattern": pattern, "raster_type": raster_type}))


@mcp.tool()
def list_tables(workspace: str = "", pattern: str = "*") -> str:
    """
    List standalone tables in a workspace (GDB tables, DBF files, etc.).

    Args:
        workspace: Path to workspace. Uses current workspace if empty.
        pattern: Wildcard filter (default: "*")
    """
    return _result_text(_call("list_tables", {"workspace": workspace, "pattern": pattern}))


@mcp.tool()
def set_workspace(workspace: str) -> str:
    """
    Set arcpy.env.workspace — the default location for inputs/outputs when
    no full path is provided to geoprocessing tools.

    Args:
        workspace: Full path to folder or GDB (e.g. C:/data or C:/data/mydb.gdb)
    """
    return _result_text(_call("set_workspace", {"workspace": workspace}))


@mcp.tool()
def get_workspace() -> str:
    """Get the current arcpy.env.workspace setting."""
    return _result_text(_call("get_workspace"))


@mcp.tool()
def clear_selection(layer: str) -> str:
    """
    Clear any active selection on a layer.

    Args:
        layer: Exact layer name as shown in the Contents pane
    """
    return _result_text(_call("clear_selection", {"layer": layer}))


@mcp.tool()
def get_unique_values(layer: str, field: str, limit: int = 100) -> str:
    """
    Get all unique values in a field. Useful for exploring data and building
    WHERE clauses for select_by_attribute.

    Args:
        layer: Exact layer name as shown in the Contents pane
        field: Field name to get unique values for
        limit: Max number of unique values to return (default: 100)
    """
    return _result_text(_call("get_unique_values", {
        "layer": layer, "field": field, "limit": limit}))


@mcp.tool()
def set_layer_visibility(layer: str, visible: bool) -> str:
    """
    Toggle a layer's visibility in the Contents pane.

    Args:
        layer: Exact layer name as shown in the Contents pane
        visible: True to show, False to hide
    """
    return _result_text(_call("set_layer_visibility", {"layer": layer, "visible": visible}))


@mcp.tool()
def create_map(name: str = "Map") -> str:
    """
    Create a new map in the current ArcGIS Pro project.
    After creation, open it by double-clicking it in the Catalog pane under Maps.

    Args:
        name: Name for the new map (default: "Map")
    """
    return _result_text(_call("create_map", {"name": name}))


@mcp.tool()
def describe_data(path: str) -> str:
    """
    Describe a dataset — returns coordinate system, geometry type, extent, and data type.
    Use this to verify a file exists and check its projection before geoprocessing.

    spatialReference.type is one of:
      - "Projected"  → metric geoprocessing can run directly.
      - "Geographic" → coordinates in degrees; reproject before distance/area work.
      - "Unknown"    → NO coordinate system is defined. Do NOT reproject (nothing to
                       project from); the CRS must be defined first (DefineProjection).

    Args:
        path: Full path to the dataset (shapefile, raster, feature class, GDB, etc.)
    """
    return _result_text(_call("describe_data", {"path": path}))


@mcp.tool()
def list_directory(path: str, pattern: str = "*") -> str:
    """
    List files in a directory, optionally filtered by pattern.

    Args:
        path: Full path to the directory (e.g. C:/Users/User/Documents/Rusunawa)
        pattern: Glob pattern to filter results (e.g. "*.shp", "*.tif", default: "*")
    """
    return _result_text(_call("list_directory", {"path": path, "pattern": pattern}))


@mcp.tool()
def create_layout(name: str = "Layout", map_name: str = "", width: float = 11,
                  height: float = 8.5, units: str = "INCH", margin: float = 0.5) -> str:
    """
    Create a new layout in the project with a map frame already added.
    After creation, call export_layout() to export it as PDF/PNG/etc.

    Args:
        name: Layout name (default: "Layout")
        map_name: Map to put in the frame — uses active map if empty
        width: Page width (default: 11)
        height: Page height (default: 8.5)
        units: Page units — INCH, CENTIMETER, MILLIMETER, POINT (default: INCH)
        margin: Frame margin from page edge in page units (default: 0.5)
    """
    return _result_text(_call("create_layout", {
        "name": name, "map_name": map_name, "width": width,
        "height": height, "units": units, "margin": margin,
    }))


@mcp.tool()
def execute_python(code: str) -> str:
    """
    Execute arbitrary Python/arcpy code inside the ArcGIS Pro bridge.
    Use this as a LAST RESORT for arcpy/arcpy.mp operations not covered by other tools.

    Available variables: arcpy, os, proj (the ArcGISProject), get_map()
    Set  result = <value>  in your code to return data (must be JSON-serializable).
    print() output is captured and returned as 'stdout'.

    Note: if the bridge runs with safe_mode enabled (default in the hardening config),
    this tool is disabled and returns a 'Policy: execute_python is disabled' error.
    The user can enable it with allow_execute_python=true in ~/.arcgis_mcp/config.json.

    Example:
        code = \"\"\"
        m = proj.listMaps()[0]
        result = [lyr.name for lyr in m.listLayers()]
        \"\"\"
    """
    return _result_text(_call("execute_python", {"code": code}))


@mcp.tool()
def list_layouts() -> str:
    """List all layouts in the current ArcGIS Pro project with their page size and units."""
    return _result_text(_call("list_layouts"))


@mcp.tool()
def export_layout(name: str, output: str, format: str = "PDF", dpi: int = 150) -> str:
    """
    Export a layout to a file (PDF, PNG, JPG, BMP, TIF, SVG, or EPS).

    Args:
        name: Exact layout name as shown in the Catalog pane
        output: Full output file path including extension (e.g. C:/output/map.pdf)
        format: Export format — PDF, PNG, JPG, BMP, TIF, SVG, EPS (default: PDF)
        dpi: Resolution in dots per inch for raster formats (default: 150)
    """
    return _result_text(_call("export_layout", {
        "name": name,
        "output": output,
        "format": format,
        "dpi": dpi,
    }))


@mcp.tool()
def get_layer_features(layer: str, limit: int = 10, fields: list = None) -> str:
    """
    Preview rows from a layer's attribute table.

    Args:
        layer: Exact layer name as shown in the Contents pane
        limit: Number of rows to return (default: 10)
        fields: List of field names to include. If empty, returns all non-geometry fields.
    """
    return _result_text(_call("get_layer_features", {
        "layer": layer,
        "limit": limit,
        "fields": fields or [],
    }))


@mcp.tool()
def run_recipe(name: str, params: dict = None) -> str:
    """
    Run a high-value 'recipe' (multi-step workflow) by name.

    Recipes:
      - qa_layer:               QC report for a layer (CRS, count, fields, null
                                geometry, issues).  params: {"layer_name": "..."}
      - export_attributes_csv:  layer attribute table -> CSV.
                                params: {"layer_name": "...", "out_path": "C:/out/x.csv",
                                         "fields": [...optional...], "limit": 100}
      - batch_export_layouts:   export ALL layouts to PDF/PNG/JPG/TIF.
                                params: {"out_dir": "C:/out", "fmt": "PDF", "dpi": 150}
      - field_stats:            min/max/mean/sum/count/nulls for a numeric field.
                                params: {"layer_name": "...", "field": "..."}
      - value_counts:           frequency of each distinct value in a field.
                                params: {"layer_name": "...", "field": "...", "top": 20}

    Note: recipes require the bridge to be started with the hardening layer loaded
    (MCP Bridge toolbox Start button). Otherwise this returns "Unknown command: 'recipe'".

    Args:
        name: recipe name
        params: dict of recipe arguments
    """
    return _result_text(_call("recipe", {"name": name, "params": params or {}}))


@mcp.tool()
def publish_web_layer(layer: str, service_name: str, summary: str = "", tags: str = "",
                      public: bool = False, overwrite: bool = False) -> str:
    """
    Publish a layer as a hosted feature service to ArcGIS Online / Enterprise, using
    whichever portal is currently active in ArcGIS Pro. Always confirm with the user
    before calling this — it creates or overwrites content on their portal account.

    Publishes over REST via the ArcGIS API for Python (reusing your existing Pro
    sign-in — no separate login needed), not the classic arcpy.server pipeline,
    which doesn't work reliably from this bridge. Can take up to ~30-60s for
    typical layers; this is normal, don't treat a slow response as a failure.

    Args:
        layer: Exact layer name as shown in the Contents pane
        service_name: Name for the hosted feature service (must be unique in the target folder)
        summary: Short description shown on the portal item (optional)
        tags: Comma-separated tags for the portal item (optional)
        public: Share publicly if True; private (owner-only) if False (default: False)
        overwrite: Overwrite an existing service with the same name if True (default: False)
    """
    return _result_text(_call("publish_web_layer", {
        "layer": layer,
        "service_name": service_name,
        "summary": summary,
        "tags": tags,
        "public": public,
        "overwrite": overwrite,
    }))


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
