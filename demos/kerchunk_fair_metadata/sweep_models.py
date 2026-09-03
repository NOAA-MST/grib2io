#!/usr/bin/env python
"""Generate and validate one kerchunk manifest per model found in COM.

Purpose
-------
NAM and BLEND each exposed a bug in the Reference Generator that GFS never
triggered, because each model structures its GRIB2 differently.  Rather than
discover the rest one at a time, this walks the operational COM tree, picks one
representative file per model, and tries to generate a manifest from it.

Every model is attempted independently: a crash, a hang, or an unreadable result
is recorded and the sweep continues.  The output is a table of what worked, what
failed, and how, plus a JSON report for follow-up.

Usage
-----
    # See what it would attempt, without generating anything
    python sweep_models.py --dry-run

    # Full sweep (expect this to take a while)
    python sweep_models.py --outdir /path/to/scratch

    # Narrow it down
    python sweep_models.py --models nam blend gefs hrrr
    python sweep_models.py --max-size 500 --timeout 600

Notes
-----
Representative file selection is deliberately crude: newest version directory,
newest date directory, then the first GRIB2-looking file under a bounded depth.
That is enough to exercise a model's structure, which is what we are testing.
Use --dry-run first and override anything it picks badly with --file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime

COM_ROOT = "/lfs/h1/ops/prod/com"

# Directories under COM that are not forecast model output.
SKIP_MODELS = {
    "logs", "sms", "ecf", "util", "obsproc", "dcom", "wgrbbul", "wmo",
}

# Subdirectories that hold non-GRIB2 or uninteresting content.
SKIP_DIRS = {
    "gempak", "wmo", "logs", "init", "nwges", "cfssst", "misc", "sfcsig",
    "bufr", "ncdc", "ncf", "restart", "track",
}

GRIB_RE = re.compile(r"(\.grib2$|pgrb|\.grb2$|\.gr2$)", re.IGNORECASE)
DATE_RE = re.compile(r"\.(\d{8})$")


def newest_subdir(path, pattern=None):
    """Return the lexically greatest subdirectory, optionally filtered."""
    try:
        entries = [e for e in os.scandir(path) if e.is_dir()]
    except OSError:
        return None
    if pattern:
        entries = [e for e in entries if pattern.search(e.name)]
    if not entries:
        return None
    return max(entries, key=lambda e: e.name).path


def find_representative(model_dir, max_size_mb, max_depth=4):
    """Find one GRIB2 file under a model directory.

    Walks newest version -> newest dated directory -> first GRIB2 file, avoiding
    the subdirectories that hold non-GRIB2 products.
    """
    # Version directory (v12.3, v5.0, ...), if present.
    version = newest_subdir(model_dir, re.compile(r"^v?\d"))
    base = version or model_dir

    # Dated directory (model.YYYYMMDD).
    dated = newest_subdir(base, DATE_RE)
    if dated is None:
        dated = base

    best = None
    start_depth = dated.rstrip("/").count("/")
    for root, dirs, files in os.walk(dated):
        if root.count("/") - start_depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith((".idx", ".grib2ioidx", ".md5", ".sha256")):
                continue
            if not GRIB_RE.search(name):
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < 1024:
                continue
            if max_size_mb and size > max_size_mb * 1_000_000:
                continue
            return full, size
    return best, 0


def discover(max_size_mb, only=None):
    """Return {model: (path, size)} for every model with a usable file."""
    found = {}
    try:
        models = sorted(e.name for e in os.scandir(COM_ROOT) if e.is_dir())
    except OSError as e:
        print(f"Cannot read {COM_ROOT}: {e}", file=sys.stderr)
        return found

    for model in models:
        if model in SKIP_MODELS:
            continue
        if only and model not in only:
            continue
        path, size = find_representative(os.path.join(COM_ROOT, model), max_size_mb)
        if path:
            found[model] = (path, size)
    return found


def run_one(model, src, outdir, timeout, checker):
    """Generate a manifest for one file and validate it. Returns a result dict."""
    out = os.path.join(outdir, f"{model}.json")
    result = {"model": model, "source": src, "output": out}

    t0 = time.time()
    try:
        proc = subprocess.run(
            ["grib2io", "kerchunk", src, "--output", out],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.update(status="TIMEOUT", detail=f"exceeded {timeout}s",
                      seconds=time.time() - t0)
        return result
    except Exception as e:
        result.update(status="ERROR", detail=f"{type(e).__name__}: {e}",
                      seconds=time.time() - t0)
        return result

    result["seconds"] = round(time.time() - t0, 1)

    if proc.returncode != 0:
        # Pull the most informative line out of the traceback.
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"exit {proc.returncode}"
        result.update(status="GENERATE_FAILED", detail=detail[:300],
                      traceback="\n".join(err[-25:]))
        return result

    if not os.path.exists(out):
        result.update(status="NO_OUTPUT", detail="command succeeded but wrote nothing")
        return result

    result["manifest_mb"] = round(os.path.getsize(out) / 1e6, 2)

    if not checker:
        result.update(status="GENERATED", detail="not validated")
        return result

    try:
        chk = subprocess.run(
            [sys.executable, checker, out], capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.update(status="CHECK_TIMEOUT", detail=f"checker exceeded {timeout}s")
        return result

    out_text = chk.stdout or ""
    fails = [ln.strip() for ln in out_text.splitlines() if ln.startswith("[FAIL]")]
    result["check_output"] = out_text
    if chk.returncode == 0:
        result.update(status="PASS", detail="all checks passed")
    else:
        result.update(status="CHECK_FAILED",
                      detail="; ".join(f[:80] for f in fails[:3]) or "see report")
    return result


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default="./sweep_out",
                    help="where manifests and the report are written")
    ap.add_argument("--models", nargs="*", default=None,
                    help="limit to these model directory names")
    ap.add_argument("--file", nargs=2, action="append", metavar=("MODEL", "PATH"),
                    default=[], help="override the file chosen for a model")
    ap.add_argument("--checker", default=None,
                    help="path to check_manifests.py (skips validation if unset)")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-model timeout in seconds (default 900)")
    ap.add_argument("--max-size", type=int, default=2000,
                    help="skip source files larger than this many MB (default 2000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be attempted and stop")
    args = ap.parse_args()

    only = set(args.models) if args.models else None
    targets = discover(args.max_size, only)
    for model, path in args.file:
        targets[model] = (path, os.path.getsize(path) if os.path.exists(path) else 0)

    if not targets:
        print("No models found. Check COM_ROOT and --models.")
        return 1

    print(f"{len(targets)} model(s) to attempt:\n")
    for model, (path, size) in sorted(targets.items()):
        print(f"  {model:12s} {size/1e6:8.1f} MB  {path}")

    if args.dry_run:
        print("\nDry run — nothing generated. Re-run without --dry-run.")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    checker = args.checker
    if checker and not os.path.exists(checker):
        print(f"\nChecker not found at {checker}; continuing without validation.")
        checker = None

    results = []
    print(f"\n{'=' * 78}\nGenerating\n{'=' * 78}")
    for model, (path, _size) in sorted(targets.items()):
        print(f"\n--- {model}")
        r = run_one(model, path, args.outdir, args.timeout, checker)
        results.append(r)
        print(f"    {r['status']}: {r.get('detail', '')}")

    # ---- summary ----
    print(f"\n{'=' * 78}\nSummary\n{'=' * 78}")
    print(f"{'model':12s} {'status':16s} {'sec':>6s} {'MB':>8s}  detail")
    print("-" * 78)
    for r in results:
        print(f"{r['model']:12s} {r['status']:16s} "
              f"{r.get('seconds', 0):6.1f} {r.get('manifest_mb', 0):8.2f}  "
              f"{r.get('detail', '')[:34]}")

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    report = os.path.join(args.outdir, "sweep_report.json")
    with open(report, "w") as f:
        json.dump({"generated": datetime.now().isoformat(),
                   "com_root": COM_ROOT,
                   "results": results}, f, indent=2)
    print(f"\nFull report (with tracebacks): {report}")

    problems = [r for r in results if r["status"] not in ("PASS", "GENERATED")]
    if problems:
        print(f"\n{len(problems)} model(s) need attention:")
        for r in problems:
            print(f"  {r['model']:12s} {r['status']}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())