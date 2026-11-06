#!/usr/bin/env bash
# ~/homelab/homeassistant/setup.sh: Home Assistant's first start without ever listening on the LAN
# (RUNBOOK.md step 7d). HA runs on the host network (Matter), so by default it would answer on
# every interface at :8123.
#
#   ./homeassistant/setup.sh init        before the first start: seed configuration.yaml with an http
#                                        block (listen only on the compose bridge gateway 172.21.0.1,
#                                        trust Caddy at 172.21.1.11), and the empty include files
#   ./homeassistant/setup.sh promote     right after the first start: HA imports the block only as a
#                                        5-minute trial ("pending") that reverts to all interfaces unless
#                                        confirmed in its UI; this confirms it in .storage/http (HA stopped)
#   ./homeassistant/setup.sh drop-http   once HA has imported it (since 2026.8 these settings live in
#                                        Settings → System → Network; HA raises a repair until the
#                                        block is gone): remove the block and restart HA
#   ./homeassistant/setup.sh status      where HA and the Matter server listen
set -euo pipefail
cd "$(dirname "$0")/.."

GW=172.21.0.1       # Caddyfile (homeassistant) and docker-compose.yml expect exactly this
CADDY=172.21.1.11   # docker-compose.yml: caddy ipv4_address

# ha_sh SCRIPT [<stdin]: run a shell in a throwaway HA container on its config volume, nothing else started
ha_sh() { docker compose run --rm --no-deps -T --entrypoint /bin/sh homeassistant -c "$1"; }

gateway_ok() {
  local gw
  gw=$(docker network inspect homelab_internal -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  [[ "$gw" == "$GW" ]] || { echo "✗ homelab_internal gateway is '${gw:-?}', not $GW (is the stack up?)" >&2; return 1; }
}

init() {
  gateway_ok
  if [[ -n "$(docker compose ps -a -q homeassistant 2>/dev/null)" ]]; then
    echo "✗ the homeassistant container exists already; init is for before its first start" >&2; exit 1
  fi
  if ha_sh 'test -e /config/configuration.yaml'; then
    echo "· /config/configuration.yaml exists already: left alone"; return 0
  fi
  ha_sh 'cat > /config/configuration.yaml && cd /config && echo "[]" > automations.yaml && touch scripts.yaml scenes.yaml' <<EOF
# Seeded by homeassistant/setup.sh init (RUNBOOK.md 7d). Edit HA in its UI; this file only loads
# the defaults and the UI-made automations, scripts and scenes.
default_config:

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml

# >>> homelab http: imported into Settings → System → Network at the first start, then removed by
# ./homeassistant/setup.sh drop-http. Never on the LAN: only the compose bridge, where Caddy is.
http:
  server_host:
    - $GW
  use_x_forwarded_for: true
  trusted_proxies:
    - $CADDY
# <<< homelab http
EOF
  echo "✓ configuration.yaml seeded (listens on $GW:8123 only, trusts $CADDY)"
  echo "  next: docker compose up -d matter-server homeassistant && ./homeassistant/setup.sh status"
}

drop_http() {
  ha_sh 'grep -q "^# >>> homelab http" /config/configuration.yaml' || { echo "· no http block left"; return 0; }
  ha_sh 'sed -i "/^# >>> homelab http/,/^# <<< homelab http/d" /config/configuration.yaml'
  docker compose restart homeassistant >/dev/null
  echo "✓ http block removed, HA restarted. Check Settings → System → Network → HTTP server still shows"
  echo "  listen address $GW, Trust X-Forwarded-For on, trusted proxy $CADDY; then: ./homeassistant/setup.sh status"
}

status() {
  echo "Home Assistant (8123):"; ss -tlnH 'sport = :8123' | awk '{print "  " $4}'
  echo "Matter server (5580):";  ss -tlnH 'sport = :5580' | awk '{print "  " $4}'
  local where; where=$(ss -tlnH 'sport = :8123' | awk '{print $4}')
  [[ -n "$where" ]] || { echo "✗ HA isn't listening (yet?): docker compose logs homeassistant" >&2; return 1; }
  if grep -qvE "^$GW:8123$" <<<"$where"; then
    echo "✗ HA listens beyond $GW: fix Settings → System → Network → HTTP server → Listen addresses" >&2; return 1
  fi
  # A pending trial listens right but reverts to every interface after 5 min
  if docker compose exec -T homeassistant python3 -c 'import json,sys; sys.exit(bool(json.load(open("/config/.storage/http"))["data"].get("pending")))'; then :
  else echo "✗ HA's HTTP config is only a 5-minute trial: ./homeassistant/setup.sh promote" >&2; return 1; fi
}

# Pending → stable in .storage/http, the same as confirming it in the UI (HA 2026.10's store: keys
# stable/pending/yaml_migration_done, pending must stay present, null = none).
promote() {
  ha_sh 'test -e /config/.storage/http' || { echo "✗ no /config/.storage/http yet: start HA once first" >&2; return 1; }
  docker compose stop homeassistant >/dev/null
  docker compose run --rm --no-deps -T --entrypoint python3 homeassistant - "$GW" "$CADDY" <<'PY'
import json, sys
gw, caddy = sys.argv[1], sys.argv[2]
p = "/config/.storage/http"
d = json.load(open(p)); data = d["data"]
if data.get("pending"):
    data["stable"] = {**data["pending"], "error": None, "error_message": None}
data["stable"].update(server_host=[gw], use_x_forwarded_for=True, trusted_proxies=[caddy + "/32"])
data["pending"] = None
json.dump(d, open(p, "w"), indent=2)
print("OUT:✓ stable HTTP config: listen", gw, "trust", caddy)
PY
  docker compose start homeassistant >/dev/null
  echo "  HA restarting; then: ./homeassistant/setup.sh status"
}

case "${1:-}" in
  init) init ;;
  promote) promote ;;
  drop-http) drop_http ;;
  status) status ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
