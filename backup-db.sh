#!/usr/bin/env bash
#
# Snapshot the live SQLite database to a timestamped file.
#
# Uses `sqlite3 .backup`, NOT `cp`: the DB runs in WAL mode (see get_db() in
# server.py), so a plain copy taken mid-write can miss the -wal file and yield a
# torn/stale snapshot. `.backup` takes a consistent online copy while the server
# keeps serving.
#
# Old snapshots are pruned, keeping the most recent $KEEP files. Run it from a
# systemd timer or cron every few minutes during the event (see DEPLOY.md).
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/spendenlauf}"
DB="${DB:-$APP_DIR/spendenlauf.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP="${KEEP:-288}"   # e.g. 288 × 5 min ≈ 24h of history

mkdir -p "$BACKUP_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
dest="$BACKUP_DIR/spendenlauf-$stamp.db"

# Consistent online snapshot of the WAL-mode database.
sqlite3 "$DB" ".backup '$dest'"

# Quick integrity check; drop the snapshot if it didn't come out clean.
if [ "$(sqlite3 "$dest" 'PRAGMA integrity_check;')" != "ok" ]; then
  echo "WARN: integrity check failed for $dest — removing" >&2
  rm -f "$dest"
  exit 1
fi

# Prune: keep only the newest $KEEP snapshots.
ls -1t "$BACKUP_DIR"/spendenlauf-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "Backed up to $dest"
