"""High-value 'recipes' — multi-step GIS workflows exposed as one call.

This is where the sellable value lives: wrap common deliverables (QA reports,
attribute exports, batch map export) into single operations instead of many
low-level tool calls.

Dependencies (arcpy / map / project) are passed IN so the orchestration logic is
unit-testable without ArcGIS Pro. Each recipe returns a JSON-serializable dict.

Integration sketch (bridge handler):
    def handle_recipe(args):
        name = args["name"]; params = args.get("params", {})
        return _ok(RECIPES[name](arcpy=arcpy, m=_get_map(), proj=_proj, **params))
"""
import csv
import os
import re


def _find_layer(m, layer_name):
    if m is None:
        raise RuntimeError("No active map.")
    layers = m.listLayers(layer_name)
    if not layers:
        raise ValueError("Layer '%s' not found in the active map." % layer_name)
    return layers[0]


def _safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "layout"


# ── Recipe 1: layer QA / QC report ───────────────────────────────────────────

def qa_layer(arcpy, m, layer_name, **_):
    """Quality-control report for a feature layer: CRS, geometry, count, fields,
    null-geometry detection, and a list of human-readable issues/warnings."""
    lyr = _find_layer(m, layer_name)
    # arcpy tools don't resolve a layer object/name from the bridge thread —
    # operate on the data-source path (same lesson as resolve_dataset).
    src = getattr(lyr, "dataSource", None) or lyr
    desc = arcpy.Describe(src)
    sr = getattr(desc, "spatialReference", None)

    crs = None
    if sr is not None:
        crs = {
            "name": getattr(sr, "name", None),
            "type": getattr(sr, "type", None),
            "wkid": getattr(sr, "factoryCode", None),
            "unit": getattr(sr, "linearUnitName", None) if getattr(sr, "type", None) == "Projected" else None,
        }

    fields = [{"name": f.name, "type": f.type, "nullable": getattr(f, "isNullable", None)}
              for f in arcpy.ListFields(src)]

    count = int(arcpy.management.GetCount(src).getOutput(0))

    # null / empty geometry scan
    null_geom = 0
    try:
        with arcpy.da.SearchCursor(src, ["SHAPE@"]) as cur:
            for row in cur:
                if row[0] is None:
                    null_geom += 1
    except Exception:
        null_geom = None  # not a feature layer or no geometry

    issues = []
    if crs and crs.get("type") == "Geographic":
        issues.append("CRS is geographic (degrees) — reproject before metric geoprocessing.")
    if crs and (crs.get("name") in (None, "Unknown")):
        issues.append("Layer has no/unknown spatial reference.")
    if count == 0:
        issues.append("Layer has no features.")
    if null_geom:
        issues.append("%d feature(s) have null/empty geometry." % null_geom)

    return {
        "recipe": "qa_layer",
        "layer": layer_name,
        "source": getattr(lyr, "dataSource", None),
        "geometry_type": getattr(desc, "shapeType", None),
        "crs": crs,
        "feature_count": count,
        "field_count": len(fields),
        "fields": fields,
        "null_geometry": null_geom,
        "issues": issues,
        "ok": not issues,
    }


# ── Recipe 2: attribute table -> CSV ──────────────────────────────────────────

def export_attributes_csv(arcpy, m, layer_name, out_path, fields=None, limit=None, **_):
    """Export a layer's attribute table to CSV (for reporting / Excel)."""
    lyr = _find_layer(m, layer_name)
    src = getattr(lyr, "dataSource", None) or lyr
    if not fields:
        fields = [f.name for f in arcpy.ListFields(src)
                  if f.type not in ("Geometry", "Blob", "Raster")]
    if not fields:
        raise ValueError("No exportable fields found.")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    rows_written = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        with arcpy.da.SearchCursor(src, fields) as cur:
            for row in cur:
                if limit is not None and rows_written >= limit:
                    break
                w.writerow(["" if v is None else v for v in row])
                rows_written += 1

    return {
        "recipe": "export_attributes_csv",
        "layer": layer_name,
        "output": out_path,
        "fields": fields,
        "rows": rows_written,
    }


# ── Recipe 3: batch export all layouts ───────────────────────────────────────

_EXPORT_METHODS = {
    "PDF": "exportToPDF", "PNG": "exportToPNG", "JPG": "exportToJPEG",
    "JPEG": "exportToJPEG", "TIF": "exportToTIFF", "TIFF": "exportToTIFF",
}


def batch_export_layouts(proj, out_dir, fmt="PDF", dpi=150, **_):
    """Export EVERY layout in the project to out_dir as PDF/PNG/JPG/TIF."""
    fmt = str(fmt).upper()
    method = _EXPORT_METHODS.get(fmt)
    if not method:
        raise ValueError("Unsupported format '%s'. Use: %s" % (fmt, ", ".join(sorted(_EXPORT_METHODS))))
    os.makedirs(out_dir, exist_ok=True)

    exported, errors = [], []
    layouts = proj.listLayouts()
    for lyt in layouts:
        out = os.path.join(out_dir, _safe_filename(lyt.name) + "." + fmt.lower())
        try:
            if fmt == "PDF":
                getattr(lyt, method)(out)
            else:
                getattr(lyt, method)(out, resolution=dpi)
            exported.append({"layout": lyt.name, "output": out})
        except Exception as e:  # noqa: BLE001
            errors.append({"layout": lyt.name, "error": str(e)})

    return {
        "recipe": "batch_export_layouts",
        "format": fmt,
        "dir": out_dir,
        "exported": exported,
        "errors": errors,
        "count": len(exported),
        "total_layouts": len(layouts),
    }


# ── Recipe 4: numeric field statistics ───────────────────────────────────────

def field_stats(arcpy, m, layer_name, field, **_):
    """Min/max/mean/sum/count/nulls for a numeric field (QC of measurements)."""
    lyr = _find_layer(m, layer_name)
    src = getattr(lyr, "dataSource", None) or lyr
    values, nulls = [], 0
    with arcpy.da.SearchCursor(src, [field]) as cur:
        for row in cur:
            v = row[0]
            if v is None:
                nulls += 1
            else:
                values.append(v)
    n = len(values)
    numeric = all(isinstance(v, (int, float)) for v in values) if values else False
    stats = {"count": n, "nulls": nulls}
    if n and numeric:
        stats.update({
            "min": min(values),
            "max": max(values),
            "sum": sum(values),
            "mean": round(sum(values) / n, 4),
        })
    return {"recipe": "field_stats", "layer": layer_name, "field": field,
            "numeric": numeric, "stats": stats}


# ── Recipe 5: categorical value counts ───────────────────────────────────────

def value_counts(arcpy, m, layer_name, field, top=20, **_):
    """Frequency of each distinct value in a field (categorical QC)."""
    lyr = _find_layer(m, layer_name)
    src = getattr(lyr, "dataSource", None) or lyr
    counts, nulls = {}, 0
    with arcpy.da.SearchCursor(src, [field]) as cur:
        for row in cur:
            v = row[0]
            if v is None:
                nulls += 1
            else:
                counts[v] = counts.get(v, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return {"recipe": "value_counts", "layer": layer_name, "field": field,
            "distinct": len(counts), "nulls": nulls,
            "top": [{"value": k, "count": c} for k, c in ordered]}


# GP tools each recipe invokes internally. handle_recipe() runs check_gp_tool()
# on every entry here BEFORE dispatch, so recipes are subject to the same
# safe_mode allowlist/blocklist as run_geoprocessing — no unrestricted side door.
# A recipe that only reads (SearchCursor / Describe / ListFields / mp exports)
# declares no GP tool. Add every management.* / analysis.* tool a new recipe calls
# here, or it runs ungated. `needs_execute_python` (function attr, default False)
# gates any future recipe that runs arbitrary/eval'd code behind check_execute_python.
qa_layer.gp_tools = ["management.GetCount"]
export_attributes_csv.gp_tools = []
batch_export_layouts.gp_tools = []
field_stats.gp_tools = []
value_counts.gp_tools = []


RECIPES = {
    "qa_layer": qa_layer,
    "export_attributes_csv": export_attributes_csv,
    "batch_export_layouts": batch_export_layouts,
    "field_stats": field_stats,
    "value_counts": value_counts,
}
