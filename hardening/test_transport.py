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
        bt.write_port_file(d, port, "tok123")
        host2, port2, tok2 = bt.read_port_file(d)
        check("port file round-trip (host/port/token)", port2 == port and host2 == bt.HOST and tok2 == "tok123")

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

        # P1: connecting to a dead port -> 'transport-connect:' (safe to fall back)
        dead = bt.send_request(99, "ping", timeout=2)
        check("dead port -> transport-connect (pre-dispatch)",
              str(dead.get("error", "")).startswith("transport-connect:"))
        # a successful response never carries a transport-* error
        good = bt.send_request(port, "ping", timeout=5)
        check("live request has no transport error", not str(good.get("error", "") or "").startswith("transport-"))

        # P2: token handshake — wrong token is treated as a pre-dispatch failure
        tsrv = bt.TransportServer(dispatch, token="secret")
        tport = tsrv.start()
        try:
            okt = bt.send_request(tport, "ping", token="secret", timeout=5)
            check("correct token -> request succeeds", okt.get("data") == "pong")
            badt = bt.send_request(tport, "ping", token="WRONG", timeout=5)
            check("wrong token -> transport-connect (fallback)",
                  str(badt.get("error", "")).startswith("transport-connect:"))
        finally:
            tsrv.stop()

        # roadmap #1: the worker-wait is CONFIGURABLE, not a hardcoded 600 s literal
        check("default response_timeout is 600",
              bt.TransportServer(dispatch).response_timeout == 600)
        check("custom response_timeout honored",
              bt.TransportServer(dispatch, response_timeout=42).response_timeout == 42)
        check("non-positive response_timeout falls back to default",
              bt.TransportServer(dispatch, response_timeout=0).response_timeout == 600)

        # An op slower than the configured wait -> the connection thread stops
        # blocking and replies (ok False) instead of hanging on a fixed 600 s. The
        # op keeps running on the worker; the client is just told there's no
        # response yet (this is what a >timeout geoprocessing run now does cleanly).
        slowsrv = bt.TransportServer(dispatch, response_timeout=0.05)
        sport = slowsrv.start()
        try:
            r = bt.send_request(sport, "slow", {"n": 7}, timeout=5)
            check("op slower than response_timeout -> ok False", r.get("ok") is False)
            check("slow op reply is the 'no response' fallback",
                  "no response" in (r.get("error") or ""))
        finally:
            slowsrv.stop()

        # roadmap #4: hardening port.json permissions must never break the write
        import tempfile as _tf
        pd = _tf.mkdtemp()
        pp = bt.write_port_file(pd, port, "tok-perms")
        check("port file still written after permission hardening", os.path.exists(pp))
        _h, _p, _t = bt.read_port_file(pd)
        check("hardened port file still round-trips", _p == port and _t == "tok-perms")

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
