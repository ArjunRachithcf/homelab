#!/usr/bin/env bash
# downloads/run.sh CMD [ARG]: downloads/setup.py (init, apply, status, unmonitor-new-seasons, guard FREE_PCT, fakes,
# bindery-init, seerr-init, seerr-users) in a throwaway container: the stack's python image, on this compose
# project's downloads network (and Seerr's, requests, when it's chosen), with its volumes. Not a compose service:
# heartbeat.sh and the console flag every compose container that isn't running. Secrets go to the container by name
# (-e KEY, the value in this script's environment), never on a command line: the .env's, and from the caller's
# environment SEERR_ADMINS, SEERR_FAMILY, SEERR_KIDS (seerr-users) and JELLYFIN_URL (tests only). The caller's
# JELLYFIN_ADMIN_PASSWORD (seerr-init, typed once, never stored) goes on stdin: in no container's environment.
# The project (`name:` in docker-compose.yml, or COMPOSE_PROJECT_NAME / -p) decides which networks and volumes: a test
# under another project name never reaches the live ones.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22   # backup.sh's
ENV_FILE=${ENV_FILE:-.env}
# shellcheck source=../lib/components.sh
source lib/components.sh   # component_listed
cmd=${1:-}
case $cmd in
  init|apply|status|unmonitor-new-seasons|bindery-init|seerr-init|seerr-users|fakes) [[ $# == 1 ]] || cmd="" ;;
  guard) [[ $# == 2 ]] || cmd="" ;;
  *) cmd="" ;;
esac
usage="init|apply|status|unmonitor-new-seasons|guard FREE_PCT|fakes|bindery-init|seerr-init|seerr-users"
[[ -n $cmd ]] || { echo "usage: downloads/run.sh $usage" >&2; exit 2; }

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed -e 's/\r$//' -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'; }
project=$(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')
# From .env: the instance, each app's API key and password, the choices
from_env=(COMPONENTS ADMIN_USER DOMAIN NETBIRD_PROXY_PORT BRAND DOWNLOADS_NET MEDIA_DIR AUDIOBOOKS_DIR JELLYFIN_ADMIN_USER
          QBITTORRENT_API_KEY QBITTORRENT_PASSWORD PROWLARR_API_KEY PROWLARR_PASSWORD RADARR_API_KEY RADARR_PASSWORD
          SONARR_API_KEY SONARR_PASSWORD LIDARR_API_KEY LIDARR_PASSWORD BAZARR_API_KEY BAZARR_AUTH BINDERY_API_KEY
          BINDERY_OIDC_CLIENT_SECRET SEERR_API_KEY SEERR_NTFY_TOKEN GLUETUN_CONTROL_KEY
          DOWNLOADS_SEED_RATIO DOWNLOADS_SEED_DAYS DOWNLOADS_KEEP_FREE_PCT LIDARR_QUALITY_PROFILE BAZARR_LANGUAGES
          SEERR_MOVIE_QUOTA SEERR_TV_QUOTA SEERR_QUOTA_DAYS SEERR_NTFY_TOPIC)
# From the caller, never from .env
from_caller=(SEERR_ADMINS SEERR_FAMILY SEERR_KIDS JELLYFIN_URL)
for k in "${from_env[@]}"; do printf -v "$k" '%s' "$(env_get "$k")"; done
HOST_UID=$(env_get HOST_UID); HOST_UID=${HOST_UID:-$(id -u)}
HOST_GID=$(env_get HOST_GID); HOST_GID=${HOST_GID:-$(id -g)}
DOWNLOADS_NET=${DOWNLOADS_NET:-198.51.100}
pass=()
for k in HOST_UID HOST_GID "${from_env[@]}" "${from_caller[@]}"; do
  if [[ -n ${!k:-} ]]; then export "${k?}"; pass+=(-e "$k"); fi
done
common=(--cap-drop ALL --security-opt no-new-privileges --label homelab.downloads-run="$cmd"
        -v "$PWD/downloads/setup.py:/app/setup.py:ro,z" "${pass[@]}")

if [[ $cmd == init ]]; then
  # root, but only to hand the fresh volumes and folders to HOST_UID: no network
  vols=()
  for v in qbittorrent-config prowlarr-config radarr-config sonarr-config recyclarr-config lidarr-config bazarr-config \
           bindery-config seerr-config; do
    # only what compose made (docker compose create): a volume made here would lack compose's labels
    docker volume inspect "${project}_$v" >/dev/null 2>&1 && vols+=(-v "${project}_$v:/vol/$v")
  done
  media=${MEDIA_DIR:-/srv/media}
  exec docker run --rm "${common[@]}" --network none --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    "${vols[@]}" -v "$media:/data:z" "$PY_IMAGE" python3 -I /app/setup.py init
fi
# Seerr is on its own network: only the commands that reach it join it
net="${project}_arr"
[[ $cmd == seerr-* ]] && net="${project}_requests"
how=(--rm)
if [[ $cmd == seerr-init && -n ${JELLYFIN_ADMIN_PASSWORD:-} ]]; then
  # its stdin open; not --rm: that races docker start -ai for the exit code ("No such container"), removed below
  how=(-i -e JELLYFIN_ADMIN_PASSWORD_STDIN=1)
fi
id=$(docker create "${how[@]}" "${common[@]}" --network "$net" --user "$HOST_UID:$HOST_GID" --read-only \
  "$PY_IMAGE" python3 -I /app/setup.py "$@")
if [[ $cmd == apply || $cmd == status ]] && component_listed requests \
    && ! docker network connect "${project}_requests" "$id"; then
  docker rm -f "$id" >/dev/null   # never started: --rm wouldn't take it, with every key in its environment
  exit 1
fi
if [[ ${how[0]} == -i ]]; then
  trap 'docker rm -f "$id" >/dev/null 2>&1' EXIT
  printf '%s' "$JELLYFIN_ADMIN_PASSWORD" | docker start -ai "$id"
  exit   # docker start's status (pipefail), the container removed on the way out
fi
exec docker start -a "$id"
