#!/usr/bin/env bash
# downloads/guard.sh (homelab-downloads-guard.timer, every 10 min; component downloads):
#  1. qBittorrent, Prowlarr and Byparr live in gluetun's network namespace. A restarted or recreated gluetun is a new
#     one, and theirs keeps only lo: they're recreated when gluetun started after them or their healthcheck says so.
#  2. The media folder's disk below DOWNLOADS_KEEP_FREE_PCT free: every running torrent stopped, 5 points above it
#     again: started (downloads/setup.py guard). Fails, so the unit mails, only when it has just stopped them, or
#     can't while the disk is low: a broken API on a roomy disk is a line in the log, not a mail every 10 minutes.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
ENV_FILE=${ENV_FILE:-.env}
# shellcheck source=../lib/components.sh
source lib/components.sh   # component_listed
component_listed downloads || exit 0
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'; }

# when the service's container started (seconds since the epoch), empty when it isn't running
started() {
  local id t
  id=$(docker compose ps -q --status running "$1" 2>/dev/null) || return 0
  [[ -n $id ]] || return 0
  t=$(docker inspect -f '{{.State.StartedAt}}' "$id") && date -d "$t" +%s.%N
}
health() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$(docker compose ps -q "$1")" 2>/dev/null || true; }

rc=0
g=$(started gluetun)
if [[ -n $g ]]; then
  stale=()
  for s in qbittorrent prowlarr byparr; do
    t=$(started "$s")
    [[ -n $t ]] || continue   # not running: nothing to bring back
    if awk -v a="$t" -v b="$g" 'BEGIN { exit !(a < b) }' || [[ $(health "$s") == unhealthy ]]; then stale+=("$s"); fi
  done
  if ((${#stale[@]})); then
    if docker compose up -d --force-recreate --no-deps "${stale[@]}" >/dev/null; then
      echo "· ${stale[*]}: were in gluetun's old network namespace, recreated"
    else
      echo "✗ ${stale[*]}: in gluetun's old network namespace, and recreating failed" >&2; rc=1
    fi
  fi
fi

[[ -n $(started qbittorrent) ]] || exit "$rc"   # stopped on purpose: no torrents to look after
media=$(env_get MEDIA_DIR); media=${media:-/srv/media}
free=$(df -P "$media" | awk 'NR == 2 { printf "%.1f", 100 * $4 / $2 }')
keep=$(env_get DOWNLOADS_KEEP_FREE_PCT); keep=${keep:-15}
code=0; out=$(bash downloads/run.sh guard "$free" 2>&1) || code=$?
[[ -z $out ]] || echo "$out"
case $code in
  0) ;;
  3) rc=1 ;;   # stopped them just now: the mail says why
  *) if awk -v f="$free" -v k="$keep" 'BEGIN { exit !(f < k) }'; then
       echo "✗ the disk has $free % free (below $keep %), and the torrents couldn't be stopped" >&2; rc=1
     else
       echo "· qBittorrent didn't answer the guard ($free % free, no need to stop anything)"
     fi ;;
esac
exit "$rc"
