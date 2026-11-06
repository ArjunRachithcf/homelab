#!/usr/bin/env bash
# ~/homelab/heartbeat.sh: the outside view (RUNBOOK.md step 7d). Run by homelab-heartbeat.timer every
# 5 min. Checks this box, then pings HEARTBEAT_URL (a healthchecks.io check): success → <url>,
# anything wrong → <url>/fail with the list in the body. healthchecks.io mails you on a /fail
# AND when pings stop (PC off, no internet, NetBird or Docker dead), which nothing on this box
# can report itself. Read-only; HEARTBEAT_URL empty = off.
# Also writes site/status/heartbeat.json for the home page (RUNBOOK.md 7e): this run's result,
# backup age, free disk space, and healthchecks.io's own view of the check (read-only API key
# HEARTBEAT_API_KEY_RO; empty = left out), then runs ops-report.py for the detail sheets
# and console-report.py for the console.
#   ./heartbeat.sh            check and ping
#   ./heartbeat.sh --dry-run  check and print, no ping (the status file is still written)
set -uo pipefail
cd "$(dirname "$0")" || exit 1

env_get() { sed -n "s/^$1=//p" .env | tail -n1 | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'; }
URL=$(env_get HEARTBEAT_URL)
API_KEY=$(env_get HEARTBEAT_API_KEY_RO)
DOMAIN=$(env_get DOMAIN)
NB_IP=$(env_get NETBIRD_IP)
PORT=$(env_get NETBIRD_PROXY_PORT); PORT=${PORT:-9443}
PHOTOS=$(env_get IMMICH_UPLOAD_DIR); PHOTOS=${PHOTOS:-/srv/photos}
dry=0; [[ ${1:-} == --dry-run ]] && dry=1
[[ -n "$URL" || $dry == 1 ]] || { echo "HEARTBEAT_URL empty in .env: heartbeat off"; exit 0; }

problems=()
bad() { problems+=("$1"); }

# NetBird up with our address (Caddy and sshd listen only there)
ip -4 -o addr show wt0 2>/dev/null | grep -qw "$NB_IP" || bad "wt0 doesn't have $NB_IP (NetBird down?)"

# Every compose container running, none unhealthy
while IFS=$'\t' read -r svc state health; do
  [[ "$state" == running ]] || bad "container $svc is $state"
  [[ "$health" == unhealthy ]] && bad "container $svc is unhealthy"
done < <(docker compose ps -a --format '{{.Service}}\t{{.State}}\t{{.Health}}' 2>/dev/null)
[[ -n "$(docker compose ps -q 2>/dev/null)" ]] || bad "docker compose ps: no containers (Docker down?)"

# The public path end to end: public DNS → NetBird cluster → TLS passthrough → Caddy :8444 →
# Authentik forward-auth (302 to the login). Resolved via Quad9, since this peer's own DNS
# answers with the NetBird IP. Two tries: a single slow handshake isn't worth a mail.
public_ok() {
  local ip code
  ip=$(dig +short "home.$DOMAIN" @9.9.9.9 2>/dev/null | grep -E '^[0-9.]+$' | head -n1)
  [[ -n "$ip" ]] || return 1
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' --resolve "home.$DOMAIN:$PORT:$ip" "https://home.$DOMAIN:$PORT/")
  [[ "$code" == 302 ]]
}
public_ok || { sleep 20; public_ok; } || bad "https://home.$DOMAIN:$PORT not reachable from the internet (NetBird proxy / Caddy)"

# The LAN closed as the firewall role left it (once it has run here): default zone, wt0, mesh guard
# shellcheck source=lib/firewall.sh
source lib/firewall.sh
if lan_firewall_here; then
  while read -r p; do [[ -n $p ]] && bad "LAN: $p"; done < <(lan_problems)
fi

# Files rendered from .env (mail templates, logos): missing or stale after a pull or an .env change
./homelab configure --check >/dev/null 2>&1 || bad "rendered files out of date: run ./homelab configure"

# Backups: hourly, so the newest archive is never older than ~2 h
newest=$(ls -t backups/homelab-*.tar.age 2>/dev/null | head -n1)
if [[ -z "$newest" ]]; then bad "no backup archive in backups/"
elif (( $(date +%s) - $(stat -c %Y "$newest") > 3 * 3600 )); then bad "newest backup is older than 3 h ($(basename "$newest"))"
fi
backup_time=$([[ -n "$newest" ]] && stat -c %Y "$newest" || echo 0)

# Disks: a full disk breaks everything quietly
# shellcheck source=lib/components.sh
source lib/components.sh   # component_on: only the chosen components (COMPONENTS in .env)
mounts=(/); component_on photos && mounts+=("$PHOTOS")
for mnt in "${mounts[@]}"; do
  use=$(df --output=pcent "$mnt" 2>/dev/null | tail -n1 | tr -dc 0-9)
  [[ -n "$use" ]] || { bad "$mnt not mounted"; continue; }
  (( use < 90 )) || bad "$mnt is ${use}% full"
done
! component_on photos || mountpoint -q "$PHOTOS" || bad "$PHOTOS is not mounted"

if (( ${#problems[@]} )); then
  body=$(printf '%s\n' "${problems[@]}")
  printf 'FAIL\n%s\n' "$body"
  (( dry )) || curl -fsS -m 15 --retry 3 -o /dev/null --data-raw "$body" "$URL/fail"
else
  echo "OK"
  (( dry )) || curl -fsS -m 15 --retry 3 -o /dev/null "$URL"
fi

# The home page's status (site/status/heartbeat.json), after the ping so healthchecks.io's view
# includes it. Written to a temp file and renamed: Caddy never reads half a file.
outside=""
if [[ -n "$API_KEY" ]]; then
  outside=$(curl -fsS -m 15 -H "X-Api-Key: $API_KEY" https://healthchecks.io/api/v3/checks/ 2>/dev/null) || outside=""
fi
mkdir -p site/status
tmp=$(mktemp site/status/.heartbeat-XXXXXX)
OUTSIDE="$outside" BACKUP_TIME="$backup_time" PHOTOS="$PHOTOS" python3 - "${problems[@]}" > "$tmp" <<'PY'
import json, os, shutil, sys, time
checks = []
try:
    checks = json.loads(os.environ["OUTSIDE"] or "{}").get("checks", [])
except ValueError:
    pass
# One check per box; with several, the one named "homelab" (or the first)
check = next((c for c in checks if c.get("name") == "homelab"), checks[0] if checks else None)
disks = []
for label, mnt in (("System and media", "/"), ("Photos and files", os.environ["PHOTOS"])):
    try:
        u = shutil.disk_usage(mnt)
        disks.append({"label": label, "mount": mnt, "free": u.free, "total": u.total})
    except OSError:
        pass
json.dump({
    "time": int(time.time()),
    "ok": not sys.argv[1:],
    "problems": sys.argv[1:],
    "backup_time": int(os.environ["BACKUP_TIME"]),
    "disks": disks,
    "outside": {k: check.get(k) for k in ("status", "last_ping", "next_ping")} if check else None,
}, sys.stdout)
PY
chmod 644 "$tmp" && mv -f "$tmp" site/status/heartbeat.json

# The detail sheets' data (ops-report.py: history, incidents, backups); never fails the heartbeat
python3 ops-report.py || echo "ops-report.py failed" >&2
# The console's overview (console-report.py: jobs, containers, certificates, what needs a look)
python3 console-report.py || echo "console-report.py failed" >&2
