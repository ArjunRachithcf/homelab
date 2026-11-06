#!/usr/bin/env bash
# ~/homelab/backup-watch.sh: back up soon after any real change (RUNBOOK.md step 9).
# Run every 5 min by homelab-backup-watch.timer; `./backup-watch.sh fingerprint` shows what it watches.
#
# A fingerprint of what matters, from cheap read-only queries: Immich assets/albums/people,
# Paperless documents/notes/tags/users, Authentik users/groups/MFA devices, and .env. When it
# changes, the watcher waits until things have been quiet for QUIET (phones syncing, Immich's ML
# jobs), at most MAX_WAIT after the first change, and no sooner than MIN_GAP after its last push.
# Then it starts the same units the timers use, so every archive is made the same way:
#   homelab-backup.service        backup.sh (+ root-only files)          also hourly
#   homelab-photos-copy.service   photos-local.sh copy (second disk)     also hourly
#   homelab-offbox.service        backup-photos.sh quick (Proton)        also every 4 h
# (a polkit rule from ansible/roles/timers lets this user start exactly these three).
# Skips 02:00–05:30, when the nightly jobs run anyway.
set -euo pipefail
cd "$(dirname "$0")"
QUIET=600 MAX_WAIT=1800 MIN_GAP=1800
STATE=${STATE_DIRECTORY:-$HOME/.local/state/homelab}; mkdir -p "$STATE"
STATEFILE=$STATE/backup-watch.state

# shellcheck source=lib/components.sh
source lib/components.sh   # component_on: only the chosen components (COMPONENTS in .env)

psql_in() {  # psql_in SERVICE SQL: one line of output, from the service's own DB user
  docker compose exec -T "$1" sh -c 'psql -X -q -At -F" " -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<<"$2"
}

fingerprint() {  # fails (and so does the unit) if any source can't be read
  local immich=off paperless=off authentik
  component_on photos && immich=$(psql_in immich-db "
    SELECT count(*), max(\"updatedAt\"), max(\"deletedAt\") FROM asset;
    SELECT count(*), max(\"updatedAt\") FROM album;
    SELECT count(*) FROM album_asset;
    SELECT count(*), max(\"updatedAt\") FROM person;" | paste -sd' ')
  component_on paperless && paperless=$(docker compose exec -T paperless python3 -c "
import sqlite3
c = sqlite3.connect('file:/usr/src/paperless/data/db.sqlite3?mode=ro', uri=True)
for q in ('SELECT count(*), max(modified) FROM documents_document',
          'SELECT count(*) FROM documents_note', 'SELECT count(*) FROM documents_tag',
          'SELECT count(*) FROM documents_document_tags', 'SELECT count(*) FROM auth_user'):
    print(*c.execute(q).fetchone())" | paste -sd' ')
  authentik=$(psql_in authentik-db "
    SELECT count(*), max(last_updated) FROM authentik_core_user;
    SELECT count(*) FROM authentik_core_user_groups;
    SELECT count(*) FROM authentik_core_group;
    SELECT (SELECT count(*) FROM authentik_stages_authenticator_totp_totpdevice)
         + (SELECT count(*) FROM authentik_stages_authenticator_webauthn_webauthndevice)
         + (SELECT count(*) FROM authentik_stages_authenticator_static_statictoken);" | paste -sd' ')
  [[ -n $immich && -n $paperless && -n $authentik ]] || { echo "fingerprint: a source returned nothing" >&2; return 1; }
  printf 'immich %s\npaperless %s\nauthentik %s\nenv %s\n' "$immich" "$paperless" "$authentik" "$(sha256sum .env | cut -c1-16)"
}

if [[ ${1:-} == fingerprint ]]; then fingerprint; exit 0; fi

hm=$(date +%H%M)
if (( 10#$hm >= 200 && 10#$hm < 530 )); then exit 0; fi

now=$(date +%s)
current=$(fingerprint)
fp=$(sha256sum <<<"$current" | cut -c1-16)
backed="" seen="" first=0 quiet_since=0 last_push=0 seen_lines=""
# shellcheck disable=SC1090
[[ -f $STATEFILE ]] && source "$STATEFILE"
save() {
  { printf 'backed=%q\nseen=%q\nfirst=%q\nquiet_since=%q\nlast_push=%q\n' \
      "$backed" "$fp" "$first" "$quiet_since" "$last_push"
    printf 'seen_lines=%q\n' "$current"; } >"$STATEFILE.new"
  mv "$STATEFILE.new" "$STATEFILE"
}

if [[ $fp == "$backed" ]]; then first=0; save; exit 0; fi
if [[ $fp != "$seen" ]]; then
  changed=$(comm -13 <(sort <<<"$seen_lines") <(sort <<<"$current") | cut -d' ' -f1 | paste -sd' ')
  echo "change: ${changed:-first run}"
  quiet_since=$now; (( first )) || first=$now
fi
if (( now - quiet_since < QUIET && now - first < MAX_WAIT )) || (( now - last_push < MIN_GAP )); then
  save; exit 0
fi

echo "backing up (first change $(( (now - first) / 60 )) min ago)…"
save   # a failed push leaves backed as it was: the next run tries again
units=(homelab-backup)
# The second-disk copy is Linux only (jobs.toml: mountpoint, /srv paths)
component_on photos && command -v systemctl >/dev/null && units+=(homelab-photos-copy)
component_on offsite && units+=(homelab-offbox)
for unit in "${units[@]}"; do
  if command -v systemctl >/dev/null; then
    systemctl start --no-ask-password "$unit.service"
  else  # macOS: the same job, in turn like systemctl start does (lib/platform/launchd-run.sh)
    bash lib/platform/launchd-run.sh "${unit#homelab-}"
  fi
done
backed=$fp first=0 last_push=$(date +%s)
save
echo "done: archive + second disk + Proton"
