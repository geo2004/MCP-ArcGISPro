"""Socket transport for the MCP bridge — replaces file-polling IPC.

Length-prefixed JSON over a localhost TCP socket:
  * No 0.1 s polling latency, no lock-file races.
  * Request/response correlation via an integer `id`.
  * Many connections accepted concurrently, but requests are dispatched SERIALLY
    on a single worker thread — required because arcpy is not thread-safe and must
    be driven from one consistent thread (same constraint the file bridge had).
  * One port per ArcGIS Pro instance -> natural per-session isolation (roadmap #6).

No arcpy import -> fully unit-testable on loopback.

Wire-up sketch (see README-TRANSPORT.md):
  bridge:  srv = TransportServer(lambda op, args: HANDLERS[op](args)); port = srv.start()
           write_port_file(IPC_DIR, port)
  server:  port = read_port_file(IPC_DIR); resp = send_request(port, op, args, timeout=CFG.timeout_seconds)
"""
import itertools
import json
import os
import socket
import struct
import threading
import queue

HOST = "127.0.0.1"
_HEADER = struct.Struct(">I")  # 4-byte big-endian length prefix
_id_counter = itertools.count(1)


def _next_id():
    return next(_id_counter)


# ── framing ─────────────────────────────────────────────────────────────────

def _send_msg(sock, obj):
    data = json.dumps(obj, default=str).encode("utf-8")
    sock.sendall(_HEADER.pack(len(data)) + data)


def _recv_exactly(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed mid-message")
        buf.extend(chunk)
    return bytes(buf)


def _recv_msg(sock):
    (length,) = _HEADER.unpack(_recv_exactly(sock, _HEADER.size))
    return json.loads(_recv_exactly(sock, length).decode("utf-8"))


# ── port discovery / per-session isolation ───────────────────────────────────

def pick_free_port(host=HOST):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def write_port_file(ipc_dir, port):
    os.makedirs(ipc_dir, exist_ok=True)
    path = os.path.join(ipc_dir, "port.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"host": HOST, "port": int(port)}, f)
    return path


def read_port_file(ipc_dir):
    with open(os.path.join(ipc_dir, "port.json"), "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("host", HOST), int(d["port"])


# ── server (runs inside ArcGIS Pro) ──────────────────────────────────────────

class TransportServer:
    """Accepts connections on per-connection threads; dispatches requests serially
    on ONE worker thread. `dispatch(op, args)` returns a dict result."""

    def __init__(self, dispatch, host=HOST, port=0):
        self.dispatch = dispatch
        self.host = host
        self.port = port
        self._srv = None
        self._stop = threading.Event()
        self._q = queue.Queue()

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self.port = self._srv.getsockname()[1]
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        threading.Thread(target=self._worker, daemon=True, name="MCP-Transport-Worker").start()
        threading.Thread(target=self._accept_loop, daemon=True, name="MCP-Transport-Accept").start()
        return self.port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        try:
            while not self._stop.is_set():
                try:
                    req = _recv_msg(conn)
                except (ConnectionError, OSError, json.JSONDecodeError):
                    break
                box, done = {}, threading.Event()
                self._q.put((req, box, done))
                done.wait(timeout=600)
                resp = box.get("resp") or {"id": req.get("id"), "ok": False,
                                           "error": "no response", "data": None}
                try:
                    _send_msg(conn, resp)
                except OSError:
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _worker(self):
        while not self._stop.is_set():
            try:
                req, box, done = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            rid = req.get("id")
            try:
                result = self.dispatch(req.get("op", ""), req.get("args", {}) or {})
                if not isinstance(result, dict):
                    result = {"ok": True, "error": None, "data": result}
                result = dict(result)
                result["id"] = rid
            except Exception as e:  # noqa: BLE001 - report any handler error to the client
                result = {"id": rid, "ok": False,
                          "error": "%s: %s" % (type(e).__name__, e), "data": None}
            box["resp"] = result
            done.set()

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass


# ── client (runs in the MCP server process) ──────────────────────────────────

def send_request(port, op, args=None, host=HOST, timeout=60):
    """One-shot request. Returns the response dict, or an error dict on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        _send_msg(s, {"id": _next_id(), "op": op, "args": args or {}})
        return _recv_msg(s)
    except (socket.timeout, ConnectionError, OSError) as e:
        return {"ok": False, "error": "transport: %s" % e, "data": None}
    finally:
        try:
            s.close()
        except OSError:
            pass
