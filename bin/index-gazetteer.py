#!/usr/bin/env python3
"""
index-gazetteer.py - turn the basemap's place labels into a searchable index.

    # with `pmtiles serve <ARCHIVE>/08-maps --port 8081 --cors="*"` running:
    python bin/index-gazetteer.py --build          # walk z0..z8, write the artifact
    python bin/index-gazetteer.py --status         # what is in it
    python bin/index-gazetteer.py --find maracaibo # look one up

WHY THIS EXISTS. The archive holds a whole-planet basemap and a climate model that
answers for any latitude and longitude, and no way to get from a NAME to a pair of
coordinates. `bin/koppen-lookup.py` has been able to answer "what is the climate
here" since 2026-08-30 provided you already knew where "here" was in decimal
degrees. Nothing in 2.2 TB could turn "Maracaibo" into 10.65, -71.64.

There is no gazetteer artifact on the drive and none was acquired. The basemap's
own `places` layer is the source: OSM place labels, already verified, already
checksummed, already in the manifest. This reads them back out.

STANDARD LIBRARY ONLY, deliberately, for the reason in 13-ark-node/README.md.
MVT is protobuf, and decoding the three field types this needs is forty lines -
cheaper than depending on a wheel set that is entirely win_amd64 (spec 11.9).

WHAT IT IS NOT. Not a geocoder: it resolves names to points, it does not parse
addresses, and it has no routing. Not authoritative on disputed names - it stores
every name the tile carries, in every language the tile carries, and says which.
"""

import argparse
import errno
import gzip
import http.client
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index", "gazetteer.sqlite3")
ARCHIVE_NAME = "20260825"          # the basemap, without .pmtiles
DEFAULT_TILES = "http://127.0.0.1:8081"

# ---------------------------------------------------------------------------
# MVT, in as little code as will do the job.
# Tile { repeated Layer layers = 3 }
# Layer { string name = 1; repeated Feature features = 2; repeated string keys = 3;
#         repeated Value values = 4; uint32 extent = 5 }
# Feature { uint64 id = 1; packed uint32 tags = 2; GeomType type = 3;
#           packed uint32 geometry = 4 }
# ---------------------------------------------------------------------------


def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _fields(b):
    i = 0
    n = len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if w == 2:
            ln, i = _varint(b, i)
            yield f, b[i:i + ln]
            i += ln
        elif w == 0:
            v, i = _varint(b, i)
            yield f, v
        elif w == 5:
            yield f, b[i:i + 4]
            i += 4
        elif w == 1:
            yield f, b[i:i + 8]
            i += 8
        else:
            raise ValueError("unknown wire type %d" % w)


def _packed(b):
    out = []
    i = 0
    n = len(b)
    while i < n:
        v, i = _varint(b, i)
        out.append(v)
    return out


def _value(b):
    for f, v in _fields(b):
        if f == 1:
            return v.decode("utf-8", "replace")
        if f in (4, 5):
            return v
        if f == 6:
            return (v >> 1) ^ -(v & 1)
    return None


def _tile_to_lonlat(z, x, y, px, py, extent):
    """Tile-local pixel to WGS84. The y term is the inverse Mercator, which is
    why this is not a linear scale in both axes."""
    n = 2.0 ** z
    lon = (x + px / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n))))
    return lon, lat


def places_in_tile(raw, z, x, y):
    """Every point feature in the `places` layer, as dicts. [] if the tile has
    no such layer, which is most of the planet - it is mostly water."""
    if not raw:
        return []
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = []
    for f, v in _fields(raw):
        if f != 3:
            continue
        name = None
        keys, vals, feats = [], [], []
        extent = 4096
        for lf, lv in _fields(v):
            if lf == 1:
                name = lv.decode("utf-8", "replace")
            elif lf == 2:
                feats.append(lv)
            elif lf == 3:
                keys.append(lv.decode("utf-8", "replace"))
            elif lf == 4:
                vals.append(_value(lv))
            elif lf == 5:
                extent = lv
        if name != "places":
            continue
        for fb in feats:
            tags, geom, gtype = [], [], None
            for ff, fv in _fields(fb):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    gtype = fv
                elif ff == 4:
                    geom = _packed(fv)
            if gtype != 1 or len(geom) < 3:
                continue          # points only; a place with no point is a label hint
            props = {}
            for i in range(0, len(tags) - 1, 2):
                ki, vi = tags[i], tags[i + 1]
                if ki < len(keys) and vi < len(vals):
                    props[keys[ki]] = vals[vi]
            dx = (geom[1] >> 1) ^ -(geom[1] & 1)
            dy = (geom[2] >> 1) ^ -(geom[2] & 1)
            lon, lat = _tile_to_lonlat(z, x, y, dx, dy, extent)
            # A TILE CARRIES A BUFFER, so a label point can sit outside its own
            # tile and come back past the edge of the world. Japan's country
            # label decoded to longitude -220.78, which is 139.22 on the other
            # side of the antimeridian: real data, not junk. 532 rows of the
            # first build were thrown out here. Wrap, do not discard.
            wrapped = not (-180.0 <= lon <= 180.0)
            lon = ((lon + 180.0) % 360.0) - 180.0
            out.append({"props": props, "lat": lat, "lon": lon, "z": z,
                        "wrapped": wrapped})
    return out


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS place (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL,
  lang     TEXT NOT NULL,          -- '' for the default `name`, else the name:xx suffix
  kind     TEXT,
  poprank  INTEGER,
  lat      REAL NOT NULL,
  lon      REAL NOT NULL,
  minzoom  INTEGER NOT NULL,       -- the shallowest zoom this place was seen at
  norm     TEXT NOT NULL           -- casefolded, accent-stripped, for lookup
);
CREATE INDEX IF NOT EXISTS place_norm ON place(norm);
CREATE INDEX IF NOT EXISTS place_lat ON place(lat);
CREATE TABLE IF NOT EXISTS progress (z INTEGER, x INTEGER, y INTEGER,
                                     PRIMARY KEY (z, x, y));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

import unicodedata


# HOW CLOSE IS "THE SAME PLACE". Decoding the same OSM node at two zooms puts
# it within one tile pixel of itself, which is about 5 km at z1 and 30 m at z8,
# so anything above that separates a re-sighting from a different town. Country
# and region labels are polygon anchors that can move much further between
# zooms, so they get a looser threshold; no two countries share a name anyway.
SAME_PLACE_KM = {None: 25.0, "locality": 25.0, "region": 120.0, "country": 400.0}


def _km(lat1, lon1, lat2, lon2):
    """Great-circle distance, and the longitude difference is wrapped so a pair
    either side of the antimeridian is near, not half a planet apart."""
    dlon = math.radians((lon2 - lon1 + 180.0) % 360.0 - 180.0)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


# LETTERS NFD WILL NOT TAKE APART. Stripping combining marks turns Bogotá into
# bogota, and it does nothing at all for ø, ł, đ, æ, ð, þ: those are single
# codepoints with no decomposition, so the stripper walks past them. Measured
# consequence: Tromsø's default name normalised to `tromsø`, an operator typing
# `tromso` got NOT FOUND, and the only reason `--find tromso` returned anything
# was that Turkish and Swedish rows happened to spell it without the slash.
# A gazetteer that can only be searched in an alphabet the operator may not have
# is not much of a gazetteer.
TRANSLIT = {"ø": "o", "œ": "oe", "æ": "ae", "ł": "l", "đ": "d", "ð": "d",
            "þ": "th", "ı": "i", "ŧ": "t", "ħ": "h", "ŀ": "l", "ĸ": "k",
            "ʼ": "", "'": "", "\u2019": ""}

# Bumped whenever the rule above changes. It is written into meta at build time
# and checked on every read: the `norm` column is a CACHE of this function, and
# changing the function without rebuilding leaves an index that looks fine and
# quietly fails to match. Nothing else in the file would have said so.
NORM_VERSION = 2


def normalise(s):
    """Casefold and flatten so 'Bogotá', 'BOGOTA' and 'bogota' are one key, and
    so are 'Tromsø' and 'Tromso'. NOT for display: the original is stored beside
    it, and `label()` is what a human should read."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.casefold()
    s = "".join(TRANSLIT.get(c, c) for c in s)
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# fetching, with ONE connection instead of one per tile.
#
# The first version called urllib.request.urlopen once per tile. Every call
# opens a fresh TCP socket and closes it, and a closed socket sits in TIME_WAIT
# for about two minutes still holding its local port. At roughly 200 tiles a
# second the walk died at 26,000 tiles with WinError 10048, "only one usage of
# each socket address is normally permitted". That is the CLIENT running out of
# ephemeral ports. The tile server was running the whole time, which is why the
# old handler printed a fix that was already true: it treated every URLError as
# "the server is down". A well formed error about the wrong thing.
#
# So: one keep-alive connection per base URL, reused for the whole walk, a
# reconnect on any socket level failure, and a wait and retry when the ports are
# genuinely exhausted, because that condition drains on its own.
# ---------------------------------------------------------------------------

_POOL = {}                       # base -> (HTTPConnection, path prefix)
_EXHAUSTED = {10048, 10055, errno.EADDRINUSE, errno.EADDRNOTAVAIL}


def _open(base, timeout):
    u = urllib.parse.urlsplit(base)
    if u.scheme != "http":
        raise SystemExit("--tiles must be an http:// URL, got %r" % base)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80,
                                      timeout=timeout)
    return conn, u.path.rstrip("/")


def _exhaustion(e):
    return (getattr(e, "winerror", None) in _EXHAUSTED
            or getattr(e, "errno", None) in _EXHAUSTED)


def fetch(base, z, x, y, timeout=10, tries=8):
    delay = 2.0
    last = None
    for _attempt in range(tries):
        conn = None
        try:
            # OPENING THE CONNECTION IS INSIDE THE TRY ON PURPOSE. Windows
            # raises port exhaustion at connect, not at read, so a version of
            # this that opened the socket above the try would retry everything
            # except the failure it was written for. A test that simulated the
            # failure at connect time caught exactly that.
            entry = _POOL.get(base)
            if entry is None:
                entry = _POOL[base] = _open(base, timeout)
            conn, prefix = entry
            path = "%s/%s/%d/%d/%d.mvt" % (prefix, ARCHIVE_NAME, z, x, y)
            conn.request("GET", path, headers={"Connection": "keep-alive"})
            r = conn.getresponse()
            body = r.read()      # read it even when the tile is empty: an unread
                                 # body leaves the connection unusable for the next
            if r.status in (204, 404):
                return b""
            if r.status != 200:
                raise SystemExit("tile server returned HTTP %d for %s"
                                 % (r.status, path))
            return body
        except (http.client.HTTPException, OSError) as e:
            last = e
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            _POOL.pop(base, None)
            if isinstance(e, ConnectionRefusedError):
                raise SystemExit(
                    "nothing is listening at %s.\n"
                    "  start it:  pmtiles serve <ARCHIVE>/08-maps --port 8081"
                    " --cors=\"*\"" % base)
            if _exhaustion(e):
                print("  ephemeral ports exhausted, waiting %.0fs. The tile"
                      " server is fine; this is the client." % delay, flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            # any other socket error: the server closed an idle connection or a
            # read timed out. Retry on a fresh connection before giving up.
    raise SystemExit(
        "gave up on tile z%d/%d/%d after %d attempts: %s\n"
        "  the walk is resumable: rerun the same command and it continues from"
        " the progress table." % (z, x, y, tries, last))


def build(db, base, maxzoom, quiet=False, seconds=0):
    """Walk z0..maxzoom, pruning subtrees whose parent tile does not exist.

    PRUNING WAS SUPPOSED TO BE WHY THIS FINISHES, and on this archive it never
    fires: the basemap carries a tile everywhere, ocean included, so all 87,381
    tiles of z0..z8 exist and get walked. The prune stays because a different
    basemap will be sparse, but the honest number is that this walks the lot."""
    cur = db.cursor()
    done = {(z, x, y) for z, x, y in cur.execute("SELECT z,x,y FROM progress")}

    # WHAT COUNTS AS "ALREADY HAVE IT". The first version keyed on name and
    # language alone, so the planet held exactly ONE Franklin, ONE Cairo, ONE
    # Richmond. Measured on the finished build: `--find franklin` returned
    # Franklin, Nebraska and nothing else, `cairo` returned Cairo, Georgia
    # 10,404 km from Egypt, `richmond` held 24 rows that were 24 languages of
    # the same Richmond. Every other real place with that name had been thrown
    # away as a duplicate, and the lookup gave one confident answer with no hint
    # that it had discarded thirty others.
    #
    # A name is not an identity. A name AT A LOCATION is. The same place seen
    # again at a deeper zoom lands within a tile pixel of itself, so proximity
    # separates "seen this already" from "different town, same name". The
    # threshold has to clear the quantisation, which is worst at z1 at about
    # 5 km, and stay under the distance between two same-named towns.
    seen = {}
    for row in cur.execute("SELECT norm, lang, lat, lon, minzoom FROM place"):
        seen.setdefault((row[0], row[1]), []).append((row[2], row[3]))

    # BREADTH-FIRST, AND THE ORDER IS THE POINT.
    # The first version used a list and popped from the end, so it dove straight
    # to z8 in one corner of the planet: 10,500 tiles walked for 996 names, most
    # of them ocean. Worse, it meant an interrupted build left a deep sliver
    # instead of anything usable. A queue finishes each zoom before starting the
    # next, so stopping early yields COMPLETE coverage down to whatever zoom it
    # reached - and this build gets interrupted, because the harness that runs it
    # kills long jobs.
    from collections import deque
    frontier = deque([(0, 0, 0)])
    t0 = time.time()
    n_tiles = n_empty = n_new = n_wrapped = n_offworld = 0
    while frontier:
        if seconds and (time.time() - t0) > seconds:
            db.commit()
            if not quiet:
                print("  stopping at the %ds limit - rerun to continue" % seconds,
                      flush=True)
            break
        z, x, y = frontier.popleft()
        if z > maxzoom:
            continue
        if (z, x, y) in done:
            # already walked, but its children still need queueing
            for cx, cy in ((2 * x, 2 * y), (2 * x + 1, 2 * y),
                           (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)):
                frontier.append((z + 1, cx, cy))
            continue
        raw = fetch(base, z, x, y)
        n_tiles += 1
        if not raw:
            n_empty += 1
            cur.execute("INSERT OR IGNORE INTO progress VALUES (?,?,?)", (z, x, y))
            continue                      # no tile, no children
        for p in places_in_tile(raw, z, x, y):
            lat, lon = p["lat"], p["lon"]
            if p["wrapped"]:
                n_wrapped += 1
            if abs(lat) > 85.06:
                # past the top or bottom of the projection: a buffered label
                # point with nowhere real to be. Counted, not silently dropped.
                n_offworld += 1
                continue
            props = p["props"]
            kind = props.get("kind")
            poprank = props.get("population_rank")
            near_km = SAME_PLACE_KM.get(kind, SAME_PLACE_KM[None])
            for k, v in props.items():
                if k == "name":
                    lang = ""
                elif k.startswith("name:") and ":" not in k[5:]:
                    lang = k[5:]
                else:
                    continue
                if not isinstance(v, str) or not v.strip():
                    continue
                nm = v.strip()
                key = (normalise(nm), lang)
                bucket = seen.setdefault(key, [])
                if any(_km(lat, lon, a, b) < near_km for a, b in bucket):
                    continue             # this same place, from a shallower zoom
                cur.execute(
                    "INSERT INTO place (name,lang,kind,poprank,lat,lon,minzoom,norm) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (nm, lang, kind, poprank, lat, lon, z, key[0]))
                bucket.append((lat, lon))
                n_new += 1
        cur.execute("INSERT OR IGNORE INTO progress VALUES (?,?,?)", (z, x, y))
        for cx, cy in ((2 * x, 2 * y), (2 * x + 1, 2 * y),
                       (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)):
            frontier.append((z + 1, cx, cy))
        if n_tiles % 2000 == 0:
            db.commit()
            if not quiet:
                print("  %6d tiles (%d empty), %7d names, %.0fs"
                      % (n_tiles, n_empty, n_new, time.time() - t0), flush=True)
    db.commit()
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('maxzoom', ?)", (str(maxzoom),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('built', ?)",
                (time.strftime("%Y-%m-%d"),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)",
                ("08-maps/%s.pmtiles places layer" % ARCHIVE_NAME,))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('norm_version', ?)",
                (str(NORM_VERSION),))
    db.commit()
    if not quiet and (n_wrapped or n_offworld):
        print("  %d label points sat in a neighbouring tile's buffer and were "
              "wrapped across the antimeridian; %d fell outside the projection "
              "and were skipped" % (n_wrapped, n_offworld))
    return n_tiles, n_empty, n_new


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------

def norm_check(db):
    """Is the stored `norm` column the output of the CURRENT normaliser?

    Returns None when it is, or a sentence saying what to do when it is not.
    A stale cache here does not raise and does not look wrong: it simply stops
    matching some names, which is the quietest kind of wrong this build keeps
    finding."""
    row = db.execute("SELECT v FROM meta WHERE k='norm_version'").fetchone()
    have = int(row[0]) if row and str(row[0]).isdigit() else 0
    if have == NORM_VERSION:
        return None
    return ("this index was built with name-normaliser v%d and this program is "
            "v%d, so some names will not match. Rebuild:\n"
            "  python bin/index-gazetteer.py --build --fresh" % (have, NORM_VERSION))


def candidates(db, name, limit=12):
    """Every distinct PLACE that answers to this name, in any language, best
    first. Grouping is by location, not by name: 'Tokyo' is one place with
    forty names, 'Franklin' is forty places with one name, and a lookup that
    cannot tell those apart is the whole defect this replaced.

    Best-first is by population_rank, which is the only prominence signal the
    tiles carry. It is what puts Cairo, Egypt above Cairo, Georgia, and it is
    why the answer must NOT prefer the default `name` field: Tokyo's default
    name is 東京都 and Athens's is Αθήνα, so preferring it hands you the
    American town every time."""
    rows = db.execute(
        "SELECT name, lang, kind, poprank, lat, lon, minzoom FROM place "
        "WHERE norm = ? ORDER BY (poprank IS NULL), poprank DESC, minzoom",
        (normalise(name),)).fetchall()
    out = []
    for nm, lang, kind, pr, lat, lon, mz in rows:
        near = SAME_PLACE_KM.get(kind, SAME_PLACE_KM[None])
        for g in out:
            if _km(lat, lon, g["lat"], g["lon"]) < near:
                g["names"].append((nm, lang))
                break
        else:
            out.append({"name": nm, "lang": lang, "kind": kind, "poprank": pr,
                        "lat": lat, "lon": lon, "minzoom": mz, "names": [(nm, lang)]})
        if len(out) >= limit and len(rows) > 400:
            break
    return out


def names_at(db, lat, lon, km=5.0):
    """The default and English names of the place AT this point, nearest first.

    A cluster is built from rows whose name normalises to the QUERY, so its
    members are only the spellings that happen to match: asking for `cairo`
    finds the Vietnamese and Swedish rows, both spelled Cairo, and not
    القاهرة, which is the same place under a name that does not normalise to
    `cairo`. Printing the first member made the finished index answer
    "Cairo [vi]", a true row and a useless label. So the display name is
    fetched by location.

    NEAREST, and within a few kilometres. The first version of this took
    whichever row the scan reached first inside a generous radius, and
    answered Tokyo with "Setagaya", Athens with "Piraeus", San Jose with
    "Campbell" and Kathmandu with "Bhaktapur": all real neighbours, all inside
    the radius, none of them the place asked for. A label that is confidently
    the wrong town is worse than a language tag."""
    dlat = km / 111.0
    dlon = km / max(1.0, 111.0 * math.cos(math.radians(lat)))
    best = {}
    for nm, lang, la, lo in db.execute(
            "SELECT name, lang, lat, lon FROM place WHERE lat BETWEEN ? AND ? "
            "AND lang IN ('', 'en', 'es')",
            (lat - dlat, lat + dlat)):
        if abs(((lo - lon + 180.0) % 360.0) - 180.0) > dlon:
            continue
        d = _km(lat, lon, la, lo)
        if d <= km and (lang not in best or d < best[lang][0]):
            best[lang] = (d, nm)
    return {k: v[1] for k, v in best.items()}


def label(db, g):
    """What a human should read: the English name, then the local one.

    THE ORDER IS PREFERENCE, NOT WHATEVER SORTED FIRST. Every language row of a
    place carries the same population rank, so which one lands first is
    arbitrary. After the transliteration fix, `--find tromso` printed **Tromso**,
    the TURKISH row, because it now matched the query while the town's own name
    Tromsø did not sort ahead of it. The right point on the map, under a spelling
    that belongs to nobody.

    So: English if the tiles carry one, otherwise the place's own default name,
    and only then whatever row happened to match. The local name follows in
    brackets when it is a different string, which is how Cairo reads as
    Cairo (القاهرة) and Tromsø reads as itself."""
    at = names_at(db, g["lat"], g["lon"])
    local = at.get("")
    en = at.get("en") or local or g["name"]
    if local and local != en:
        return "%s (%s)" % (en, local)
    return en


def primary_name_ok(db, g, shown):
    """Is the name a reader sees FIRST one of this place's own names?

    The bracketed half can be anything; the half before it is what gets read,
    quoted and typed back. It must be the English name or the place's own
    default name, never a third language that happened to match the query.
    Checking it this way needs no copy of label()'s preference order: it asks
    the index which names belong to lang '' or en at that point and requires the
    primary to be one of them. A check that carried its own idea of the right
    answer would only ever agree with itself."""
    primary = shown.split(" (")[0]
    at = names_at(db, g["lat"], g["lon"])
    own = [v for k, v in at.items() if k in ("", "en")]
    return (not own) or primary in own


def show(db, name):
    stale = norm_check(db)
    if stale:
        print("WARNING: " + stale)
    got = candidates(db, name)
    if not got:
        print("not in the gazetteer: %r" % name)
        return 1
    print("%d place%s named %r:" % (len(got), "" if len(got) == 1 else "s", name))
    for g in got:
        print("  %-38s %-9s pop_rank=%-4s %9.4f, %9.4f  (z%d)"
              % (label(db, g), g["kind"] or "",
                 g["poprank"] if g["poprank"] is not None else "-",
                 g["lat"], g["lon"], g["minzoom"]))
    return 0


# ---------------------------------------------------------------------------
# self test
# ---------------------------------------------------------------------------

# COORDINATES FROM OUTSIDE THE ARCHIVE. Every one of these was written down
# from general knowledge, not read out of the gazetteer, which is the only
# reason this is a test and not the index agreeing with itself. Keep it that
# way: never "correct" an entry here to match what the build produced.
#
# Each row is (query, expected lat, expected lon, how far off is acceptable).
# The tolerance is generous because a label anchor is not a city hall.
# A fifth field, where it matters, is a string the printed label MUST contain.
# Distance alone said Tromsø was right while the label read "Tromso", a Turkish
# spelling of a Norwegian town: correct coordinates under a name that belongs to
# nobody. The check meant to catch a wrong label could not see a wrong spelling
# of the right place, because by then both normalised to the same key.
REFERENCE = [
    ("Maracaibo",  10.654,  -71.640, 25),
    ("Pereira",     4.814,  -75.694, 25),
    ("Medellin",    6.244,  -75.581, 25, "Medell\u00edn"),
    ("Nashville",  36.163,  -86.781, 25),
    ("Cali",        3.452,  -76.532, 25),
    ("Bogota",      4.711,  -74.072, 25),
    ("Chicago",    41.878,  -87.630, 25),
    ("Tokyo",      35.690,  139.692, 25, "\u6771\u4eac\u90fd"),
    ("Cairo",      30.044,   31.236, 25, "\u0627\u0644\u0642\u0627\u0647\u0631\u0629"),
    ("Athens",     37.984,   23.728, 25),
    ("Moscow",     55.751,   37.618, 25),
    ("Kathmandu",  27.717,   85.324, 25),
    ("Reykjavik",  64.146,  -21.942, 25),
    ("Ushuaia",   -54.802,  -68.303, 25),
    ("Beijing",    39.906,  116.391, 25),
    ("Lagos",       6.455,    3.394, 40),
    ("Perth",     -31.953,  115.857, 25),
    ("Anchorage",  61.217, -149.900, 25),
    # TYPED WITHOUT THE SLASH, on purpose: the default name is Tromsø, and an
    # operator with an English keyboard cannot produce it.
    ("Tromso",     69.649,   18.955, 25, "Troms\u00f8"),
    ("Malmo",      55.605,   13.001, 25, "Malm\u00f6"),
]
# HOMONYMS: pairs of real, far apart places that share a name. Both members of
# every pair must be in the index. "More than one candidate" was the first
# version of this test and it was too weak to be worth running: the broken index
# passed it for Franklin by holding Franklin, Nebraska twice under two language
# tags. So the test names the actual coordinates it expects to find.
#
# Every pair has one member that was already proven present, and one that is a
# capital or a first-rank city no planet basemap omits. That keeps the test
# about the index's ability to hold two places with one name, and not a guess
# about how deep the basemap's coverage of small towns goes.
HOMONYMS = [
    ("Cairo",    [(30.044,  31.236, "Egypt"),      (30.877, -84.208, "Georgia")]),
    ("Athens",   [(37.984,  23.728, "Greece"),     (33.959, -83.375, "Georgia")]),
    ("Moscow",   [(55.751,  37.618, "Russia"),     (41.337, -75.519, "Pennsylvania")]),
    ("San Jose", [( 9.933, -84.080, "Costa Rica"), (37.335, -121.893, "California")]),
]


def selftest(db):
    fails = 0
    stale = norm_check(db)
    if stale:
        print("STALE INDEX: %s\n" % stale)
        fails += 1
    print("resolution, against coordinates written from outside the archive:")
    for row in REFERENCE:
        q, la, lo, tol = row[:4]
        want = row[4] if len(row) > 4 else None
        got = candidates(db, q)
        if not got:
            print("  %-12s NOT FOUND" % q); fails += 1; continue
        g = got[0]
        d = _km(la, lo, g["lat"], g["lon"])
        shown = label(db, g)
        # THE LABEL IS PART OF THE ANSWER. A lookup that returns the right
        # coordinates under a neighbouring town's name is still wrong to read,
        # and that is exactly what the first display rule did: Tokyo came back
        # as "Setagaya". So the name shown has to contain the name asked for.
        named = normalise(q) in normalise(shown)
        spelt = want is None or want in shown
        own = primary_name_ok(db, g, shown)
        ok = d <= tol and named and spelt and own
        fails += 0 if ok else 1
        why = ("ok" if ok else "WRONG PLACE" if d > tol
               else "WRONG NAME" if not named
               else "MISSPELT, expected %s" % want if not spelt
               else "NOT ITS OWN NAME, %r is another language" % shown.split(" (")[0])
        print("  %-12s %-26s %8.1f km  %s   (%d candidate%s)"
              % (q, shown, d, why, len(got), "" if len(got) == 1 else "s"))
    print("\nhomonyms, two real places to a name, both of which must be held:")
    for q, wanted in HOMONYMS:
        got = candidates(db, q, limit=99)
        miss = []
        for la, lo, where in wanted:      # not `label`: that is a function here
            if not any(_km(la, lo, g["lat"], g["lon"]) <= 40 for g in got):
                miss.append(where)
        fails += 0 if not miss else 1
        print("  %-10s %2d candidates  %s"
              % (q, len(got), "ok" if not miss else "MISSING " + ", ".join(miss)))
    bad = db.execute("SELECT COUNT(*) FROM place WHERE lat IS NULL OR lon IS NULL "
                     "OR lat < -85.06 OR lat > 85.06 OR lon < -180 OR lon > 180"
                     ).fetchone()[0]
    print("\ncoordinates outside the world: %d  %s" % (bad, "ok" if bad == 0 else "BAD"))
    fails += 0 if bad == 0 else 1
    print("\nselftest: %s" % ("PASS" if fails == 0 else "%d FAILURES" % fails))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--find", metavar="NAME")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="discard the existing index and walk from scratch")
    ap.add_argument("--maxzoom", type=int, default=8)
    ap.add_argument("--tiles", default=DEFAULT_TILES)
    ap.add_argument("--db", default=OUT)
    ap.add_argument("--seconds", type=int, default=0,
                    help="stop cleanly after N seconds; progress is saved, rerun to continue")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    # ONLY --build MAKES THE FILE. sqlite3.connect creates what it opens, so on
    # a starter clone `--status` or `--selftest` used to leave an EMPTY
    # gazetteer.sqlite3 behind, which the node then read as a stale one
    # (found 2026-10-04 while choosing what CI runs).
    if not a.build and not os.path.exists(a.db):
        print("no gazetteer at %s - nothing to %s. Build it with the tile server "
              "running:  python bin/index-gazetteer.py --build"
              % (a.db, "test" if a.selftest else "show"))
        return 1
    os.makedirs(os.path.dirname(a.db), exist_ok=True)
    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)

    if a.fresh:
        if not a.build:
            raise SystemExit("--fresh only means anything with --build")
        db.executescript("DELETE FROM place; DELETE FROM progress; DELETE FROM meta;")
        db.commit()
        print("discarded the previous index, walking from z0")

    if a.build:
        t, e, n = build(db, a.tiles, a.maxzoom, a.quiet, a.seconds)
        print("walked %d tiles, %d empty, %d new names" % (t, e, n))

    if a.status or a.build:
        c = db.cursor()
        tot = c.execute("SELECT COUNT(*) FROM place").fetchone()[0]
        distinct = c.execute("SELECT COUNT(DISTINCT norm) FROM place").fetchone()[0]
        langs = c.execute("SELECT COUNT(DISTINCT lang) FROM place").fetchone()[0]
        walked = c.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
        print("gazetteer: %s name rows, %s distinct keys, %d languages, %s tiles walked"
              % ("{:,}".format(tot), "{:,}".format(distinct), langs, "{:,}".format(walked)))
        for kind, n in c.execute(
                "SELECT kind, COUNT(*) FROM place WHERE lang='' "
                "GROUP BY kind ORDER BY 2 DESC LIMIT 8"):
            print("   %-18s %s" % (kind or "(none)", "{:,}".format(n)))

    rc = 0
    if a.find:
        rc |= show(db, a.find)
    if a.selftest:
        rc |= selftest(db)
    return rc


if __name__ == "__main__":
    sys.exit(main())
