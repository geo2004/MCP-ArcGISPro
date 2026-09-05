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

## Configurable worker wait (roadmap #1)

`TransportServer(response_timeout=...)` controls how long a connection thread waits for
the single worker to finish a request before it stops blocking and replies (the worker
keeps running — it is never interrupted). `pro_bridge.py` passes `CFG.timeout_seconds`,
so a geoprocessing run longer than the old hardcoded **600 s** literal no longer makes the
server answer "no response" while the worker is still busy.

Because the client's request timeout is the *same* `CFG.timeout_seconds` and its
recv-timeout clock starts earlier (it sends before the server enqueues), the **client**
trips first on a too-slow op. That surfaces on the client as `transport-inflight:` — which
is explicitly **never retried over file IPC** (retrying could duplicate a side-effecting
geoprocessing run). The server-side `response_timeout` is only the backstop for the case
where a client waits even longer than the bridge. To allow long runs, raise
`timeout_seconds` in `~/.arcgis_mcp/config.json` (it governs both ends).

## Security / threat model (roadmap #4)

The socket transport is bound to **`127.0.0.1` only** — never reachable off-host. Each
launch writes a fresh random **token** (per-launch nonce) into `port.json`; the bridge
greets every new connection with it and the client verifies it before sending the request.

What the token defends against: a **stale/recycled port**. After the bridge stops, its
`port.json` can outlive it and the OS may hand that port number to an unrelated local
process. The handshake makes the client detect "this peer is not my bridge" *before*
dispatching (reported as `transport-connect:`, which is safe to fall back from).

What the token does **not** defend against: another process **running as the same user**.
Such a process can already read `port.json`, sniff loopback, or attach to ArcGIS Pro
directly — the OS user boundary, not this token, is the real security perimeter. The token
is an integrity/anti-staleness check, not a secret against a same-user adversary.

Hardening applied without touching the handshake (so the deployed `MCP_Bridge.pyt` toolbox
keeps working): `write_port_file()` now calls a **best-effort** `_restrict_file_permissions()`
that, on Windows, runs `icacls <port.json> /inheritance:r /grant:r <user>:F` to strip
inherited ACEs and grant read only to the current user (`chmod 0o600` on POSIX). It is
wrapped so any failure never breaks writing the port file. This narrows the token's exposure
to *other* local users; it does not — and cannot at this layer — protect against a malicious
process already running under the same account. The wire protocol is unchanged.

## Status

Wired into the live bridge (`pro_bridge.py` starts the socket server next to the kept file
poll loop; `arcgis_mcp_server.py` prefers the socket and falls back to file IPC). Per-session
isolation (#6) comes for free once each instance writes its own `port.json` (extend to a
per-project filename if running several Pro instances at once).

**Verified:** `python hardening/test_transport.py` (loopback server+client, 25 concurrent
clients correlated correctly, 200 KB framing, handler-exception path, port-file round-trip,
token handshake, configurable `response_timeout`, permission-hardened port file).
