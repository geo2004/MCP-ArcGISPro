"""
ArcGIS Pro MCP Bridge
=====================
Launch ONCE per ArcGIS Pro session. PREFERRED: use the "MCP Bridge" toolbox
(Start button) — it sets __file__ so the optional hardening/recipes/socket layer
loads automatically.

Manual alternative in the Python window — pass __file__ so the hardening package
(which lives next to this script) is importable; without it, recipes/socket are
silently unavailable:

    p = r"C:/path/to/MCP-ArcGISPro/pro_bridge.py"
    exec(open(p).read(), {"__file__": p})

(Plain `exec(open(p).read())` still runs the core bridge, but only with the
built-in defaults — no hardening, no recipes, no socket transport.)

Strategy:
  - arcpy.mp.ArcGISProject("CURRENT") is called ONCE in the main thread and cached.
  - A daemon background thread handles all file polling — main thread is never blocked.
  - Main thread free → ArcGIS Pro UI stays responsive, COM marshaling works.

To stop:  _bridge_active = False
"""

import os
import json
import time
import threading
import traceback

try:
    import arcpy
except ImportError:
    raise RuntimeError("Run this inside ArcGIS Pro's Python window.")

# ── IPC directory ─────────────────────────────────────────────────────────────
IPC_DIR     = os.path.join(os.path.expanduser("~"), ".arcgis_mcp")
CMD_FILE    = os.path.join(IPC_DIR, "command.json")
RESULT_FILE = os.path.join(IPC_DIR, "result.json")
LOCK_FILE   = os.path.join(IPC_DIR, "lock")

os.makedirs(IPC_DIR, exist_ok=True)

# ── Cache project reference in the MAIN thread ────────────────────────────────
# ArcGISProject("CURRENT") must be called from the Python window's main thread.
# The cached object is then safely used from the background thread.
_proj = arcpy.mp.ArcGISProject("CURRENT")
arcpy.env.overwriteOutput = True

# ── Hardening layer (optional; degrades gracefully if not found) ──────────────
# Loaded when launched via the toolbox / a path that sets __file__. If the
# hardening package isn't importable, the bridge keeps working with built-in
# defaults (original behavior).
import sys
_HARDENING = False
try:
    # Locate the repo dir robustly. __file__ is set when launched via the toolbox;
    # in the documented `exec(open(...).read())` manual launch it is undefined, so
    # fall back to scanning cwd + sys.path for the 'hardening' package.
    _base = None
    try:
        _base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        for _cand in [os.getcwd()] + list(sys.path):
            try:
                if _cand and os.path.isdir(os.path.join(_cand, "hardening")):
                    _base = _cand
                    break
            except Exception:
                pass
    if _base and _base not in sys.path:
        sys.path.insert(0, _base)
    from hardening.bridge_config import BridgeConfig
    from hardening.bridge_safety import check_gp_tool, check_execute_python, SafetyError
    from hardening.bridge_helpers import resolve_param
    from hardening.bridge_logging import get_logger, audit
    CFG = BridgeConfig.load()
    LOG = get_logger(IPC_DIR, CFG.log_level)
    _HARDENING = True
    print("[MCP Bridge] Hardening loaded (safe_mode=%s, timeout=%ss)." % (CFG.safe_mode, CFG.timeout_seconds))
except Exception as _e:  # noqa: BLE001 - graceful fallback by design
    print("[MCP Bridge] Hardening not loaded (%r); using built-in defaults." % _e)

    LOG = None  # audit() below is a no-op, but LOG must exist (was a silent NameError per command)

    class _CFG:
        protocol_version = 1
        # Fallback (no hardening) preserves the ORIGINAL base-bridge behavior of
        # auto-creating a map. The safer auto_create_map=False default only applies
        # when the hardening config is actually in effect.
        auto_create_map = True
    CFG = _CFG()

    class SafetyError(Exception):
        pass

    def check_gp_tool(tool, cfg):
        return None

    def check_execute_python(cfg):
        return None

    def resolve_param(_arcpy, _m, p):
        return p

    def get_logger(*a, **k):
        return None

    def audit(*a, **k):
        return None

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ok(data):
    return {"ok": True, "error": None, "data": data}

def _err(msg):
    return {"ok": False, "error": str(msg), "data": None}

def _get_map():
    """Return active map → first available map → auto-create one if none exist."""
    m = _proj.activeMap
    if m is not None:
        return m
    maps = _proj.listMaps()
    if maps:
        return maps[0]
    # No maps at all
    if not getattr(CFG, "auto_create_map", False):
        raise RuntimeError("No map in this project. Create or open a Map in ArcGIS Pro, "
                           "or set auto_create_map=true in config.json.")
    m = _proj.createMap("Map")
    print("[MCP Bridge] No map found — created 'Map' automatically (auto_create_map=true).")
    return m

# ── Command handlers ──────────────────────────────────────────────────────────

def handle_ping(_args):
    maps = _proj.listMaps()
    active_map  = _proj.activeMap
    active_view = _proj.activeView

    map_list = [{"name": m.name, "type": m.mapType} for m in maps]

    view_info = None
    if active_view is not None:
        view_info = {
            "name": active_view.map.name if hasattr(active_view, "map") else str(active_view),
            "type": active_view.mapView.map.mapType if hasattr(active_view, "mapView") else "unknown",
        }

    status = "ready"
    hint   = None
    if not maps:
        status = "no_maps"
        hint   = "Project has no maps. I'll create one automatically when needed, but open it in ArcGIS Pro to see the result."
    elif active_view is None:
        status = "map_not_opened"
        hint   = f"Map '{maps[0].name}' exists but no view is open. Double-click it in the Catalog pane to open it (needed for zoom operations)."

    return _ok({
        "status":       status,
        "hint":         hint,
        "version":      arcpy.GetInstallInfo()["Version"],
        "project":      _proj.filePath or "(unsaved)",
        "maps":         map_list,
        "activeMap":    active_map.name if active_map else None,
        "activeView":   view_info,
        # Additive diagnostics: lets the client explain 'Policy:' rejections
        # (execute_python disabled / GP tool blocked) instead of blind retries.
        "hardening":    _HARDENING,
        "safeMode":     getattr(CFG, "safe_mode", None),
        "executePythonEnabled": (not getattr(CFG, "safe_mode", False))
                                or bool(getattr(CFG, "allow_execute_python", False)),
    })


def handle_get_project_info(_args):
    maps = [{"name": m.name, "spatialReference": m.spatialReference.name}
            for m in _proj.listMaps()]
    return _ok({
        "filePath": _proj.filePath,
        "defaultGeodatabase": _proj.defaultGeodatabase,
        "maps": maps,
        "activeMap": _proj.activeMap.name if _proj.activeMap else None,
    })


def handle_get_active_map_name(_args):
    return _ok({"name": _get_map().name})


def handle_list_layers(args):
    map_name = args.get("map_name")
    if map_name:
        maps = _proj.listMaps(map_name)
        if not maps:
            raise RuntimeError("Map '%s' not found. Available: %s"
                               % (map_name, [mm.name for mm in _proj.listMaps()]))
        m = maps[0]
    else:
        m = _proj.activeMap
    if m is None:
        raise RuntimeError("No map found.")
    layers = [{"name": lyr.name, "visible": lyr.visible,
               "isFeatureLayer": lyr.isFeatureLayer,
               "isRasterLayer": lyr.isRasterLayer,
               "isGroupLayer": lyr.isGroupLayer}
              for lyr in m.listLayers()]
    return _ok({"map": m.name, "layers": layers})


def handle_add_vector_layer(args):
    path = args.get("path")
    if not path:
        raise ValueError("'path' is required")
    m = _get_map()
    lyr = m.addDataFromPath(path)
    return _ok({"added": lyr.name, "map": m.name})


def handle_add_raster_layer(args):
    path = args.get("path")
    if not path:
        raise ValueError("'path' is required")
    m = _get_map()
    lyr = m.addDataFromPath(path)
    return _ok({"added": lyr.name, "map": m.name})


def handle_remove_layer(args):
    name = args.get("name")
    if not name:
        raise ValueError("'name' is required")
    m = _get_map()
    layers = m.listLayers(name)
    if not layers:
        raise RuntimeError(f"Layer '{name}' not found.")
    m.removeLayer(layers[0])
    return _ok({"removed": name})


def handle_zoom_to_layer(args):
    name = args.get("name")
    if not name:
        raise ValueError("'name' is required")
    m = _get_map()
    layers = m.listLayers(name)
    if not layers:
        raise RuntimeError(f"Layer '{name}' not found.")
    view = _proj.activeView
    if view is None:
        return _ok({
            "zoomedTo": None,
            "warning": (
                f"Layer '{name}' is in map '{m.name}' but no view is open — "
                "open the map in ArcGIS Pro (double-click in Catalog pane) to see it."
            ),
        })
    # Use the data-source extent (Describe) instead of MapView.getLayerExtent():
    #   * Layer.getExtent() does not exist in ArcGIS Pro 3.x (raised AttributeError).
    #   * MapView.getLayerExtent() depends on the view's render state and can return
    #     a degenerate (zero-size) extent when the map view isn't the active/rendered
    #     tab, and it defaults to the selection-only extent.
    # Describe(...).extent is deterministic and always the FULL layer extent (matching
    # this tool's documented behavior). camera.setExtent reprojects it to the map's
    # spatial reference automatically.
    view.camera.setExtent(arcpy.Describe(layers[0]).extent)
    return _ok({"zoomedTo": name})


def handle_count_features(args):
    layer = args.get("layer")
    if not layer:
        raise ValueError("'layer' is required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    count = int(arcpy.management.GetCount(layers[0]).getOutput(0))
    return _ok({"layer": layer, "count": count})


def handle_select_by_attribute(args):
    layer = args.get("layer")
    where_clause = args.get("where_clause", "")
    selection_type = args.get("selection_type", "NEW_SELECTION")
    if not layer:
        raise ValueError("'layer' is required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    # Count on layers[0] directly, not the SelectLayerByAttribute Result object —
    # the Result token doesn't resolve from this background thread (ERROR 000732),
    # even though the selection itself applies fine to the real Layer object.
    arcpy.management.SelectLayerByAttribute(layers[0], selection_type, where_clause)
    count = int(arcpy.management.GetCount(layers[0]).getOutput(0))
    return _ok({"layer": layer, "selectedCount": count, "whereClause": where_clause})


def handle_update_features(args):
    """Bulk-set the same field value(s) on every row matching where_clause.
    For per-row computed values, use execute_python with an UpdateCursor instead."""
    layer = args.get("layer")
    where_clause = args.get("where_clause", "")
    updates = args.get("updates")
    if not layer:
        raise ValueError("'layer' is required")
    if not updates or not isinstance(updates, dict):
        raise ValueError("'updates' is required — a dict of {field_name: new_value}")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")

    fields = list(updates.keys())
    values = list(updates.values())
    updated = 0
    with arcpy.da.UpdateCursor(layers[0], fields, where_clause) as cur:
        for _ in cur:
            cur.updateRow(values)
            updated += 1
    return _ok({"layer": layer, "whereClause": where_clause, "fields": fields, "updatedCount": updated})


def handle_save_project(_args):
    _proj.save()
    return _ok({"saved": _proj.filePath})


def handle_run_geoprocessing(args):
    tool_path = args.get("tool")
    params = args.get("params", [])
    if not tool_path:
        raise ValueError("'tool' is required (e.g. 'analysis.Buffer')")
    check_gp_tool(tool_path, CFG)
    # Resolve ONLY the first positional parameter if it is a layer NAME -> its
    # dataSource path. The first argument is the input dataset for virtually every
    # tool; resolving every string would corrupt non-dataset args (field names,
    # SQL/value strings) that happen to match a layer name, e.g.
    # management.AddField(table, "Roads", "TEXT") when a "Roads" layer exists.
    # Use _get_map() (falls back to the first map when no view is active). For tools
    # with additional dataset inputs, pass those as full paths.
    if params:
        try:
            _gp_map = _get_map()
        except Exception:
            _gp_map = None
        params = [resolve_param(arcpy, _gp_map, params[0])] + list(params[1:])
    parts = tool_path.split(".")
    try:
        if len(parts) == 2:
            fn = getattr(getattr(arcpy, parts[0]), parts[1])
        elif len(parts) == 1:
            fn = getattr(arcpy, parts[0])
        else:
            raise ValueError(f"Invalid tool path: {tool_path}")
    except AttributeError:
        raise ValueError(
            f"Geoprocessing tool not found: '{tool_path}'. Check the spelling and use "
            f"the dotted form '<toolbox>.<Tool>' (e.g. 'analysis.Buffer', "
            f"'management.Project') or the legacy '<Tool>_<toolbox>' form."
        )
    result = fn(*params)
    outputs = [result.getOutput(i) for i in range(result.outputCount)]
    return _ok({"tool": tool_path, "outputs": outputs})


def handle_get_layer_features(args):
    layer = args.get("layer")
    limit = int(args.get("limit", 10))
    fields = args.get("fields") or []
    if not layer:
        raise ValueError("'layer' is required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    lyr = layers[0]
    if not fields:
        fields = [f.name for f in arcpy.ListFields(lyr) if f.type not in ("Geometry", "Blob")]
    rows = []
    with arcpy.da.SearchCursor(lyr, fields) as cur:
        for i, row in enumerate(cur):
            if i >= limit:
                break
            rows.append(dict(zip(fields, row)))
    return _ok({"layer": layer, "fields": fields, "rows": rows})


def handle_list_fields(args):
    dataset = args.get("dataset")
    if not dataset:
        raise ValueError("'dataset' is required")

    # Accept a Contents-pane layer name (not just a path) as the docstring promises —
    # arcpy.ListFields() doesn't reliably resolve a bare layer name from this thread.
    resolved = dataset
    layers = _get_map().listLayers(dataset)
    if layers:
        resolved = arcpy.Describe(layers[0]).catalogPath

    if not arcpy.Exists(resolved):
        raise RuntimeError(f"Dataset not found: {dataset}")
    fields = []
    for f in arcpy.ListFields(resolved):
        fields.append({
            "name": f.name,
            "aliasName": f.aliasName,
            "type": f.type,
            "length": f.length,
            "nullable": f.isNullable,
            "editable": f.editable,
        })
    return _ok({"dataset": dataset, "fields": fields, "count": len(fields)})


# These three listings are read-only queries. When a caller passes an explicit
# `workspace`, we temporarily point arcpy.env.workspace at it JUST to run the list,
# then restore the previous value in a finally block — a query must not mutate the
# project's global workspace as a side effect. To set the workspace intentionally,
# callers use set_workspace(). The returned "workspace" is the one actually listed
# (the passed workspace, or the current one when none was given), which is what the
# caller expects to see the results for.

def handle_list_feature_classes(args):
    workspace = args.get("workspace")
    pattern   = args.get("pattern", "*")
    feat_type = args.get("feature_type", "")  # Point, Line, Polygon, etc.
    _prev = arcpy.env.workspace
    try:
        if workspace:
            arcpy.env.workspace = workspace
        listed_ws = arcpy.env.workspace
        fcs = arcpy.ListFeatureClasses(pattern, feat_type) or []
    finally:
        if workspace:
            arcpy.env.workspace = _prev
    return _ok({"workspace": listed_ws, "feature_classes": sorted(fcs)})


def handle_list_rasters(args):
    workspace = args.get("workspace")
    pattern   = args.get("pattern", "*")
    raster_type = args.get("raster_type", "")
    _prev = arcpy.env.workspace
    try:
        if workspace:
            arcpy.env.workspace = workspace
        listed_ws = arcpy.env.workspace
        rasters = arcpy.ListRasters(pattern, raster_type) or []
    finally:
        if workspace:
            arcpy.env.workspace = _prev
    return _ok({"workspace": listed_ws, "rasters": sorted(rasters)})


def handle_list_tables(args):
    workspace = args.get("workspace")
    pattern   = args.get("pattern", "*")
    _prev = arcpy.env.workspace
    try:
        if workspace:
            arcpy.env.workspace = workspace
        listed_ws = arcpy.env.workspace
        tables = arcpy.ListTables(pattern) or []
    finally:
        if workspace:
            arcpy.env.workspace = _prev
    return _ok({"workspace": listed_ws, "tables": sorted(tables)})


def handle_set_workspace(args):
    workspace = args.get("workspace")
    if not workspace:
        raise ValueError("'workspace' is required")
    if not arcpy.Exists(workspace):
        raise RuntimeError(f"Workspace not found: {workspace}")
    arcpy.env.workspace = workspace
    return _ok({"workspace": arcpy.env.workspace})


def handle_get_workspace(_args):
    return _ok({"workspace": arcpy.env.workspace or "(not set)"})


def handle_clear_selection(args):
    layer = args.get("layer")
    if not layer:
        raise ValueError("'layer' is required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    arcpy.management.SelectLayerByAttribute(layers[0], "CLEAR_SELECTION")
    return _ok({"cleared": layer})


def handle_get_unique_values(args):
    """Get unique values for a field — useful for building WHERE clauses."""
    layer = args.get("layer")
    field = args.get("field")
    limit = int(args.get("limit", 100))
    if not layer or not field:
        raise ValueError("'layer' and 'field' are required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    values = set()
    with arcpy.da.SearchCursor(layers[0], [field]) as cur:
        for row in cur:
            if row[0] is not None:
                values.add(row[0])
            if len(values) >= limit:
                break
    return _ok({"layer": layer, "field": field, "values": sorted(values, key=str), "count": len(values)})


def handle_set_layer_visibility(args):
    layer   = args.get("layer")
    visible = args.get("visible")
    if not layer or visible is None:
        raise ValueError("'layer' and 'visible' (true/false) are required")
    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    layers[0].visible = bool(visible)
    return _ok({"layer": layer, "visible": layers[0].visible})


def handle_create_map(args):
    name = args.get("name", "Map")
    m = _proj.createMap(name)
    return _ok({
        "created": m.name,
        "note": "Map created. Open it in ArcGIS Pro by double-clicking it in the Catalog pane under Maps."
    })


def handle_describe_data(args):
    path = args.get("path")
    if not path:
        raise ValueError("'path' is required")
    if not arcpy.Exists(path):
        raise RuntimeError(f"Path does not exist: {path}")
    desc = arcpy.Describe(path)
    result = {
        "path": path,
        "dataType": desc.dataType,
        "name": desc.name,
    }
    if hasattr(desc, "spatialReference"):
        sr = desc.spatialReference
        # Distinguish an UNDEFINED/unknown CRS from a real geographic one. The old
        # code collapsed everything non-"Projected" to "Geographic", so a dataset
        # with no coordinate system was reported as Geographic and made the agent
        # reproject a file that has no source CRS to reproject FROM. arcpy reports an
        # undefined SR as type "Unknown" with name "Unknown"/factoryCode 0.
        sr_type = getattr(sr, "type", None) if sr is not None else None
        sr_name = getattr(sr, "name", None) if sr is not None else None
        sr_wkid = getattr(sr, "factoryCode", 0) if sr is not None else 0
        if sr is None or sr_name in (None, "", "Unknown") or not sr_wkid:
            reported_type = "Unknown"
        elif sr_type in ("Projected", "Geographic"):
            reported_type = sr_type
        else:
            reported_type = "Unknown"
        result["spatialReference"] = {
            "name": sr_name,
            "wkid": sr_wkid,
            "type": reported_type,
            "linearUnitName": (sr.linearUnitName if reported_type == "Projected" else None),
        }
    if hasattr(desc, "shapeType"):
        result["shapeType"] = desc.shapeType
    if hasattr(desc, "extent"):
        ext = desc.extent
        result["extent"] = {"xmin": ext.XMin, "ymin": ext.YMin,
                            "xmax": ext.XMax, "ymax": ext.YMax}
    if hasattr(desc, "featureCount"):
        result["featureCount"] = desc.featureCount
    return _ok(result)


def handle_list_directory(args):
    import glob
    path    = args.get("path")
    pattern = args.get("pattern", "*")
    if not path:
        raise ValueError("'path' is required")
    if not os.path.isdir(path):
        raise RuntimeError(f"Directory not found: {path}")
    matches = glob.glob(os.path.join(path, pattern))
    files = sorted([os.path.basename(f) for f in matches])
    return _ok({"path": path, "pattern": pattern, "files": files, "count": len(files)})


def handle_create_layout(args):
    name     = args.get("name", "Layout")
    map_name = args.get("map_name", "")
    width    = float(args.get("width", 11))
    height   = float(args.get("height", 8.5))
    units    = args.get("units", "INCH")
    margin   = float(args.get("margin", 0.5))

    # Resolve the map BEFORE creating the layout, so a bad map_name doesn't
    # leave an orphan empty layout in the project.
    if map_name:
        maps = _proj.listMaps(map_name)
        if not maps:
            raise RuntimeError("Map '%s' not found. Available: %s"
                               % (map_name, [mm.name for mm in _proj.listMaps()]))
        m = maps[0]
    else:
        m = _get_map()

    layout = _proj.createLayout(width, height, units, name)

    # Page-coordinate extent for the map frame (inset by margin on all sides)
    ext = arcpy.Extent(margin, margin, width - margin, height - margin)
    mf  = layout.createMapFrame(ext, m)
    try:
        if m.listLayers():
            mf.camera.setExtent(mf.getLayerExtent(m.listLayers()[0]))
    except Exception as e:  # noqa: BLE001 - initial zoom is best-effort
        # Don't fail layout creation just because the initial extent couldn't be
        # computed (e.g. first layer is a basemap/group layer with no extent).
        print("[MCP Bridge] create_layout: could not set initial extent (%s)." % e)

    return _ok({
        "layout":   layout.name,
        "mapFrame": mf.name,
        "map":      m.name,
        "size":     f"{width} x {height} {units}",
        "note":     f"Now call export_layout(name='{layout.name}', output='...', format='PNG')",
    })


def handle_execute_python(args):
    """
    Run arbitrary Python/arcpy code inside the bridge.
    Variables available: arcpy, os, proj (_proj), get_map (_get_map).
    Set  result = <value>  to return data back to Claude.
    Print statements are captured and returned as 'stdout'.
    """
    import io, contextlib

    check_execute_python(CFG)
    code = args.get("code", "")
    if not code:
        raise ValueError("'code' is required")

    local_ns = {
        "arcpy":   arcpy,
        "os":      os,
        "proj":    _proj,
        "get_map": _get_map,
        "result":  None,
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<execute_python>", "exec"), local_ns)  # noqa: S102

    response = {"executed": True}
    if local_ns.get("result") is not None:
        response["result"] = local_ns["result"]
    if buf.getvalue():
        response["stdout"] = buf.getvalue()
    return _ok(response)


def handle_list_layouts(_args):
    layouts = []
    for lyt in _proj.listLayouts():
        layouts.append({
            "name": lyt.name,
            "width": lyt.pageWidth,
            "height": lyt.pageHeight,
            "units": lyt.pageUnits,
        })
    return _ok({"layouts": layouts})


def handle_export_layout(args):
    name   = args.get("name")
    output = args.get("output")
    fmt    = args.get("format", "PDF").upper()
    dpi    = int(args.get("dpi", 150))

    if not name:
        raise ValueError("'name' is required")
    if not output:
        raise ValueError("'output' is required (full file path)")

    layouts = _proj.listLayouts(name)
    if not layouts:
        raise RuntimeError(f"Layout '{name}' not found. Available: {[l.name for l in _proj.listLayouts()]}")
    lyt = layouts[0]

    os.makedirs(os.path.dirname(output) if os.path.dirname(output) else ".", exist_ok=True)

    EXPORT_METHODS = {
        "PDF":  "exportToPDF",
        "PNG":  "exportToPNG",
        "JPG":  "exportToJPEG",
        "JPEG": "exportToJPEG",
        "BMP":  "exportToBMP",
        "TIF":  "exportToTIFF",
        "TIFF": "exportToTIFF",
        "SVG":  "exportToSVG",
        "EPS":  "exportToEPS",
    }
    method = EXPORT_METHODS.get(fmt)
    if not method:
        raise ValueError(f"Unsupported format '{fmt}'. Use: PDF, PNG, JPG, BMP, TIF, SVG, EPS")
    if fmt == "PDF":
        getattr(lyt, method)(output)
    else:
        getattr(lyt, method)(output, resolution=dpi)

    return _ok({"exported": output, "layout": name, "format": fmt})


def handle_publish_web_layer(args):
    """Publish a layer as a hosted feature service to the ArcGIS Pro project's
    active portal (ArcGIS Online or Enterprise). Uses the ArcGIS API for Python
    (arcgis package) over REST, reusing the existing Pro sign-in token -- not
    arcpy.server, which doesn't work reliably from this bridge's background thread.
    Requires the user to already be signed in to a portal in ArcGIS Pro."""
    layer        = args.get("layer")
    service_name = args.get("service_name")
    summary      = args.get("summary", "")
    tags         = args.get("tags", "")
    public       = bool(args.get("public", False))
    overwrite    = bool(args.get("overwrite", False))

    if not layer:
        raise ValueError("'layer' is required")
    if not service_name:
        raise ValueError("'service_name' is required")

    portal = arcpy.GetActivePortalURL()
    if not portal:
        raise RuntimeError(
            "No active portal. Sign in to ArcGIS Online / Enterprise in ArcGIS Pro "
            "first (Settings > Portals), then set that portal active."
        )

    m = _get_map()
    layers = m.listLayers(layer)
    if not layers:
        raise RuntimeError(f"Layer '{layer}' not found.")
    catalog_path = arcpy.Describe(layers[0]).catalogPath

    # Uses the ArcGIS API for Python (arcgis package) over plain REST, reusing the
    # existing Pro sign-in token via arcpy.GetSigninToken() -- deliberately NOT
    # arcpy.mp.CreateWebLayerSDDraft / arcpy.server.StageService. Those are COM-based
    # and need the signed-in portal session in a way that's only available on ArcGIS
    # Pro's true main thread, which this bridge's background polling thread is not --
    # confirmed with a controlled test: the identical call fails instantly with a
    # generic ERROR 999999 from here, every time, but succeeds (in real time, ~10-30s)
    # when run directly in the Python window's own console instead. REST calls have
    # no such thread affinity, and this path is live-verified working from here.
    from arcgis.gis import GIS
    from arcgis.features import GeoAccessor

    token_info = arcpy.GetSigninToken()
    if not token_info:
        raise RuntimeError("Could not get a sign-in token for the active portal.")
    gis = GIS(portal, token=token_info["token"])

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    existing = [i for i in gis.content.search(
        query=f'title:"{service_name}" AND owner:{gis.users.me.username}',
        item_type="Feature Layer", max_items=10,
    ) if i.title == service_name]

    if existing and not overwrite:
        raise RuntimeError(
            f"A feature layer named '{service_name}' already exists "
            f"(id={existing[0].id}). Pass overwrite=True to replace it, or choose a different name."
        )

    sdf = GeoAccessor.from_featureclass(catalog_path)

    try:
        if existing:
            # True overwrite (same item/URL kept) rather than delete + republish --
            # documented via the installed arcgis package's own to_featurelayer()
            # docstring, not live-tested for this exact branch yet.
            lyr_item = sdf.spatial.to_featurelayer(
                title=service_name, gis=gis, tags=tag_list,
                overwrite=True,
                service={"featureServiceId": existing[0].id, "layer": 0},
            )
        else:
            lyr_item = sdf.spatial.to_featurelayer(title=service_name, gis=gis, tags=tag_list)
    except Exception as e:
        raise RuntimeError(f"Publish failed: {e}")

    if summary:
        lyr_item.update(item_properties={"snippet": summary})
    if public:
        lyr_item.share(everyone=True)

    return _ok({
        "layer": layer,
        "serviceName": service_name,
        "portal": portal,
        "public": public,
        "itemId": lyr_item.id,
        "url": lyr_item.homepage,
        "overwritten": bool(existing),
    })


# ── Dispatch table ────────────────────────────────────────────────────────────

def handle_recipe(args):
    """Run a high-value recipe by name (qa_layer, export_attributes_csv, batch_export_layouts)."""
    name = args.get("name")
    params = args.get("params", {}) or {}
    if not name:
        raise ValueError("'name' is required (e.g. 'qa_layer')")
    import importlib
    from hardening import recipes as _recipes_mod
    importlib.reload(_recipes_mod)  # hot-reload so new/edited recipes need no bridge restart
    RECIPES = _recipes_mod.RECIPES
    fn = RECIPES.get(name)
    if not fn:
        raise ValueError("Unknown recipe '%s'. Available: %s" % (name, sorted(RECIPES)))
    # Gate recipes through the SAME safe_mode policy as run_geoprocessing/execute_python
    # so they aren't an unrestricted side door: every GP tool a recipe declares must
    # pass check_gp_tool (allowlist + blocklist), and any recipe that runs arbitrary
    # code must pass check_execute_python. A recipe that reads only (cursors/Describe/
    # mp exports) declares no GP tool and is allowed. Raises SafetyError if blocked.
    for _t in (getattr(fn, "gp_tools", []) or []):
        check_gp_tool(_t, CFG)
    if getattr(fn, "needs_execute_python", False):
        check_execute_python(CFG)
    # Use _get_map() (falls back to the first map when no view is active) instead of
    # _proj.activeMap (None without an open view). Defensive None fallback for recipes
    # that don't need a map (e.g. batch_export_layouts).
    try:
        _m = _get_map()
    except Exception:
        _m = None
    return _ok(fn(arcpy=arcpy, m=_m, proj=_proj, **params))


HANDLERS = {
    "ping":                handle_ping,
    "get_project_info":    handle_get_project_info,
    "get_active_map_name": handle_get_active_map_name,
    "list_layers":         handle_list_layers,
    "add_vector_layer":    handle_add_vector_layer,
    "add_raster_layer":    handle_add_raster_layer,
    "remove_layer":        handle_remove_layer,
    "zoom_to_layer":       handle_zoom_to_layer,
    "count_features":      handle_count_features,
    "select_by_attribute": handle_select_by_attribute,
    "update_features":     handle_update_features,
    "save_project":        handle_save_project,
    "run_geoprocessing":   handle_run_geoprocessing,
    "get_layer_features":  handle_get_layer_features,
    "list_fields":          handle_list_fields,
    "list_feature_classes": handle_list_feature_classes,
    "list_rasters":         handle_list_rasters,
    "list_tables":          handle_list_tables,
    "set_workspace":        handle_set_workspace,
    "get_workspace":        handle_get_workspace,
    "clear_selection":      handle_clear_selection,
    "get_unique_values":    handle_get_unique_values,
    "set_layer_visibility": handle_set_layer_visibility,
    "create_map":           handle_create_map,
    "describe_data":        handle_describe_data,
    "list_directory":       handle_list_directory,
    "create_layout":       handle_create_layout,
    "execute_python":      handle_execute_python,
    "list_layouts":        handle_list_layouts,
    "export_layout":       handle_export_layout,
    "publish_web_layer":   handle_publish_web_layer,
}

# Expose recipes only when the hardening package actually loaded — otherwise the
# 'recipe' op would import hardening.recipes and raise ModuleNotFoundError. When it
# is not registered, run_recipe gets a clean "Unknown command: 'recipe'" instead.
if _HARDENING:
    HANDLERS["recipe"] = handle_recipe

# ── Background polling thread ─────────────────────────────────────────────────

_bridge_active = True

def _poll_loop():
    # Clear stale files
    for f in (CMD_FILE, RESULT_FILE, LOCK_FILE):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    # Adaptive backoff: poll fast (5 ms) right after activity so a quick command is
    # picked up almost instantly, then grow the idle sleep toward the configured
    # poll_interval cap while nothing is happening. Replaces the fixed 0.1 s wait,
    # which added up to 100 ms of latency to every file-IPC command.
    _MIN_POLL = 0.005
    _MAX_POLL = float(getattr(CFG, "poll_interval", 0.1)) or 0.1
    _idle = _MIN_POLL

    while _bridge_active:
        try:
            if not os.path.exists(CMD_FILE):
                time.sleep(_idle)
                _idle = min(_idle * 2, _MAX_POLL)
                continue

            _idle = _MIN_POLL  # command present -> stay responsive for the next one

            if os.path.exists(LOCK_FILE):
                time.sleep(_MIN_POLL)
                continue

            open(LOCK_FILE, "w").close()
            try:
                try:
                    with open(CMD_FILE, "r", encoding="utf-8") as f:
                        cmd = json.load(f)
                except (json.JSONDecodeError, ValueError) as e:
                    # Corrupt/garbage command file: discard it and answer with a
                    # readable error instead of retrying it forever.
                    os.remove(CMD_FILE)
                    with open(RESULT_FILE, "w", encoding="utf-8") as f:
                        json.dump(_err("Malformed command file (invalid JSON): %s" % e),
                                  f, default=str)
                    continue
                os.remove(CMD_FILE)

                op      = cmd.get("op", "")
                handler = HANDLERS.get(op)

                _t0 = time.time()
                if handler:
                    try:
                        result = handler(cmd.get("args", {}))
                    except SafetyError as e:
                        result = _err(f"Policy: {e}")
                    except Exception as e:
                        result = _err(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                else:
                    result = _err(f"Unknown command: '{op}'. Available: {sorted(HANDLERS)}")

                # hardening: stamp protocol version + write an audit line
                if isinstance(result, dict):
                    result.setdefault("protocol", getattr(CFG, "protocol_version", 1))
                if getattr(CFG, "audit", True):
                    try:
                        audit(LOG, op, cmd.get("args", {}), result.get("ok"), (time.time() - _t0) * 1000)
                    except Exception:
                        pass

                with open(RESULT_FILE, "w", encoding="utf-8") as f:
                    json.dump(result, f, default=str)
            finally:
                try:
                    os.remove(LOCK_FILE)
                except FileNotFoundError:
                    pass

        except Exception as e:
            print(f"[MCP Bridge] Poll error: {e}")
            time.sleep(0.1)

    # loop exited (_bridge_active=False) — also stop the socket transport if running
    try:
        _transport.stop()
    except Exception:
        pass

# ── Start ─────────────────────────────────────────────────────────────────────

_thread = threading.Thread(target=_poll_loop, daemon=True, name="MCP-Bridge")
_thread.start()

print(f"[MCP Bridge] Bridge is active. Listening for commands from Claude Desktop.")
print(f"[MCP Bridge] Project : {_proj.filePath or '(unsaved)'}")
print(f"[MCP Bridge] IPC dir : {IPC_DIR}")
print(f"[MCP Bridge] To stop : _bridge_active = False")

# ── Optional socket transport (additive; file IPC stays as the fallback) ──────
_transport = None
try:
    from hardening.bridge_transport import TransportServer, write_port_file

    def _dispatch(op, args):
        handler = HANDLERS.get(op)
        if not handler:
            return _err("Unknown command: '%s'" % op)
        _t0 = time.time()
        try:
            result = handler(args or {})
        except SafetyError as e:
            result = _err("Policy: %s" % e)
        except Exception as e:
            result = _err("%s: %s" % (type(e).__name__, e))
        if isinstance(result, dict):
            result.setdefault("protocol", getattr(CFG, "protocol_version", 1))
        # same observability as the file poll loop (this is now the preferred path)
        if getattr(CFG, "audit", True):
            try:
                audit(LOG, op, args or {}, result.get("ok"), (time.time() - _t0) * 1000)
            except Exception:
                pass
        return result

    _ttoken = os.urandom(8).hex()  # per-launch nonce so clients can verify the peer
    # Wait for the worker up to the SAME configured timeout the client uses (was a
    # hardcoded 600 s inside the transport). A geoprocessing run longer than the old
    # literal no longer makes the server reply "no response" while the worker is still
    # busy. The client's request timeout (also CFG.timeout_seconds) trips first, so a
    # too-slow op surfaces as a not-retried 'transport-inflight' rather than a phantom.
    _resp_timeout = getattr(CFG, "timeout_seconds", None)
    _transport = TransportServer(_dispatch, token=_ttoken, response_timeout=_resp_timeout)
    _tport = _transport.start()
    write_port_file(IPC_DIR, _tport, _ttoken)
    print("[MCP Bridge] Socket transport on 127.0.0.1:%d (file IPC still active)." % _tport)
except Exception as _te:
    print("[MCP Bridge] Socket transport not started (%r); file IPC only." % _te)
