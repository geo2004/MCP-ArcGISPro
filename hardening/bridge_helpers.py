"""Response builders + robust dataset resolution for the MCP bridge.

These replace ad-hoc result dicts and the brittle "pass a layer name to arcpy" pattern
that fails from the bridge's background thread (observed live: Buffer rejected a layer
name and required the full dataSource path).

`resolve_dataset` takes `arcpy` as a parameter so this module stays import-safe and unit
testable without ArcGIS Pro.
"""

PROTOCOL_VERSION = 1


def ok(data, **meta):
    """Success envelope. Backward compatible: keeps ok/data/error keys."""
    r = {"ok": True, "error": None, "data": data, "protocol": PROTOCOL_VERSION}
    r.update(meta)
    return r


def err(message, code="ERROR", detail=None):
    """Error envelope. `error` stays a human string (backward compatible) and adds
    structured `error_code` / `error_detail` for clients that want them."""
    return {
        "ok": False,
        "error": str(message),
        "error_code": code,
        "error_detail": detail,
        "data": None,
        "protocol": PROTOCOL_VERSION,
    }


def err_from_exc(exc, code=None):
    import traceback
    return err(str(exc), code=code or type(exc).__name__, detail=traceback.format_exc())


def resolve_dataset(arcpy, m, name_or_path):
    """Resolve a Contents-pane layer NAME or a dataset PATH into something arcpy tools
    accept reliably.

    Order:
      1. If it already exists as a dataset path -> return it unchanged.
      2. Else if a layer with that name exists in map `m` -> return its dataSource path
         (falls back to the layer object if the source can't be read).
      3. Else raise ValueError.
    """
    if not name_or_path:
        raise ValueError("dataset name/path is required")

    try:
        if arcpy.Exists(name_or_path):
            return name_or_path
    except Exception:
        pass  # arcpy.Exists can throw on odd inputs; treat as "not a path"

    if m is not None:
        layers = m.listLayers(name_or_path)
        if layers:
            lyr = layers[0]
            src = getattr(lyr, "dataSource", None)
            if src:
                return src
            return lyr  # tools that resolve against the active map can still use it

    raise ValueError(
        "'{}' is neither an existing dataset nor a layer in the active map.".format(name_or_path)
    )


def resolve_param(arcpy, m, p):
    """Soft resolver for geoprocessing parameters.

    If `p` is a layer NAME present in map `m`, return its dataSource path;
    otherwise return `p` unchanged. NEVER raises — so non-dataset params
    (distances like "10 Meters", field names, numbers, output paths) pass
    through untouched. Use this to map a tool's positional params before
    calling it, fixing the "layer name doesn't resolve in the background
    thread" bug without breaking other parameters.
    """
    if not isinstance(p, str) or not p:
        return p
    try:
        if arcpy.Exists(p):
            return p
    except Exception:
        pass
    try:
        if m is not None:
            layers = m.listLayers(p)
            if layers and getattr(layers[0], "dataSource", None):
                return layers[0].dataSource
    except Exception:
        pass
    return p
