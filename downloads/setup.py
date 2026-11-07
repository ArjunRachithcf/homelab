#!/usr/bin/env python3
"""downloads/setup.py: what the downloads components need that their containers don't do themselves.

Runs in a throwaway container (downloads/run.sh: the stack's python image, stdlib only), never on the host:

    init                    root, before the apps start: their config volumes owned by HOST_UID, the download folders
                            beside the library, qBittorrent's sign-in (password hash, API key) in its config
    apply                   qBittorrent's preferences and categories; Radarr's and Sonarr's sign-in, root folder,
                            download client and media management; Prowlarr's sign-in and its two apps. A second run
                            changes nothing
    status                  JSON for the wizard's checks (no secrets)
    unmonitor-new-seasons   Sonarr: no series monitors seasons that don't exist yet (its Library Import leaves "all")
    guard FREE_PCT          below DOWNLOADS_KEEP_FREE_PCT free: every running torrent stopped and tagged "guard"; 5
                            points above it: those started again. Exit 3 when it stopped some just now

Settings come from the environment, passed by name by downloads/run.sh: HOST_UID, HOST_GID, ADMIN_USER,
DOWNLOADS_ADMIN_PASSWORD, QBITTORRENT_API_KEY, PROWLARR_API_KEY, RADARR_API_KEY, SONARR_API_KEY, GLUETUN_CONTROL_KEY,
DOWNLOADS_SEED_RATIO,
DOWNLOADS_SEED_DAYS, DOWNLOADS_KEEP_FREE_PCT, MOVIES_TV (1 when movies-tv is chosen). Nothing printed holds a secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VOL = Path(os.environ.get("SETUP_VOL", "/vol"))      # the config volumes, one folder each (init)
DATA = Path(os.environ.get("SETUP_DATA", "/data"))   # MEDIA_DIR (init)
QBT_CONF = "qbittorrent-config/qBittorrent/config/qBittorrent.conf"
# The apps as the downloads network sees them (qBittorrent and Prowlarr live in gluetun's network namespace): what
# they're told about each other. SETUP_* only for where this script reaches them (tests/unit/test_downloads.py).
IN = {"qbt": "http://gluetun:8080", "prowlarr": "http://gluetun:9696", "radarr": "http://radarr:7878",
      "sonarr": "http://sonarr:8989", "gluetun": "http://gluetun:8000"}
QBT, PROWLARR, RADARR, SONARR, GLUETUN = (os.environ.get(f"SETUP_{k.upper()}", v) for k, v in IN.items())
# qBittorrent.conf as its official image writes it (MigrationVersion 9999: the [WebUI] section of 5.3, no migration)
QBT_DEFAULT = ("[BitTorrent]\nSession\\DefaultSavePath=/data/torrents\nSession\\TempPathEnabled=false\n"
               "[Meta]\nMigrationVersion=9999\n[WebUI]\nPort=8080\n")
PBKDF2_ROUNDS = 100_000


def env(key: str, default: str | None = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise SystemExit(f"setup.py: {key} isn't set")
    return v


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


# ── init ──────────────────────────────────────────────────────────────────

def qbt_hash(password: str, salt: bytes | None = None) -> str:
    """qBittorrent's Password_PBKDF2 value: PBKDF2-HMAC-SHA512, 100 000 rounds, base64(salt):base64(key)."""
    salt = os.urandom(16) if salt is None else salt
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, PBKDF2_ROUNDS, 64)
    return f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"'


def qbt_hash_ok(stored: str, password: str) -> bool:
    m = re.fullmatch(r'"?@ByteArray\(([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)\)"?', stored.strip())
    if not m:
        return False
    salt, key = base64.b64decode(m.group(1)), base64.b64decode(m.group(2))
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha512", password.encode(), salt, PBKDF2_ROUNDS, len(key)), key)


def ini_set(text: str, section: str, values: dict[str, str]) -> str:
    """Set keys in one section of a Qt INI file, in place; every other line stays as it was. A missing key goes
    after the section's last line, a missing section at the end."""
    lines, head = text.splitlines(), f"[{section}]"
    if head not in (ln.strip() for ln in lines):
        lines.append(head)
    start = next(i for i, ln in enumerate(lines) if ln.strip() == head)
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("[")), len(lines))
    body, todo = lines[start + 1:end], dict(values)
    for j, ln in enumerate(body):
        k = ln.split("=", 1)[0]
        if "=" in ln and k in todo:
            body[j] = f"{k}={todo.pop(k)}"
    last = max((j for j, ln in enumerate(body) if ln.strip()), default=-1)
    body[last + 1:last + 1] = [f"{k}={v}" for k, v in todo.items()]
    lines[start + 1:end] = body
    return "\n".join(lines) + "\n"


def ini_get(text: str, section: str, key: str) -> str | None:
    cur = None
    for ln in text.splitlines():
        if ln.startswith("["):
            cur = ln.strip()[1:-1]
        elif cur == section and ln.split("=", 1)[0] == key:
            return ln.split("=", 1)[1]
    return None


def chown_tree(top: Path, uid: int, gid: int) -> None:
    for root, dirs, files in os.walk(top):
        os.chown(root, uid, gid)
        for f in dirs + files:
            os.lchown(os.path.join(root, f), uid, gid)


def init() -> int:
    uid, gid = int(env("HOST_UID")), int(env("HOST_GID"))
    for vol in sorted(p for p in VOL.iterdir() if p.is_dir()):
        st = vol.stat()
        if (st.st_uid, st.st_gid) != (uid, gid):   # a fresh volume is root's; so was one the image chowned itself
            chown_tree(vol, uid, gid)
            say(f"{vol.name}: now {uid}:{gid}")
    if DATA.is_dir():
        want = ["torrents", "torrents/movies", "torrents/tv"]
        if env("MOVIES_TV", "0") == "1":
            want += ["Movies", "TV Shows"]
        for rel in want:
            p = DATA / rel
            if not p.exists():
                p.mkdir(mode=0o755)
                os.chown(p, uid, gid)
                say(f"{rel}/ made")
            elif rel == "torrents" and p.stat().st_uid != uid:
                os.chown(p, uid, gid)   # Docker makes a missing bind source as root
    conf = VOL / QBT_CONF
    if (VOL / "qbittorrent-config").is_dir():
        user, password, key = env("ADMIN_USER"), env("DOWNLOADS_ADMIN_PASSWORD"), env("QBITTORRENT_API_KEY")
        old = conf.read_text() if conf.is_file() else QBT_DEFAULT
        stored = ini_get(old, "WebUI", "Password_PBKDF2")
        web = {"Username": user, "APIKey": key,
               # Caddy forwards the browser's Host (…:9443), which isn't qBittorrent's own port
               "HostHeaderValidation": "false",
               # no bypass for 127.0.0.1: Prowlarr shares that address (gluetun's namespace)
               "LocalHostAuth": "true"}
        if not (stored and qbt_hash_ok(stored, password)):
            web["Password_PBKDF2"] = qbt_hash(password)
        new = ini_set(old, "WebUI", web)
        if new != old or not conf.is_file():
            conf.parent.mkdir(parents=True, exist_ok=True)
            for d in (conf.parent, conf.parent.parent):
                os.chown(d, uid, gid)
            tmp = conf.with_suffix(".tmp")
            tmp.write_text(new)
            os.chmod(tmp, 0o600)
            os.chown(tmp, uid, gid)
            tmp.replace(conf)
            say("qBittorrent's sign-in written")
    return 0


# ── HTTP ──────────────────────────────────────────────────────────────────

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method: str, url: str, headers: dict[str, str] | None = None, body=None, form=None) -> tuple[int, object, dict]:
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=60) as r:
            code, raw, hdrs = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        code, raw, hdrs = e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(f"{urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}") from None
    text = raw.decode(errors="replace")
    try:
        return code, (json.loads(text) if text[:1] in "[{" else text), hdrs
    except ValueError:
        return code, text, hdrs


class Unreachable(Exception):
    pass


class Failed(Exception):
    pass


class Api:
    """A Servarr app (Radarr, Sonarr: /api/v3; Prowlarr: /api/v1) or qBittorrent (/api/v2, Bearer key)."""

    def __init__(self, name: str, base: str, key: str, prefix: str, bearer: bool = False):
        self.name, self.base, self.prefix = name, base, prefix
        self.headers = {"Authorization": f"Bearer {key}"} if bearer else {"X-Api-Key": key}

    def call(self, method: str, path: str, body=None, form=None, ok=(200, 201, 202, 204)):
        code, data, _ = http(method, self.base + self.prefix + path, self.headers, body, form)
        if code not in ok:
            raise Failed(f"{self.name}: {method} {self.prefix}{path}: HTTP {code} {str(data)[:300]}")
        return data

    def get(self, path):
        return self.call("GET", path)


# ── apply ─────────────────────────────────────────────────────────────────

def qbt() -> Api:
    return Api("qBittorrent", QBT, env("QBITTORRENT_API_KEY"), "/api/v2", bearer=True)


def apply_qbittorrent() -> None:
    q = qbt()
    days, ratio = int(env("DOWNLOADS_SEED_DAYS", "14")), float(env("DOWNLOADS_SEED_RATIO", "2"))
    want = {
        "save_path": "/data/torrents", "temp_path_enabled": False,
        # categories decide the folder (movies, tv), as TRaSH Guides set it up for hardlinks
        "auto_tmm_enabled": True, "torrent_changed_tmm_enabled": True,
        "save_path_changed_tmm_enabled": False, "category_changed_tmm_enabled": False,
        # seed to ratio 2 or for 14 days, then stop: Radarr and Sonarr remove it, the library's hardlink stays
        "max_ratio_enabled": True, "max_ratio": ratio, "max_seeding_time_enabled": True,
        "max_seeding_time": days * 1440, "max_ratio_act": 0,
        # only through the tunnel; no router ports (gluetun sets listen_port from the VPN's forwarded port)
        "current_network_interface": "tun0", "current_interface_address": "0.0.0.0", "upnp": False, "lsd": False,
        "web_ui_host_header_validation_enabled": False, "bypass_local_auth": False,
    }
    have = q.get("/app/preferences")
    diff = {k: v for k, v in want.items() if have.get(k) != v}
    if diff:
        q.call("POST", "/app/setPreferences", form={"json": json.dumps(diff)})
        say(f"qBittorrent: {', '.join(sorted(diff))} set")
    cats = q.get("/torrents/categories")
    wanted = {"movies": "/data/torrents/movies", "tv": "/data/torrents/tv"}
    for cat, path in wanted.items():
        if cat not in cats:
            q.call("POST", "/torrents/createCategory", form={"category": cat, "savePath": path})
            say(f"qBittorrent: category {cat}")
        elif cats[cat].get("savePath") != path:
            q.call("POST", "/torrents/editCategory", form={"category": cat, "savePath": path})
            say(f"qBittorrent: category {cat} → {path}")


def signed_in(base: str, user: str, password: str) -> bool:
    """Servarr's form login: 302 to / with a cookie when it works, to /login?…loginFailed=true when not."""
    code, _, hdrs = http("POST", base + "/login", form={"username": user, "password": password, "rememberMe": "on"})
    loc = hdrs.get("Location", "")
    return code in (302, 303) and "loginFailed" not in loc and "login" not in loc.lower().split("?")[0]


def apply_login(a: Api, base: str) -> None:
    user, password = env("ADMIN_USER"), env("DOWNLOADS_ADMIN_PASSWORD")
    if signed_in(base, user, password):
        return
    host = a.get("/config/host")
    host.update({"authenticationMethod": "forms", "authenticationRequired": "enabled",
                 "username": user, "password": password, "passwordConfirmation": password})
    a.call("PUT", f"/config/host/{host.get('id', 1)}", body=host)
    if not signed_in(base, user, password):
        raise Failed(f"{a.name}: the sign-in doesn't work after setting it")
    say(f"{a.name}: sign-in set")


def apply_arr(a: Api, base: str, root: str, category_field: str, category: str, unmonitor_field: str) -> None:
    apply_login(a, base)
    if not any(r.get("path") == root for r in a.get("/rootfolder")):
        a.call("POST", "/rootfolder", body={"path": root})
        say(f"{a.name}: root folder {root}")
    fields = {"host": "gluetun", "port": 8080, "useSsl": False, "urlBase": "", "apiKey": env("QBITTORRENT_API_KEY"),
              "username": "", "password": "", category_field: category}
    clients = [c for c in a.get("/downloadclient") if c.get("implementation") == "QBittorrent"]
    if clients:
        c = clients[0]
        have = {f["name"]: f.get("value") for f in c["fields"]}
        stale = [k for k, v in fields.items() if k != "apiKey" and have.get(k) != v]
        if stale or not (c.get("enable") and c.get("removeCompletedDownloads") and c.get("removeFailedDownloads")):
            for f in c["fields"]:
                if f["name"] in fields:
                    f["value"] = fields[f["name"]]
            c.update({"enable": True, "removeCompletedDownloads": True, "removeFailedDownloads": True})
            a.call("PUT", f"/downloadclient/{c['id']}", body=c)
            say(f"{a.name}: qBittorrent client updated")
    else:
        schema = next(s for s in a.get("/downloadclient/schema") if s.get("implementation") == "QBittorrent")
        for f in schema["fields"]:
            if f["name"] in fields:
                f["value"] = fields[f["name"]]
        schema.update({"name": "qBittorrent", "enable": True, "priority": 1, "tags": [],
                       "removeCompletedDownloads": True, "removeFailedDownloads": True})
        a.call("POST", "/downloadclient", body=schema)
        say(f"{a.name}: qBittorrent client added")
    mm = a.get("/config/mediamanagement")
    want = {"copyUsingHardlinks": True, unmonitor_field: True, "downloadPropersAndRepacks": "doNotPrefer",
            "minimumFreeSpaceWhenImporting": max(int(mm.get("minimumFreeSpaceWhenImporting") or 0), 10240)}
    if any(mm.get(k) != v for k, v in want.items()):
        mm.update(want)   # the whole object back: a missing boolean would be saved as false
        a.call("PUT", f"/config/mediamanagement/{mm.get('id', 1)}", body=mm)
        say(f"{a.name}: media management (hardlinks, unmonitor deleted, propers)")


def apply_prowlarr(movies_tv: bool) -> None:
    p = Api("Prowlarr", PROWLARR, env("PROWLARR_API_KEY"), "/api/v1")
    apply_login(p, PROWLARR)
    if not movies_tv:
        return
    apps = {a.get("implementation"): a for a in p.get("/applications")}
    for impl, base, key in (("Radarr", IN["radarr"], env("RADARR_API_KEY")), ("Sonarr", IN["sonarr"], env("SONARR_API_KEY"))):
        if impl in apps:
            continue
        schema = next(s for s in p.get("/applications/schema") if s.get("implementation") == impl)
        for f in schema["fields"]:
            f["value"] = {"prowlarrUrl": IN["prowlarr"], "baseUrl": base, "apiKey": key}.get(f["name"], f.get("value"))
        schema.update({"name": impl, "syncLevel": "fullSync", "enable": True, "tags": []})
        try:
            p.call("POST", "/applications", body=schema)
        except Failed as e:
            # In gluetun's namespace "radarr" resolves only through gluetun's DNS, which runs while the tunnel is up
            if "cannot connect" in str(e):
                raise Failed(f"Prowlarr can't reach {impl}: is the VPN up (the start step's checks)? Then again") from e
            raise
        say(f"Prowlarr: {impl} added (its search sites sync there)")


def arrs() -> list[tuple[Api, str, str, str, str, str]]:
    return [(Api("Radarr", RADARR, env("RADARR_API_KEY"), "/api/v3"), RADARR, "/data/Movies", "movieCategory", "movies",
             "autoUnmonitorPreviouslyDownloadedMovies"),
            (Api("Sonarr", SONARR, env("SONARR_API_KEY"), "/api/v3"), SONARR, "/data/TV Shows", "tvCategory", "tv",
             "autoUnmonitorPreviouslyDownloadedEpisodes")]


def apply() -> int:
    movies_tv = env("MOVIES_TV", "0") == "1"
    apply_qbittorrent()
    if movies_tv:
        for a in arrs():
            apply_arr(*a)
    apply_prowlarr(movies_tv)
    say("all set")
    return 0


# ── status, unmonitor-new-seasons, guard ──────────────────────────────────

def status() -> int:
    out: dict = {}
    try:
        g = Api("gluetun", GLUETUN, env("GLUETUN_CONTROL_KEY"), "")
        ip, pf = g.get("/v1/publicip/ip"), g.get("/v1/portforward")
        out["vpn"] = {"public_ip": ip.get("public_ip", ""), "country": ip.get("country", ""), "port": pf.get("port", 0)}
    except (Unreachable, Failed) as e:
        out["vpn"] = {"error": str(e)}
    try:
        q = qbt()
        prefs = q.get("/app/preferences")
        out["qbittorrent"] = {"listen_port": prefs.get("listen_port"), "interface": prefs.get("current_network_interface"),
                              "upnp": prefs.get("upnp"), "save_path": prefs.get("save_path"),
                              "categories": sorted(q.get("/torrents/categories"))}
    except (Unreachable, Failed) as e:
        out["qbittorrent"] = {"error": str(e)}
    try:
        p = Api("Prowlarr", PROWLARR, env("PROWLARR_API_KEY"), "/api/v1")
        out["prowlarr"] = {"apps": sorted(a.get("implementation") for a in p.get("/applications")),
                           "indexers": len(p.get("/indexer"))}
    except (Unreachable, Failed) as e:
        out["prowlarr"] = {"error": str(e)}
    if env("MOVIES_TV", "0") == "1":
        for a, base, root, *_ in arrs():
            try:
                clients = [c for c in a.get("/downloadclient") if c.get("implementation") == "QBittorrent"]
                item = {"root": any(r.get("path") == root and r.get("accessible") for r in a.get("/rootfolder")),
                        "client": bool(clients and clients[0].get("enable")),
                        "indexers": len(a.get("/indexer")),
                        "profiles": sorted(q.get("name", "") for q in a.get("/qualityprofile"))}
                if a.name == "Sonarr":
                    series = a.get("/series")
                    item["monitor_new_seasons"] = sum(1 for s in series if s.get("monitorNewItems", "all") != "none")
                    item["series"] = len(series)
                else:
                    item["movies"] = len(a.get("/movie"))
                    item["monitored_collections"] = sum(1 for c in a.get("/collection") if c.get("monitored"))
                out[a.name.lower()] = item
            except (Unreachable, Failed) as e:
                out[a.name.lower()] = {"error": str(e)}
    print(json.dumps(out, sort_keys=True))
    return 0


def unmonitor_new_seasons() -> int:
    a = arrs()[1][0]
    ids = [s["id"] for s in a.get("/series") if s.get("monitorNewItems", "all") != "none"]
    if ids:
        a.call("PUT", "/series/editor", body={"seriesIds": ids, "monitorNewItems": "none"})
    say(f"Sonarr: {len(ids)} series no longer monitor new seasons" if ids else "Sonarr: no series monitors new seasons")
    return 0


def guard(free_pct: float) -> int:
    keep = float(env("DOWNLOADS_KEEP_FREE_PCT", "15"))
    q = qbt()
    if free_pct < keep:
        run = [t["hash"] for t in q.get("/torrents/info") if not str(t.get("state", "")).startswith("stopped")]
        if run:
            hashes = "|".join(run)
            q.call("POST", "/torrents/addTags", form={"hashes": hashes, "tags": "guard"})
            q.call("POST", "/torrents/stop", form={"hashes": hashes})
            print(f"{len(run)} torrent(s) stopped: {free_pct:.1f} % free, below {keep:g} %")
            return 3
        return 0
    if free_pct >= keep + 5:
        held = [t["hash"] for t in q.get("/torrents/info?tag=guard")]
        if held:
            hashes = "|".join(held)
            q.call("POST", "/torrents/start", form={"hashes": hashes})
            q.call("POST", "/torrents/removeTags", form={"hashes": hashes, "tags": "guard"})
            print(f"{len(held)} torrent(s) started again: {free_pct:.1f} % free")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "init":
            return init()
        if cmd == "apply":
            return apply()
        if cmd == "status":
            return status()
        if cmd == "unmonitor-new-seasons":
            return unmonitor_new_seasons()
        if cmd == "guard" and len(argv) == 3:
            return guard(float(argv[2]))
    except (Unreachable, Failed) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
