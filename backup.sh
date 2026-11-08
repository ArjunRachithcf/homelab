#!/usr/bin/env bash
# ~/homelab/backup.sh: encrypted backup (RUNBOOK.md step 9). Run by homelab-backup.service:
# hourly (homelab-backup.timer) and after every change (backup-watch.sh), or by hand: ./backup.sh
#
# Paperless export (originals + metadata) + Authentik and Immich DB dumps (zstd) + Jellyfin
# (users, plugins, watch state) + Caddy's certificates + the step 7d apps (Home Assistant, Matter
# fabric, AdGuard config, Navidrome, Audiobookshelf, Actual, Karakeep) + the step 7e apps (Kavita, OpenCloud's
# config and metadata without the files themselves, Sharry's shares without the uploaded files) + the step 7f family apps (their databases and
# uploads; not RomM's games) + .env + stack config, tarred and encrypted
# with age to BACKUP_AGE_RECIPIENT (a public key). The private key that decrypts it is NOT on this
# box (laptop + Proton Pass), so a stolen disk can't read it. Run by the unit, the tar also holds
# netbird/ and plex/ from backup-stage.sh (root); a run by hand leaves them out.
# Output: backups/homelab-YYYY-MM-DD-HHMM.tar.age. Kept: everything from the last 48 h, then the
# first archive of each day for 14 days. backup-photos.sh copies them to Proton Drive.
set -euo pipefail
cd "$(dirname "$0")"
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22   # throwaway helper: sqlite3 backup API, tar
umask 077
# One at a time (hourly timer, change watcher, by hand)
mkdir -p "$HOME/.local/state/homelab"; exec 9>"$HOME/.local/state/homelab/backup.lock"
flock -w 900 9 || { echo "another backup.sh is still running" >&2; exit 1; }

RECIPIENT=$(sed -n 's/^BACKUP_AGE_RECIPIENT=//p' .env | tail -n1 | sed "s/^'\(.*\)'$/\1/")
[[ "$RECIPIENT" == age1* ]] || { echo "BACKUP_AGE_RECIPIENT (age1…) missing in .env" >&2; exit 1; }
command -v age >/dev/null || { echo "age not installed (the age package: dnf, apt or pacman)" >&2; exit 1; }
command -v zstd >/dev/null || { echo "zstd not installed (ansible --tags base)" >&2; exit 1; }

install -d -m 700 backups
work=$(mktemp -d backups/.work-XXXXXX)
trap 'rm -rf "$work"' EXIT
out="backups/homelab-$(date +%F-%H%M).tar.age"

# shellcheck source=lib/components.sh
source lib/components.sh   # component_on: only the chosen components (COMPONENTS in .env)
core=()   # archive members besides the apps, in this order
if component_on paperless; then
  echo "Paperless export…"
  # --delete: only what the manifest names. Without it every old export name stayed (a title the AI or someone changed,
  # a deleted document): on one box 717 of 825 files, 593 MB of every hourly archive, found 2026-11-08
  docker compose exec -T paperless document_exporter ../export --delete >/dev/null
  chmod 700 export; chmod -R go-rwx export          # manifest holds users/password hashes
fi

echo "Authentik DB dump…"
docker compose exec -T authentik-db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | zstd -q -3 > "$work/authentik.sql.zst"
[[ $(zstd -dc "$work/authentik.sql.zst" | head -c 1M | wc -c) -gt 0 ]] || { echo "empty Authentik dump" >&2; exit 1; }
core+=(authentik.sql.zst)

if component_on photos; then
  echo "Immich DB dump…"
  docker compose exec -T immich-db sh -c 'pg_dump --clean --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB"' | zstd -q -3 > "$work/immich.sql.zst"
  [[ $(zstd -dc "$work/immich.sql.zst" | head -c 1M | wc -c) -gt 0 ]] || { echo "empty Immich dump" >&2; exit 1; }
  core+=(immich.sql.zst)
fi

# Volumes, read through throwaway containers (no network; the volume names follow the compose project)
vol() { echo "$(basename "$PWD")_$1"; }
if component_on jellyfin; then
echo "Jellyfin config…"
# jellyfin.db is SQLite in WAL mode: copied with SQLite's backup API (a reader needs its -shm, so the
# volume is mounted read-write, as Jellyfin's own user) and put back at data/jellyfin.db; log/ is
# left out. Python's tarfile, since BusyBox tar honors only one -C.
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$(vol jellyfin-config)":/c "$PY_IMAGE" python3 -c '
import os, sqlite3, sys, tarfile
dst = sqlite3.connect("/tmp/jellyfin.db")
sqlite3.connect("/c/data/jellyfin.db").backup(dst); dst.close()
skip = lambda ti: None if ti.name.startswith("data/jellyfin.db") else ti
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as t:
    for name in sorted(os.listdir("/c")):
        if name != "log": t.add("/c/" + name, arcname=name, filter=skip)
    t.add("/tmp/jellyfin.db", arcname="data/jellyfin.db")' > "$work/jellyfin-config.tar"
tar -tf "$work/jellyfin-config.tar" | grep -qx data/jellyfin.db || { echo "Jellyfin backup has no jellyfin.db" >&2; exit 1; }
core+=(jellyfin-config.tar)
fi  # jellyfin

echo "Caddy certificates…"
docker run --rm --network none -v "$(vol caddy-data)":/d:ro "$PY_IMAGE" tar -cf - -C /d . > "$work/caddy-data.tar"
core+=(caddy-data.tar)

# vol_tar VOLUME SKIP_REGEX [SQLITE…]: VOLUME as $work/VOLUME.tar, without paths matching SKIP_REGEX
# (caches, logs, derived data). Each SQLITE (path or glob in the volume) is copied with SQLite's backup API,
# as the file's owner so a -shm it creates stays the app's (WAL-safe while the app runs). A volume
# that doesn't exist yet (app not started) is skipped.
apps=()
vol_tar() {
  local name=$1 skip=$2; shift 2
  if ! docker volume inspect "$(vol "$name")" >/dev/null 2>&1; then echo "  $name: not created yet, skipped"; return 0; fi
  docker run --rm --network none -v "$(vol "$name")":/v "$PY_IMAGE" python3 -c '
import glob, os, re, sqlite3, sys, tarfile, tempfile
skip = re.compile(sys.argv[1])
# A SQLITE may be a pattern (Gramps: <tree id>/sqlite.db, FreshRSS: users/<name>/db.sqlite)
dbs = [rel for pat in sys.argv[2:] for rel in (sorted(p[3:] for p in glob.glob("/v/" + pat)) or [pat])]
def copy_db(rel):
    src = "/v/" + rel
    if not os.path.exists(src): return None
    dst = tempfile.mkdtemp() + "/db"; os.chmod(os.path.dirname(dst), 0o777)
    st = os.stat(src); os.setegid(st.st_gid); os.seteuid(st.st_uid)
    try:
        s, d = sqlite3.connect(src), sqlite3.connect(dst); s.backup(d); d.close(); s.close()
    finally:
        os.seteuid(0); os.setegid(0)
    return dst
copies = {rel: copy_db(rel) for rel in dbs}
db_files = {rel + x for rel in copies for x in ("", "-wal", "-shm", "-journal")}
flt = lambda ti: None if ti.name in db_files or skip.search(ti.name) else ti
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as t:
    for name in sorted(os.listdir("/v")):
        t.add("/v/" + name, arcname=name, filter=flt)
    for rel, dst in copies.items():
        if dst: t.add(dst, arcname=rel)' "$skip" "$@" > "$work/$name.tar"
  apps+=("$name.tar")
}
NONE='(?!)'   # skip nothing
echo "Apps (RUNBOOK.md 7d)…"
# HA: everything but the recorder history (large, hourly churn), its own backups and caches
vol_tar homeassistant-config '^(home-assistant_v2\.db|home-assistant\.log|backups|tts|deps|\.cache)' zigbee.db
vol_tar matter-data "$NONE"          # the Matter fabric: without it every device must be paired again
vol_tar adguard-conf "$NONE"         # adguard-work is query log + stats + filter lists: not kept
vol_tar navidrome-data '^cache' navidrome.db
vol_tar abs-config "$NONE" absdatabase.sqlite
vol_tar abs-metadata '^(cache|logs|streams|backups|downloads)'
vol_tar actual-data "$NONE" server-files/account.sqlite
vol_tar karakeep-data "$NONE" db.db
echo "Apps (RUNBOOK.md 7e)…"
vol_tar kavita-config '^(cache|logs|temp)' kavita.db
vol_tar opencloud-config "$NONE"      # opencloud.yaml: its secrets and keys
vol_tar termix-data "$NONE"           # hosts, the SSH key (encrypted), its own keys in .env
vol_tar ntfy-data '^cache\.db' user.db  # users, access, tokens (not the 72 h message cache)
vol_tar n8n-data '^(binaryData|n8nEventLog)' database.sqlite  # workflows, credentials (N8N_ENCRYPTION_KEY: .env)
# OpenCloud's data from the newest snapshot (files-local.sh, consistent): the user directory,
# spaces' metadata, NATS state. Not the files (storage/users: Proton + /srv/files-copy), not the
# derived search index and thumbnails.
# shellcheck disable=SC2010  # snapshot names are fixed date stamps
snap=$(ls -d "${NVME1_DIR:-/srv/nvme1}"/files-snap/20* 2>/dev/null | grep -v '\.part$' | tail -n1 || true)
if [[ -n "$snap" ]]; then
  tar -cf "$work/opencloud-data.tar" -C "$snap" --exclude=./storage/users --exclude=./search --exclude=./thumbnails .
  apps+=("opencloud-data.tar")
else
  echo "  opencloud-data: no snapshot yet, skipped"
fi
# pg_app NAME DB_SERVICE: that app's Postgres as $work/NAME.sql.zst (skipped while it isn't running)
pg_app() {
  local name=$1 svc=$2
  if [[ -z "$(docker compose ps -q --status running "$svc" 2>/dev/null)" ]]; then echo "  $name: not running, skipped"; return 0; fi
  docker compose exec -T "$svc" sh -c 'pg_dump --clean --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB"' | zstd -q -3 > "$work/$name.sql.zst"
  [[ $(zstd -dc "$work/$name.sql.zst" | head -c 1M | wc -c) -gt 0 ]] || { echo "empty $name dump" >&2; exit 1; }
  apps+=("$name.sql.zst")
}
# Sharry's shares and accounts; the uploaded files (volume sharry-files) are left out: ≤ 30 days, the sender has them
pg_app sharry sharry-db
# The family apps (RUNBOOK.md 7f), each only when its component is chosen
family=(chores things family-tree notes wiki news games places)
for c in "${family[@]}"; do component_on "$c" && { echo "Apps (RUNBOOK.md 7f)…"; break; }; done
component_on chores && vol_tar donetick-data "$NONE" donetick.db
component_on things && vol_tar homebox-data "$NONE" homebox.db      # also the attachments (receipts, manuals, photos)
if component_on family-tree; then
  vol_tar gramps-users "$NONE" users.sqlite
  vol_tar gramps-tree "$NONE" '*/sqlite.db'
  vol_tar gramps-media "$NONE"
fi
component_on notes && vol_tar memos-data "$NONE" memos_prod.db
if component_on wiki; then
  vol_tar outline-data "$NONE"                 # uploaded images and files
  pg_app outline outline-db
fi
component_on news && vol_tar freshrss-data '^(cache|favicons)' 'users/*/db.sqlite'
if component_on games; then
  vol_tar romm-assets "$NONE"                  # saves, states, uploads (the games themselves are media, not backed up)
  vol_tar romm-config "$NONE"
  if [[ -n "$(docker compose ps -q --status running romm-db 2>/dev/null)" ]]; then
    docker compose exec -T romm-db sh -c 'mariadb-dump --single-transaction -u root -p"$MARIADB_ROOT_PASSWORD" "$MARIADB_DATABASE"' | zstd -q -3 > "$work/romm.sql.zst"
    [[ $(zstd -dc "$work/romm.sql.zst" | head -c 1M | wc -c) -gt 0 ]] || { echo "empty romm dump" >&2; exit 1; }
    apps+=("romm.sql.zst")
  else
    echo "  romm: not running, skipped"
  fi
fi
if component_on places; then
  vol_tar dawarich-storage "$NONE"             # imported files, exports
  pg_app dawarich dawarich-db
fi
# Downloads on request (opt-in, so only when listed): the apps' settings and libraries, not the downloads (media)
# nor their logs and own backups. Their API keys and logins are in .env.
if component_listed downloads; then
  echo "Downloads…"
  vol_tar qbittorrent-config '^qBittorrent/(logs|data/logs|cache)'   # settings, categories, resume data
  vol_tar prowlarr-config '^(logs|logs\.db|Backups|asp)' prowlarr.db
fi
if component_listed movies-tv; then
  vol_tar radarr-config '^(logs|logs\.db|Backups|MediaCover|asp)' radarr.db
  vol_tar sonarr-config '^(logs|logs\.db|Backups|MediaCover|asp)' sonarr.db
fi
component_listed albums && vol_tar lidarr-config '^(logs|logs\.db|Backups|MediaCover|asp|Sentry)' lidarr.db
component_listed subtitles && vol_tar bazarr-config '^(log|cache|backup|restore)' db/bazarr.db
component_listed book-requests && vol_tar bindery-config "$NONE" bindery.db
component_listed requests && vol_tar seerr-config '^(logs|cache)' db/db.sqlite3   # also settings.json: its settings

# Root-only files from backup-stage.sh (only when run by homelab-backup.service)
staged=()
for d in netbird plex; do [[ -d "${RUNTIME_DIRECTORY:-/nonexistent}/$d" ]] && staged+=("$d"); done
(( ${#staged[@]} )) || echo "Run by hand: no netbird/ or plex/ in this archive (they come from homelab-backup.service)."

echo "Encrypting → $out"
config=(.env docker-compose.yml Caddyfile caddy site authentik ssh immich proton-drive homeassistant sharry memos)
component_on paperless && config=(export "${config[@]}")
profile=()   # Immich's profile pictures (not in Proton Photos)
component_on photos && profile=(-C "$(sed -n 's/^IMMICH_UPLOAD_DIR=//p' .env | tail -n1)" profile)
tar -cf - -C "$work" "${core[@]}" "${apps[@]}" \
  -C "$PWD" "${config[@]}" \
  "${profile[@]}" \
  ${staged[@]:+-C "$RUNTIME_DIRECTORY" "${staged[@]}"} \
  | age -r "$RECIPIENT" -o "$out.part"
mv "$out.part" "$out"

# Retention: all of the last 48 h, then the first archive of each day for 14 days
./backup-retention.sh 48 14 < <(cd backups && ls homelab-*.tar.age) | sed 's|^|backups/|' | xargs -r rm -f --
echo "Done: $out ($(du -h "$out" | cut -f1)). $(ls backups/homelab-*.tar.age | wc -l) backup(s) kept."
