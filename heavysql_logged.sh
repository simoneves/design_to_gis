#!/usr/bin/env bash
# Run SQL through heavysql inside the HeavyDB container and append the command,
# output and exit code to a log.
# Usage: heavysql_logged.sh "SQL STATEMENT;"   (or pipe SQL on stdin)
# Environment: HEAVY_CONTAINER, HEAVY_MOUNT, HEAVYSQL, HEAVY_USER, HEAVY_PASSWORD, HEAVY_DB, HEAVY_PORT, HEAVY_LOG
# HEAVY_CONTAINER defaults to the one running container that mounts the workspace at HEAVY_MOUNT.

HEAVY_MOUNT=${HEAVY_MOUNT:-/design_to_gis}
if [ -z "$HEAVY_CONTAINER" ]; then
  containers=$(docker ps --filter "volume=$HEAVY_MOUNT" --format '{{.Names}}')
  if [ -z "$containers" ] || [ "$(printf '%s\n' "$containers" | wc -l)" -ne 1 ]; then
    echo "heavysql_logged.sh: expected one running container mounting $HEAVY_MOUNT, found:" \
      "${containers:-none}; set HEAVY_CONTAINER" >&2
    exit 2
  fi
  HEAVY_CONTAINER=$containers
fi
HEAVYSQL=${HEAVYSQL:-/opt/heavyai/bin/heavysql}
HEAVY_USER=${HEAVY_USER:-admin}
HEAVY_PASSWORD=${HEAVY_PASSWORD:-HyperInteractive}
HEAVY_DB=${HEAVY_DB:-heavyai}
HEAVY_PORT=${HEAVY_PORT:-6274}
HEAVY_LOG=${HEAVY_LOG:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/heavydb_commands.log}

if [ $# -gt 0 ]; then
  sql="$*"
else
  sql=$(cat)
fi

start=$(date --iso-8601=seconds)
output=$(printf '%s\n' "$sql" | docker exec -i "$HEAVY_CONTAINER" "$HEAVYSQL" -q \
  -u "$HEAVY_USER" -p "$HEAVY_PASSWORD" --db "$HEAVY_DB" --port "$HEAVY_PORT" 2>&1)
status=$?

{
  echo "===== $start"
  echo "\$ docker exec -i $HEAVY_CONTAINER $HEAVYSQL -q -u $HEAVY_USER -p **** --db $HEAVY_DB --port $HEAVY_PORT"
  printf '%s\n' "$sql" | sed 's/^/> /'
  echo "--- output (exit $status, finished $(date --iso-8601=seconds))"
  printf '%s\n' "$output"
  echo
} >> "$HEAVY_LOG"

printf '%s\n' "$output"
exit $status
