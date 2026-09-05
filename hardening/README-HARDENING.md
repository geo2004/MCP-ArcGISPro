# Hardening layer — product-grade improvements

Drop-in modules that address the "blockers for selling" beyond friction #1 (the
toolbox/add-in). Pure Python, **no arcpy at import time**, and **unit-tested**
(`python hardening/test_hardening.py` → 18/18). Designed to layer onto the existing
`pro_bridge.py` / `arcgis_mcp_server.py` without rewriting them.

## What each module delivers

| Module | Improvement (from the roadmap) | What it fixes |
|---|---|---|
| `bridge_config.py` | **#3 configurable timeout**, behavior toggles | Externalizes the hardcoded `TIMEOUT=15` → `timeout_seconds` (default 60). Adds `auto_create_map` (default **False** — kills the silent map-creation side effect). Loaded from `<ipc_dir>/config.json`. |
| `bridge_safety.py` | **#4 sandbox / policy** | `safe_mode` (default on): `execute_python` disabled unless opted in; geoprocessing limited to an **allowlist** of toolbox prefixes + optional **blocklist**. |
| `bridge_helpers.py` | **#5 robust layer↔path** + **#2 protocol version** + clean errors | `resolve_dataset()` turns a Contents-pane layer name into its `dataSource` (the bug we hit live with Buffer). Structured `ok()/err()` envelopes carry `protocol` + `error_code`, staying backward compatible (`error` is still a string). |
| `bridge_logging.py` | **observability / support** | Rotating `bridge.log` + one `AUDIT` line per command (op, ok, ms, truncated args). |

## Integration (≈30 min)

### In `pro_bridge.py`
```python
# near the top, after IPC_DIR is known
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))   # so hardening/ is importable
from hardening.bridge_config  import BridgeConfig
from hardening.bridge_logging import get_logger, audit
from hardening.bridge_safety  import check_gp_tool, check_execute_python, SafetyError
from hardening.bridge_helpers import ok, err, err_from_exc, resolve_dataset

CFG = BridgeConfig.load()
LOG = get_logger(CFG.ipc_dir, CFG.log_level)
```

1. **Timeout:** in `arcgis_mcp_server.py` replace `TIMEOUT = 15` with
   `TIMEOUT = BridgeConfig.load().timeout_seconds`.
2. **No silent map:** in `_get_map()`, only `createMap("Map")` when `CFG.auto_create_map`
   is true; otherwise raise a clear error telling the user to create/open a map.
3. **Robust inputs:** in `handle_run_geoprocessing` (and any handler taking a layer/dataset),
   map params through `resolve_dataset(arcpy, _get_map(), p)` before calling the tool.
4. **Safety:** at the top of `handle_run_geoprocessing` call `check_gp_tool(tool, CFG)`;
   at the top of `handle_execute_python` call `check_execute_python(CFG)`. Catch
   `SafetyError` and return `err(str(e), code="POLICY")`.
5. **Audit:** in the poll loop, time each handler and call
   `audit(LOG, op, args, result["ok"], elapsed_ms)`.
6. **Envelopes:** swap the local `_ok/_err` for the shared `ok/err` (adds protocol/version).

### Config
Copy `config.example.json` to `<ipc_dir>/config.json` and edit. Safe defaults ship locked
down (no arbitrary Python, allowlisted geoprocessing); relax per deployment.

## Status / honesty

- ✅ **Logic verified here** (18/18 unit tests, no arcpy needed).
- ⚠️ **End-to-end wiring needs ArcGIS Pro** to validate (the running bridge in this session
  is the old manually-pasted one). Integrate, relaunch the bridge, and re-run a Buffer +
  `execute_python` to confirm safety/resolution behave live.

## Still on the roadmap (designed, not yet built)

These are high-value but **risky to ship without live Pro testing**, so they're documented
rather than coded blind:

- **#2 Transport:** replace file-polling IPC with a **localhost socket / named pipe** +
  request IDs + a command **queue** (multiple in-flight, no 0.1 s latency floor, no lock
  races). Requires coordinated changes in both server and bridge; do it behind the same
  `ok/err` envelope so handlers don't change.
- **#3 Async + progress:** long tools (big buffers, rasters) should stream `arcpy`
  progress and not block; pairs with the socket transport.
- **#6 Per-session isolation:** namespace the IPC dir / socket by Pro instance + project
  so two sessions don't collide (today there's one bridge per machine).
- **i18n** of the tool instructions, and a **recipe catalog** (QA reports, batch map
  export, Excel/Word integration) as the actual sellable value.
