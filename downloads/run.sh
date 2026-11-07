#!/usr/bin/env bash
# downloads/run.sh CMD [ARG]: downloads/setup.py (init, apply, status, unmonitor-new-seasons, guard FREE_PCT) in a
# throwaway container: the stack's python image, on this compose project's downloads network, with its volumes.
# Not a compose service: heartbeat.sh and the console flag every compose container that isn't running. Secrets go to
# the container by name (-e KEY, the value in this script's environment), never on a command line.
# The project (`name:` in docker-compose.yml, or COMPOSE_PROJECT_NAME / -p) decides which network and volumes: a test
# under another project name never reaches the live ones.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22   # backup.sh's
ENV_FILE=${ENV_FILE:-.env}
# shellcheck source=../lib/components.sh
source lib/components.sh   # component_listed
cmd=${1:-}
case $cmd in
  init|apply|status|unmonitor-new-seasons) [[ $# == 1 ]] || cmd="" ;;
  guard) [[ $# == 2 ]] || cmd="" ;;
  *) cmd="" ;;
esac
[[ -n $cmd ]] || { echo "usage: downloads/run.sh init|apply|status|unmonitor-new-seasons|guard FREE_PCT" >&2; exit 2; }

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'; }
project=$(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')
export HOST_UID HOST_GID ADMIN_USER DOWNLOADS_ADMIN_PASSWORD QBITTORRENT_API_KEY PROWLARR_API_KEY RADARR_API_KEY \
  SONARR_API_KEY GLUETUN_CONTROL_KEY DOWNLOADS_SEED_RATIO DOWNLOADS_SEED_DAYS DOWNLOADS_KEEP_FREE_PCT MOVIES_TV
HOST_UID=$(env_get HOST_UID); HOST_UID=${HOST_UID:-$(id -u)}
HOST_GID=$(env_get HOST_GID); HOST_GID=${HOST_GID:-$(id -g)}
for k in ADMIN_USER DOWNLOADS_ADMIN_PASSWORD QBITTORRENT_API_KEY PROWLARR_API_KEY RADARR_API_KEY SONARR_API_KEY \
         GLUETUN_CONTROL_KEY DOWNLOADS_SEED_RATIO DOWNLOADS_SEED_DAYS DOWNLOADS_KEEP_FREE_PCT; do
  printf -v "$k" '%s' "$(env_get "$k")"
done
MOVIES_TV=0; component_listed movies-tv && MOVIES_TV=1
pass=(); for k in HOST_UID HOST_GID ADMIN_USER DOWNLOADS_ADMIN_PASSWORD QBITTORRENT_API_KEY PROWLARR_API_KEY \
                  RADARR_API_KEY SONARR_API_KEY GLUETUN_CONTROL_KEY DOWNLOADS_SEED_RATIO DOWNLOADS_SEED_DAYS \
                  DOWNLOADS_KEEP_FREE_PCT MOVIES_TV; do
  [[ -n ${!k} ]] && pass+=(-e "$k")
done
common=(--rm --cap-drop ALL --security-opt no-new-privileges --label homelab.downloads-run="$cmd"
        -v "$PWD/downloads/setup.py:/app/setup.py:ro,z" "${pass[@]}")

if [[ $cmd == init ]]; then
  # root, but only to hand the fresh volumes and folders to HOST_UID: no network
  vols=()
  for v in qbittorrent-config prowlarr-config radarr-config sonarr-config recyclarr-config; do
    # only what compose made (docker compose create): a volume made here would lack compose's labels
    docker volume inspect "${project}_$v" >/dev/null 2>&1 && vols+=(-v "${project}_$v:/vol/$v")
  done
  media=$(env_get MEDIA_DIR); media=${media:-/srv/media}
  exec docker run "${common[@]}" --network none --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    "${vols[@]}" -v "$media:/data:z" "$PY_IMAGE" python3 -I /app/setup.py init
fi
exec docker run "${common[@]}" --network "${project}_downloads" --user "$HOST_UID:$HOST_GID" --read-only \
  "$PY_IMAGE" python3 -I /app/setup.py "$@"
