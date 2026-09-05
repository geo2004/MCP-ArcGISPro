"""Tests for the socket transport. Real loopback sockets, no arcpy.
Run: python hardening/test_transport.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
import bridge_transport as bt

_passed = 0
_failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS:", label)
    else:
        _failed += 1
        print("  FAIL:", label)


def dispatch(op, args):
    if op == "ping":
        return {"ok": True, "error": None, "data": "pong"}
    if op == "echo":
        return {"ok": True, "error": None, "data": args}
    if op == "slow":
        time.sleep(0.2)
        return {"ok": True, "error": None, "data": args.get("n")}
    if op == "boom":
        raise RuntimeError("kaboom")
    return {"ok": False, "error": "unknown op", "data": None}


def main():
    srv = bt.TransportServer(dispatch)
    port = srv.start()
    try:
        check("server started on a port", isinstance(port, int) and port > 0)

        r = bt.send_request(port, "ping", timeout=5)
        check("ping -> pong", r.get("ok") and r.get("data") == "pong")
        check("response carries an id", "id" in r)

        payload = {"name": "puntos", "big": "x" * 200000}  # ~200 KB round-trip
        r = bt.send_request(port, "echo", payload, timeout=5)
        check("echo round-trips args", r.get("data") == payload)
        check("large payload framed correctly", len(r["data"]["big"]) == 200000)

        r = bt.send_request(port, "boom", timeout=5)
        check("handler exception -> ok False", r.get("ok") is False)
        check("error mentions the exception", "kaboom" in (r.get("error") or ""))

        r = bt.send_request(port, "nope", timeout=5)
        check("unknown op handled", r.get("ok") is False)

        # port file round-trip (per-session discovery)
        import tempfile
        d = tempfile.mkdtemp()
        bt.write_port_file(d, port)
        host2, port2 = bt.read_port_file(d)
        check("port file round-trip", port2 == port and host2 == bt.HOST)

        # concurrency: many simultaneous clients, each gets the correct answer
        results = {}
        errors = []

        def worker(i):
            try:
                resp = bt.send_request(port, "echo", {"i": i}, timeout=10)
                results[i] = resp.get("data", {}).get("i")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        check("25 concurrent clients, no errors", not errors)
        check("each concurrent response correlated correctly",
              all(results.get(i) == i for i in range(25)))

        # latency sanity: a request returns well under the old 0.1s poll floor
        t0 = time.time()
        bt.send_request(port, "ping", timeout=5)
        check("ping latency < 100ms (no poll floor)", (time.time() - t0) < 0.1)
    finally:
        srv.stop()

    print("\n{} passed, {} failed".format(_passed, _failed))
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
