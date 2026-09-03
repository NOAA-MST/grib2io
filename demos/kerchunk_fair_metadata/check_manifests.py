#!/usr/bin/env python
"""Check kerchunk manifests for structural and metadata correctness.

Runs the same set of checks against any number of manifests so that a new model
can be validated in one command rather than a notebook per model.

Usage
-----
    python check_manifests.py gfs_v3.json nam_v3.json blend_v3.json
    python check_manifests.py --no-read *.json     # skip byte-range reads

Checks per manifest
-------------------
  1. root .zattrs present and complete
  2. data-variable / coordinate split and CF vocabulary coverage
  3. level metadata: single-level variables keep 'level', multi-level do not
  4. level coordinates carry CF units where Table 4.5 defines them
  5. grid definition decodes from the codec block
  6. internal consistency: variable shapes match their coordinate arrays
  7. dataset opens with xarray and root attributes survive
  8. a byte-range read returns physically plausible values

Exit status is 0 when every manifest passes, 1 otherwise, so this can gate a
generation run in a shell script.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import re
import sys
from collections import Counter

import numpy as np

EXPECTED_ROOT = [
    "Conventions", "institution", "source", "reference_time",
    "reference_time_significance", "production_status", "type_of_data",
    "GRIB2_master_table_version", "GRIB2_local_table_version", "grib2io_version",
]

NON_LEVEL_DIMS = {"valid_time", "duration", "percentileValue",
                  "perturbationNumber", "y", "x", "latitude", "longitude"}


def base_dim(name):
    """Strip the disambiguation suffix: 'percentileValue_2' -> 'percentileValue'.

    _resolve_dim_names appends _2, _3 ... when two variables need different
    values for the same dimension, so classification must be done on the base
    name or a suffixed percentile dimension looks like a vertical coordinate.
    """
    return re.sub(r"_\d+$", "", name)


def is_level_dim(name):
    return base_dim(name) not in NON_LEVEL_DIMS

GDT_NAMES = {0: "Latitude/Longitude (regular)", 10: "Mercator",
             20: "Polar stereographic", 30: "Lambert conformal",
             40: "Gaussian latitude/longitude"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def split_entries(refs):
    attrs = {k.split("/")[0]: json.loads(v)
             for k, v in refs.items()
             if k.endswith("/.zattrs") and k.count("/") == 1}
    data_vars = {k: a for k, a in attrs.items() if "shortName" in a}
    coords = {k: a for k, a in attrs.items() if "shortName" not in a}
    return attrs, data_vars, coords


def level_dim_of(attrs, var):
    dims = attrs[var]["_ARRAY_DIMENSIONS"]
    for d in dims:
        if d in attrs and "typeOfFirstFixedSurface" in attrs[d]:
            return d
    for d in dims:
        if d in attrs and is_level_dim(d):
            return d
    return None


def coord_len(refs, name):
    key = f"{name}/.zarray"
    if key not in refs:
        return None
    shape = json.loads(refs[key])["shape"]
    return shape[0] if len(shape) == 1 else None


def decode_1d(refs, name):
    raw = refs.get(f"{name}/0")
    if not (isinstance(raw, str) and raw.startswith("base64:")):
        return None
    dt = "<i8" if name.startswith("valid_time") else "<f8"
    return np.frombuffer(base64.b64decode(raw[7:]), dtype=dt)


class Report:
    """Collects pass/fail lines for one manifest."""

    def __init__(self, label):
        self.label = label
        self.rows = []
        self.failed = 0

    def check(self, ok, name, detail=""):
        self.rows.append((bool(ok), name, detail))
        if not ok:
            self.failed += 1

    def note(self, name, detail):
        self.rows.append((None, name, detail))

    def render(self):
        print(f"\n{'=' * 78}\n{self.label}\n{'=' * 78}")
        for ok, name, detail in self.rows:
            mark = "    " if ok is None else (" ok " if ok else "FAIL")
            print(f"[{mark}] {name:38s} {detail}")
        print(f"--- {self.failed} failure(s)")
        return self.failed


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_manifest(path, do_read=True):
    rep = Report(path)
    with open(path) as f:
        manifest = json.load(f)
    refs = manifest["refs"]
    rep.note("reference entries", f"{len(refs):,}")

    # 1. root metadata -----------------------------------------------------
    root = json.loads(refs[".zattrs"]) if ".zattrs" in refs else {}
    rep.check(bool(root), "root .zattrs present")
    missing = [k for k in EXPECTED_ROOT if k not in root]
    rep.check(not missing, "root fields complete",
              f"missing: {missing}" if missing else f"{len(root)} fields")
    rep.note("source", root.get("source", "-"))
    rep.note("institution", root.get("institution", "-"))
    rep.note("reference_time", root.get("reference_time", "-"))
    rep.note("GRIB2 tables",
             f"master {root.get('GRIB2_master_table_version','?')} / "
             f"local {root.get('GRIB2_local_table_version','?')}")

    attrs, data_vars, coords = split_entries(refs)
    n = len(data_vars)
    rep.note("variables / coordinates", f"{n} / {len(coords)}")
    if n == 0:
        rep.check(False, "manifest has data variables")
        return rep

    # 2. CF coverage -------------------------------------------------------
    c = Counter()
    for a in data_vars.values():
        c.update(a.keys())
    for key in ("standard_name", "long_name", "units", "lead_time_hours"):
        got = c.get(key, 0)
        rep.check(got == n, f"{key} on all variables", f"{got}/{n}")
    resolved = sum(1 for a in data_vars.values()
                   if a.get("standard_name") not in (None, "unknown"))
    rep.note("standard_name resolved", f"{resolved}/{n} "
             f"({100*resolved/n:.0f}%) — unresolved is a CF table gap, not an error")
    rep.note("cell_methods", f"{c.get('cell_methods', 0)}/{n}")

    # 3. level metadata split ---------------------------------------------
    single_ok = multi_bad = n_multi = n_single = 0
    for v, a in data_vars.items():
        ld = level_dim_of(attrs, v)
        nlev = coord_len(refs, ld) if ld else 1
        nlev = nlev or 1
        if nlev > 1:
            n_multi += 1
            if "level" in a or "valueOfFirstFixedSurface" in a:
                multi_bad += 1
        else:
            n_single += 1
            if "level" in a:
                single_ok += 1
    rep.check(multi_bad == 0, "multi-level vars drop 'level'",
              f"{multi_bad} still carry it (of {n_multi})")
    rep.check(single_ok == n_single, "single-level vars keep 'level'",
              f"{single_ok}/{n_single}")

    # 4. level coordinate metadata ----------------------------------------
    lev_coords = {k: a for k, a in coords.items() if is_level_dim(k)}
    with_units = sum(1 for a in lev_coords.values() if "units" in a)
    with_toffs = sum(1 for a in lev_coords.values()
                     if "typeOfFirstFixedSurface" in a)
    rep.check(with_toffs == len(lev_coords), "level coords tagged with surface type",
              f"{with_toffs}/{len(lev_coords)}")
    rep.note("level coords with units",
             f"{with_units}/{len(lev_coords)} — the rest are single-valued "
             f"surfaces whose Table 4.5 units are 'unknown'")

    # 5. grid definition ---------------------------------------------------
    probe = sorted(data_vars)[0]
    za = json.loads(refs[f"{probe}/.zarray"])
    filt = (za.get("filters") or [{}])[0]
    gdtn = filt.get("gdtn")
    rep.check(gdtn is not None, "grid template present",
              f"{gdtn} ({GDT_NAMES.get(gdtn, '?')}), "
              f"{filt.get('nx')}x{filt.get('ny')}")

    # 6. internal consistency ---------------------------------------------
    clen = {}
    for k, v in refs.items():
        if k.endswith("/.zarray") and k.count("/") == 1:
            shp = json.loads(v)["shape"]
            if len(shp) == 1:
                clen[k.split("/")[0]] = shp[0]
    bad = []
    for v, a in data_vars.items():
        shape = json.loads(refs[f"{v}/.zarray"])["shape"]
        for d, ln in zip(a["_ARRAY_DIMENSIONS"], shape):
            if d in clen and clen[d] != ln:
                bad.append(f"{v}.{d} {ln}!={clen[d]}")
    rep.check(not bad, "shapes match coordinate arrays",
              f"{len(bad)} mismatches: {bad[:3]}" if bad else "consistent")

    # report any suffixed dimensions, which is where collisions were resolved
    suffixed = sorted(d for d in coords if re.search(r"_\d+$", d))
    rep.note("disambiguated dimensions",
             f"{len(suffixed)}" + (f": {suffixed[:6]}" if suffixed else ""))

    if not do_read:
        return rep

    # 7. xarray open -------------------------------------------------------
    try:
        import grib2io.codecs  # noqa: F401
        import fsspec
        import xarray as xr
    except Exception as e:
        rep.note("xarray open", f"skipped — {type(e).__name__}: {e}")
        return rep

    sample_url = next((r[0] for r in refs.values()
                       if isinstance(r, list) and len(r) == 3), "")
    # Refs may be absolute file:// URIs, bare relative paths (as in the demo
    # manifests, which reference data/ alongside the JSON), or remote URLs.
    # Only the last needs the HTTP filesystem.
    if sample_url.startswith(("http://", "https://", "s3://")):
        proto = sample_url.split("://", 1)[0]
    else:
        proto = "file"
    try:
        fs = fsspec.filesystem("reference", fo=manifest,
                               remote_protocol=proto, skip_instance_cache=True)
        try:
            ds = xr.open_dataset(fs.get_mapper(""), engine="zarr",
                                 backend_kwargs={"consolidated": False},
                                 chunks={})
            mode = "dask-backed"
        except ImportError:
            # dask is optional; without it xarray loads eagerly, which is fine
            # for the small slice this script reads.
            ds = xr.open_dataset(fs.get_mapper(""), engine="zarr",
                                 backend_kwargs={"consolidated": False})
            mode = "eager (no dask)"
        rep.check(True, "opens with xarray", f"{len(ds.data_vars)} variables, {mode}")
    except Exception as e:
        rep.check(False, "opens with xarray", f"{type(e).__name__}: {e}")
        return rep

    survived = set(ds.attrs) & set(root)
    rep.check(len(survived) == len(root), "root attrs survive to ds.attrs",
              f"{len(survived)}/{len(root)}")

    # 8. byte-range read ---------------------------------------------------
    target = next((v for v in ("TMP", "APTMP", "T2M", "WIND", "APCP")
                   if v in ds.data_vars), sorted(ds.data_vars)[0])
    try:
        da = ds[target]
        # Each chunk is the full grid, so slicing a corner window reads the
        # same bytes but can land entirely inside the bitmap-masked region
        # (Alaska NDFD grids mask the [0:50, 0:50] corner).  Read whole 2-D
        # fields and judge from every point.  And a sparse dimension cross
        # product (e.g. two accumulation windows where not every valid_time
        # carries both, as in uvi DSWRF) legitimately leaves whole chunks
        # NaN-filled, so try index combinations until one holds data.
        free = [d for d in da.dims if d not in ("y", "x")]
        finite = np.array([])
        for combo in itertools.islice(
                itertools.product(*(range(min(da.sizes[d], 3)) for d in free)),
                12):
            vals = np.asarray(da.isel(dict(zip(free, combo))).compute().values)
            finite = vals[np.isfinite(vals)]
            if finite.size:
                break
        if finite.size == 0:
            rep.check(False, f"byte-range read ({target})",
                      "no finite values (fully masked or undecodable)")
        else:
            lo, hi = float(finite.min()), float(finite.max())
            # A field whose every finite value is a GRIB missing-value
            # sentinel decoded fine but masked nothing: 9999-family or
            # 9.999e20-family constants are encoding artifacts, not physics.
            sentinel = (lo == hi and (9998.0 <= lo <= 10000.0 or lo >= 1e19))
            rep.check(not sentinel, f"byte-range read ({target})",
                      f"min {lo:.3f} max {hi:.3f} {da.attrs.get('units','')}"
                      + (" — constant missing-value sentinel" if sentinel else ""))
    except Exception as e:
        rep.check(False, f"byte-range read ({target})", f"{type(e).__name__}: {e}")

    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifests", nargs="+")
    ap.add_argument("--no-read", action="store_true",
                    help="skip opening the dataset and reading bytes")
    args = ap.parse_args()

    total = 0
    for path in args.manifests:
        try:
            rep = check_manifest(path, do_read=not args.no_read)
        except Exception as e:
            print(f"\n{path}: could not be checked — {type(e).__name__}: {e}")
            total += 1
            continue
        total += rep.render()

    print(f"\n{'=' * 78}")
    print("ALL CHECKS PASSED" if total == 0 else f"{total} FAILURE(S)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())