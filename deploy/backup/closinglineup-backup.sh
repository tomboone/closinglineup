#!/bin/sh
# Nightly pg_dump of the closinglineup database, kept on the USB drive and synced to B2.
# Run by closinglineup-backup.timer as tom. Install and operate per ../README.md, step 9.
set -eu

ENV_FILE=${ENV_FILE:-$HOME/.config/closinglineup-backup.env}
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ENV_FILE"
: "${MOUNT_POINT:?}" "${BACKUP_DIR:?}" "${REMOTE:?}"

# Reports to a Kuma push monitor (the personal Kuma): status=up after a good run, status=down with
# the reason on failure. A run that never happens is caught by the monitor's heartbeat interval.
ping() {  # $1=status (up|down) $2=msg
  [ -n "${PING_URL:-}" ] || return 0
  # Tolerate a URL pasted with Kuma's default "?status=up&msg=OK&ping=" query.
  curl -fsS -m 15 --retry 3 -o /dev/null --get "${PING_URL%%\?*}" \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" || echo "ping failed" >&2
}
fail() { echo "$1" >&2; ping down "$1"; exit 1; }

# A dropped drive leaves /mnt/music as an empty directory on the root disk: a dump written there is
# lost with the next mount, and syncing an empty BACKUP_DIR would hide every dump on B2.
mountpoint -q "$MOUNT_POINT" || fail "$MOUNT_POINT is not mounted"

# The compose project is the one this script was checked out with (~/closinglineup/deploy), whose
# .env carries the settings `docker compose` needs to read the file.
cd "$(dirname "$0")/.." || fail "no compose project next to $0"

# Custom format, for pg_restore. pg_dump reads one snapshot, so the dump is consistent while the
# worker keeps writing. Written under a dot-name (which rclone below skips) and renamed once checked,
# so a failed run never leaves a file that looks like a good dump.
mkdir -p "$BACKUP_DIR" || fail "cannot create $BACKUP_DIR"
ts=$(date +%Y%m%d-%H%M%S)
tmp="$BACKUP_DIR/.closinglineup-$ts.dump"
docker compose exec -T postgres pg_dump -U closinglineup -d closinglineup --format=custom >"$tmp" \
  || { rm -f "$tmp"; fail "pg_dump failed"; }
docker compose exec -T postgres pg_restore --list <"$tmp" >/dev/null \
  || { rm -f "$tmp"; fail "dump failed pg_restore --list"; }
mv "$tmp" "$BACKUP_DIR/closinglineup-$ts.dump" || fail "dump rename failed"
find "$BACKUP_DIR" -name 'closinglineup-*.dump' -mtime +"${KEEP_DAYS:-14}" -delete \
  || fail "pruning old dumps failed"

# B2 hides rather than deletes (the remote's hard_delete = false), so a dump aged out here stays
# recoverable per the bucket's lifecycle.
rclone sync "$BACKUP_DIR" "$REMOTE" --exclude '.*' --fast-list --log-level NOTICE \
  || fail "B2 sync failed"

ping up OK
