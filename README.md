# ArcGIS Pro MCP Bridge

Control ArcGIS Pro from Claude Desktop using the Model Context Protocol (MCP).  
Pure Python — no C#, no .NET SDK, no Visual Studio required.

---

## What It Does

Connects Claude Desktop (or any MCP client) to a live ArcGIS Pro session, enabling natural language control of:

- Add / remove layers (vector and raster)
- Run any ArcPy geoprocessing tool (buffer, clip, intersect, project, dissolve, and more)
- Check coordinate systems and reproject data
- Query and select features by attribute
- List layers, fields, feature classes, rasters, and tables
- Create layouts and export maps (PDF, PNG, JPG, TIF, SVG, EPS)
- Zoom to layers, toggle visibility, save projects
- Bulk-edit attribute values on features matching a query
- Publish a layer as a hosted feature service to ArcGIS Online / Enterprise
- Execute arbitrary ArcPy / arcpy.mp code

---

## Requirements

- ArcGIS Pro 3.x (tested on 3.6.1)
- Claude Desktop
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — lightweight Python package manager
- The `arcgis` package (ArcGIS API for Python) in ArcGIS Pro's Python environment, for `publish_web_layer` only — bundled by default in ArcGIS Pro's `arcgispro-py3` environment on recent versions; the rest of the bridge doesn't need it


---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Geo2004/MCP-ArcGISPro.git
```

### 2. Install uv (if not already installed)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` handles the `mcp` package dependency automatically — no manual `pip install` needed.

### 3. Configure Claude Desktop

Open your Claude Desktop config file and add the `arcgis-pro` entry under `mcpServers`.

**Config file location:**

| Installation | Path |
|---|---|
| Standard installer | `%APPDATA%\Claude\claude_desktop_config.json` |
| Windows Store (Microsoft Store) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |

Not sure which you have? Open Claude Desktop → Settings → Developer → Hit Edit Config Button.

```json
{
  "mcpServers": {
    "arcgis-pro": {
      "command": "C:/Users/<YourUsername>/.local/bin/uv.exe",
      "args": [
        "--directory",
        "C:/path/to/MCP-ArcGISPro",
        "run",
        "arcgis_mcp_server.py"
      ]
    }
  }
}
```

Replace `<YourUsername>` and the directory path with your actual values.

### 3. Start the bridge in ArcGIS Pro

Every session, before using Claude:

1. Open **ArcGIS Pro** and load a project (or create a new one)
2. Open a **Map**, **Scene**, or **Globe** so it is active
3. Go to the **Analysis** tab → click **Python** to open the Python window
4. Run:

```python
p = r"C:/path/to/MCP-ArcGISPro/pro_bridge.py"
exec(open(p).read(), {"__file__": p})
```

> Passing `{"__file__": p}` lets the optional hardening/recipes/socket layer (which
> lives next to `pro_bridge.py`) load. Plain `exec(open(p).read())` still runs the
> core bridge, but only with built-in defaults. Easiest of all: use the **MCP Bridge
> toolbox** (Start button), which handles this for you.

You should see:
```
[MCP Bridge] Project cached: C:\path\to\your.aprx
[MCP Bridge] Running in background thread. IPC: C:\Users\..\.arcgis_mcp
[MCP Bridge] Python window is free.  To stop: _bridge_active = False
```

### 4. Restart Claude Desktop

Restart Claude Desktop after updating the config. The ArcGIS Pro tools will appear automatically.

---

## Usage

Just talk to Claude naturally. Examples:

> *"Add the roads shapefile from C:/data to the map"*

> *"Check the coordinate system of parcels.shp and run a 500m buffer, dissolve all overlaps"*

> *"List all layers in my current map"*

> *"Export the layout as PNG to C:/output/map.png"*

> *"Select all features where KECAMATAN = 'Semarang Tengah' and count them"*

> *"Set STATUS to 'Verified' for every feature where INSPECTED = 1"*

> *"Publish the parcels layer to ArcGIS Online as a private hosted layer named 'Parcels_2026'"*

> *"Run a slope analysis on my DEM raster"*

---

## Available Tools (31)

| Category | Tools |
|---|---|
| **Connection** | `ping` |
| **Project** | `get_project_info`, `save_project` |
| **Map** | `get_active_map_name`, `create_map` |
| **Layers** | `list_layers`, `add_vector_layer`, `add_raster_layer`, `remove_layer`, `zoom_to_layer`, `set_layer_visibility` |
| **Data** | `describe_data`, `list_directory`, `list_fields`, `get_layer_features`, `get_unique_values` |
| **Workspace** | `set_workspace`, `get_workspace`, `list_feature_classes`, `list_rasters`, `list_tables` |
| **Selection** | `select_by_attribute`, `clear_selection`, `count_features` |
| **Editing** | `update_features` *(bulk-set field values on matching rows)* |
| **Geoprocessing** | `run_geoprocessing` *(any arcpy tool by dotted name)* |
| **Layout & Export** | `list_layouts`, `create_layout`, `export_layout` |
| **Sharing** | `publish_web_layer` *(hosted feature service to ArcGIS Online/Enterprise)* |
| **Advanced** | `execute_python` *(arbitrary arcpy/arcpy.mp code)* |

---

## How It Works

```
Claude Desktop
    ↓  stdio (MCP)
arcgis_mcp_server.py   ← runs via uv, any Python
    ↓  file-based IPC  (~/.arcgis_mcp/)
pro_bridge.py          ← runs in ArcGIS Pro's Python window, on a background thread
    ↓                                          ↓
arcpy / arcpy.mp                        arcgis (ArcGIS API for Python)
    ↓                                          ↓
ArcGIS Pro (live session)               ArcGIS Online / Enterprise portal, over REST
```

The bridge uses file-based IPC (command / result JSON files in `~/.arcgis_mcp/`) — no sockets, no named pipes, no compilation required.

Almost everything goes through `arcpy`/`arcpy.mp` against the live local session. **`publish_web_layer` is the one exception**, and deliberately so: `arcpy.server`/`arcpy.mp.CreateWebLayerSDDraft` are COM-based and need the signed-in portal session in a way that's only available on ArcGIS Pro's true main thread — not the background thread this bridge dispatches every command from. Rather than block on that, `publish_web_layer` uses the `arcgis` package instead, which talks to the portal over plain REST (reusing the same Pro sign-in via `arcpy.GetSigninToken()`, no separate login) — REST has no thread affinity, so it works fine from here. Same pattern is available for other portal-side operations (content management, sharing, editing an already-published layer, user/group admin) if this bridge grows in that direction later — those aren't subject to the thread limitation either.

---

## Known Limitations

- **Opening a map view automatically** is not possible via Python/ArcPy alone — it's an application-shell action that lives in the compiled ArcGIS Pro SDK, not the arcpy document API. The user must open the map view manually once per session by double-clicking it in the Catalog pane. All other operations work without an open view — for zooming/visualizing a layer specifically, `create_layout()` + `export_layout()` render headlessly and don't need an open view at all.
- **Interactive sketch-tool digitizing** (drawing features by hand) requires the ArcGIS Pro UI and cannot be automated — by definition, freehand digitizing needs a human at the screen. Non-interactive attribute edits don't have this limitation: use `update_features`.

---

## Stopping the Bridge

In the ArcGIS Pro Python window:

```python
_bridge_active = False
```

Or simply close the Python window or restart ArcGIS Pro.

---

## License

MIT
