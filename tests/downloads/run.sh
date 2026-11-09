#!/usr/bin/env bash
# tests/downloads/run.sh: the downloads components' real images, set up as downloads-wizard.sh does, from the real
# docker-compose.yml in compose project homelab-dltest (a docker wrapper refuses the live `homelab` and its volumes),
# its networks on other ranges than the live ones. gluetun gets a random WireGuard key: its kernel WireGuard link
# comes up, the tunnel never carries anything. Proven:
#   - gluetun with only NET_ADMIN, no /dev/net/tun, no permission errors (on an SELinux host: under SELinux);
#   - its kill switch: nothing leaves its eth0 but WireGuard to the endpoint (a capture), and no DNS answers;
#   - LinuxServer's non-root mode, qBittorrent's, Seerr's and Bindery's images on fresh volumes (downloads/run.sh init);
#   - Authentik as the only login: what Caddy's fixed address forwards for a NetBird peer gets in without the app's
#     login, a neighbour (forged headers or not), the host and gluetun's loopback get the login or a refusal;
#   - downloads/run.sh apply against the real APIs, twice; status; Prowlarr's s6 readiness;
#   - a gluetun restart: qBittorrent, Prowlarr and Byparr in a dead namespace, downloads/guard.sh brings them back;
#   - Recyclarr's profiles, with the sizes and the x265 bonus from .env (its config as ./homelab configure renders
#     it); Lidarr's library scan leaves the files as they were; Bazarr's seeded login;
#   - Bindery's first run shut by its Authentik provider; Seerr's first admin from Jellyfin (set up over its startup
#     API with an adult and a kid), their permissions, a kid's request waiting and an adult's in Radarr, the claim
#     refused afterwards; no generated secret in any container's log.
# Needs docker and the internet (the images, TRaSH Guides, TMDB). CI: .github/workflows/downloads.yml.
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
# Jellyfin on Seerr's network directly (Caddy isn't started here), without the GPU
cat > "$work/override.yml" <<'EOF'
services:
  jellyfin:
    devices: !reset []
    group_add: !reset []
    networks: !override [requests]
EOF
export PATH="$work/bin:$PATH" COMPOSE_PROJECT_NAME=homelab-dltest COMPOSE_FILE="docker-compose.yml:$work/override.yml" \
  COMPOSE_ENV_FILES="$work/test.env" ENV_FILE="$work/test.env"
# Only once the project is known to be the test's: a `down -v` on the live one would delete its data
verified=0
cleanup() {
  if ((verified)); then docker compose -p homelab-dltest down -v --remove-orphans >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# The full example with fresh secrets, this user, a media folder of its own, networks clear of the live ones (a
# dotenv file isn't shell: the values go into variables first, then into the file)
mkdir -p "$work/media/Movies" "$work/media/TV Shows" "$work/media/Music/Test Artist/Test Album" "$work/media/Books/Books" \
  "$work/media/audiobooks"
rnd() { openssl rand -hex 16; }
DN=198.18.10 RN=198.18.11
secrets=(VPN_WIREGUARD_PRIVATE_KEY GLUETUN_CONTROL_KEY QBITTORRENT_API_KEY QBITTORRENT_PASSWORD PROWLARR_API_KEY
         PROWLARR_PASSWORD RADARR_API_KEY RADARR_PASSWORD SONARR_API_KEY SONARR_PASSWORD LIDARR_API_KEY LIDARR_PASSWORD
         BAZARR_API_KEY BAZARR_AUTH BINDERY_API_KEY BINDERY_OIDC_CLIENT_SECRET SEERR_API_KEY)
export ADMIN_USER=tester JELLYFIN_ADMIN_USER=jfadmin VPN_WIREGUARD_PRIVATE_KEY GLUETUN_CONTROL_KEY QBITTORRENT_API_KEY \
  QBITTORRENT_PASSWORD PROWLARR_API_KEY PROWLARR_PASSWORD RADARR_API_KEY RADARR_PASSWORD SONARR_API_KEY SONARR_PASSWORD \
  LIDARR_API_KEY LIDARR_PASSWORD BAZARR_API_KEY BAZARR_AUTH BINDERY_API_KEY BINDERY_OIDC_CLIENT_SECRET SEERR_API_KEY
VPN_WIREGUARD_PRIVATE_KEY=$(head -c32 /dev/urandom | base64)
QBITTORRENT_API_KEY=qbt_$(openssl rand -hex 14); BINDERY_API_KEY=$(openssl rand -hex 32); SEERR_API_KEY=$(openssl rand -hex 24)
BAZARR_AUTH=$(printf 'homelab:%s' "$(rnd)" | base64 | tr -d '\n')
for k in GLUETUN_CONTROL_KEY QBITTORRENT_PASSWORD PROWLARR_API_KEY PROWLARR_PASSWORD RADARR_API_KEY RADARR_PASSWORD \
         SONARR_API_KEY SONARR_PASSWORD LIDARR_API_KEY LIDARR_PASSWORD BAZARR_API_KEY BINDERY_OIDC_CLIENT_SECRET; do
  printf -v "$k" '%s' "$(rnd)"
done
JF_ADMIN_PW=$(rnd)
mine=(HOST_UID HOST_GID MEDIA_DIR AUDIOBOOKS_DIR ADMIN_USER JELLYFIN_ADMIN_USER DOWNLOADS_NET REQUESTS_NET
      MOVIE_SIZE_MB_PER_MIN EPISODE_SIZE_MB_PER_MIN DOWNLOADS_PREFER_X265 "${secrets[@]}")
{
  grep -vE "^($(IFS='|'; echo "${mine[*]}"))=" tests/fixtures/full.env
  echo "HOST_UID=$(id -u)"; echo "HOST_GID=$(id -g)"; echo "MEDIA_DIR=$work/media"; echo "AUDIOBOOKS_DIR=$work/media/audiobooks"
  echo "DOWNLOADS_NET=$DN"; echo "REQUESTS_NET=$RN"
  echo "MOVIE_SIZE_MB_PER_MIN=10/40/70"; echo "EPISODE_SIZE_MB_PER_MIN=7/20/35"; echo "DOWNLOADS_PREFER_X265=true"
  for k in ADMIN_USER JELLYFIN_ADMIN_USER "${secrets[@]}"; do echo "$k=${!k}"; done
} > "$work/test.env"
chmod 600 "$work/test.env"
# Recyclarr's config as ./homelab configure renders it, from this .env: never the checkout's own recyclarr/configs/
mkdir -p "$work/recyclarr"
python3 - "$work/test.env" "$work/recyclarr/recyclarr.yml" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "lib")
import configure, render
env = render.read_env(Path(sys.argv[1]))
src = configure.ROOT / "recyclarr/recyclarr.src.yml"
open(sys.argv[2], "w").write(render.render(src.read_text(), render.values(env) | configure.recyclarr(env), src.name))
PY
cat >> "$work/override.yml" <<EOF
  recyclarr:
    volumes: !override
      - {type: volume, source: recyclarr-config, target: /config, volume: {nocopy: true}}
      - $work/recyclarr:/config/configs:ro,z
EOF
APPS=(qbittorrent prowlarr byparr radarr sonarr recyclarr lidarr bazarr)
[[ $(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])') == homelab-dltest ]] \
  || { echo "✗ not project homelab-dltest" >&2; exit 1; }
verified=1
# req NETWORK IP METHOD URL [HEADER=VALUE…] [--data BODY]: one request from that address on that network (IP "": any),
# or from a container's namespace (NETWORK container:NAME); prints "STATUS LOCATION" and whether the body has a key
req() {
  local net=$1 ip=$2; shift 2
  docker run --rm -i --network "$net" ${ip:+--ip "$ip"} --cap-drop ALL "$PY_IMAGE" python3 -c '
import sys, urllib.error, urllib.request as u
class NR(u.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
args = sys.argv[1:]
data = None
if "--data" in args:
    i = args.index("--data"); data = args[i + 1].encode(); args = args[:i] + args[i + 2:]
method, url, *hdrs = args
h = dict(x.split("=", 1) for x in hdrs)
try:
    r = u.build_opener(NR).open(u.Request(url, data=data, headers=h, method=method), timeout=20)
    code, body, loc = r.status, r.read().decode(errors="replace"), r.headers.get("Location", "")
except urllib.error.HTTPError as e:
    code, body, loc = e.code, e.read().decode(errors="replace"), e.headers.get("Location", "")
except OSError as e:
    print("refused"); sys.exit(0)
print(code, loc or "-", "key" if "apikey" in body.lower() else "")' "$@"
}
ws() { [[ $1 == "$2"* ]]; }   # ws OUTPUT PREFIX
G=""

echo "== gluetun, a random key"
docker compose create gluetun "${APPS[@]}" >/dev/null
bash downloads/run.sh init
docker compose up -d gluetun >/dev/null
for _ in $(seq 1 30); do docker compose logs gluetun 2>&1 | grep -q 'Wireguard setup is complete' && break; sleep 2; done
log=$(docker compose logs --no-log-prefix gluetun 2>&1)
expect "kernel WireGuard set up" grep -q 'Wireguard setup is complete' <<<"$log"
expect "no permission errors" bash -c '! grep -qiE "permission denied|operation not permitted" <<<"$1"' _ "$log"
G=$(docker compose ps -q gluetun)
subnet=$(docker network inspect homelab-dltest_arr -f '{{(index .IPAM.Config 0).Subnet}}')
expect "the downloads network on its pinned range" test "$subnet" == "$DN.0/24"

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
  [[ $(docker compose logs radarr sonarr prowlarr lidarr 2>&1 | grep -c 'Application started') -ge 4 ]] && break; sleep 3
done
for s in "${APPS[@]}"; do expect "$s running" test -n "$(docker compose ps -q --status running "$s")"; done
expect "qBittorrent: no temporary password (its sign-in was pre-seeded)" \
  bash -c '! docker compose logs qbittorrent 2>&1 | grep -qi "temporary password"'
for _ in $(seq 1 30); do docker compose logs prowlarr 2>&1 | grep -q 'ls.io-init\] done' && break; sleep 2; done
expect "Prowlarr ready for s6 (its check asks gluetun's address)" bash -c 'docker compose logs prowlarr 2>&1 | grep -q "ls.io-init\] done"'
expect "Bazarr kept its seeded login (Basic)" \
  bash -c 'docker compose exec -T bazarr grep -A4 "^auth:" /config/config/config.yaml | grep -q "type: basic"'

echo "== Authentik as the only login"
# Caddy's fixed address with a NetBird peer's forwarded address: in without the app's login. The same without that
# header, from a neighbour (with a forged one), from the host, from gluetun's loopback: their login or a refusal.
for a in radarr:7878 sonarr:8989 lidarr:8686 gluetun:9696; do
  n=${a%%:*}; [[ $n == gluetun ]] && n=prowlarr
  out=$(req homelab-dltest_arr "$DN.11" GET "http://$a/initialize.json" X-Forwarded-For=100.64.1.2)
  expect "$n: Caddy's NetBird client in, no second login" ws "$out" "200 - key"
  out=$(req homelab-dltest_arr "$DN.11" GET "http://$a/initialize.json")
  expect "$n: Caddy itself, no forwarded client: its login" ws "$out" "302 http://"
  out=$(req homelab-dltest_arr "$DN.11" GET "http://$a/initialize.json" X-Forwarded-For=203.0.113.9)
  expect "$n: a client from the internet: its login" ws "$out" "302 http://"
  out=$(req homelab-dltest_arr "" GET "http://$a/initialize.json" X-Forwarded-For=100.64.1.2)
  expect "$n: a neighbour with a forged forwarded address: its login" ws "$out" "302 http://"
  out=$(req homelab-dltest_arr "" GET "http://$a/initialize.json" X-Forwarded-For=127.0.0.1)
  expect "$n: a neighbour claiming loopback: its login" ws "$out" "302 http://"
done
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: gluetun' "http://$DN.10:9696/initialize.json")
expect "Prowlarr from the host: its login" test "$code" == 302
out=$(req "container:$G" "" GET http://127.0.0.1:9696/initialize.json)
expect "Prowlarr not on gluetun's loopback (qBittorrent's and Byparr's)" test "$out" == refused
out=$(req "container:$G" "" GET "http://$DN.10:9696/initialize.json" Host=gluetun)
expect "Prowlarr from inside gluetun's namespace: its login" ws "$out" "302 http://"
# A socket bound to 127.0.0.1 reaches a local address over lo, and loopback is local to Prowlarr (no login): shut by
# gluetun's post-rules
out=$(docker run --rm --network "container:$G" --cap-drop ALL "$PY_IMAGE" python3 -c '
import socket, sys
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.settimeout(4)
try:
    s.connect((sys.argv[1], 9696)); print("connected")
except OSError:
    print("refused")' "$DN.10")
expect "Prowlarr from gluetun's loopback, by a bound socket: no way in" test "$out" == refused
expect "qBittorrent: Caddy's address in" ws "$(req homelab-dltest_arr "$DN.11" GET http://gluetun:8080/api/v2/app/version)" "200"
expect "qBittorrent: a neighbour refused" ws "$(req homelab-dltest_arr "" GET http://gluetun:8080/api/v2/app/version)" "403"
expect "qBittorrent: its loopback refused" ws "$(req "container:$G" "" GET http://127.0.0.1:8080/api/v2/app/version)" "403"
expect "Bazarr: without its login 401" ws "$(req homelab-dltest_arr "$DN.11" GET http://bazarr:6767/)" "401"
expect "Bazarr: with Caddy's Basic in" \
  ws "$(req homelab-dltest_arr "$DN.11" GET http://bazarr:6767/ "Authorization=Basic $BAZARR_AUTH")" "200"
expect "Bazarr's API: without its key 401" ws "$(req homelab-dltest_arr "$DN.11" GET http://bazarr:6767/api/system/status)" "401"
expect "Byparr: only on gluetun's loopback" test "$(req homelab-dltest_arr "" GET http://gluetun:8191/)" == refused

echo "== gluetun restarted: the guard brings qBittorrent, Prowlarr and Byparr back"
# Recreated with Docker's resolver kept (TEST ONLY: Prowlarr then finds radarr without a tunnel; for real, gluetun's
# own DNS does that once the tunnel is up)
cat >> "$work/override.yml" <<'EOF'
  gluetun:
    environment:
      DNS_KEEP_NAMESERVER: "on"
EOF
docker compose up -d --no-deps gluetun >/dev/null
sleep 5
expect "qBittorrent lost its network" bash -c '! docker compose exec -T qbittorrent test -e /sys/class/net/eth0'
bash downloads/guard.sh || true
sleep 20
for s in qbittorrent prowlarr byparr; do
  expect "$s back in gluetun's namespace" docker compose exec -T "$s" test -e /sys/class/net/eth0
done
G=$(docker compose ps -q gluetun)

echo "== nothing from the tunnel's address leaves this machine, while gluetun keeps restarting it"
# Its kill switch accepts an established connection's packets on any interface: without its post-rules, Byparr's and
# Prowlarr's connections went out by eth0 from the tunnel's address (Proton: 10.2.0.2) and on to the LAN card
# The tunnel's address (the host's own may be private too: a CI runner's is), read while gluetun has one
tun=""
for _ in $(seq 1 20); do
  tun=$(docker compose exec -T gluetun ip -4 -o addr show tun0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
  [[ -n $tun ]] && break; sleep 1
done
tun=${tun:-10.2.0.2}
docker run -d --rm --name homelab-dltest-hostcap --network host --cap-drop ALL --cap-add NET_RAW --cap-add NET_ADMIN \
  homelab-dltest-tcpdump:1 timeout 120 tcpdump -lni any "src host $tun" >/dev/null
sleep 3
for u in https://example.com/ https://www.wikipedia.org/ https://www.debian.org/; do
  req "container:$G" "" POST http://127.0.0.1:8191/v1 Content-Type=application/json \
    --data "{\"cmd\":\"request.get\",\"url\":\"$u\",\"maxTimeout\":30000}" >/dev/null
done
sleep 5
leaked=$(docker logs homelab-dltest-hostcap 2>/dev/null | grep -E ' IP6? ' | grep -vE '(docker0|br-|veth)[^ ]* ' || true)
docker stop homelab-dltest-hostcap >/dev/null 2>&1 || true
expect "no packet from the tunnel's address ($tun) out of this machine (VPN restarts: $(docker compose logs gluetun 2>&1 | grep -c 'restarting VPN'))" \
  test -z "$leaked"
[[ -z $leaked ]] || head -5 <<<"$leaked" | sed 's/^/    /'
expect "gluetun reads its chains back (its post-rules are plain)" bash -c '! docker compose logs gluetun 2>&1 | grep -q "ERROR \[firewall\]"'

echo "== Jellyfin (Seerr's sign-in): its first run, an adult and a kid"
docker compose up -d --no-deps jellyfin >/dev/null
JF=$(docker inspect -f '{{(index .NetworkSettings.Networks "homelab-dltest_requests").IPAddress}}' "$(docker compose ps -q jellyfin)")
for _ in $(seq 1 60); do curl -sf "http://$JF:8096/System/Info/Public" 2>/dev/null | grep -q '"Version"' && break; sleep 3; done
JF_ADMIN_PW=$JF_ADMIN_PW JF="http://$JF:8096" python3 - <<'EOF' && ok "Jellyfin set up" || bad "Jellyfin set up"
import json, os, urllib.request as u
jf = os.environ["JF"]
def call(path, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f'MediaBrowser Token="{token}"'
    else:
        h["Authorization"] = 'MediaBrowser Client="test", Device="test", DeviceId="dltest", Version="1"'
    r = u.urlopen(u.Request(jf + path, data=json.dumps(body).encode() if body is not None else None, headers=h,
                            method="POST" if body is not None else "GET"), timeout=30)
    t = r.read()
    return json.loads(t) if t else None
call("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
call("/Startup/User")
call("/Startup/User", {"Name": "jfadmin", "Password": os.environ["JF_ADMIN_PW"]})
call("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
call("/Startup/Complete", {})
tok = call("/Users/AuthenticateByName", {"Username": "jfadmin", "Pw": os.environ["JF_ADMIN_PW"]})["AccessToken"]
for n in ("adult", "kid"):
    call("/Users/New", {"Name": n, "Password": n + "-password-1234"}, tok)
for name, kind, path in (("Movies", "movies", "/media/Movies"), ("TV Shows", "tvshows", "/media/TV Shows")):
    q = u.quote(name)
    call(f"/Library/VirtualFolders?name={q}&collectionType={kind}&refreshLibrary=false",
         {"LibraryOptions": {"PathInfos": [{"Path": path}]}}, tok)
EOF

echo "== Lidarr's library: a file before its first scan"
python3 - "$work/media/Music/Test Artist/Test Album/01 - Test Track.wav" <<'EOF'
import sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\0" * 8000)
EOF
before=$(stat -c '%Y %s' "$work/media/Music/Test Artist/Test Album/01 - Test Track.wav") \
  sum=$(sha256sum < "$work/media/Music/Test Artist/Test Album/01 - Test Track.wav")

echo "== Bindery and Seerr: each started and set up back to back"
docker compose create bindery seerr >/dev/null
bash downloads/run.sh init >/dev/null
docker compose up -d --no-deps bindery >/dev/null
for _ in $(seq 1 40); do [[ $(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q bindery)") == healthy ]] && break; sleep 3; done
expect "bindery-init" bash downloads/run.sh bindery-init
expect "Bindery: its first-run setup shut" ws "$(req homelab-dltest_arr "" POST http://bindery:8787/api/v1/auth/setup \
  Content-Type=application/json --data '{"username":"mallory","password":"0123456789"}')" "403"
docker compose up -d --no-deps seerr >/dev/null
for _ in $(seq 1 40); do [[ $(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q seerr)") == healthy ]] && break; sleep 3; done
expect "seerr-init: Jellyfin's admin is Seerr's" \
  env JELLYFIN_ADMIN_PASSWORD="$JF_ADMIN_PW" JELLYFIN_URL=http://jellyfin:8096 bash downloads/run.sh seerr-init
out=$(req homelab-dltest_requests "" POST http://seerr:5055/api/v1/auth/jellyfin Content-Type=application/json --data \
  '{"username":"x","password":"y","hostname":"evil.example","port":8096,"useSsl":false,"urlBase":"","serverType":2}')
expect "Seerr: nobody points it at another Jellyfin afterwards ($out)" bash -c '! [[ $1 == 200* ]]' _ "$out"

echo "== apply, twice"
docker compose exec -T recyclarr recyclarr sync >/dev/null || true
for _ in $(seq 1 30); do bash downloads/run.sh status 2>/dev/null | grep -q '"categories"' && break; sleep 3; done
out=$(JELLYFIN_URL=http://jellyfin:8096 bash downloads/run.sh apply 2>&1) && code=0 || code=$?
# Prowlarr tests Byparr by fetching through it, so through the dead tunnel: whether that passes varies (Proton
# answers some handshakes without a registered key). When it doesn't, that part says so and the others are set up
if [[ $code == 0 ]]; then
  expect "apply" test "$(tail -n1 <<<"$out")" == "  all set"
else
  expect "apply: everything but Byparr (the dead tunnel)" test "$code:$(tail -n1 <<<"$out")" == "1:  not set up: Byparr"
  grep -q 'Byparr couldn.t fetch its test page: is the VPN up' <<<"$out" || echo "$out" | sed 's/^/    /'
fi
docker compose restart seerr >/dev/null
for _ in $(seq 1 40); do [[ $(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q seerr)") == healthy ]] && break; sleep 3; done
out=$(JELLYFIN_URL=http://jellyfin:8096 bash downloads/run.sh apply 2>&1) || true
expect "a second apply changes nothing (but Byparr's part)" test "$(grep -vE 'Byparr|^  all set$' <<<"$out")" == ""
st=$(JELLYFIN_URL=http://jellyfin:8096 bash downloads/run.sh status)
python3 - "$st" <<'EOF' && ok "status: categories, root folders, clients, apps, the others" || bad "status: $1"
import json, sys
d = json.loads(sys.argv[1])
assert d["qbittorrent"]["categories"] == ["audiobooks", "books", "movies", "music", "tv"] and d["qbittorrent"]["interface"] == "tun0", d["qbittorrent"]
assert d["qbittorrent"]["whitelist"].endswith(".11/32"), d["qbittorrent"]
for a in ("radarr", "sonarr", "lidarr"):
    assert d[a]["root"] and d[a]["client"], d[a]
assert d["prowlarr"]["apps"] == ["Lidarr", "Radarr", "Sonarr"], d["prowlarr"]
assert not d["lidarr"]["tags_written"] and d["lidarr"]["monitor_new_albums"] == 0, d["lidarr"]
assert d["bazarr"]["sonarr"] and d["bazarr"]["radarr"] and d["bazarr"]["signed_in"], d["bazarr"]
assert d["bindery"]["ready"] and not d["bindery"]["setup_open"] and d["bindery"]["client"] and d["bindery"]["prowlarr"], d["bindery"]
assert d["seerr"]["initialized"] and d["seerr"]["local_login"] is False and d["seerr"]["radarr"], d["seerr"]
assert d["locked"] and all(d["locked"].values()), d["locked"]
EOF
# Lidarr's library scan (as after adding its root folder): the file found, and left as it was
scanned=$(docker run --rm --network homelab-dltest_arr -e LIDARR_API_KEY --cap-drop ALL "$PY_IMAGE" python3 -c '
import json, os, time, urllib.request as u
H = {"X-Api-Key": os.environ["LIDARR_API_KEY"], "Content-Type": "application/json"}
def call(m, p, b=None):
    return json.load(u.urlopen(u.Request("http://lidarr:8686/api/v1" + p, data=json.dumps(b).encode() if b else None,
                                         headers=H, method=m), timeout=30))
c = call("POST", "/command", {"name": "RescanFolders", "folders": ["/data/Music"]})
for _ in range(60):
    if call("GET", "/command/%d" % c["id"])["status"] in ("completed", "failed"):
        break
    time.sleep(3)
print(" ".join(x["path"] for x in call("GET", "/trackfile?unmapped=true")))' 2>&1) || true
expect "Lidarr's scan found the file" grep -qF "/data/Music/Test Artist/Test Album/01 - Test Track.wav" <<<"$scanned"
track="$work/media/Music/Test Artist/Test Album/01 - Test Track.wav"
expect "Lidarr's scan left it as it was" test "$(stat -c '%Y %s' "$track") $(sha256sum < "$track")" == "$before $sum"

echo "== Seerr: the family from Jellyfin, a kid's request waiting, an adult's in Radarr"
expect "seerr-users" env SEERR_FAMILY=adult,kid SEERR_KIDS=kid JELLYFIN_URL=http://jellyfin:8096 bash downloads/run.sh seerr-users
out=$(docker run --rm --network homelab-dltest_requests -e SEERR_API_KEY -e RADARR_API_KEY --cap-drop ALL "$PY_IMAGE" python3 -c '
import json, os, time, urllib.request as u
def call(method, path, uid=None, body=None):
    h = {"X-Api-Key": os.environ["SEERR_API_KEY"], "Content-Type": "application/json"}
    if uid: h["X-API-User"] = str(uid)
    r = u.urlopen(u.Request("http://seerr:5055/api/v1" + path, data=json.dumps(body).encode() if body else None,
                            headers=h, method=method), timeout=60)
    return json.load(r)
users = {x["jellyfinUsername"]: x for x in call("GET", "/user?take=50")["results"]}
print("perms", users["adult"]["permissions"], users["kid"]["permissions"])
call("POST", "/request", users["kid"]["id"], {"mediaType": "movie", "mediaId": 10331})
call("POST", "/request", users["adult"]["id"], {"mediaType": "movie", "mediaId": 11236})
time.sleep(10)
print("requests", sorted((r["requestedBy"]["jellyfinUsername"], r["status"]) for r in call("GET", "/request?take=10")["results"]))
r = u.urlopen(u.Request("http://radarr:7878/api/v3/movie", headers={"X-Api-Key": os.environ["RADARR_API_KEY"]}))
print("radarr", [m["tmdbId"] for m in json.load(r)])' 2>&1) || true
expect "an adult approved automatically, a kid only asks (160, 32)" grep -qx "perms 160 32" <<<"$out"
expect "the kid's request waits, the adult's approved" grep -qx "requests \[('adult', 2), ('kid', 1)\]" <<<"$out"
expect "the adult's film in Radarr, the kid's not" grep -qx "radarr \[11236\]" <<<"$out"
[[ $(grep -c . <<<"$out") -ge 3 ]] || echo "$out" | sed 's/^/    /'

echo "== Recyclarr"
st=$(bash downloads/run.sh status)
expect "TRaSH Guides' profiles in Radarr and Sonarr" python3 -c '
import json, sys; d = json.loads(sys.argv[1])
sys.exit(not ("HD Bluray + WEB" in d["radarr"]["profiles"] and "WEB-1080p" in d["sonarr"]["profiles"]))' "$st"
out=$(docker run --rm --network homelab-dltest_requests -e RADARR_API_KEY -e SONARR_API_KEY --cap-drop ALL "$PY_IMAGE" python3 -c '
import json, os, urllib.request as u
def get(base, key, path):
    return json.load(u.urlopen(u.Request(base + "/api/v3" + path, headers={"X-Api-Key": os.environ[key]}), timeout=60))
for name, base, key, profile in (("radarr", "http://radarr:7878", "RADARR_API_KEY", "HD Bluray + WEB"),
                                 ("sonarr", "http://sonarr:8989", "SONARR_API_KEY", "WEB-1080p")):
    q = {d["quality"]["name"]: d for d in get(base, key, "/qualitydefinition")}["WEBDL-1080p"]
    p = next(x for x in get(base, key, "/qualityprofile") if x["name"] == profile)
    x265 = [f["score"] for f in p["formatItems"] if f["name"] == "x265 (HD)"]
    print(name, "/".join(f"{float(q[k]):g}" for k in ("minSize", "preferredSize", "maxSize")), x265)' 2>&1) || true
expect "Radarr: films 10/40/70 MB a minute, x265 a bonus" grep -qx "radarr 10/40/70 \[2000\]" <<<"$out"
expect "Sonarr: episodes 7/20/35 MB a minute, x265 a bonus" grep -qx "sonarr 7/20/35 \[2000\]" <<<"$out"
grep -qx "radarr 10/40/70 \[2000\]" <<<"$out" && grep -qx "sonarr 7/20/35 \[2000\]" <<<"$out" || echo "$out" | sed 's/^/    /'

echo "== secrets in the logs"
docker compose logs --no-log-prefix > "$work/logs.txt" 2>&1
for k in "${secrets[@]}" JF_ADMIN_PW; do
  expect "$k in no container's log" bash -c '! grep -qF -- "$1" "$2"' _ "${!k}" "$work/logs.txt"
done

echo
echo "$pass passed, $fail failed"
((fail == 0))
