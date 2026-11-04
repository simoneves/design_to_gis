# design_to_gis

Converts `design.json`, an amaze-router / Allegro export of a high-density chip package layout (coordinates in microns), into one GeoPackage per feature type, then imports them into HeavyDB as fake geospatial data.

Paths below are relative to the `<workspace>` folder (`design_to_gis`). The earlier agent chat, from when the project lived in another folder, was copied into this project's chat history.

## Where the context is

- `design_to_gis_plan.md`: the full record of decisions, table design, transform, verification results, bugs fixed, HeavyDB findings and known limitations. Read this first.
- `heavydb_commands.log`: every command sent to HeavyDB, with output and exit codes.
- `design_gis/conversion.json`: the settings and per-table feature counts of the current outputs.

## Files

- `design_to_gis.py`: the converter. Run `python3 design_to_gis.py --help` for options.
- `heavysql_logged.sh`: runs SQL through `heavysql` and appends it to `heavydb_commands.log`.
- `design_gis/`: the current outputs, 17 `<prefix><table>.gpkg` files in UTM 31N (EPSG:32631), generated with `--prefix <prefix>`. `TABLE_PREFIX` in `conversion.json` records the prefix used.

## Current state

- HeavyDB runs in a Docker container on the default ports, with this folder mounted at `/design_to_gis` and data in `/var/lib/heavyai`. `heavysql_logged.sh` finds the container by that mount.
- The 17 `<prefix><table>` tables in database `heavyai` are imported with `COPY <prefix><table> FROM '/design_to_gis/design_gis/<prefix><table>.gpkg' WITH (source_type='geo_file');`. HeavyDB reprojects them to EPSG:4326 with `COMPRESSED(32)` encoding.
- Leave the database's other tables alone.

## Working rules

- Send every HeavyDB command through `./heavysql_logged.sh`, so it is logged.
- Don't delete files or drop tables without asking.
- Column names must avoid HeavyDB keywords. `text` and `fixed` were renamed to `label_text` and `is_fixed`.
