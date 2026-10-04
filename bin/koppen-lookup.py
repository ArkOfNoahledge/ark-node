#!/usr/bin/env python3
"""
koppen-lookup.py - what climate is this place, and has it moved?

Reads the Koppen-Geiger rasters in 08-maps/koppen-geiger/ and answers for any
latitude and longitude, entirely offline.

    python bin/koppen-lookup.py                       # the default set of places
    python bin/koppen-lookup.py 10.4806 -66.9036      # one point
    python bin/koppen-lookup.py 4.711 -74.0721 Bogota

WHY THIS EXISTS. On 2026-08-30 the archive held 847GB of map data and could not
read it: numpy, scipy and Pillow were vendored, nothing else was, and Pillow
ignores a GeoTIFF's georeferencing tags - so there was no way to turn a latitude
and longitude into a pixel. This script is the acceptance test for the geospatial
toolchain (spec 12.9), and it is deliberately a QUESTION rather than an import
check. `import rasterio` proves a package exists. This proves the node can use
the data it holds, which is the thing that was actually missing.

It also answers something no single map can: the same place is looked up in all
four observed periods, so the output shows whether the classification at that
spot has SHIFTED between 1901 and 2020. That comparison is the reason the whole
archive was taken rather than only the present-day layer.

TWO THINGS IT REFUSES TO PRETEND ABOUT.

A point that wanders and returns is not a point that moved, so those are reported
separately and the full sequence is printed rather than the endpoints. The first
version showed Bogota as "Csb -> Csb" under a "changed" heading, which is a
change to nowhere.

And a 1 km pixel in mountains can span a thousand metres of elevation. Merida sits
in an Andean valley at 1,600 m with peaks above 4,000 m a few kilometres away, and
a single sample there is close to meaningless. So the eight neighbouring pixels are
sampled too. The flag fires when the neighbourhood crosses a MAIN CLIMATE GROUP -
the first letter, A tropical through E polar - not when it merely holds several
sub-classes. Cfb beside Csb is two flavours of temperate; Aw beside Cfb is a
boundary running through the pixel. A confident wrong answer about what grows
somewhere is worse than an admitted uncertainty.

AND A THIRD THING, added 2026-08-31 when the netCDF companion arrived. The .nc
files hold the SAME classification as the .tif - verified equal at three points -
plus kg_confidence, the percentage of the constrained CMIP6 ensemble that agreed
on the class at that pixel. That is NOT the same question as the neighbourhood
check, and collapsing the two would hide the thing that matters most in mountains:

    Merida    confidence 100%, neighbourhood crosses A -> C
    Bogota    confidence  50%, neighbourhood stays inside group C

Merida is a pixel the models are certain about TODAY, sitting on a boundary. Bogota
is a pixel in uniform terrain that the models themselves could not agree on.
Different failures, different remedies - move the sample point for the first,
distrust the class for the second. Both are printed, separately, and never merged.

Confidence also qualifies the century comparison, which is the reason the whole
archive was taken. Merida's agreement runs 50 -> 50 -> 66 -> 100 across the four
periods, so its apparent Cwb -> Aw shift has one end the ensemble was sure about
and one end it was not. Any reported shift now carries both endpoint agreements,
and a weak endpoint downgrades the claim from "moved" to "unresolved".

Requires: rasterio (bundles GDAL). netCDF4 is OPTIONAL - without it the tool
degrades to the GeoTIFF answers and says so. No system libraries, no network.
"""

import sys, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KOPPEN = os.path.join(ROOT, "08-maps", "koppen-geiger")
RASTER = "koppen_geiger_0p00833333.tif"          # ~1 km
NCRASTER = "koppen_geiger_0p00833333.nc"         # same grid, plus confidence
PERIODS = ["1901_1930", "1931_1960", "1961_1990", "1991_2020"]

DEFAULTS = [
    (10.4806, -66.9036, "Caracas, VE"),
    ( 8.5897, -71.1561, "Merida, VE (Andes)"),
    (10.6427, -71.6125, "Maracaibo, VE"),
    ( 4.7110, -74.0721, "Bogota, CO"),
    (10.3910, -75.4794, "Cartagena, CO"),
]


NODATA = "water/none"   # the rasters use a class value absent from legend.txt for
                        # ocean and large lakes. Naming it beats printing "?".


def load_legend():
    """Class number -> (code, description). The legend ships with the data."""
    out = {}
    path = os.path.join(KOPPEN, "legend.txt")
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*(\d+):\s+(\S+)\s+(.*?)\s*\[", line)
        if m:
            out[int(m.group(1))] = (m.group(2), m.group(3).strip())
    return out


class NcPeriod(object):
    """One period's netCDF companion, coordinate axes cached.

    The axes are 43,200 and 21,600 float64 values. Reading them once per period
    rather than once per point is the difference between a fast tool and one that
    re-reads 350 kB for every lookup.
    """

    def __init__(self, ds):
        self.ds = ds
        self.lat = ds.variables["lat"][:]
        self.lon = ds.variables["lon"][:]
        self.cls = ds.variables["kg_class"]
        self.conf = ds.variables["kg_confidence"]

    def sample(self, lat, lon):
        """(class number, confidence percent) at the nearest cell."""
        import numpy as np
        i = int(np.abs(self.lat - lat).argmin())
        j = int(np.abs(self.lon - lon).argmin())
        return int(self.cls[i, j]), int(self.conf[i, j])

    def close(self):
        self.ds.close()


def open_nc(period):
    """The netCDF companion for one period, or None if it cannot be opened.

    Every failure path returns None on purpose. A missing netCDF4, a missing file
    and a corrupt file all mean the same thing to the caller - answer from the
    GeoTIFFs and say the confidence layer is unavailable. Graceful degradation is
    a stated priority of this build; a lookup tool that refuses to answer because
    an optional enrichment is absent would violate it.
    """
    try:
        import netCDF4
    except ImportError:
        return None
    path = os.path.join(KOPPEN, period, NCRASTER)
    if not os.path.exists(path):
        return None
    try:
        return NcPeriod(netCDF4.Dataset(path))
    except Exception:
        return None


def main():
    try:
        import rasterio
    except ImportError:
        sys.exit("rasterio is not installed. This is the toolchain gap of spec 12.9.\n"
                 "  pip install --no-index --find-links 09-software/python-wheels/ rasterio")

    legend = load_legend()
    if not legend:
        sys.exit("could not parse " + os.path.join(KOPPEN, "legend.txt"))

    args = sys.argv[1:]
    if args:
        lat, lon = float(args[0]), float(args[1])
        label = " ".join(args[2:]) or "%.4f, %.4f" % (lat, lon)
        points = [(lat, lon, label)]
    else:
        points = DEFAULTS

    srcs = {}
    for p in PERIODS:
        f = os.path.join(KOPPEN, p, RASTER)
        if os.path.exists(f):
            srcs[p] = rasterio.open(f)
    if not srcs:
        sys.exit("no rasters found under " + KOPPEN)

    ncs = {}
    for p in PERIODS:
        n = open_nc(p)
        if n is not None:
            ncs[p] = n

    any_src = next(iter(srcs.values()))
    print("Koppen-Geiger  %s  (%d x %d, %s)" % (RASTER, any_src.width, any_src.height, any_src.crs))
    print("GDAL %s, bundled in the rasterio wheel. No network used.\n" % rasterio.__gdal_version__)

    w = max(len(lbl) for _, _, lbl in points)
    print("place".ljust(w) + "  " + "  ".join(p.replace("_", "-") for p in PERIODS))
    print("-" * (w + 2 + len(PERIODS) * 11))

    for lat, lon, label in points:
        cells = []
        for p in PERIODS:
            src = srcs.get(p)
            if src is None:
                cells.append("--".ljust(9))
                continue
            v = int(next(src.sample([(lon, lat)]))[0])       # rasterio takes (x, y)
            cells.append(legend.get(v, (NODATA, ""))[0].ljust(9))
        print(label.ljust(w) + "  " + "  ".join(cells))

    print()
    for lat, lon, label in points:
        src = srcs.get("1991_2020") or any_src
        v = int(next(src.sample([(lon, lat)]))[0])
        code, desc = legend.get(v, (NODATA, "no data - ocean or large lake"))
        print("  %s: %s - %s" % (label, code, desc))

    # A point that wanders and returns is NOT the same as one that moved. The
    # first version of this script reported both as "changed" and printed only
    # the endpoints, so Bogota appeared as "Csb -> Csb" - a change to nowhere.
    #
    # AND A SHIFT IS ONLY AS STRONG AS ITS ENDPOINTS. Added 2026-08-31, when the
    # confidence layer showed Merida's century "shift" running from a 50%-agreement
    # start to a 100%-agreement end. Reporting that as a movement, with no mention
    # that half the ensemble disagreed about where it started, is the same overclaim
    # this script has already been corrected for twice. WEAK is set below 75% -
    # below three-quarters agreement a majority-vote class is closer to a coin flip
    # between two candidates than a determination. The threshold is a judgement and
    # is stated here so it can be argued with rather than discovered in the code.
    WEAK = 75

    def endpoint_conf(lat, lon, first, last):
        """(confidence at first period, at last period), or (None, None)."""
        a = ncs.get(first)
        b = ncs.get(last)
        if a is None or b is None:
            return None, None
        return a.sample(lat, lon)[1], b.sample(lat, lon)[1]

    shifted, wandered = [], []
    for lat, lon, label in points:
        have = [p for p in PERIODS if p in srcs]
        seq = [int(next(srcs[p].sample([(lon, lat)]))[0]) for p in have]
        codes = [legend.get(v, (NODATA,))[0] for v in seq]
        moved = seq[0] != seq[-1]
        varied = len(set(seq)) > 1
        if not (moved or varied):
            continue
        c0, c1 = endpoint_conf(lat, lon, have[0], have[-1])
        if c0 is None:
            note = ""
        elif min(c0, c1) < WEAK:
            # THE CAVEAT DIFFERS BY BUCKET, and the first version of this used one
            # sentence for both - so Bogota, filed under "varied but returned",
            # was told not to be read "as movement" when nothing had claimed it
            # was. A caveat aimed at a claim the tool is not making reads as
            # noise, and teaches the reader to skip the ones that matter.
            why = ("Treat as unresolved, not as movement." if moved else
                   "As likely ensemble disagreement as a real excursion.")
            note = "   [agreement %d%% -> %d%%  <-- WEAK ENDPOINT. %s]" % (c0, c1, why)
        else:
            note = "   [agreement %d%% -> %d%%]" % (c0, c1)
        line = "%s: %s%s" % (label, " -> ".join(codes), note)
        (shifted if moved else wandered).append(line)

    print()
    if shifted:
        print("ENDPOINTS DIFFER, 1901-1930 vs 1991-2020:")
        for c in shifted:
            print("   ", c)
    if wandered:
        print("VARIED BUT RETURNED (endpoints identical):")
        for c in wandered:
            print("   ", c)
    if not shifted and not wandered:
        print("Stable across all four periods at these points.")

    # CONFIDENCE, from the netCDF companion. This is a DIFFERENT question from the
    # neighbourhood check below and the two must not be merged - see the docstring.
    print()
    if not ncs:
        print("Confidence layer unavailable (netCDF4 missing, or no .nc files under")
        print("08-maps/koppen-geiger/). Classes above are still correct; the ensemble")
        print("agreement behind each one is simply not being shown.")
    else:
        print("Ensemble agreement, % (kg_confidence, netCDF companion):")
        print("place".ljust(w) + "  " + "  ".join(p.replace("_", "-") for p in PERIODS))
        print("-" * (w + 2 + len(PERIODS) * 11))
        disagree = []
        for lat, lon, label in points:
            cells = []
            for p in PERIODS:
                n = ncs.get(p)
                if n is None:
                    cells.append("--".ljust(9))
                    continue
                ncls, conf = n.sample(lat, lon)
                cells.append(("%d%%" % conf).ljust(9))
                # CROSS-CONTAINER CHECK. The .tif and the .nc are two independent
                # encodings of one claim. If they ever disagree, one of the two
                # files is wrong and the manifest cannot tell which - so say so
                # loudly rather than silently trusting whichever was read last.
                src = srcs.get(p)
                if src is not None:
                    tcls = int(next(src.sample([(lon, lat)]))[0])
                    if tcls != ncls:
                        disagree.append("%s %s: tif=%d nc=%d" % (label, p, tcls, ncls))
            print(label.ljust(w) + "  " + "  ".join(cells))
        print()
        if disagree:
            print("*** GeoTIFF AND netCDF DISAGREE. One of these files is wrong: ***")
            for d in disagree:
                print("   ", d)
        else:
            print("GeoTIFF and netCDF agree on every class above - two independent")
            print("encodings of the same claim, checked rather than assumed.")

    # TERRAIN CAVEAT. A 1 km pixel in mountains can span a thousand metres of
    # elevation, so a single sample there is close to meaningless - the pixel
    # next door may say something else entirely. Sampling the eight neighbours
    # makes that visible instead of letting one confident-looking class hide it.
    print()
    print("Neighbourhood check, 1991-2020 (this point plus 8 neighbours at ~1 km):")
    src = srcs.get("1991_2020") or any_src
    step = src.res[0]
    for lat, lon, label in points:
        ring = []
        for dy in (-step, 0.0, step):
            for dx in (-step, 0.0, step):
                v = int(next(src.sample([(lon + dx, lat + dy)]))[0])
                ring.append(legend.get(v, (NODATA,))[0])
        uniq = sorted(set(ring))
        # COUNTING CLASSES IS THE WRONG TEST, and the first version used it.
        # Cfb next to Csb is two flavours of temperate and unremarkable. Aw next
        # to Cfb crosses from tropical to temperate INSIDE ONE KILOMETRE, which is
        # a boundary running through the pixel. What matters is the main climate
        # group - the first letter: A tropical, B arid, C temperate, D cold,
        # E polar - not how many sub-classes happen to appear.
        groups = set(c[0] for c in uniq if c != NODATA)
        if len(groups) > 1:
            flag = "  <-- BOUNDARY IN THIS PIXEL. A point sample here is not trustworthy; sample a wider area."
        elif NODATA in uniq and len(uniq) > 1:
            flag = "  (coast or inland water nearby)"
        else:
            flag = ""
        print("  %s %s%s" % (label.ljust(w), "/".join(uniq), flag))

    print()
    print("A class is only as good as the terrain it sits in. Where the neighbourhood")
    print("crosses a climate group, a century-scale 'shift' at that point is more")
    print("likely the boundary moving across one pixel than the place changing.")
    if ncs:
        print()
        print("Read the two together. HIGH confidence on a crossing neighbourhood means")
        print("the models are sure about this pixel and the pixel next door is a")
        print("different world - move the sample point. LOW confidence on a uniform")
        print("neighbourhood means the terrain is not the problem; the classification")
        print("itself is uncertain - distrust the class, not the location.")

    for src in srcs.values():
        src.close()
    for n in ncs.values():
        n.close()


if __name__ == "__main__":
    main()
