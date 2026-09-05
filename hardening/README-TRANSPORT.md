# Socket transport (roadmap #2) — drop-in replacement for file-polling IPC

`bridge_transport.py` replaces the `command.json`/`result.json`/`lock` file polling
with a localhost TCP socket using length-prefixed JSON.

**Why it's better**
- No 0.1 s polling latency (measured < 100 ms per call in tests) and no lock-file races.
- Request/response correlation via an `id`; many clients accepted concurrently.
- Requests dispatched **serially on one worker thread** — required for arcpy thread-affinity.
- One port per ArcGIS Pro instance → natural **per-session isolation** (roadmap #6).

**Verified:** `python hardening/test_transport.py` → 12/12 (loopback server+client, 25 concurrent
clients correlated correctly, 200 KB framing, handler-exception path, port-file round-trip).

## Recommended wiring (additive — keep file IPC as fallback)

Refactor the handler invocation in `pro_bridge.py` into one function so the socket server
and the (kept) file loop share the same safety/audit/protocol wrapper:

```python
def _dispatch(op, args):
    handler = HANDLERS.get(op)
    if not handler:
        return _err("Unknown command: '%s'" % op)
    try:
        result = handler(args or {})
    except SafetyError as e:
        result = _err("Policy: %s" % e)
    except Exception as e:
        result = _err("%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    if isinstance(result, dict):
        result.setdefault("protocol", getattr(CFG, "protocol_version", 1))
    return result
```

Then start the socket server next to the existing poll loop:

```python
from hardening.bridge_transport import TransportServer, write_port_file
_srv = TransportServer(_dispatch)
write_port_file(IPC_DIR, _srv.start())
print("[MCP Bridge] Socket transport on port %d" % _srv.port)
# (the file poll loop can stay running for backward compatibility)
```

In `arcgis_mcp_server.py`, prefer the socket and fall back to file IPC:

```python
def _call(op, args=None):
    try:
        from hardening.bridge_transport import read_port_file, send_request
        host, port = read_port_file(IPC_DIR)
        resp = send_request(port, op, args or {}, host=host, timeout=TIMEOUT)
        if "transport:" not in (resp.get("error") or ""):
            return resp                      # socket worked
    except Exception:
        pass
    return _call_via_files(op, args)         # existing file-based path, unchanged
```

## Status

Module + tests complete and committed. **Not yet wired into the live bridge** — the wiring
above is additive and low-risk (file IPC remains the fallback), but should be validated once
end-to-end in ArcGIS Pro before relying on it. Per-session isolation (#6) comes for free once
each instance writes its own `port.json` (extend to a per-project filename if running several
Pro instances at once).
