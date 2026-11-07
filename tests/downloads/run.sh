#!/usr/bin/env bash
# tests/downloads/run.sh: the downloads components' real images, set up as downloads-wizard.sh does, from the real
# docker-compose.yml in compose project homelab-dltest (a docker wrapper refuses the live `homelab` and its volumes).
# gluetun gets a random WireGuard key: its kernel WireGuard link comes up, the tunnel never carries anything. Proven:
#   - gluetun with only NET_ADMIN, no /dev/net/tun, no permission errors (on an SELinux host: under SELinux);
#   - its kill switch: nothing leaves its eth0 but WireGuard to the endpoint (a capture), and no DNS answers;
#   - LinuxServer's non-root mode and qBittorrent's own image on fresh volumes (downloads/run.sh init);
#   - downloads/run.sh apply against the real APIs, twice; status; the logins (no API key without one);
#   - a gluetun restart: qBittorrent and Prowlarr in a dead namespace, downloads/guard.sh brings them back;
#   - Recyclarr's profiles; no generated secret in any container's log.
# Needs docker and the internet (the images, TRaSH Guides). CI: .github/workflows/downloads.yml.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22
work=$(mktemp -d)
pass=0; fail=0
ok()  { echo "  ✓ $*"; pass=$((pass + 1)); }
bad() { echo "  ✗ $*"; fail=$((fail + 1)); }
expect() { local what=$1; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }

# docker for this test only: never the live project or its volumes and networks
mkdir -p "$work/bin"
real_docker=$(command -v docker)
cat > "$work/bin/docker" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in homelab|homelab_*|*[:=/]homelab_*) echo "docker wrapper: refused '\$a' (the live project)" >&2; exit 99 ;; esac
done
exec "$real_docker" "\$@"
EOF
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" COMPOSE_PROJECT_NAME=homelab-dltest COMPOSE_FILE=docker-compose.yml \
  COMPOSE_ENV_FILES="$work/test.env" ENV_FILE="$work/test.env"
# Only once the project is known to be the test's: a `down -v` on the live one would delete its data
verified=0
cleanup() {
  if ((verified)); then docker compose -p homelab-dltest down -v --remove-orphans >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# The full example with fresh secrets, this user, a media folder of its own (a dotenv file isn't shell: the values
# go into variables first, then into the file)
mkdir -p "$work/media/Movies" "$work/media/TV Shows"
rnd() { openssl rand -hex 16; }
export ADMIN_USER=tester VPN_WIREGUARD_PRIVATE_KEY GLUETUN_CONTROL_KEY QBITTORRENT_API_KEY DOWNLOADS_ADMIN_PASSWORD \
  PROWLARR_API_KEY RADARR_API_KEY SONARR_API_KEY
VPN_WIREGUARD_PRIVATE_KEY=$(head -c32 /dev/urandom | base64); GLUETUN_CONTROL_KEY=$(rnd)
QBITTORRENT_API_KEY=qbt_$(openssl rand -hex 14); DOWNLOADS_ADMIN_PASSWORD=$(rnd)
PROWLARR_API_KEY=$(rnd); RADARR_API_KEY=$(rnd); SONARR_API_KEY=$(rnd)
mine=(HOST_UID HOST_GID MEDIA_DIR ADMIN_USER VPN_WIREGUARD_PRIVATE_KEY GLUETUN_CONTROL_KEY QBITTORRENT_API_KEY
      DOWNLOADS_ADMIN_PASSWORD PROWLARR_API_KEY RADARR_API_KEY SONARR_API_KEY)
{
  grep -vE "^($(IFS='|'; echo "${mine[*]}"))=" tests/fixtures/full.env
  echo "HOST_UID=$(id -u)"; echo "HOST_GID=$(id -g)"; echo "MEDIA_DIR=$work/media"
  for k in "${mine[@]:3}"; do echo "$k=${!k}"; done
} > "$work/test.env"
chmod 600 "$work/test.env"
APPS=(qbittorrent prowlarr radarr sonarr recyclarr)
[[ $(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])') == homelab-dltest ]] \
  || { echo "✗ not project homelab-dltest" >&2; exit 1; }
verified=1

echo "== gluetun, a random key"
docker compose create gluetun "${APPS[@]}" >/dev/null
bash downloads/run.sh init
docker compose up -d gluetun >/dev/null
for _ in $(seq 1 30); do docker compose logs gluetun 2>&1 | grep -q 'Wireguard setup is complete' && break; sleep 2; done
log=$(docker compose logs --no-log-prefix gluetun 2>&1)
expect "kernel WireGuard set up" grep -q 'Wireguard setup is complete' <<<"$log"
expect "no permission errors" bash -c '! grep -qiE "permission denied|operation not permitted" <<<"$1"' _ "$log"
G=$(docker compose ps -q gluetun)
subnet=$(docker network inspect homelab-dltest_downloads -f '{{(index .IPAM.Config 0).Subnet}}')

echo "== its kill switch (tunnel down)"
printf 'FROM %s\nRUN apk add --no-cache tcpdump\n' "$PY_IMAGE" | docker build -q -t homelab-dltest-tcpdump:1 - >/dev/null
docker run -d --rm --name homelab-dltest-cap --network "container:$G" --cap-drop ALL --cap-add NET_RAW --cap-add NET_ADMIN \
  homelab-dltest-tcpdump:1 timeout 25 tcpdump -Q out -lni eth0 "not (udp and port 51820) and not net $subnet and not icmp" >/dev/null
sleep 3
probe=$(docker run --rm --network "container:$G" --cap-drop ALL "$PY_IMAGE" python3 -c '
import socket
for host, port in (("192.0.2.1", 443), ("1.1.1.1", 443), ("9.9.9.9", 53)):
    try: socket.create_connection((host, port), timeout=3).close()
    except OSError: pass
try: socket.gethostbyname("example.com"); print("DNS ANSWERED")
except OSError: print("no DNS")')
expect "no DNS answer through the dead tunnel" test "$probe" == "no DNS"
# What gluetun's namespace sends (-Q out: the host's own multicast arrives on that bridge too, and is dropped)
cap=$(docker logs homelab-dltest-cap 2>/dev/null | grep -E ' IP6? ' || true)
docker stop homelab-dltest-cap >/dev/null 2>&1 || true
expect "nothing left gluetun's eth0 but WireGuard (other packets: $(grep -c . <<<"$cap" || true))" test -z "$cap"
[[ -z $cap ]] || head -5 <<<"$cap" | sed 's/^/    /'

echo "== the apps (non-root, fresh volumes)"
docker compose up -d --no-deps "${APPS[@]}" >/dev/null
for _ in $(seq 1 60); do
  [[ $(docker compose logs radarr sonarr prowlarr 2>&1 | grep -c 'Application started') -ge 3 ]] && break; sleep 3
done
for s in "${APPS[@]}"; do expect "$s running" test -n "$(docker compose ps -q --status running "$s")"; done
expect "qBittorrent: no temporary password (its sign-in was pre-seeded)" \
  bash -c '! docker compose logs qbittorrent 2>&1 | grep -qi "temporary password"'

echo "== gluetun restarted: the guard brings qBittorrent and Prowlarr back"
# Recreated with Docker's resolver kept (TEST ONLY: Prowlarr then finds radarr without a tunnel; for real, gluetun's
# own DNS does that once the tunnel is up)
cat > "$work/override.yml" <<'EOF'
services:
  gluetun:
    environment:
      DNS_KEEP_NAMESERVER: "on"
EOF
export COMPOSE_FILE="docker-compose.yml:$work/override.yml"
docker compose up -d --no-deps gluetun >/dev/null
sleep 5
expect "qBittorrent lost its network" bash -c '! docker compose exec -T qbittorrent test -e /sys/class/net/eth0'
bash downloads/guard.sh || true
sleep 20
expect "qBittorrent back in gluetun's namespace" docker compose exec -T qbittorrent test -e /sys/class/net/eth0
expect "Prowlarr back in gluetun's namespace" docker compose exec -T prowlarr test -e /sys/class/net/eth0

echo "== apply, twice"
for _ in $(seq 1 30); do bash downloads/run.sh status 2>/dev/null | grep -q '"categories"' && break; sleep 3; done
expect "apply" bash downloads/run.sh apply
expect "a second apply changes nothing" test "$(bash downloads/run.sh apply)" == "  all set"
st=$(bash downloads/run.sh status)
python3 - "$st" <<'EOF' && ok "status: categories, root folders, clients, Prowlarr's apps" || bad "status: $1"
import json, sys
d = json.loads(sys.argv[1])
assert d["qbittorrent"]["categories"] == ["movies", "tv"] and d["qbittorrent"]["interface"] == "tun0", d["qbittorrent"]
assert d["radarr"]["root"] and d["radarr"]["client"] and d["sonarr"]["root"] and d["sonarr"]["client"], d
assert d["prowlarr"]["apps"] == ["Radarr", "Sonarr"], d["prowlarr"]
EOF

echo "== the logins"
out=$(docker run --rm --network homelab-dltest_downloads --cap-drop ALL -e ADMIN_USER -e DOWNLOADS_ADMIN_PASSWORD \
  -e RADARR_API_KEY -e GLUETUN_CONTROL_KEY "$PY_IMAGE" python3 -c '
import os, urllib.request as u, urllib.error as e, urllib.parse as p
class NR(u.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
o = u.build_opener(NR)
def code(url, data=None, headers=None):
    try: r = o.open(u.Request(url, data=data, headers=headers or {}), timeout=15); return r.status, r.read().decode()
    except e.HTTPError as x: return x.code, x.read().decode()
for name, base in (("radarr", "http://radarr:7878"), ("sonarr", "http://sonarr:8989"), ("prowlarr", "http://gluetun:9696")):
    c, b = code(base + "/initialize.json")
    print(name, "initialize", c, "apiKey" in b)
    print(name, "api-without-key", code(base + ("/api/v1/indexer" if name == "prowlarr" else "/api/v3/system/status"))[0])
print("qbit api-without-login", code("http://gluetun:8080/api/v2/app/version")[0])
f = p.urlencode({"username": os.environ["ADMIN_USER"], "password": os.environ["DOWNLOADS_ADMIN_PASSWORD"]}).encode()
print("qbit login", code("http://gluetun:8080/api/v2/auth/login", f)[0])
print("gluetun control-without-key", code("http://gluetun:8000/v1/publicip/ip")[0])')
for line in "radarr initialize 302 False" "sonarr initialize 302 False" "prowlarr initialize 302 False" \
            "radarr api-without-key 401" "sonarr api-without-key 401" "prowlarr api-without-key 401" \
            "qbit api-without-login 403" "qbit login 204" "gluetun control-without-key 401"; do
  expect "$line" grep -qxF "$line" <<<"$out"
done

echo "== Recyclarr"
expect "recyclarr sync" docker compose exec -T recyclarr recyclarr sync
st=$(bash downloads/run.sh status)
expect "TRaSH Guides' profiles in Radarr and Sonarr" python3 -c '
import json, sys; d = json.loads(sys.argv[1])
sys.exit(not ("HD Bluray + WEB" in d["radarr"]["profiles"] and "WEB-1080p" in d["sonarr"]["profiles"]))' "$st"

echo "== secrets in the logs"
docker compose logs --no-log-prefix > "$work/logs.txt" 2>&1
for k in VPN_WIREGUARD_PRIVATE_KEY GLUETUN_CONTROL_KEY QBITTORRENT_API_KEY DOWNLOADS_ADMIN_PASSWORD PROWLARR_API_KEY \
         RADARR_API_KEY SONARR_API_KEY; do
  expect "$k in no container's log" bash -c '! grep -qF -- "$1" "$2"' _ "${!k}" "$work/logs.txt"
done

echo
echo "$pass passed, $fail failed"
((fail == 0))
