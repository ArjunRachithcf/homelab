#!/usr/bin/env bash
# ~/homelab/files-local.sh: local care of OpenCloud's files (RUNBOOK.md step 7e). Nightly by
# homelab-files-local.timer (03:50, before the Proton copy at 04:30), or by hand.
#
# 1. Snapshot: OpenCloud stops for a few seconds, its whole storage is reflink-copied (XFS: no
#    extra space, takes seconds) to $SNAPS/<date-time>, OpenCloud starts again. The snapshot is
#    consistent (files, metadata and the user directory from the same moment); the two newest are kept.
#    Reflinks only work inside one mount, so this reads $DISK/files, not the /srv/files bind mount.
# 2. Second copy on the primary SSD (Kingston, btrfs) in $COPY/current, from the snapshot, with
#    xattrs (the posix storage driver keeps its metadata there). Files deleted or replaced since
#    the last copy move to $COPY/deleted/<date>/ and are kept 30 days.
# backup.sh tars the newest snapshot's config/metadata; backup-photos.sh uploads its user files to Proton.
set -euo pipefail
cd "$(dirname "$0")"
DISK=${NVME1_DIR:-/srv/nvme1}
SRC=$DISK/files
SNAPS=$DISK/files-snap
COPY=${FILES_COPY_DIR:-/srv/files-copy}
KEEP_SNAPS=2
KEEP_DELETED_DAYS=30
# One at a time (the nightly run, a run by hand)
mkdir -p "$HOME/.local/state/homelab"; exec 9>"$HOME/.local/state/homelab/files-local.lock"
flock -w 3600 9 || { echo "another files-local.sh is still running" >&2; exit 1; }

# Never snapshot or mirror an empty mountpoint: rsync --delete would empty the copy
mountpoint -q "$DISK" && [[ -d "$SRC/storage" ]] || { echo "$DISK not mounted or $SRC has no OpenCloud storage yet" >&2; exit 1; }
[[ -d "$SNAPS" && -w "$SNAPS" ]] || { echo "$SNAPS missing (ansible --tags storage)" >&2; exit 1; }
[[ -d "$COPY" && -w "$COPY" ]] || { echo "$COPY missing or not writable (ansible --tags storage)" >&2; exit 1; }

snap="$SNAPS/$(date +%F-%H%M)"
echo "Snapshot → $snap…"
running=$(docker compose ps -q --status running opencloud)
stopped_at=$(date +%s)
[[ -n "$running" ]] && docker compose stop -t 30 opencloud >/dev/null
# Start it again whatever happens below
trap '[[ -n "$running" ]] && docker compose start opencloud >/dev/null' EXIT
cp -a --reflink=always "$SRC" "$snap.part"
mv "$snap.part" "$snap"
[[ -n "$running" ]] && docker compose start opencloud >/dev/null
trap - EXIT
echo "OpenCloud was stopped $(( $(date +%s) - stopped_at ))s."

# Keep the newest KEEP_SNAPS (names sort by time)
mapfile -t snaps < <(find "$SNAPS" -mindepth 1 -maxdepth 1 -type d -name '20*' ! -name '*.part' | sort)
(( ${#snaps[@]} > KEEP_SNAPS )) && rm -rf -- "${snaps[@]:0:${#snaps[@]}-KEEP_SNAPS}"
find "$SNAPS" -mindepth 1 -maxdepth 1 -name '*.part' -exec rm -rf {} +

# Second guard: if the storage suddenly has far fewer files than the copy, something is wrong
# (a broken mount, a bad migration). Stop rather than move the copy to deleted/.
if [[ -d "$COPY/current/storage" ]]; then
  have=$(find "$snap/storage" -type f | wc -l); had=$(find "$COPY/current/storage" -type f | wc -l)
  (( have * 2 >= had )) || { echo "storage has $have files, the copy $had: not mirroring" >&2; exit 1; }
fi

echo "Copy → $COPY/current…"
mkdir -p "$COPY/current"
rsync -aHAX --delete --backup --backup-dir="$COPY/deleted/$(date +%F)" "$snap/" "$COPY/current/"
find "$COPY/deleted" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DELETED_DAYS" -exec rm -rf {} + 2>/dev/null || true
echo "Done: $(du -sh "$COPY" | cut -f1) on the primary SSD."
