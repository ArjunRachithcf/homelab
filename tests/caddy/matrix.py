#!/usr/bin/env python3
"""tests/caddy/matrix.py: what the real Caddyfile does, request by request (tests/golden.sh).

Runs Caddy (CADDY, or `caddy` on PATH: 2.11 with caddy-dns/infomaniak, like caddy/Dockerfile) on the
repo's Caddyfile with the environment compose gives it (docker compose config, an instance's .env),
local certificates and high ports, every upstream replaced by a stub that names itself, and a stub
Authentik outpost for forward-auth. Then asks every host × listener × path × user and prints one
line per request: status, redirect target, which upstream answered (or a hash of what Caddy itself
served, e.g. the rendered home page). Same output = same behaviour, whatever the file looks like.

    matrix.py [--env FILE] [--out FILE] [--dump DIR]   exit 2 when caddy or docker compose is missing (golden.sh skips it)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import configure  # noqa: E402
import render  # noqa: E402

ENV = ROOT / "tests/fixtures/full.env"  # --env
# Real port → the port Caddy listens on in the test (no root needed)
PORTS = {80: 18080, 443: 18443, 8444: 18444, 9443: 19443}
HOSTS: list[str] = []  # the chosen components' names and one nothing serves (main: hosts())
PATHS = [
    ("GET", "/"), ("GET", "/if/admin/"), ("GET", "/outpost.goauthentik.io/start"), ("GET", "/web/"),
    ("GET", "/ops/data/jobs.json"), ("POST", "/ops/run/backup"), ("GET", "/status/paperless"),
    ("GET", "/status/heartbeat.json"), ("GET", "/apps.json"), ("GET", "/logo.png"), ("GET", "/manifest.webmanifest"),
    ("GET", "/{slug}/fonts/inter.woff2"), ("GET", "/home/fonts/inter.woff2"), ("GET", "/brand/fonts/inter.woff2"),
    ("GET", "/.well-known/openid-configuration"), ("GET", "/rest/ping?u=x"), ("GET", "/rest/ping?c=NavidromeUI"),
    ("GET", "/s/abc"), ("POST", "/api/v2/sec/share/abc/validity"), ("GET", "/api/server/ping"),
    ("GET", "/console/"), ("GET", "/console/guides/backup-failed.md"), ("GET", "/status/ops"),
    ("POST", "/ops/run/restart-paperless"), ("POST", "/ops/run/run-backup"), ("POST", "/ops/run/clean-bins"),
    ("GET", "/dav/public-files/abc"), ("GET", "/remote.php/dav/public-files/abc"),
    ("POST", "/graph/v1beta1/drives/d/items/i/createLink"), ("POST", "/graph/v1beta1/drives/d/root/createLink"),
    ("POST", "/graph/v1.0/drives/d/items/i/createLink"), ("POST", "/graph/v1.0/drives/d/root/createLink"),
    ("GET", "/api/token/create_owner/"), ("POST", "/api/users"), ("POST", "/api/login"),
]
# Stub Authentik users (cookie stub_user=<key>); "" = not signed in
USERS = {
    "": None,
    "anna": ("anna", "family|paperless-users", "Anna Example"),
    "kim": ("kim", "family|kids", "Kim Example"),
    "admin": ("", "authentik Admins|family|paperless-users|budget", ""),  # main(): the instance's ADMIN_USER, ADMIN_NAME
    "op": ("olga", "family|operators", "Olga Example"),
    # The outpost sends no groups header for her: the client's forged one must not survive
    "nog": ("nina", "", "Nina Example"),
}
# Requests that sign in with something other than the cookie: (host, method, path, headers), on every TLS listener
CREDENTIALS = [
    ("paperless", "GET", "/api/documents/", {"Authorization": "Basic YWRtaW46eA=="}),
    ("paperless", "GET", "/api/documents/", {"Authorization": "basic YWRtaW46eA=="}),
    ("paperless", "GET", "/api/documents/", {"Authorization": " BASIC YWRtaW46eA=="}),
    ("paperless", "GET", "/api/documents/", {"Authorization": "Token abc"}),
    ("paperless", "POST", "/api/token/", {}),
    ("paperless", "POST", "/api/token", {}),
    ("paperless", "POST", "/api/%74oken/", {}),
    ("paperless", "POST", "/api/token/?x=1", {}),
    ("paperless", "GET", "/api/auth/headless/app/v1/config", {}),
    ("games", "POST", "/api/token", {}),
    ("games", "POST", "/api/token/", {}),
    # Downloads (qBittorrent, Prowlarr, Radarr, Sonarr, Lidarr, Bazarr, Bindery): Authentik before every path, whatever
    # key comes along; their API keys never leave the downloads network
    *[(h, "GET", path, hdrs) for h in ("qbit", "prowlarr", "radarr", "sonarr", "lidarr", "subs", "bindery")
      for path, hdrs in (
        ("/initialize.json", {}),
        ("/api/v3/system/status", {"X-Api-Key": "x"}),
        ("/api/v1/indexer?apikey=x", {}),
        ("/api/v2/app/version", {"Authorization": "Bearer qbt_x"}),
        ("/signalr/messages", {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                               "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}),
        ("/api/system/status", {"X-API-KEY": "x"}), ("/setup", {}))],
    # Seerr (requests, public, no forward-auth): its own accounts, Quick Connect, its API's docs and the image proxies
    # its pages don't use are shut on both listeners, also in another case or %-encoded (its routes ignore case);
    # Jellyfin's sign-in reaches it; avatars only with its session cookie; Next.js's middleware header never arrives
    ("requests", "POST", "/api/v1/auth/jellyfin", {"X-Matrix-Probe": "seerr", "X-Middleware-Subrequest": "middleware",
                                                   "X-Api-Key": "a-guessed-key", "X-Api-User": "1"}),
    ("requests", "GET", "/api/v1/settings/public", {}),
    *[("requests", "POST", path, {}) for path in (
        "/api/v1/auth/local", "/api/v1/auth/local/", "/api/v1/Auth/Local", "/api/v1/auth/%6Cocal", "/api/v1/auth/plex",
        "/api/v1/auth/reset-password", "/api/v1/auth/reset-password/abc", "/api/v1/auth/jellyfin/quickconnect/initiate",
        "/api/v1/auth/jellyfin/quickconnect/authenticate", "/api/v1/user/2/settings/linked-accounts/jellyfin/quickconnect",
        "/api/v1/user/2/settings/linked-accounts/jellyfin/quickconnect/")],
    *[("requests", "GET", path, {}) for path in (
        "/api/v1/auth/jellyfin/quickconnect/check?secret=x", "/api-docs", "/api-docs/swagger-ui.css", "/API-DOCS",
        "/_next/image?url=%2Flogo_full.svg&w=64&q=75", "/imageproxy/tmdb/t/p/w300/x.jpg",
        "/avatarproxy/0123456789abcdef0123456789abcdef")],
    ("requests", "GET", "/avatarproxy/0123456789abcdef0123456789abcdef", {"Cookie": "connect.sid=s%3Aabc"}),
]
# Seerr before its own setup step made its admin ours (site/status/requests-ready not there yet): nothing reaches it
UNCLAIMED = [("requests", "GET", "/", {}), ("requests", "POST", "/api/v1/auth/jellyfin", {}),
             ("requests", "GET", "/api/v1/settings/public", {})]
# Signed in to Authentik (the stub's cookie), then: (host, method, path, headers, user)
SIGNED_IN = [
    # Bindery's first-run setup (a local admin) shut at any time, its own sign-in through Authentik open
    *[("bindery", m, path, {}, "anna") for m, path in (
        ("GET", "/setup"), ("GET", "/setup/admin"), ("POST", "/api/v1/auth/setup"), ("POST", "/api/v1/auth/setup/"),
        ("POST", "/api/v1/Auth/Setup"), ("GET", "/api/v1/auth/oidc/authentik/login"), ("GET", "/"))],
    # Bazarr: its Basic login is Caddy's (BAZARR_AUTH), whatever the browser sends
    ("subs", "GET", "/", {"X-Matrix-Probe": "auth"}, "admin"),
    ("subs", "GET", "/api/system/status", {"X-Matrix-Probe": "auth", "Authorization": "Basic Zm9yZ2VkOmZvcmdlZA=="}, "admin"),
]
# OpenCloud: every way a public link's token opens it, refused; what the apps and signed-in people need, open
PUBLIC_LINKS = [
    ("files", "GET", "/archiver?public-token=x&id=y", {}),
    ("files", "GET", "/archiver?id=y", {"public-token": "x"}),
    ("files", "GET", "/app/open?file_id=y&public-token=x", {}),
    ("files", "POST", "/app/new?parent_container_id=y&filename=z&public-token=x", {}),
    ("files", "GET", "/ocs/v1.php/apps/files_sharing/api/v1/tokeninfo/unprotected/x", {}),
    ("files", "GET", "/ocs/v2.php/apps/files_sharing/api/v1/tokeninfo/unprotected/x", {}),
    ("files", "POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", {}),
    ("files", "GET", "/ocs/v1.php/cloud/capabilities", {}),
    ("files", "GET", "/archiver?id=y", {}),
]
# A client's own True-Client-IP and X-Real-IP never reach an app as sent (Navidrome's login limiter trusts them
# from Caddy); X-Matrix-Probe has the stub print the client IP headers it got
PROBE_IP = {"True-Client-IP": "6.6.6.6", "X-Real-IP": "6.6.6.6", "X-Matrix-Probe": "ip"}
FORWARDING = [
    ("music", "GET", "/rest/ping.view?c=app", PROBE_IP),
    ("stuff", "GET", "/api/v1/status", PROBE_IP),
]
DUMP: Path | None = None  # --dump: keep every body Caddy itself served, named by its hash
EXPECTED_AUTH = ""  # main(): what Caddy sends Bazarr (Basic, BAZARR_AUTH from the instance's .env)
UPSTREAM = re.compile(r"(?<![\w.{}$-])((?:[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*)|(?:\d+(?:\.\d+){3})):(\d{2,5})\b")


def hosts(env: dict[str, str]) -> list[str]:
    """Every name the chosen components serve, public and peer, and one that nothing serves."""
    sys.path.insert(0, str(ROOT / "lib/setup"))
    import engine
    comps = engine.components()
    names = [n for c in engine.current(comps, env)[0] for n in comps[c]["public_names"] + comps[c]["peer_names"]]
    return list(dict.fromkeys(names)) + ["nope"]


def caddy_env() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "compose", "-p", "homelab", "--env-file", str(ENV), "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True, env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
    ).stdout
    env = json.loads(out)["services"]["caddy"]["environment"]
    return {k: str(v) for k, v in env.items()}


def stub_handler(name: str, domain: str):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.path.startswith("/outpost.goauthentik.io/auth/caddy"):
                m = re.search(r"stub_user=(\w+)", self.headers.get("Cookie", ""))
                user = USERS.get(m.group(1)) if m else None
                if user:
                    self.send_response(200)
                    self.send_header("X-Authentik-Username", user[0])
                    if user[1]:
                        self.send_header("X-Authentik-Groups", user[1])
                    self.send_header("X-Authentik-Name", user[2])
                    self.send_header("X-Authentik-Email", user[0] + "@example.org")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(302)
                    self.send_header("Location", f"https://auth.{domain}/outpost.goauthentik.io/start?rd=stub")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                return
            body = (f"UPSTREAM {name} {self.command} {self.path} xau={self.headers.get('X-Authentik-Username', '-')}"
                    f" xag={self.headers.get('X-Authentik-Groups', '-')}")
            probe = self.headers.get("X-Matrix-Probe")
            if probe == "ip":  # FORWARDING rows: the client IP the app would use
                body += "".join(f" {h}={self.headers.get(h, '-')}" for h in ("True-Client-IP", "X-Real-IP", "X-Forwarded-For"))
            elif probe == "seerr":   # Next.js's own subrequests' header, Seerr's API key (its admin)
                body += "".join(f" {h.lower()}={self.headers.get(h, '-')}"
                                for h in ("X-Middleware-Subrequest", "X-Api-Key", "X-Api-User"))
            elif probe == "auth":    # the Authorization the app got: Caddy's own (BAZARR_AUTH) or something else
                got = self.headers.get("Authorization", "")
                body += " authorization=" + ("caddy's" if got and got == EXPECTED_AUTH
                                             else hashlib.sha256(got.encode()).hexdigest()[:12] if got else "-")
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_HEAD = answer

    return H


def connect(port: int, real_port: int, host: str, proxy: bool = True) -> http.client.HTTPConnection:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    if real_port == 8444 and proxy:
        s.sendall(f"PROXY TCP4 203.0.113.7 127.0.0.1 40000 {real_port}\r\n".encode())
    if real_port != 80:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Caddy's local CA, test only
        s = ctx.wrap_socket(s, server_hostname=host)
    conn = http.client.HTTPConnection(host, real_port, timeout=5)
    conn.sock = s
    return conn


def one(conn: http.client.HTTPConnection, real_port: int, host: str, method: str, path: str, user: str,
        extra: dict[str, str] | None = None) -> str:
    hdrs = {"Host": host if real_port in (80, 443) else f"{host}:{real_port}", "User-Agent": "matrix",
            "X-Authentik-Username": "forged", "X-Authentik-Groups": "authentik Admins"}  # must never reach an app as-is
    hdrs.update(extra or {})
    if user:
        hdrs["Cookie"] = f"stub_user={user}"
    conn.request(method, path, body=b"" if method == "POST" else None, headers=hdrs)
    r = conn.getresponse()
    body = r.read()
    loc = r.getheader("Location", "")
    if body.startswith(b"UPSTREAM "):
        what = body.decode()
    elif body:
        sha = hashlib.sha256(body).hexdigest()[:12]
        what = f"{(r.getheader('Content-Type') or '?').split(';')[0]} sha={sha}"
        if DUMP:
            (DUMP / sha).write_bytes(body)
    else:
        what = "-"
    if r.getheader("Connection", "").lower() == "close":
        conn.close()
    return f"{r.status} {loc or '-'} {what}"


def batch(port: int, real_port: int, host: str, user: str) -> list[str]:
    """Every path for one host × listener × user, over as few connections as Caddy allows."""
    out, conn = [], None
    for method, path in PATHS:
        for attempt in (1, 2):
            try:
                if conn is None or conn.sock is None:
                    conn = connect(port, real_port, host)
                out.append(one(conn, real_port, host, method, path, user))
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                if conn:
                    conn.close()
                conn = None
                if attempt == 2:
                    out.append(f"ERR {type(e).__name__}")
    if conn:
        conn.close()
    return out


def main() -> None:
    global DUMP, ENV, EXPECTED_AUTH, HOSTS, PATHS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ENV), help="the instance's .env (default: tests/fixtures/full.env)")
    ap.add_argument("--out", default="-")
    ap.add_argument("--dump", help="folder for the bodies Caddy served itself (to diff two runs)")
    a = ap.parse_args()
    ENV = Path(a.env).resolve()
    HOSTS = hosts(render.read_env(ENV))
    USERS["admin"] = (render.read_env(ENV)["ADMIN_USER"], USERS["admin"][1], render.read_env(ENV)["ADMIN_NAME"])
    # The brand's own font path (the home page's), once, among the other paths
    slug = render.read_env(ENV)["BRAND_SLUG"]
    PATHS = list(dict.fromkeys((m, p.replace("{slug}", slug)) for m, p in PATHS))
    if a.dump:
        DUMP = Path(a.dump)
        DUMP.mkdir(parents=True, exist_ok=True)
    caddy = os.environ.get("CADDY") or shutil.which("caddy")
    if not caddy or not shutil.which("docker"):
        print("matrix.py: caddy or docker compose missing", file=sys.stderr)
        sys.exit(2)
    env = caddy_env()
    domain, alias = env["DOMAIN"], env["DOMAIN_ALIAS"]
    EXPECTED_AUTH = f"Basic {env.get('BAZARR_AUTH', '')}"
    tmp = Path(tempfile.mkdtemp(prefix="caddy-matrix-"))
    try:
        # The site as ./homelab configure leaves it, plus a fixed heartbeat (no live data)
        shutil.copytree(ROOT / "site", tmp / "site", ignore=shutil.ignore_patterns("status"))
        for path, data in configure.outputs(render.read_env(ENV)).items():
            rel = path.relative_to(ROOT)
            if rel.parts[0] == "site":
                (tmp / rel).write_bytes(data)
        (tmp / "site/status").mkdir()
        # Seerr's admin is ours (downloads-wizard.sh's requests step): the UNCLAIMED rows go without it, at the end
        (tmp / "site/status/requests-ready").write_text("")
        (tmp / "site/status/heartbeat.json").write_text(json.dumps(
            {"time": 1, "ok": False, "outside": None, "problems": ["stub problem"], "backup_time": None, "disks": []}))

        # The main Caddyfile and the chosen components' files (as ./homelab configure lays them out)
        files = {"Caddyfile": (ROOT / "Caddyfile").read_text()}
        for path, data in configure.outputs(render.read_env(ENV)).items():
            rel = path.relative_to(ROOT)
            if rel.parts[:2] == ("caddy", "enabled"):
                files[str(Path(*rel.parts[1:]))] = data.decode()
        files["Caddyfile"] = re.sub(r"\tcert_issuer acme \{.*?\n\t\}\n",
                                    f"\tlocal_certs\n\tskip_install_trust\n\tstorage file_system {tmp}/data\n"
                                    f"\thttp_port {PORTS[80]}\n\thttps_port {PORTS[443]}\n", files["Caddyfile"], count=1, flags=re.S)
        ups = sorted(set(m.group(0) for t in files.values() for m in UPSTREAM.finditer(t)))
        stubs = {u: 20000 + i for i, u in enumerate(ups)}
        for name, conf in files.items():
            conf = conf.replace("servers :8444 {", f"servers :{PORTS[8444]} {{")
            conf = re.sub(r":8444\b", f":{PORTS[8444]}", conf)
            conf = conf.replace("/srv/site", str(tmp / "site"))
            conf = UPSTREAM.sub(lambda m: f"127.0.0.1:{stubs[m.group(0)]}", conf)
            conf = conf.replace("{$PUBLIC_PORT}", "{$PUBLIC_PORT_LISTEN}")
            (tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp / name).write_text(conf)
        for u, port in stubs.items():
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), stub_handler(u, domain))
            srv.handle_error = lambda *args: None  # clients that hang up early (Caddy's health probes)
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        cenv = dict(env, PUBLIC_PORT=env.get("PUBLIC_PORT", "9443"),
                    HOME=str(tmp), XDG_DATA_HOME=str(tmp / "xdg"), XDG_CONFIG_HOME=str(tmp / "xdg"), PATH=os.environ["PATH"])
        # The public port is a site address too: Caddy listens on its stand-in (PUBLIC_PORT_LISTEN)
        real_public = int(cenv["PUBLIC_PORT"])
        cenv["PUBLIC_PORT_LISTEN"] = str(PORTS[9443])
        log = open(tmp / "caddy.log", "w")
        proc = subprocess.Popen([caddy, "run", "--config", str(tmp / "Caddyfile"), "--adapter", "caddyfile"],
                                env=cenv, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", PORTS[443]), timeout=0.2).close()
                    break
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            if proc.poll() is not None:
                print((tmp / "caddy.log").read_text()[-3000:], file=sys.stderr)
                sys.exit(1)
            time.sleep(1.0)
            # Caddy issues its local certificates in the background: one good handshake per host and
            # TLS listener first, or a slow runner gets a whole host × port of SSLErrors
            for h in HOSTS:
                for d in (domain, alias):
                    for real, port in PORTS.items():
                        if real == 80:
                            continue
                        for _ in range(60):
                            try:
                                connect(port, real, f"{h}.{d}").close()
                                break
                            except (OSError, ssl.SSLError):
                                time.sleep(0.5)
            groups = [(port, real, f"{h}.{d}", user) for h in HOSTS for d in (domain, alias)
                      for real, port in PORTS.items() for user in USERS]
            with concurrent.futures.ThreadPoolExecutor(16) as ex:
                answers = list(ex.map(lambda g: batch(*g), groups))
            jobs, results = [], []
            for (port, real, host, user), res in zip(groups, answers):
                for (method, path), r in zip(PATHS, res):
                    jobs.append((port, real, host, method, path, user))
                    results.append(r)
            # :8444 only with a PROXY header (fallback_policy require): without one, nothing is served
            for h in HOSTS:
                host = f"{h}.{domain}"
                try:
                    r = one(connect(PORTS[8444], 8444, host, proxy=False), 8444, host, "GET", "/", "")
                except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                    r = f"ERR {type(e).__name__}"
                jobs.append((PORTS[8444], "8444-noproxy", host, "GET", "/", ""))
                results.append(r)
            for h, method, path, extra, *as_user in CREDENTIALS + FORWARDING + PUBLIC_LINKS + SIGNED_IN:
                if h not in HOSTS:   # that component isn't chosen in this instance
                    continue
                user = as_user[0] if as_user else ""
                for d in (domain, alias):
                    for real, port in PORTS.items():
                        if real == 80:
                            continue
                        host = f"{h}.{d}"
                        try:
                            r = one(connect(port, real, host), real, host, method, path, user, extra)
                        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                            r = f"ERR {type(e).__name__}"
                        label = "".join(f" [{k}: {v}]" for k, v in extra.items())
                        jobs.append((port, real, host, method, path + label, user))
                        results.append(r)
            (tmp / "site/status/requests-ready").unlink()
            for h, method, path, extra in UNCLAIMED:
                if h not in HOSTS:
                    continue
                for d in (domain, alias):
                    for real, port in PORTS.items():
                        if real == 80:
                            continue
                        host = f"{h}.{d}"
                        try:
                            r = one(connect(port, real, host), real, host, method, path, "", extra)
                        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                            r = f"ERR {type(e).__name__}"
                        jobs.append((port, real, host, method, path + " [before its setup step]", ""))
                        results.append(r)
        finally:
            proc.terminate()
            proc.wait(10)
        lines = []
        for (port, real, host, method, path, user), res in zip(jobs, results):
            # Redirects name the real public port, whatever the stand-in
            res = res.replace(f":{PORTS[9443]}", f":{real_public}").replace(f":{PORTS[8444]}", ":8444")
            lines.append(f"{host}:{real} {method} {path} user={user or '-'} → {res}")
        text = "\n".join(sorted(lines)) + "\n"
        if a.out == "-":
            sys.stdout.write(text)
        else:
            Path(a.out).write_text(text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
