# Plan: Convert `design.json` to world-space GIS files

Status: complete. This plan was written after the work was done and records what was decided and why.

## Goal

Convert the amaze-router / Allegro package export `design.json` (a high-density package layout, 504 MB) into GDAL-readable vector files for import into a database that only understands world-space geospatial data.

Constraints from the request:

- The format must be supported by GDAL.
- Files may contain only the six basic OGC geometry types (Point, LineString, Polygon and their Multi variants) plus scalar attributes.
- One table per feature type, one file per table.
- Only tables with geometry. These include anything given as a position plus a size for a standard shape, such as a circular drill hole or a bounding box. Attribute-only tables (nets, components, padstacks, rules) are out of scope.
- Coordinates are mapped into a projected world CRS (UTM-like) with an offset and a microns-to-metres-or-larger scale.
- The script must be reusable, with the transform factors as command-line parameters that have sensible defaults.

## 1. Understand the source

- [x] Infer the schema by loading the whole file and merging the types of every object (`/tmp/schema_infer.py`).
- [x] Check how sections reference each other, which data is duplicated, and which sections use the most space (`/tmp/checks.py`).
- [x] List the geometry primitives in each section.

Findings that shaped the conversion:

| Finding | Consequence |
|---|---|
| Units are microns; the design spans about ±37.7 mm and the drawing extent is ±228.6 mm | The transform must keep ±228.6 km inside a UTM zone at 1 m per µm |
| Shape contours (`nativeCenterContours.root`) have arcs with a center, `isClockwise` and `isCircle` | Arcs can be converted to straight segments exactly, to any tolerance |
| Path (trace) arcs only have two endpoints | The 15 trace arcs can't be reconstructed; flag them |
| 4,764 paths change width partway along | Split traces into runs of constant width |
| Shapes store the same outline four times | Use only `nativeCenterContours` |
| `layerPadPolygons` is `false` | Pads and pins can only be points or bounding-box rectangles |
| Padstacks define `holeType: circle_drill` and `drillDiameter` (65 µm for 185,531 vias, 200 µm for 8,655, and 65 µm for one pin) | Drill holes are exact circles: `via_drill` and `pin_drill` |
| Via bounding boxes are square, centered on the via, and match their padstack (for example 90 µm), but the pad shape isn't recorded | `via_extent` as squares, not assumed circles |
| Text has bounding boxes; tees and DRC markers have positions only | `label_extent`; no extent tables for tees or DRC markers |
| Traces have a centerline and a width, with round ends and corners | `trace_copper`, the centerline widened by half the width on each side |
| Two pin IDs and ten shape net IDs point at missing objects | Keep the IDs as attributes; don't enforce joins |

## 2. Choose the format

| Option | Decision |
|---|---|
| GeoJSON | Rejected: requires longitude and latitude, has no types, and would be larger than the source |
| Shapefile | Rejected: 10-character field names and a 2 GB limit |
| GeoParquet | Rejected: this GDAL 3.11.4 build has no Parquet driver |
| FlatGeobuf | Supported as an option (`--format FlatGeobuf`) |
| **GeoPackage** | **Default**: typed columns, R-tree spatial index, a custom CRS, and dataset metadata |

Curved geometry types (CircularString, CurvePolygon) are excluded by the six-type constraint, so all arcs are converted to straight segments.

## 3. Table design

One file per table. Every feature keeps its source `dbid` and, where it applies, `net_id`, `net_name`, `layer` and `layer_index` (0 to 19, from `constraints.layerOrder`).

The names below are the base names. `--prefix` is added to both the file name and the table name inside it, so `--prefix <prefix>` gives `<prefix>via.gpkg` containing table `<prefix>via`.

| Table | Geometry | Source |
|---|---|---|
| `trace` | LineString | `paths`, split into runs of constant width |
| `trace_copper` | Polygon | `trace` widened by half its width, with round ends and corners |
| `copper_area` | Polygon (MultiPolygon only if a repair splits an area) | `shapes`: outline plus voids as holes |
| `via` | Point | `vias`, with start and end layers |
| `via_extent` | Polygon | `vias[].bBox`, with `width_um` and `height_um` |
| `via_drill` | Polygon (circle) | Via position plus the padstack's drill diameter |
| `pin` | Point | `pins` |
| `pin_extent` | Polygon | `pins[].bBox` |
| `pin_drill` | Polygon (circle) | Pin position plus the padstack's drill diameter |
| `symbol` | Point | `symbols`, joined with `components` |
| `symbol_extent` | Polygon | `symbols[].bBox` |
| `ratsnest` | LineString | `nets[].rats`, a two-point line between `pinXY` values |
| `tee` | Point | `tees` |
| `label` | Point | `text` |
| `label_extent` | Polygon | `text[].bBox` |
| `drc_marker` | Point | `drcs`, with `actual_um` and `expected_um` parsed from strings |
| `design_extent` | Polygon | `meta.bBox`, plus transform parameters and `stats` as columns |

The first version had 12 tables. `trace_copper`, `via_extent`, `via_drill`, `pin_drill` and `label_extent` were added once position-plus-size shapes were confirmed to be in scope.

Out of scope: attribute-only tables for nets, components, padstacks and constraints. Their most useful fields are copied onto the features instead.

## 4. Coordinate transform

```
world_x = offset_x + scale * x_um
world_y = offset_y + scale * y_um
```

| Parameter | Default | Reason |
|---|---|---|
| `--crs` | `EPSG:32631` (WGS 84 / UTM 31N) | A standard projected CRS in metres |
| `--scale` | `1.0` | 1 µm becomes 1 m, so metre distances equal micron distances; ±228.6 km fits in the zone |
| `--offset-x` | `500000` | The zone's central meridian, where scale distortion is lowest |
| `--offset-y` | `1000000` | Keeps all northings positive (about 9°N) |
| `--arc-tolerance-um` | `0.5` | Well below the 3.5 µm minimum neck width and 12 µm minimum trace width |

A scale of 1 km per µm was considered and rejected as a default, because it would put the design about 37,000 km across, far outside any UTM zone. The script checks the transformed extent against the CRS's area of use and warns if it falls outside.

## 5. Arc conversion

- [x] Confirm the direction convention: all 123,112 non-circular shape arcs have their computed midpoint inside their bounding box when `isClockwise = true` means a negative (clockwise, y-up) sweep.
- [x] Confirm that every outline and void ring chains end to start and closes.
- [x] Choose the segment count from the maximum allowed chord deviation, `step = 2·acos(1 − tol/r)`, with at least 8 segments per full turn.
- [x] Handle full circles (start equals end) as a ±2π sweep.
- [x] Repair invalid polygons with GDAL `MakeValid` and keep only the polygonal parts. No repairs were needed for this design.
- [x] Draw drill circles with the same arc conversion, at the arc tolerance.
- [x] Widen traces with GEOS `Buffer`, using 1.5 times the segments per quarter circle that the tolerance alone requires. GEOS rounds the segment count at each corner, so a segment can span up to 1.5 times the nominal angle. Without the factor, the error reached 1.07 µm.

## 6. Implementation

- [x] `design_to_gis.py`: a single script using the `osgeo.ogr` and `osgeo.osr` bindings.
- [x] A table registry that maps each table name to its geometry type, typed field list and producer function.
- [x] Load the JSON once, then write tables in parallel with forked worker processes (`-j`).
- [x] Write each file inside a transaction where the driver supports it, with a spatial index.
- [x] Store the transform, CRS, tolerance and source information as dataset and layer metadata, plus a `conversion.json` summary that also counts dropped and repaired features. A partial run (`--tables`) merges its results into the existing summary when the transform settings match.
- [x] CLI options: `input`, `-o/--out-dir`, `--crs`, `--scale`, `--offset-x`, `--offset-y`, `--arc-tolerance-um`, `--format`, `--prefix`, `--tables`, `-j/--jobs`.
- [x] `--prefix` must start with a letter or underscore and contain only letters, digits and underscores, so the names are valid SQL identifiers. It is recorded as `TABLE_PREFIX` in the file metadata and `conversion.json`.
- [x] Column names avoid HeavyDB keywords: `label_text` instead of `text`, and `is_fixed` instead of `fixed`.

## 7. Verification

| Check | Result |
|---|---|
| Feature counts | 17 files (417 MB) written; only zero-length or degenerate features would be dropped, and none were |
| Pin positions converted back to microns | Within 6.5e-11 µm of the source |
| Deviation of 64,513 circular voids from the true circles | At most 0.4991 µm, inside the 0.5 µm tolerance |
| Deviation of 194,186 via drill circles from the true circles | At most 0.4937 µm |
| Widened trace outlines vs the exact half-width (sample of 10,000) | At most 0.464 µm inside and 0.027 µm outside |
| Validity | All copper areas, drill circles and widened traces are valid; none repaired |
| CRS and metadata written | Confirmed with `ogrinfo` |
| Non-default run (FlatGeobuf, UTM 33N, custom offsets, 1 µm tolerance) | Works |
| Out-of-zone warnings at scale 3 and scale 1000 | Warns instead of crashing |
| Invalid prefix (`1bad`) | Rejected with a clear error |
| Import into HeavyDB (see section 8) | All 17 tables, row counts match |

Bugs found during verification and fixed:

1. Clockwise full circles got zero sweep, which dropped 64,513 voids and 2 outlines.
2. The extent check crashed when the transformed coordinates couldn't be converted to longitude and latitude.
3. Widened traces exceeded the arc tolerance (1.07 µm) until the 1.5 times segment factor was added.
4. A partial `--tables` run overwrote `conversion.json` with only the tables it wrote.
5. Seven tables failed to import into HeavyDB because the `fixed` column name is a keyword in its SQL grammar. The column was renamed to `is_fixed`.

## 8. Target database: HeavyDB

Findings from the HeavyDB source (`/home/seves/work/heavyai/heavydb`):

- `COPY ... WITH (source_type='geo_file')` reads files through GDAL 3.10.3, built with SQLite, so both GeoPackage and FlatGeobuf work. The table is created if it doesn't exist.
- The importer always reprojects geometry into `geo_coords_srid`, which must be 4326 (the default), 3857 or 900913. UTM can't be stored as-is.
- The default `geo_coords_encoding` is `COMPRESSED(32)`, which packs coordinates as longitude and latitude whatever the SRID. Projected data (3857 or 900913) must be imported with `geo_coords_encoding='NONE'`, or the coordinates are silently corrupted.
- Polygon columns become MULTIPOLYGON. Points and lines keep their type.
- Integer fields with GDAL's boolean subtype import as INT. Strings import as dictionary-encoded TEXT.
- Reserved words in column names get a trailing underscore, so `label.text` was renamed to `label_text`.
- Some grammar keywords aren't on that reserved list and make the generated `CREATE TABLE` fail instead. `FIXED` (from `ENCODING FIXED`) is one, so the `fixed` column was renamed to `is_fixed`. No other column names clash with the keywords in `java/calcite/src/main/codegen/config.fmpp`.

Setup used: rendering needs coordinates in degrees, so the default UTM 31N files (`design_gis/`) are imported with default options, and HeavyDB reprojects them to EPSG:4326 with `COMPRESSED(32)` encoding:

```sql
COPY <prefix>via FROM '/design_to_gis/design_gis/<prefix>via.gpkg' WITH (source_type='geo_file');
```

- HeavyDB runs in a Docker container, with the `<workspace>` folder mounted at `/design_to_gis`. If the workspace moves, the container mount must point at its new location. The data directory `/var/lib/heavyai` is mounted from the host, so the tables persist across container restarts.
- Files are generated with `--prefix <prefix>`, giving tables `<prefix>trace`, `<prefix>via` and so on.
- All 17 tables import with row counts matching the source.
- Sample positions match a local GDAL reprojection to within one compression step: about 9 mm east-west and 5 mm north-south, or 0.009 µm and 0.005 µm in design terms.
- The package lands at longitude 2.66° to 3.34° and latitude 8.70° to 9.39°.
- Commands are run through `heavysql_logged.sh`, which appends each statement, its output and exit code to `heavydb_commands.log`.

`ST_Distance` and `ST_Area` on these tables return degrees. For metres (equal to design microns), cast to geography or use `ST_Transform(geom, 32631)`.

A Web Mercator (3857) variant imported with `geo_coords_encoding='NONE'` was tried first and kept coordinates exact, but it isn't in degrees, so it was dropped.

## 9. Known limitations and possible follow-ups

- The 15 trace arcs are straight lines (`has_unrecoverable_arc = 1`). Fixing this needs the exporter to emit arc centers for paths.
- Real pad outlines need a re-export with `layerPadPolygons` enabled. Until then, `via_extent` and `pin_extent` are bounding boxes; pads named like `90P` are probably 90 µm circles, but the file doesn't say so.
- `trace_copper` widens each constant-width run separately and doesn't merge overlapping runs of the same net.
- Small traces lose some area because round ends are drawn as polygons. For example, a 12 µm trace end is drawn with 12 segments, so a very short run can be about 5% under its true area while staying within the distance tolerance.
- UTM scale distortion of up to about 0.04% affects geodesic measurements. Planar distances in the GeoPackage files' UTM coordinates are exact.
- In HeavyDB the data is stored in degrees with 32-bit compression, which rounds positions to about 0.009 µm east-west and 0.005 µm north-south in design terms.
