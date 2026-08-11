# FAIR metadata in grib2io Kerchunk reference manifests

This directory documents and demonstrates the metadata enrichment added to
grib2io's Kerchunk reference generator (`grib2io.kerchunk.ReferenceGenerator`).
The goal is to make the generated JSON reference manifests **self-describing**
and substantially more **FAIR** — Findable, Accessible, Interoperable, Reusable —
at essentially zero additional generation cost, because every field added here is
already decoded by grib2io from the GRIB2 message in hand.

- [Motivation](#motivation)
- [What changed](#what-changed)
  - [New: root-level dataset metadata](#new-root-level-dataset-metadata)
  - [Enriched: per-variable metadata](#enriched-per-variable-metadata)
- [Field reference](#field-reference)
- [How to generate a manifest](#how-to-generate-a-manifest)
- [How to open a manifest](#how-to-open-a-manifest)
- [Example files in this directory](#example-files-in-this-directory)

---

## Motivation

A Kerchunk manifest lets tools read the original GRIB2 bytes lazily as a virtual
Zarr store (via `fsspec` + xarray) without rewriting the data. The manifests
grib2io produced were correct, but they carried only a thin slice of the metadata
grib2io had already decoded. Assessed against the FAIR principles they fell short
on two axes:

- **Findable** — the manifest carried *no* dataset-level metadata. A reference
  file separated from its filename and directory path was unidentifiable: nothing
  inside the JSON recorded which model produced the data, which center published
  it, what cycle it belonged to, or what conventions it followed. Organizations
  publishing manifests (e.g., NOMADS) could not catalog or index them from their
  contents alone.
- **Interoperable** — variables carried GRIB2-native codes but none of the CF
  vocabulary (`standard_name`, `cell_methods`, `long_name`) that downstream
  Zarr/xarray tooling keys on.

GRIB2 Sections 0/1/4 carry exactly the identification metadata needed to close
both gaps, and grib2io already decodes it to human-readable strings — it just was
not being written out. This change writes it out.

The GRIB2-native code keys (`discipline`, `parameterCategory`, etc.) are retained
alongside the new CF keys so that provenance is preserved (**Reusable**).

---

## What changed

### New: root-level dataset metadata

A root `.zattrs` object is now written to the manifest (previously absent
entirely). It becomes the xarray **global attributes** when the store is opened,
and identifies the dataset from GRIB2 Sections 0/1/4:

```json
{
  "Conventions": "CF-1.8",
  "institution": "US National Weather Service - NCEP",
  "source": "Analysis from GFS (Global Forecast System)",
  "reference_time": "2026-08-10T00:00:00",
  "reference_time_significance": "Start of Forecast",
  "production_status": "Operational Products",
  "type_of_data": "Forecast Products",
  "GRIB2_master_table_version": 2,
  "GRIB2_local_table_version": 1,
  "grib2io_version": "2.8.1"
}
```

### Enriched: per-variable metadata

Each variable's `.zattrs` gains CF vocabulary and a few decoded, human-readable
fields, while keeping all of the original GRIB2-native keys. Below is a real
2-metre temperature variable from the GFS example:

```json
{
  "_ARRAY_DIMENSIONS": ["valid_time", "height_above_ground", "y", "x"],
  "coordinates": "latitude longitude",
  "standard_name": "air_temperature",     // NEW  (CF)
  "long_name": "Temperature",             // NEW  (renamed from fullName)
  "units": "K",
  "discipline": 0,
  "parameterCategory": 0,
  "parameterNumber": 0,
  "typeOfFirstFixedSurface": 103,
  "valueOfFirstFixedSurface": 2.0,
  "level": "2 m above ground",            // NEW  (human-readable)
  "shortName": "TMP",
  "valid_time": "2026-08-10T00:00:00",
  "lead_time_hours": 0.0                   // NEW
}
```

Statistical variables (accumulations, max/min, etc.) additionally gain
`cell_methods` (e.g. `"time: sum"`) and `duration_hours` (the processing window).

> **Breaking note:** the previous `fullName` key is **renamed** to `long_name`
> to follow CF convention. Consumers that keyed on `fullName` should switch to
> `long_name`.

---

## Field reference

### Root `.zattrs`

| Attribute | Source (`Grib2Message`) | Notes |
|---|---|---|
| `Conventions` | — | Fixed `"CF-1.8"`. |
| `institution` | `originatingCenter` (+ `originatingSubCenter`) | Decoded center name; sub-center appended when defined. |
| `source` | `generatingProcess` | Decoded model/process description. |
| `reference_time` | `refDate` | Model cycle, ISO 8601. |
| `reference_time_significance` | `significanceOfReferenceTime` | e.g. "Start of Forecast". |
| `production_status` | `productionStatus` | e.g. "Operational Products". |
| `type_of_data` | `typeOfData` | e.g. "Forecast Products". |
| `GRIB2_master_table_version` | `masterTableInfo` | Integer code. |
| `GRIB2_local_table_version` | `localTableInfo` | Integer code. |
| `grib2io_version` | `grib2io.__version__` | Emitted when available. |

Only keys that decode successfully are emitted, so a manifest never carries
`null` placeholder values.

### Variable `.zattrs` (new / changed keys)

| Attribute | Source | Notes |
|---|---|---|
| `standard_name` | `tables["shortname_to_cf"]` | CF standard name; `"unknown"` if unmapped. Same lookup grib2io's xarray backend uses. |
| `long_name` | `fullName` | Renamed from `fullName`. |
| `level` | `Level` descriptor (`str(msg.level)`) | Human-readable wgrib2-style string, e.g. `"500 mb"`, `"2 m above ground"`. |
| `lead_time_hours` | `leadTime` | Representative forecast lead in hours. Distinguishes an `f000` manifest from an `f384` one. |
| `duration_hours` | `duration` | Statistical processing window; emitted only when > 0. |
| `cell_methods` | `tables["shortname_to_cf"]` | CF cell method (e.g. `"time: maximum"`); emitted only when defined. |

Retained GRIB2-native keys: `discipline`, `parameterCategory`, `parameterNumber`,
`typeOfFirstFixedSurface`, `valueOfFirstFixedSurface`, `shortName`, `valid_time`,
`coordinates`, `_ARRAY_DIMENSIONS`.

---

## How to generate a manifest

**Command line:**

```bash
grib2io kerchunk gfs.t00z.pgrb2.0p25.f000 --output gfs_references.json
```

**Python API:**

```python
from grib2io.kerchunk import ReferenceGenerator

gen = ReferenceGenerator("gfs.t00z.pgrb2.0p25.f000.conus.grib2")
gen.generate()
gen.to_json("gfs_references.json")
```

`ReferenceGenerator` also accepts a list of files, message `filters=`, and
remote URIs / `storage_options=` for scanning objects on S3 or HTTP.

---

## How to open a manifest

```python
import fsspec, xarray as xr
import grib2io.codecs           # registers the grib2io Zarr codec

fs = fsspec.filesystem("reference", fo="gfs_references.json", remote_protocol="file")
ds = xr.open_dataset(fs.get_mapper(""), engine="zarr", consolidated=False)

print(ds.attrs["institution"])         # -> US National Weather Service - NCEP
print(ds["HGT"].attrs["standard_name"]) # -> geopotential_height
```

`remote_protocol` should match where the referenced GRIB2 bytes live
(`"file"` for the local examples here, `"s3"` / `"http"` for remote sources).

---

## Example files in this directory

Both manifests were generated with this feature branch from small CONUS
sub-region subsets pulled from [NOMADS](https://nomads.ncep.noaa.gov), cycle
`2026-08-10 00Z`. The subsets contain HGT/TMP/UGRD/VGRD at 500 mb, 850 mb,
surface, and 2 m — enough to show multiple levels, surfaces, and grid types.

| File | Source | Grid | Notes |
|---|---|---|---|
| `data/gfs.t00z.pgrb2.0p25.f000.conus.grib2` | GFS 0.25° analysis | regular lat/lon (GDT 3.0), 61×61 | |
| `gfs_references.json` | ↑ | | Manifest for the GFS subset. |
| `data/nam.t00z.awphys00.tm00.conus.grib2` | NAM 12 km | Lambert conformal (GDT 3.30), 150×123 | Contrasting projected grid. |
| `nam_references.json` | ↑ | | Manifest for the NAM subset. |

The chunk-reference URIs in the JSON were rewritten to the relative
`data/…grib2` paths so the examples are portable within this directory. When you
generate your own manifest, the URI reflects the source location (a local
`file://` path, or the remote `s3://` / `https://` object you scanned).

To reproduce the round trip:

```bash
cd demos/kerchunk_fair_metadata
python -c "
import fsspec, xarray as xr, grib2io.codecs
fs = fsspec.filesystem('reference', fo='nam_references.json', remote_protocol='file')
ds = xr.open_dataset(fs.get_mapper(''), engine='zarr', consolidated=False)
print(ds)
print(ds.attrs)
"
```
