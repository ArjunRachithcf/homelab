#!/usr/bin/env bash
# ~/homelab/backup-photos.sh: off-box copy to Proton Drive (RUNBOOK.md step 9). Run by
# homelab-photos-backup.timer after the nightly backup, or by hand: ./backup-photos.sh
# (./backup-photos.sh photos-only [N]: only the Proton Photos part, at most N new items;
#  ./backup-photos.sh quick: the newest archive + new photos, no prune, no restore test; run by
#  homelab-offbox.service every 4 h and after every change, backup-watch.sh)
#
# Uploads the age-encrypted homelab-*.tar.age (Paperless, Authentik and Immich DBs, profile
# pictures, config) to /my-files/homelab/backups with Proton's official Drive CLI (same content is
# skipped), and trashes archives there by backup-retention.sh: all of the last KEEP_HOURS, then the
# first archive of each day for KEEP_DAYS, never below KEEP_MIN archives. Since 2026-10-23 the photo folders
# no longer go to /my-files/homelab/photos: Immich's library is rebuilt from Proton Photos + the
# DB in the archive (immich/restore-from-proton.sh, tested every Sunday below).
#
# OpenCloud's files (RUNBOOK.md 7e) go to /my-files/homelab/files one by one, from the newest
# snapshot (files-local.sh): Proton encrypts them end to end and skips files with the same content,
# so only new and changed files travel (a nightly tar of everything would upload it all again).
# Deletions aren't mirrored. Sundays, 20 random files come back and are compared by hash.
#
# Then the photos themselves go to Proton Photos (timeline + album "Family", shared with the
# family in Proton): every photo/video on either Immich timeline, once per content (the two
# accounts share most photos), in batches, archived and locked items left out. This part uses
# our patched build of Proton's CLI ($PDH, proton-drive/README.md): Live Photos go up as still +
# linked video, and thumbnails come from Immich's previews (the official CLI makes none for HEIC
# or videos). Proton dates videos and photos without EXIF by the file time, so each file is a
# reflink copy (no extra space) with its time set to Immich's. Done checksums are listed in
# $STATE/proton-photos.done; deletions aren't mirrored.
#
# Session: `proton-drive auth login` once, stored in `pass` (GPG key without passphrase) so
# the timer works without a desktop login. RUNBOOK.md step 9.
set -euo pipefail
cd "$(dirname "$0")"
export PROTON_DRIVE_CREDENTIALS_STORE=pass
PD=$HOME/.local/opt/proton-drive/proton-drive   # official, kept current by proton-drive/update.sh
REMOTE=/my-files/homelab
SRC=$(sed -n 's/^IMMICH_UPLOAD_DIR=//p' .env | tail -n1)

[[ -x "$PD" ]] || { echo "proton-drive not installed: proton-drive/update.sh" >&2; exit 1; }
# shellcheck source=lib/components.sh
source lib/components.sh   # component_on: only the chosen components (COMPONENTS in .env)
# Never upload an empty mountpoint as "the library"
! component_on photos || mountpoint -q "$SRC" || { echo "$SRC is not mounted" >&2; exit 1; }
# One run at a time (the first Proton Photos upload can outlast a night)
mkdir -p "$HOME/.local/state/homelab"; exec 9>"$HOME/.local/state/homelab/backup-photos.lock"
MODE=${1:-all}; LIMIT=${2:-}
[[ $MODE =~ ^(all|quick|photos-only)$ ]] || { echo "usage: backup-photos.sh [quick | photos-only [N]]" >&2; exit 2; }
if [[ $MODE == quick ]]; then
  # after a change: wait for a running copy (the nightly one can take a while) rather than skip
  flock -w 10800 9 || { echo "backup-photos.sh still busy after 3 h" >&2; exit 1; }
else
  flock -n 9 || { echo "another backup-photos.sh is still running; skipped"; exit 0; }
fi

# folder PARENT NAME: create PARENT/NAME unless it exists
folder() { "$PD" filesystem info "$1/$2" >/dev/null 2>&1 || "$PD" filesystem create-folder "$1" "$2" >/dev/null; }
# up NAME SRC… DEST: the JSON report goes to $LOG/NAME.json (which file failed, and why). A
# failure is remembered, not fatal, so one bad photo can't hold back the encrypted backups.
LOG=${STATE_DIRECTORY:-$HOME/.local/state/homelab}/proton-drive; mkdir -p "$LOG"
failed=0
up() {
  local name="$1"; shift
  "$PD" filesystem upload --json -f create-new-revision -d merge -t "$@" >"$LOG/$name.json" \
    || { echo "✗ $name: upload had errors, see $LOG/$name.json" >&2; failed=1; }
}

KEEP_HOURS=48  # encrypted archives in Proton: all from the last 48 h,
KEEP_DAYS=60   # then the first of each day for 60 days; the newest KEEP_MIN always stay
KEEP_MIN=14
if [[ $MODE != photos-only ]]; then
folder /my-files homelab
folder "$REMOTE" backups

shopt -s nullglob
tars=(backups/homelab-*.tar.age)
[[ $MODE == quick ]] && (( ${#tars[@]} )) && tars=("${tars[-1]}")   # names sort by time
if (( ${#tars[@]} )); then
  echo "Encrypted backups → $REMOTE/backups (${#tars[@]} file(s))…"
  up backups "${tars[@]}" "$REMOTE/backups"
fi

# Prune by the date in the name (homelab-YYYY-MM-DD-HHMM.tar.age), never below KEEP_MIN archives.
# Trash, not delete: Proton keeps trashed files for a while.
if [[ $MODE != quick ]]; then
mapfile -t remote < <("$PD" filesystem list --json "$REMOTE/backups" \
  | jq -r '.[] | select(.name.ok) | .name.value | select(test("^homelab-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}\\.tar\\.age$"))' | sort)
old=()
if (( ${#remote[@]} > KEEP_MIN )); then
  mapfile -t drop < <(printf '%s\n' "${remote[@]:0:${#remote[@]}-KEEP_MIN}" | ./backup-retention.sh "$KEEP_HOURS" "$KEEP_DAYS")
  for f in "${drop[@]}"; do old+=("$REMOTE/backups/$f"); done
fi
if (( ${#old[@]} )); then
  echo "Proton: trashing ${#old[@]} archive(s) (intra-day after $KEEP_HOURS h, daily after $KEEP_DAYS days)…"
  "$PD" filesystem trash "${old[@]}" >/dev/null || failed=1
fi
fi
fi

# ── OpenCloud's files ─────────────────────────────────────────────────────
# shellcheck disable=SC2010  # snapshot names are fixed date stamps
FILES_SNAP=$(ls -d "${NVME1_DIR:-/srv/nvme1}"/files-snap/20* 2>/dev/null | grep -v '\.part$' | tail -n1 || true)
if component_on files && [[ $MODE == all && -n "$FILES_SNAP" && -d "$FILES_SNAP/storage/users" ]]; then
  folder "$REMOTE" files
  echo "OpenCloud files ($(basename "$FILES_SNAP")) → $REMOTE/files…"
  # The folder itself goes up: $REMOTE/files/users/<person>/…
  up files "$FILES_SNAP/storage/users" "$REMOTE/files"
  if [[ $(date +%u) == 7 ]]; then
    check=$(mktemp -d); bad=0; n=0
    while IFS= read -r f; do
      rel=${f#"$FILES_SNAP/storage/users/"}
      "$PD" filesystem download -f remove "$REMOTE/files/users/$rel" "$check/" >/dev/null 2>&1 || { bad=$((bad + 1)); continue; }
      [[ "$(sha256sum < "$f")" == "$(sha256sum < "$check/${f##*/}")" ]] || bad=$((bad + 1))
      n=$((n + 1)); rm -f "$check/${f##*/}"
    done < <(find "$FILES_SNAP/storage/users" -type f -size -200M | shuf -n 20)
    rm -rf "$check"
    if (( bad )); then echo "✗ OpenCloud restore test: $bad of 20 files missing or different in Proton" >&2; failed=1
    else echo "OpenCloud restore test: $n file(s) identical in Proton."; fi
  fi
fi

# ── Proton Photos ─────────────────────────────────────────────────────────
if component_on photos; then   # (until "fi  # photos" below; not indented)
ALBUM=/albums/Family
STATE=${STATE_DIRECTORY:-$HOME/.local/state/homelab}; DONE="$STATE/proton-photos.done"; touch "$DONE"
STAGE="$SRC/.proton-photos"   # same filesystem as the library, so cp --reflink works
BATCH=200

PDH=$HOME/.local/opt/proton-drive-homelab/proton-drive
[[ -x "$PDH" ]] || { echo "patched proton-drive missing: proton-drive/update.sh" >&2; exit 1; }

# checksum|still or video|Live Photo video or ""|preview|UTC time; one row per content, oldest first
photo_list() {
  docker compose exec -T immich-db psql -U postgres -d immich -At -F'|' -c "
    SELECT DISTINCT ON (a.checksum) encode(a.checksum, 'hex'), a.\"originalPath\",
           coalesce(v.\"originalPath\", ''), coalesce(f.path, ''),
           to_char(a.\"fileCreatedAt\" AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
    FROM asset a
    LEFT JOIN asset v ON v.id = a.\"livePhotoVideoId\" AND v.\"deletedAt\" IS NULL
    LEFT JOIN asset_file f ON f.\"assetId\" = a.id AND f.type = 'preview'
    WHERE a.\"deletedAt\" IS NULL AND a.visibility = 'timeline' AND a.status = 'active'
    ORDER BY a.checksum, a.\"fileCreatedAt\"" | sort -t'|' -k5
}

# upload_batch: stage the rows in $todo, upload, record what didn't fail
upload_batch() {
  rm -rf "$STAGE"; mkdir -p "$STAGE/files" "$STAGE/previews"
  local row sum path live prev t f d
  for row in "${todo[@]}"; do
    IFS='|' read -r sum path live prev t <<<"$row"
    f="$SRC/${path#/data/}"; [[ -f "$f" ]] || continue
    d="$STAGE/files/$sum"; mkdir "$d"   # own folder per item: names repeat (IMG_0055 every few years)
    cp --reflink=always "$f" "$d/"
    # Live Photo: the video next to the still under the same name, so --live-photos pairs them
    if [[ -n "$live" && -f "$SRC/${live#/data/}" && "${live,,}" == *.mov ]]; then
      local stem=${f##*/}; stem=${stem%.*}
      cp --reflink=always "$SRC/${live#/data/}" "$d/$stem.mov"
    fi
    touch -d "$t" "$d"/*
    [[ -n "$prev" && -f "$SRC/${prev#/data/}" ]] && ln -s "$SRC/${prev#/data/}" "$STAGE/previews/$sum.jpg"
  done
  local out; out=$(HOMELAB_PREVIEW_DIR="$STAGE/previews" "$PDH" photo upload --json --live-photos -c skip "$STAGE/files") || failed=1
  local bad; bad=$(jq -r '.failures[]?.name' <<<"$out" 2>/dev/null || true)
  for row in "${todo[@]}"; do
    IFS='|' read -r sum path live prev t <<<"$row"
    # "-c skip" compares content, so recording too little only costs a skipped retry
    [[ -d "$STAGE/files/$sum" ]] && ! grep -qxF "${path##*/}" <<<"$bad" && echo "$sum" >>"$DONE"
  done
  [[ -z "$bad" ]] || { echo "✗ Proton Photos: $(wc -l <<<"$bad") item(s) failed, retried next night" >&2; failed=1; }
  rm -rf "$STAGE"
}

mapfile -t new < <(photo_list | awk -F'|' 'FILENAME == ARGV[1] {done[$1]; next} !($1 in done)' "$DONE" -)
[[ -n "$LIMIT" ]] && new=("${new[@]:0:LIMIT}")
echo "Proton Photos: ${#new[@]} new item(s)…"
for (( i = 0; i < ${#new[@]}; i += BATCH )); do
  todo=("${new[@]:i:BATCH}"); upload_batch
done

# Everything in the timeline belongs in the Family album
if "$PDH" album list | grep -q ' Family ('; then
  # One CLI at a time: they share a local SQLite cache ("database is locked", 2026-10-24)
  timeline=$("$PDH" photo timeline | sort); in_album=$("$PDH" album photos "$ALBUM" | sort)
  mapfile -t missing < <(comm -23 <(cat <<<"$timeline") <(cat <<<"$in_album") | sed '/^$/d')
  echo "Album Family: adding ${#missing[@]}…"
  for (( i = 0; i < ${#missing[@]}; i += 100 )); do
    chunk=("${missing[@]:i:100}")
    "$PDH" album add-photo "$ALBUM" "${chunk[@]/#//photos/}" >/dev/null || failed=1
  done
fi

# Sundays: restore test, 20 random items back from Proton Photos with the SHA-1 Immich expects
if [[ $MODE == all && $(date +%u) == 7 ]]; then
  ./immich/restore-from-proton.sh verify 20 || failed=1
fi
fi  # photos

(( failed )) && exit 1
echo "Done."
