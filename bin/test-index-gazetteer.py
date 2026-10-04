#!/usr/bin/env python3
"""test-index-gazetteer.py - offline tests for bin/index-gazetteer.py.

    python bin/test-index-gazetteer.py

Needs no tile server and no network: it starts its own HTTP server on loopback
and serves tiles it encodes itself, so the expected answers are known rather
than read back out of the thing under test.

WHY IT EXISTS. Two defects shipped in the gazetteer's first build and neither
was visible in its output, which was green and detailed and wrong:

  1. fetch() opened a TCP connection per tile. The walk died at 26,000 tiles
     with WinError 10048, the client having exhausted its ephemeral ports, and
     the error handler blamed the tile server, which was running.
  2. The index kept ONE place per name per language, planet-wide. `--find
     cairo` answered Cairo, Georgia, 10,404 km from Egypt, with no sign that
     it had discarded every other Cairo.

Each test below fails on the code as it stood when the defect was live. That is
the only reason to trust one.
"""

import http.server
import importlib.util
import math
import os
import socket
import socketserver
import sqlite3
import sys
import threading
import errno

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "index-gazetteer.py")
_spec = importlib.util.spec_from_file_location("gz", TARGET)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

FAILS = []


def check(label, ok, detail=""):
    print("  %-58s %s%s" % (label, "ok" if ok else "FAIL", "  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# a minimal MVT encoder, so the tiles under test have known contents
# ---------------------------------------------------------------------------

def _vi(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _zz(n):
    return (n << 1) ^ (n >> 31)


def _f(num, wt):
    return _vi((num << 3) | wt)


def _L(num, payload):
    return _f(num, 2) + _vi(len(payload)) + payload


def mvt(points, extent=4096):
    """points: [(px, py, {prop: str|int})] -> one tile with a `places` layer."""
    keys, vals, feats = [], [], b""
    for px, py, props in points:
        tags = []
        for k, v in props.items():
            if k not in keys:
                keys.append(k)
            if v not in vals:
                vals.append(v)
            tags += [keys.index(k), vals.index(v)]
        body = _L(2, b"".join(_vi(t) for t in tags))
        body += _f(3, 0) + _vi(1)                                   # POINT
        body += _L(4, _vi(9) + _vi(_zz(px)) + _vi(_zz(py)))         # MoveTo
        feats += _L(2, body)
    layer = _L(1, b"places") + feats
    for k in keys:
        layer += _L(3, k.encode())
    for v in vals:
        layer += _L(4, _L(1, v.encode()) if isinstance(v, str) else _f(5, 0) + _vi(v))
    layer += _f(5, 0) + _vi(extent)
    return _L(3, layer)


def lonlat_to_px(z, x, y, lon, lat, extent=4096):
    n = 2.0 ** z
    fx = (lon + 180.0) / 360.0 * n - x
    sy = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    fy = (1 - sy / math.pi) / 2 * n - y
    return round(fx * extent), round(fy * extent)


class Serv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    connections = 0

    def get_request(self):
        r = super().get_request()
        type(self).connections += 1
        return r


def start(handler):
    s = Serv(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, "http://127.0.0.1:%d" % s.server_address[1]


# ---------------------------------------------------------------------------
# 1. one connection for the whole walk
# ---------------------------------------------------------------------------

class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    wbufsize = 65536
    close_after = 0
    served = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.served += 1
        body = b"tile:" + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if cls.close_after and cls.served % cls.close_after == 0:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)


def test_connections():
    print("\nfetch: sockets")
    Serv.connections = 0
    srv, base = start(Echo)
    n = 2000
    for i in range(n):
        assert g.fetch(base, 5, i % 64, i // 64).startswith(b"tile:")
    check("%d tiles open one TCP connection" % n, Serv.connections == 1,
          "opened %d" % Serv.connections)

    Echo.close_after = 50
    before = Serv.connections
    for i in range(300):
        assert g.fetch(base, 6, i, 0).startswith(b"tile:")
    check("300 tiles survive a server closing every 50th connection", True,
          "%d reconnects" % (Serv.connections - before))
    Echo.close_after = 0
    srv.shutdown()


def test_port_exhaustion():
    print("\nfetch: failure modes")
    srv, base = start(Echo)
    real, state = g._open, {"n": 0}

    def flaky(b, t):
        state["n"] += 1
        if state["n"] <= 2:
            e = OSError(errno.EADDRINUSE, "Only one usage of each socket address")
            e.winerror = 10048
            raise e
        return real(b, t)

    g._open, g._POOL = flaky, {}
    sleep, g.time.sleep = g.time.sleep, lambda s: None
    try:
        body = g.fetch(base, 7, 1, 1)
        check("two port-exhaustion failures are waited out, not fatal",
              body.startswith(b"tile:") and state["n"] == 3)
    except SystemExit as e:
        check("two port-exhaustion failures are waited out, not fatal", False, str(e))
    g._open, g.time.sleep, g._POOL = real, sleep, {}

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    try:
        g.fetch("http://127.0.0.1:%d" % dead, 0, 0, 0)
        check("a dead port is reported as nothing listening", False, "no error raised")
    except SystemExit as e:
        check("a dead port is reported as nothing listening",
              "nothing is listening" in str(e))
    srv.shutdown()


# ---------------------------------------------------------------------------
# 2. a name is not an identity
# ---------------------------------------------------------------------------

TOWNS = [(39.799, -89.644), (37.208, -93.293), (42.101, -72.590)]   # 3 Springfields


class Towns(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    wbufsize = 65536

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.strip("/").split("/")
        z, x, y = int(p[1]), int(p[2]), int(p[3].split(".")[0])
        pts = []
        for lat, lon in TOWNS:
            px, py = lonlat_to_px(z, x, y, lon, lat)
            if 0 <= px < 4096 and 0 <= py < 4096:
                pts.append((px, py, {"name": "Springfield", "kind": "locality",
                                     "population_rank": 8}))
        body = mvt(pts) if pts else b""
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def test_identity(tmp):
    print("\nindex: identity")
    srv, base = start(Towns)
    db = sqlite3.connect(tmp)
    db.executescript(g.SCHEMA)
    g.build(db, base, 6, quiet=True)
    rows = db.execute("SELECT lat, lon FROM place").fetchall()
    check("three towns of one name are three rows, not one", len(rows) == 3,
          "%d rows" % len(rows))
    worst = max(min(g._km(la, lo, r[0], r[1]) for r in rows) for la, lo in TOWNS)
    check("each town is within 5 km of a stored row", worst < 5, "%.2f km" % worst)
    check("candidates() groups by location, not by name",
          len(g.candidates(db, "springfield", limit=99)) == 3)
    _, _, again = g.build(db, base, 6, quiet=True)
    check("a rerun over a finished index adds nothing", again == 0, "%d added" % again)
    srv.shutdown()
    db.close()


# ---------------------------------------------------------------------------
# 3. the world does not end at the tile edge
# ---------------------------------------------------------------------------

def test_wrap():
    print("\nindex: projection")
    raw = mvt([(-2600, 1500, {"name": "Japan"})])       # a label in the buffer
    p = g.places_in_tile(raw, 1, 0, 0)[0]
    check("a buffered label point wraps into the world", -180 <= p["lon"] <= 180
          and p["wrapped"], "lon %.3f" % p["lon"])
    q = g.places_in_tile(mvt([(2000, 1500, {"name": "Anywhere"})]), 1, 0, 0)[0]
    check("an ordinary point is left alone", not q["wrapped"])
    check("distance across the antimeridian is short",
          g._km(0, 179.9, 0, -179.9) < 30, "%.1f km" % g._km(0, 179.9, 0, -179.9))


# ---------------------------------------------------------------------------
# 4. names an English keyboard can actually type
# ---------------------------------------------------------------------------

def test_normalise():
    print("\nindex: names")
    pairs = [("Tromsø", "Tromso"), ("Malmö", "Malmo"), ("Bogotá", "bogota"),
             ("Łódź", "Lodz"), ("Đà Nẵng", "Da Nang"), ("Reykjavík", "reykjavik"),
             ("Ålesund", "Alesund"), ("Þingvellir", "Thingvellir")]
    for a, b in pairs:
        check("%s and %s are one key" % (a, b), g.normalise(a) == g.normalise(b),
              "%r vs %r" % (g.normalise(a), g.normalise(b)))
    check("two different places stay different",
          g.normalise("Huma\u0144") != g.normalise("Humus"))


def test_stale_guard(tmp):
    print("\nindex: stale cache")
    db = sqlite3.connect(tmp)
    db.executescript(g.SCHEMA)
    check("an index with no recorded normaliser is reported stale",
          g.norm_check(db) is not None)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('norm_version', ?)",
               (str(g.NORM_VERSION),))
    check("one built by this program is not", g.norm_check(db) is None)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('norm_version', '1')")
    msg = g.norm_check(db)
    check("an older one names the rebuild command",
          msg is not None and "--fresh" in msg)
    db.close()


def main():
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "gz-test.sqlite3")
    if os.path.exists(tmp):
        os.remove(tmp)
    test_connections()
    test_port_exhaustion()
    test_identity(tmp)
    test_wrap()
    test_normalise()
    os.remove(tmp)
    test_stale_guard(tmp)
    try:
        os.remove(tmp)
    except OSError:
        pass
    print("\n%s" % ("all tests pass" if not FAILS
                    else "%d FAILURES: %s" % (len(FAILS), "; ".join(FAILS))))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
