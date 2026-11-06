#!/usr/bin/env bash
# ~/homelab/immich/restore-from-proton.sh: rebuild Immich's library from Proton Photos
# (RUNBOOK.md step 7c, "Restore"). Immich's database says, for every photo/video, whose it is,
# where it lives (library/<user>/YYYY/MM/DD/…) and its SHA-1; Proton Photos keeps each content
# once (with its SHA-1, Live Photo videos linked to their still). This downloads each content
# once, checks the SHA-1, and puts it at every path the database expects (reflink copies when two
# accounts share a photo).
#
#   ./immich/restore-from-proton.sh plan           what's in Proton vs. what Immich expects
#   ./immich/restore-from-proton.sh verify [N]     restore test: N random items (default 20) into
#                                                  a scratch folder, SHA-1 compared, nothing placed
#   ./immich/restore-from-proton.sh restore        fill $IMMICH_UPLOAD_DIR (missing files only;
#                                                  re-runnable). Stop immich-server first.
#
# Needs Immich's database running with the data to restore to: on a new disk, first restore
# immich.sql.zst from the newest homelab-*.tar.age (RUNBOOK.md step 9). Not in Proton Photos: items in
# Immich's trash, and thumbnails/encoded videos (Immich regenerates them: Administration → Jobs).
# Profile pictures are in the age archive.
set -euo pipefail
cd "$(dirname "$0")/.."
export PROTON_DRIVE_CREDENTIALS_STORE=pass
PDH=$HOME/.local/opt/proton-drive-homelab/proton-drive
SRC=$(sed -n 's/^IMMICH_UPLOAD_DIR=//p' .env | tail -n1)
WORK=$SRC/.proton-restore   # same filesystem as the library: placing is a rename/reflink
BATCH=100

[[ -x "$PDH" ]] || { echo "patched proton-drive missing: proton-drive/update.sh" >&2; exit 1; }
mkdir -p "$WORK"; trap 'rm -rf "$WORK/dl"' EXIT

# Immich's expectations: checksum|path|checksum of the still (Live Photo videos only)
expected() {
  docker compose exec -T immich-db psql -U postgres -d immich -At -F'|' -c "
    SELECT encode(a.checksum, 'hex'), a.\"originalPath\", coalesce(encode(s.checksum, 'hex'), '')
    FROM asset a LEFT JOIN asset s ON s.\"livePhotoVideoId\" = a.id AND s.\"deletedAt\" IS NULL
    WHERE a.\"deletedAt\" IS NULL AND a.status = 'active'"
}

# Proton's side: sha1|node uid|linked video uids (comma-separated), one call for the timeline
proton_index() {
  "$PDH" photo timeline -d --json | jq -r '.[] | [.activeRevision.claimedDigests.sha1 // "",
    .uid, ((.photo.relatedPhotoNodeUids // []) | join(","))] | join("|")'
}

# Writes $WORK/plan: checksum|node uid|path… (all paths for that content); $WORK/missing likewise
make_plan() {
  expected >"$WORK/expected"
  proton_index >"$WORK/index"
  awk -F'|' '
    FILENAME == ARGV[1] { uid[$1] = $2; rel[$1] = $3; next }
    {
      sum = $1; path = $2; still = $3
      paths[sum] = (sum in paths) ? paths[sum] "|" path : path
      if (sum in uid) node[sum] = uid[sum]
      # A Live Photo video is found through its still; one linked video per still is the
      # iPhone case (more: the SHA-1 check after download catches a wrong pick)
      else if (still != "" && (still in rel) && rel[still] != "") { split(rel[still], r, ","); node[sum] = r[1] }
    }
    END {
      for (sum in paths) {
        if (sum in node) print sum "|" node[sum] "|" paths[sum] > (ARGV[3] "/plan")
        else print sum "|" paths[sum] > (ARGV[3] "/missing")
      }
    }' "$WORK/index" "$WORK/expected" "$WORK" 2>/dev/null
  touch "$WORK/plan" "$WORK/missing"
}

summary() {
  printf 'Immich expects %s file(s) (%s distinct contents). In Proton Photos: %s; not found: %s.\n' \
    "$(wc -l <"$WORK/expected")" "$(cut -d'|' -f1 "$WORK/expected" | sort -u | wc -l)" \
    "$(wc -l <"$WORK/plan")" "$(wc -l <"$WORK/missing")"
}

# fetch ROWS…: download the nodes of these plan rows into $WORK/dl, print "sha1 file" for each
fetch() {
  rm -rf "$WORK/dl"; mkdir -p "$WORK/dl"
  local uids=() row
  for row in "$@"; do uids+=("/photos/$(cut -d'|' -f2 <<<"$row")"); done
  "$PDH" photo download -c rename "${uids[@]}" "$WORK/dl" >/dev/null || true
  find "$WORK/dl" -type f -print0 | xargs -0 -r sha1sum
}

cmd=${1:-plan}
case "$cmd" in
  plan)
    make_plan; summary
    if [[ -s "$WORK/missing" ]]; then
      echo "Not in Proton Photos (first 20):"; cut -d'|' -f2 "$WORK/missing" | head -20 | sed 's|^/data/|  |'
    fi ;;

  verify)
    n=${2:-20}; make_plan; summary
    mapfile -t rows < <(shuf -n "$n" "$WORK/plan")
    ok=0
    while read -r sum file; do
      grep -q "^$sum|" <(printf '%s\n' "${rows[@]}") && ok=$((ok + 1))
    done < <(fetch "${rows[@]}")
    echo "verify: $ok of ${#rows[@]} random item(s) downloaded with the SHA-1 Immich expects."
    (( ok == ${#rows[@]} )) || exit 1 ;;

  restore)
    mountpoint -q "$SRC" || { echo "$SRC is not mounted" >&2; exit 1; }
    if docker compose ps --status running -q immich-server | grep -q .; then
      echo "Stop Immich first: docker compose stop immich-server immich-machine-learning" >&2; exit 1
    fi
    make_plan; summary
    # Only contents with at least one path still missing
    todo=()
    while IFS= read -r row; do
      IFS='|' read -r -a f <<<"$row"
      for p in "${f[@]:2}"; do [[ -f "$SRC/${p#/data/}" ]] || { todo+=("$row"); break; }; done
    done <"$WORK/plan"
    echo "To download: ${#todo[@]}"
    placed=0; bad=0
    for (( i = 0; i < ${#todo[@]}; i += BATCH )); do
      chunk=("${todo[@]:i:BATCH}")
      declare -A got=()
      while read -r sum file; do got[$sum]=$file; done < <(fetch "${chunk[@]}")
      for row in "${chunk[@]}"; do
        IFS='|' read -r -a f <<<"$row"; sum=${f[0]}
        [[ -n "${got[$sum]:-}" ]] || { bad=$((bad + 1)); continue; }   # missing or wrong SHA-1
        for p in "${f[@]:2}"; do
          t="$SRC/${p#/data/}"; [[ -f "$t" ]] && continue
          mkdir -p "${t%/*}"; cp --reflink=auto -p "${got[$sum]}" "$t.part" && mv "$t.part" "$t"
          placed=$((placed + 1))
        done
      done
      unset got
      echo "  $((i + ${#chunk[@]})) / ${#todo[@]}"
    done
    for d in library upload thumbs encoded-video profile backups; do mkdir -p "$SRC/$d"; touch "$SRC/$d/.immich"; done
    echo "restore: $placed file(s) placed, $bad content(s) not downloaded or SHA-1 mismatch, $(wc -l <"$WORK/missing") not in Proton."
    echo "Then: start Immich, Administration → Jobs → Generate thumbnails + Transcode videos (missing)."
    (( bad == 0 )) || exit 1 ;;

  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
