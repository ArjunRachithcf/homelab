#!/usr/bin/env python3
"""downloads/setup.py: what the downloads components need that their containers don't do themselves.

Runs in a throwaway container (downloads/run.sh: the stack's python image, stdlib only), never on the host:

    init                    root, before the apps start: their config volumes owned by HOST_UID, the download folders
                            beside the libraries, qBittorrent's sign-in (password hash, API key, Caddy's address
                            needs none) in its config, Bazarr's (Basic, its API key, no analytics) in a fresh volume
    apply                   every chosen app as far as it is set up: qBittorrent's preferences and categories; the
                            sign-in, root folder, download client and media management of Radarr, Sonarr and Lidarr;
                            Prowlarr's sign-in, its apps and Byparr; Bazarr's sign-in, languages, Sonarr and Radarr,
                            providers; Bindery's folders, qBittorrent and Prowlarr, and Seerr's settings once their
                            own steps below ran. A second run changes nothing
    bindery-init            right after Bindery's first start: its sign-in through Authentik (an OIDC provider),
                            which also closes its first-run setup. Exit 1 when someone set it up already
    seerr-init              right after Seerr's first start: its admin (user 1) is Jellyfin's own admin
                            (JELLYFIN_ADMIN_USER, the password once in JELLYFIN_ADMIN_PASSWORD, never stored). Exit 1
                            when the admin or the Jellyfin it points at isn't that
    seerr-users             Jellyfin's users into Seerr, their permissions from Authentik's groups (SEERR_ADMINS,
                            SEERR_FAMILY, SEERR_KIDS: usernames, commas between): admins admin, adults approved
                            automatically, kids may only ask
    status                  JSON for the wizard's checks (no secrets)
    unmonitor-new-seasons   Sonarr: no series monitors seasons that don't exist yet (its Library Import leaves "all")
    guard FREE_PCT          below DOWNLOADS_KEEP_FREE_PCT free: every running torrent stopped and tagged "guard"; 5
                            points above it: those started again. Exit 3 when it stopped some just now

Settings come from the environment, passed by name by downloads/run.sh: COMPONENTS (the chosen ones), HOST_UID,
HOST_GID, ADMIN_USER, DOMAIN, NETBIRD_PROXY_PORT, BRAND, DOWNLOADS_NET, MEDIA_DIR, AUDIOBOOKS_DIR, each app's API key
and password (*_API_KEY, *_PASSWORD), GLUETUN_CONTROL_KEY, BINDERY_OIDC_CLIENT_SECRET, SEERR_NTFY_TOKEN, and the
choices: DOWNLOADS_SEED_RATIO, DOWNLOADS_SEED_DAYS, DOWNLOADS_KEEP_FREE_PCT, LIDARR_QUALITY_PROFILE, BAZARR_LANGUAGES,
SEERR_MOVIE_QUOTA, SEERR_TV_QUOTA, SEERR_QUOTA_DAYS, SEERR_NTFY_TOPIC. Nothing printed holds a secret.
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
BAZARR_CONF = "bazarr-config/config/config.yaml"
# The apps as the downloads network sees them (qBittorrent, Prowlarr and Byparr live in gluetun's network namespace):
# what they're told about each other. SETUP_* only for where this script reaches them (tests/unit/test_downloads.py).
IN = {"qbt": "http://gluetun:8080", "prowlarr": "http://gluetun:9696", "radarr": "http://radarr:7878",
      "sonarr": "http://sonarr:8989", "lidarr": "http://lidarr:8686", "bazarr": "http://bazarr:6767",
      "bindery": "http://bindery:8787", "seerr": "http://seerr:5055", "gluetun": "http://gluetun:8000"}
QBT, PROWLARR, RADARR, SONARR, LIDARR, BAZARR, BINDERY, SEERR, GLUETUN = (
    os.environ.get(f"SETUP_{k.upper()}", v) for k, v in IN.items())
# Byparr as Prowlarr reaches it: its own loopback (gluetun's namespace)
BYPARR = "http://localhost:8191/"
# qBittorrent.conf as its official image writes it (MigrationVersion 9999: the [WebUI] section of 5.3, no migration)
QBT_DEFAULT = ("[BitTorrent]\nSession\\DefaultSavePath=/data/torrents\nSession\\TempPathEnabled=false\n"
               "[Meta]\nMigrationVersion=9999\n[WebUI]\nPort=8080\n")
PBKDF2_ROUNDS = 100_000
# Every value this script may know and never says (Failed's text, status)
SECRET_KEYS = ("QBITTORRENT_API_KEY", "QBITTORRENT_PASSWORD", "PROWLARR_API_KEY", "PROWLARR_PASSWORD",
               "RADARR_API_KEY", "RADARR_PASSWORD", "SONARR_API_KEY", "SONARR_PASSWORD", "LIDARR_API_KEY",
               "LIDARR_PASSWORD", "BAZARR_API_KEY", "BAZARR_AUTH", "BINDERY_API_KEY", "BINDERY_OIDC_CLIENT_SECRET",
               "SEERR_API_KEY", "SEERR_NTFY_TOKEN", "GLUETUN_CONTROL_KEY", "JELLYFIN_ADMIN_PASSWORD")
# Seerr's permission bits (server/lib/permissions.ts)
ADMIN, MANAGE_REQUESTS, REQUEST, AUTO_APPROVE = 2, 16, 32, 128


def env(key: str, default: str | None = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise SystemExit(f"setup.py: {key} isn't set")
    return v


def chosen(component: str) -> bool:
    return component in env("COMPONENTS", "").split(",")


def redact(text: str) -> str:
    for k in SECRET_KEYS:
        v = os.environ.get(k, "")
        # also without what a CRLF .env leaves on it: Python's messages show that \r escaped, beside the rest
        for x in (v, v.strip()):
            if len(x) >= 6:
                text = text.replace(x, "…")
    return text


def bazarr_login() -> tuple[str, str]:
    """Bazarr's own login from BAZARR_AUTH (base64 of user:password, what Caddy sends it)."""
    user, _, password = base64.b64decode(env("BAZARR_AUTH")).decode().partition(":")
    return user, password


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def site(name: str) -> str:
    """A site's canonical address, as Caddy serves it (and as the apps link to it)."""
    return f"https://{name}.{env('DOMAIN')}:{env('NETBIRD_PROXY_PORT', '9443')}"


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


def write_owned(path: Path, text: str, uid: int, gid: int) -> None:
    """A config file only its app reads: HOST_UID's, 0600, written whole (a temporary file renamed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for d in (path.parent, *[p for p in path.parents if VOL in p.parents]):
        os.chown(d, uid, gid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.chown(tmp, uid, gid)
    tmp.replace(path)


def folders() -> list[str]:
    """The media folder's parts the chosen apps write to: downloads under torrents/, then their libraries."""
    want = ["torrents"]
    if chosen("movies-tv"):
        want += ["torrents/movies", "torrents/tv", "Movies", "TV Shows"]
    if chosen("albums"):
        want += ["torrents/music", "Music"]
    if chosen("book-requests"):
        want += ["torrents/books", "torrents/audiobooks", "Books", "Books/Books"]
        books = audiobooks_rel()
        if books:
            want.append(books)
    return want


def audiobooks_rel() -> str | None:
    """AUDIOBOOKS_DIR inside the media folder, relative to it; None (and a warning) when it's elsewhere: init sees
    only the media folder."""
    media = env("MEDIA_DIR", "/srv/media").rstrip("/")
    books = env("AUDIOBOOKS_DIR", "/srv/media/audiobooks").rstrip("/")
    if books.startswith(media + "/"):
        return books[len(media) + 1:]
    say(f"⚠ {books} is outside the media folder: make it yourself, owned by you ({env('HOST_UID')}), for Bindery")
    return None


def init() -> int:
    uid, gid = int(env("HOST_UID")), int(env("HOST_GID"))
    for vol in sorted(p for p in VOL.iterdir() if p.is_dir()) if VOL.is_dir() else []:   # none made yet: no /vol
        st = vol.stat()
        if (st.st_uid, st.st_gid) != (uid, gid):   # a fresh volume is root's; so was one the image chowned itself
            chown_tree(vol, uid, gid)
            say(f"{vol.name}: now {uid}:{gid}")
    if DATA.is_dir():
        for rel in folders():
            p = DATA / rel
            if not p.exists():
                p.mkdir(mode=0o755)
                os.chown(p, uid, gid)
                say(f"{rel}/ made")
            elif p.stat().st_uid == 0 and p.is_dir() and not any(p.iterdir()):
                os.chown(p, uid, gid)   # an empty one of root's: what Docker makes for a missing bind source
    conf = VOL / QBT_CONF
    if (VOL / "qbittorrent-config").is_dir():
        user, password = env("ADMIN_USER"), env("QBITTORRENT_PASSWORD")
        old = conf.read_text() if conf.is_file() else QBT_DEFAULT
        stored = ini_get(old, "WebUI", "Password_PBKDF2")
        web = {"Username": user, "APIKey": env("QBITTORRENT_API_KEY"),
               # Caddy forwards the browser's Host (…:9443), which isn't qBittorrent's own port
               "HostHeaderValidation": "false",
               # no bypass for 127.0.0.1: Prowlarr and Byparr share that address (gluetun's namespace)
               "LocalHostAuth": "true",
               # Caddy's fixed address only, after Authentik: no second login there
               "AuthSubnetWhitelistEnabled": "true",
               "AuthSubnetWhitelist": f"{env('DOWNLOADS_NET', '198.51.100')}.11/32"}
        if not (stored and qbt_hash_ok(stored, password)):
            web["Password_PBKDF2"] = qbt_hash(password)
        new = ini_set(old, "WebUI", web)
        if new != old or not conf.is_file():
            write_owned(conf, new, uid, gid)
            say("qBittorrent's sign-in written")
    if (VOL / "bazarr-config").is_dir() and not (VOL / BAZARR_CONF).exists():
        # Its first start writes the rest around these and keeps them; later changes go over its API (apply). Without
        # them it would start without a login, its API key in its page.
        user, password = bazarr_login()
        md5 = hashlib.md5(password.encode()).hexdigest()
        write_owned(VOL / BAZARR_CONF, "analytics:\n  enabled: false\nauth:\n"
                    f"  apikey: '{env('BAZARR_API_KEY')}'\n  password: '{md5}'\n  type: basic\n"
                    f"  username: '{user}'\n", uid, gid)
        say("Bazarr's sign-in written")
    return 0


# ── HTTP ──────────────────────────────────────────────────────────────────

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method: str, url: str, headers: dict[str, str] | None = None, body=None, form=None,
         timeout: int = 60) -> tuple[int, object, dict]:
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as r:
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


class Claimed(Failed):
    """Someone else set the app up first: nothing of ours goes to it."""


class Api:
    """An app's API: Servarr (Radarr, Sonarr: /api/v3; Prowlarr, Lidarr: /api/v1) and Seerr, Bindery (X-Api-Key),
    Bazarr (X-API-KEY, its form-encoded settings), qBittorrent (/api/v2, Bearer key)."""

    def __init__(self, name: str, base: str, key: str, prefix: str, bearer: bool = False):
        self.name, self.base, self.prefix = name, base, prefix
        self.headers = {"Authorization": f"Bearer {key}"} if bearer else {"X-Api-Key": key}

    def call(self, method: str, path: str, body=None, form=None, ok=(200, 201, 202, 204), timeout: int = 60):
        code, data, _ = http(method, self.base + self.prefix + path, self.headers, body, form, timeout)
        if code not in ok:
            raise Failed(f"{self.name}: {method} {self.prefix}{path}: HTTP {code} {redact(str(data))[:300]}")
        return data

    def get(self, path):
        return self.call("GET", path)


def fields(item: dict) -> dict:
    return {f["name"]: f.get("value") for f in item.get("fields", [])}


def set_fields(item: dict, values: dict) -> None:
    for f in item["fields"]:
        if f["name"] in values:
            f["value"] = values[f["name"]]


# ── qBittorrent ───────────────────────────────────────────────────────────

def qbt() -> Api:
    return Api("qBittorrent", QBT, env("QBITTORRENT_API_KEY"), "/api/v2", bearer=True)


def categories() -> dict[str, str]:
    """qBittorrent's categories, one per kind of download the chosen apps ask for: they decide the folder (TRaSH
    Guides' layout for hardlinks)."""
    cats = {}
    if chosen("movies-tv"):
        cats.update(movies="/data/torrents/movies", tv="/data/torrents/tv")
    if chosen("albums"):
        cats["music"] = "/data/torrents/music"
    if chosen("book-requests"):
        cats.update(books="/data/torrents/books", audiobooks="/data/torrents/audiobooks")
    return cats


def apply_qbittorrent() -> None:
    q = qbt()
    days, ratio = int(env("DOWNLOADS_SEED_DAYS", "14")), float(env("DOWNLOADS_SEED_RATIO", "2"))
    want = {
        "save_path": "/data/torrents", "temp_path_enabled": False,
        "auto_tmm_enabled": True, "torrent_changed_tmm_enabled": True,
        "save_path_changed_tmm_enabled": False, "category_changed_tmm_enabled": False,
        # seed to ratio 2 or for 14 days, then stop: the apps remove it, the library's hardlink stays
        "max_ratio_enabled": True, "max_ratio": ratio, "max_seeding_time_enabled": True,
        "max_seeding_time": days * 1440, "max_ratio_act": 0,
        # only through the tunnel; no router ports (gluetun sets listen_port from the VPN's forwarded port)
        "current_network_interface": "tun0", "current_interface_address": "0.0.0.0", "upnp": False, "lsd": False,
        "web_ui_host_header_validation_enabled": False, "bypass_local_auth": False,
        # Caddy's address only (after Authentik), everyone else signs in
        "bypass_auth_subnet_whitelist_enabled": True,
        "bypass_auth_subnet_whitelist": f"{env('DOWNLOADS_NET', '198.51.100')}.11/32",
    }
    have = q.get("/app/preferences")
    diff = {k: v for k, v in want.items() if have.get(k) != v}
    if diff:
        q.call("POST", "/app/setPreferences", form={"json": json.dumps(diff)})
        say(f"qBittorrent: {', '.join(sorted(diff))} set")
    cats = q.get("/torrents/categories")
    for cat, path in categories().items():
        if cat not in cats:
            q.call("POST", "/torrents/createCategory", form={"category": cat, "savePath": path})
            say(f"qBittorrent: category {cat}")
        elif cats[cat].get("savePath") != path:
            q.call("POST", "/torrents/editCategory", form={"category": cat, "savePath": path})
            say(f"qBittorrent: category {cat} → {path}")


# ── Radarr, Sonarr, Lidarr, Prowlarr ──────────────────────────────────────

def signed_in(base: str, user: str, password: str) -> bool:
    """Servarr's form login: 302 to / with a cookie when it works, to /login?…loginFailed=true when not."""
    code, _, hdrs = http("POST", base + "/login", form={"username": user, "password": password, "rememberMe": "on"})
    loc = hdrs.get("Location", "")
    return code in (302, 303) and "loginFailed" not in loc and "login" not in loc.lower().split("?")[0]


def apply_login(a: Api, base: str, password_key: str) -> None:
    """The app's own login, its own generated password: break-glass, and the way in for anyone but Caddy's
    signed-in clients (docker-compose.yml)."""
    user, password = env("ADMIN_USER"), env(password_key)
    if signed_in(base, user, password):
        return
    host = a.get("/config/host")
    host.update({"authenticationMethod": "forms", "username": user, "password": password,
                 "passwordConfirmation": password})
    a.call("PUT", f"/config/host/{host.get('id', 1)}", body=host)
    if not signed_in(base, user, password):
        raise Failed(f"{a.name}: the sign-in doesn't work after setting it")
    say(f"{a.name}: sign-in set")


def apply_client(a: Api, category_field: str, category: str) -> None:
    """qBittorrent as the app's download client, through its API key."""
    want = {"host": "gluetun", "port": 8080, "useSsl": False, "urlBase": "", "apiKey": env("QBITTORRENT_API_KEY"),
            "username": "", "password": "", category_field: category}
    clients = [c for c in a.get("/downloadclient") if c.get("implementation") == "QBittorrent"]
    if clients:
        c = clients[0]
        have = fields(c)
        stale = [k for k, v in want.items() if k != "apiKey" and have.get(k) != v]
        if stale or not (c.get("enable") and c.get("removeCompletedDownloads") and c.get("removeFailedDownloads")):
            set_fields(c, want)
            c.update({"enable": True, "removeCompletedDownloads": True, "removeFailedDownloads": True})
            a.call("PUT", f"/downloadclient/{c['id']}", body=c)
            say(f"{a.name}: qBittorrent client updated")
    else:
        schema = next(s for s in a.get("/downloadclient/schema") if s.get("implementation") == "QBittorrent")
        set_fields(schema, want)
        schema.update({"name": "qBittorrent", "enable": True, "priority": 1, "tags": [],
                       "removeCompletedDownloads": True, "removeFailedDownloads": True})
        a.call("POST", "/downloadclient", body=schema)
        say(f"{a.name}: qBittorrent client added")


def apply_media_management(a: Api, unmonitor_field: str) -> None:
    mm = a.get("/config/mediamanagement")
    want = {"copyUsingHardlinks": True, unmonitor_field: True, "downloadPropersAndRepacks": "doNotPrefer",
            "minimumFreeSpaceWhenImporting": max(int(mm.get("minimumFreeSpaceWhenImporting") or 0), 10240)}
    if any(mm.get(k) != v for k, v in want.items()):
        mm.update(want)   # the whole object back: a missing boolean would be saved as false
        a.call("PUT", f"/config/mediamanagement/{mm.get('id', 1)}", body=mm)
        say(f"{a.name}: media management (hardlinks, unmonitor deleted, propers)")


def apply_arr(a: Api, base: str, password_key: str, root: str, category_field: str, category: str,
              unmonitor_field: str) -> None:
    apply_login(a, base, password_key)
    if not any(r.get("path") == root for r in a.get("/rootfolder")):
        a.call("POST", "/rootfolder", body={"path": root})
        say(f"{a.name}: root folder {root}")
    apply_client(a, category_field, category)
    apply_media_management(a, unmonitor_field)


def radarr() -> Api:
    return Api("Radarr", RADARR, env("RADARR_API_KEY"), "/api/v3")


def sonarr() -> Api:
    return Api("Sonarr", SONARR, env("SONARR_API_KEY"), "/api/v3")


def lidarr() -> Api:
    return Api("Lidarr", LIDARR, env("LIDARR_API_KEY"), "/api/v1")


def arrs() -> list[tuple]:
    return [(radarr(), RADARR, "RADARR_PASSWORD", "/data/Movies", "movieCategory", "movies",
             "autoUnmonitorPreviouslyDownloadedMovies"),
            (sonarr(), SONARR, "SONARR_PASSWORD", "/data/TV Shows", "tvCategory", "tv",
             "autoUnmonitorPreviouslyDownloadedEpisodes")]


def by_name(items: list[dict], name: str) -> dict:
    """The item called name, else the first: a profile Recyclarr hasn't made yet, a renamed one."""
    return next((i for i in items if i.get("name") == name), items[0] if items else {})


def apply_lidarr() -> None:
    """Albums into Music/, the files exactly as downloaded: no tags written and no renames (a changed file is no
    longer the torrent's hardlink), and nothing monitored unless someone asks (an artist's new albums neither)."""
    a = lidarr()
    apply_login(a, LIDARR, "LIDARR_PASSWORD")
    quality = by_name(a.get("/qualityprofile"), env("LIDARR_QUALITY_PROFILE", "Standard"))
    metadata = by_name(a.get("/metadataprofile"), "Standard")
    want = {"defaultQualityProfileId": quality.get("id"), "defaultMetadataProfileId": metadata.get("id"),
            "defaultMonitorOption": "none", "defaultNewItemMonitorOption": "none"}
    roots = [r for r in a.get("/rootfolder") if r.get("path") == "/data/Music"]
    if not roots:
        # Its first scan adds the artists already there, monitored by nobody, their files where they are
        a.call("POST", "/rootfolder", body={"name": "Music", "path": "/data/Music", "defaultTags": [], **want})
        say("Lidarr: root folder /data/Music")
    elif any(roots[0].get(k) != v for k, v in want.items()):
        a.call("PUT", f"/rootfolder/{roots[0]['id']}", body=dict(roots[0], **want))
        say("Lidarr: root folder's defaults (monitor none)")
    meta = a.get("/config/metadataprovider")
    if meta.get("writeAudioTags") != "no" or meta.get("scrubAudioTags"):
        meta.update({"writeAudioTags": "no", "scrubAudioTags": False})
        a.call("PUT", f"/config/metadataprovider/{meta.get('id', 1)}", body=meta)
        say("Lidarr: no tags written into the files")
    naming = a.get("/config/naming")
    if naming.get("renameTracks"):
        naming["renameTracks"] = False
        a.call("PUT", f"/config/naming/{naming.get('id', 1)}", body=naming)
        say("Lidarr: no renames")
    apply_client(a, "musicCategory", "music")
    apply_media_management(a, "autoUnmonitorPreviouslyDownloadedTracks")


def apply_prowlarr() -> None:
    p = Api("Prowlarr", PROWLARR, env("PROWLARR_API_KEY"), "/api/v1")
    apply_login(p, PROWLARR, "PROWLARR_PASSWORD")
    want = []
    if chosen("movies-tv"):
        want += [("Radarr", IN["radarr"], "RADARR_API_KEY", None), ("Sonarr", IN["sonarr"], "SONARR_API_KEY", None)]
    if chosen("albums"):
        # without 3030 (audiobooks): those are Bindery's
        want.append(("Lidarr", IN["lidarr"], "LIDARR_API_KEY", lambda cats: [c for c in cats if c != 3030]))
    apps = {a.get("implementation"): a for a in p.get("/applications")}
    for impl, base, key, cats in want:
        if impl in apps:
            continue
        schema = next(s for s in p.get("/applications/schema") if s.get("implementation") == impl)
        for f in schema["fields"]:
            f["value"] = {"prowlarrUrl": IN["prowlarr"], "baseUrl": base,
                          "apiKey": env(key)}.get(f["name"], f.get("value"))
            if f["name"] == "syncCategories" and cats:
                f["value"] = cats(f.get("value") or [])
        schema.update({"name": impl, "syncLevel": "fullSync", "enable": True, "tags": []})
        try:
            p.call("POST", "/applications", body=schema)
        except Failed as e:
            # In gluetun's namespace "radarr" resolves only through gluetun's DNS, which runs while the tunnel is up
            if "cannot connect" in str(e).lower() or "unable to" in str(e).lower():
                raise Failed(f"Prowlarr can't reach {impl}: is the VPN up (the start step's checks)? Then again") from e
            raise
        say(f"Prowlarr: {impl} added (its search sites sync there)")


def apply_byparr(p: Api) -> None:
    """Byparr as Prowlarr's FlareSolverr proxy, only for the search sites tagged cloudflare (the indexers step)."""
    tags = {t.get("label"): t.get("id") for t in p.get("/tag")}
    if "cloudflare" not in tags:
        tags["cloudflare"] = p.call("POST", "/tag", body={"label": "cloudflare"})["id"]
        say("Prowlarr: tag cloudflare")
    want = {"host": BYPARR, "requestTimeout": 60}
    proxies = [x for x in p.get("/indexerproxy") if x.get("implementation") == "FlareSolverr"]
    if proxies and fields(proxies[0]).get("host") == BYPARR and proxies[0].get("tags") == [tags["cloudflare"]]:
        return
    item = proxies[0] if proxies else next(s for s in p.get("/indexerproxy/schema")
                                           if s.get("implementation") == "FlareSolverr")
    set_fields(item, want)
    item.update({"name": "Byparr", "tags": [tags["cloudflare"]]})
    # Prowlarr tests it by fetching a site through Byparr, so through the tunnel (forceSave skips only warnings)
    try:
        if proxies:
            p.call("PUT", f"/indexerproxy/{item['id']}?forceSave=true", body=item, timeout=180)
        else:
            p.call("POST", "/indexerproxy?forceSave=true", body=item, timeout=180)
    except Failed as e:
        if "Unable to connect to proxy" in str(e):
            raise Failed("Prowlarr: Byparr couldn't fetch its test page: is the VPN up (the start step's checks)? "
                         "Then again") from e
        raise
    say("Prowlarr: Byparr for the sites tagged cloudflare")


# ── Bazarr ────────────────────────────────────────────────────────────────

def bazarr() -> Api:
    a = Api("Bazarr", BAZARR, env("BAZARR_API_KEY"), "/api")
    a.headers = {"X-API-KEY": env("BAZARR_API_KEY")}
    return a


def bazarr_signed_in() -> bool:
    """Its login works: Caddy's credentials in, none refused (with no login at all, both would be let in)."""
    return http("GET", BAZARR + "/")[0] == 401 \
        and http("GET", BAZARR + "/", {"Authorization": f"Basic {env('BAZARR_AUTH')}"})[0] == 200


def bazarr_languages() -> list[str]:
    return [x.strip() for x in env("BAZARR_LANGUAGES", "en").split(",") if x.strip()]


def apply_bazarr() -> None:
    """Subtitles in BAZARR_LANGUAGES for what Radarr and Sonarr monitor, from providers without an account."""
    b = bazarr()
    form: dict[str, object] = {}
    if not bazarr_signed_in():
        user, password = bazarr_login()
        form.update({"settings-auth-type": "basic", "settings-auth-username": user, "settings-auth-password": password})
    s = b.get("/system/settings")
    langs = bazarr_languages()
    profile = [{"profileId": 1, "name": "+".join(x.upper() for x in langs), "cutoff": None, "mustContain": [],
                "mustNotContain": [], "originalFormat": False, "tag": None,
                "items": [{"id": i, "language": x, "hi": "False", "forced": "False", "audio_exclude": "False",
                           "audio_only_include": "False"} for i, x in enumerate(langs, 1)]}]
    have = [{k: v for k, v in p.items() if k in profile[0]} for p in b.get("/system/languages/profiles")]
    if have != profile:
        form.update({"languages-enabled": langs, "languages-profiles": json.dumps(profile)})
    g, sonarr_s, radarr_s = s.get("general", {}), s.get("sonarr", {}), s.get("radarr", {})
    general = {"serie_default_enabled": True, "serie_default_profile": 1, "movie_default_enabled": True,
               "movie_default_profile": 1, "use_embedded_subs": False}
    providers = ["embeddedsubtitles", "gestdown", "yifysubtitles"]
    if sorted(g.get("enabled_providers") or []) != providers:
        form["settings-general-enabled_providers"] = providers
    for k, v in general.items():
        if g.get(k) != v and str(g.get(k)) != str(v):
            form[f"settings-general-{k}"] = str(v).lower() if isinstance(v, bool) else v
    if s.get("analytics", {}).get("enabled"):
        form["settings-analytics-enabled"] = "false"
    # The defaults above count only for what a sync adds, so they go first; then Sonarr and Radarr
    if form:
        b.call("POST", "/system/settings", form=form)
        say(f"Bazarr: {', '.join(sorted({k.split('-')[1] if k.startswith('settings-') else k for k in form}))}")
    form = {}
    for app, cur, port, key in (("sonarr", sonarr_s, 8989, "SONARR_API_KEY"),
                                ("radarr", radarr_s, 7878, "RADARR_API_KEY")):
        # base_url: Bazarr keeps "/" as ""
        want = {"ip": app, "port": port, "base_url": "", "ssl": False, "apikey": env(key), "only_monitored": True}
        for k, v in want.items():
            if cur.get(k) != v:
                form[f"settings-{app}-{k}"] = str(v).lower() if isinstance(v, bool) else v
        if not g.get(f"use_{app}"):
            form[f"settings-general-use_{app}"] = "true"
    if form:
        b.call("POST", "/system/settings", form=form)
        say("Bazarr: Sonarr and Radarr connected")
    if not bazarr_signed_in():
        raise Failed("Bazarr: its sign-in doesn't work after setting it")


# ── Bindery ───────────────────────────────────────────────────────────────

def bindery() -> Api:
    return Api("Bindery", BINDERY, env("BINDERY_API_KEY"), "/api/v1")


def bindery_provider() -> dict:
    return {"id": "authentik", "name": env("BRAND", "Home"), "issuer": f"{site('auth')}/application/o/bindery/",
            "client_id": "bindery", "scopes": ["openid", "email", "profile"], "allowed_groups": ["family"]}


BINDERY_CLAIMED = ("Bindery: someone set it up before its sign-in through Authentik ({}): stop it, remove its volume "
                   "(bindery-config) and start this step again")


def bindery_ours(b: Api) -> None:
    """Its only sign-in is ours: no provider of anyone else's (theirs would keep them signing in, as its admin)."""
    others = [p.get("id") for p in b.get("/auth/oidc/providers") if p.get("id") != "authentik"]
    if others:
        raise Claimed(BINDERY_CLAIMED.format(f"another sign-in: {', '.join(map(str, others))}"))


def bindery_init() -> int:
    b = bindery()
    bindery_ours(b)
    if not bindery_ready(b):
        # Until its provider is there nobody can sign in through Authentik: any account is someone's first-run setup
        if b.get("/auth/users"):
            raise Claimed(BINDERY_CLAIMED.format("an account exists"))
        b.call("PUT", "/auth/oidc/providers",
               body=[dict(bindery_provider(), client_secret=env("BINDERY_OIDC_CLIENT_SECRET"))])
        say("Bindery: signs in through Authentik (its own setup page is closed)")
    elif next(p for p in b.get("/auth/oidc/providers") if p.get("id") == "authentik").get("issuer") \
            != bindery_provider()["issuer"]:
        raise Claimed(BINDERY_CLAIMED.format("its sign-in points elsewhere"))
    status = http("GET", BINDERY + "/api/v1/auth/status")[1]
    if not isinstance(status, dict) or status.get("setupRequired"):
        raise Failed(f"Bindery: its first-run setup is still open: {str(status)[:200]}")
    return 0


def bindery_ready(b: Api) -> bool:
    return any(p.get("id") == "authentik" for p in b.get("/auth/oidc/providers"))


def apply_bindery() -> None:
    """Books and audiobooks the family asks for: its two libraries, qBittorrent (categories books, audiobooks),
    Prowlarr's search sites."""
    b = bindery()
    try:
        ready = bindery_ready(b)
    except Unreachable:
        ready = False   # not started: its own step starts it
    if not ready:
        say("Bindery: not set up yet (its own step: books)")
        return
    bindery_ours(b)
    have = next(p for p in b.get("/auth/oidc/providers") if p.get("id") == "authentik")
    want = bindery_provider()
    # Not when only its discovery failed (Authentik unreachable just then): it tries again at the next sign-in
    if any(have.get(k) != v for k, v in want.items()):
        b.call("PUT", "/auth/oidc/providers", body=[dict(want, client_secret=env("BINDERY_OIDC_CLIENT_SECRET"))])
        say("Bindery: its Authentik sign-in updated")
    roots = {r.get("path"): r.get("id") for r in b.get("/rootfolder")}
    for path in ("/books", "/audiobooks"):
        if path not in roots:
            roots[path] = b.call("POST", "/rootfolder", body={"path": path})["id"]
            say(f"Bindery: library {path}")
    settings = {s.get("key"): s.get("value") for s in b.get("/setting")}
    want = {"library.defaultRootFolderId": str(roots["/books"]),
            "library.defaultAudiobookRootFolderId": str(roots["/audiobooks"]),
            # An author someone asks for: none of their books monitored (its default, all, would fetch every one)
            "author.default_monitor_mode": "none"}
    for key, value in want.items():
        if str(settings.get(key)) != value:
            b.call("PUT", f"/setting/{key}", body={"value": value})
            say(f"Bindery: {key} {value}")
    want_client = {"name": "qBittorrent", "type": "qbittorrent", "host": "gluetun", "port": 8080, "useSsl": False,
                   "urlBase": "", "username": env("ADMIN_USER"), "category": "books", "categoryAudiobook": "audiobooks",
                   "enabled": True, "enabledForBooks": True, "enabledForAudiobooks": True, "removeOnImport": False,
                   "priority": 1}
    clients = [c for c in b.get("/downloadclient") if c.get("type") == "qbittorrent"]
    if not clients:
        b.call("POST", "/downloadclient", body=dict(want_client, password=env("QBITTORRENT_PASSWORD")))
        say("Bindery: qBittorrent")
    elif any(clients[0].get(k) != v for k, v in want_client.items()):
        b.call("PUT", f"/downloadclient/{clients[0]['id']}",
               body=dict(clients[0], **want_client, password=env("QBITTORRENT_PASSWORD")))
        say("Bindery: qBittorrent updated")
    want_prowlarr = {"name": "Prowlarr", "url": IN["prowlarr"], "enabled": True, "syncOnStartup": True}
    found = b.get("/prowlarr")
    if not found:
        pid = b.call("POST", "/prowlarr", body=dict(want_prowlarr, apiKey=env("PROWLARR_API_KEY")))["id"]
        say("Bindery: Prowlarr")
    else:
        pid = found[0]["id"]
        if any(found[0].get(k) != v for k, v in want_prowlarr.items()):
            b.call("PUT", f"/prowlarr/{pid}", body=dict(found[0], **want_prowlarr, apiKey=env("PROWLARR_API_KEY")))
    # Bindery pulls the search sites (Prowlarr has no Bindery app): again at every run, quietly
    b.call("POST", f"/prowlarr/{pid}/sync")


# ── Seerr ─────────────────────────────────────────────────────────────────

def seerr() -> Api:
    return Api("Seerr", SEERR, env("SEERR_API_KEY"), "/api/v1")


def jellyfin_target() -> tuple[str, int, bool, str]:
    """Jellyfin as Seerr reaches it: its site through Caddy (JELLYFIN_URL only for tests)."""
    u = urllib.parse.urlsplit(os.environ.get("JELLYFIN_URL") or site("jellyfin"))
    ssl = u.scheme == "https"
    return u.hostname or "", u.port or (443 if ssl else 80), ssl, u.path.rstrip("/")


def seerr_ready(s: Api) -> bool:
    """User 1 exists (until then Seerr answers every API key with 403)."""
    return http("GET", SEERR + "/api/v1/settings/main", s.headers)[0] == 200


def seerr_ours(s: Api) -> None:
    host, port, _, _ = jellyfin_target()
    admin = s.get("/user/1")
    jf = s.get("/settings/jellyfin")
    user = env("JELLYFIN_ADMIN_USER", "").lower()   # unknown: nothing to tell ours by
    if not user or str(admin.get("jellyfinUsername", "")).lower() != user \
            or (jf.get("ip"), int(jf.get("port") or 0)) != (host, port):
        raise Claimed("Seerr: its admin or its Jellyfin isn't ours (someone signed in before this step): stop it, "
                      "remove its volume (seerr-config) and start this step again")


def jellyfin_password() -> str:
    """Typed once for Seerr's first sign-in: on stdin from downloads/run.sh (not in a container's environment, where
    docker inspect would show it), JELLYFIN_ADMIN_PASSWORD only when run without it (tests)."""
    if os.environ.get("JELLYFIN_ADMIN_PASSWORD_STDIN") == "1":
        return sys.stdin.read().removesuffix("\n")
    return env("JELLYFIN_ADMIN_PASSWORD")


def seerr_init() -> int:
    s = seerr()
    if not seerr_ready(s):
        host, port, ssl, base = jellyfin_target()
        code, data, _ = http("POST", SEERR + "/api/v1/auth/jellyfin", body={
            "username": env("JELLYFIN_ADMIN_USER"), "password": jellyfin_password(),
            "hostname": host, "port": port, "useSsl": ssl, "urlBase": base, "serverType": 2})
        if code in (401, 403):
            raise Failed("Seerr: Jellyfin refused that username or password (careful: a few wrong ones lock that "
                         "account in Jellyfin)")
        if code != 200:
            raise Failed(f"Seerr: its first sign-in: HTTP {code} {redact(str(data))[:300]}")
        say("Seerr: its admin is Jellyfin's own admin")
    seerr_ours(s)
    return 0


def apply_seerr() -> None:
    """The request page's settings: Jellyfin's libraries, Radarr and Sonarr (TRaSH Guides' profiles, a search right
    away), sign-in only through Jellyfin, the quotas, pending requests to ntfy."""
    s = seerr()
    try:
        ready = seerr_ready(s)
    except Unreachable:
        ready = False   # not started: its own step starts it
    if not ready:
        say("Seerr: not set up yet (its own step: requests)")
        return
    seerr_ours(s)
    jf = s.get("/settings/jellyfin")
    want_jf = {"externalHostname": site("jellyfin")}
    if any(jf.get(k) != v for k, v in want_jf.items()):
        s.call("POST", "/settings/jellyfin", body={k: jf.get(k) for k in ("ip", "port", "useSsl", "urlBase",
                                                                          "jellyfinForgotPasswordUrl")} | want_jf)
        say("Seerr: Jellyfin's links")
    libs = s.call("POST", "/settings/jellyfin/library/sync") if not jf.get("libraries") else jf["libraries"]
    for lib in libs or []:
        if lib.get("type") in ("movie", "show") and not lib.get("enabled"):
            s.call("PUT", f"/settings/jellyfin/library/{lib['id']}", body={"enabled": True})
            say(f"Seerr: library {lib.get('name')}")
    if chosen("movies-tv"):
        apply_seerr_arr(s, radarr(), 7878, "/data/Movies", "HD Bluray + WEB")
        apply_seerr_arr(s, sonarr(), 8989, "/data/TV Shows", "WEB-1080p")
    main = s.get("/settings/main")
    want = {"applicationTitle": f"{env('BRAND', 'Home')} requests", "applicationUrl": site("requests"),
            "localLogin": False, "mediaServerLogin": True, "newPlexLogin": True, "defaultPermissions": REQUEST,
            "partialRequestsEnabled": True, "cacheImages": False, "versionCheck": False,
            "defaultQuotas": {"movie": {"quotaLimit": int(env("SEERR_MOVIE_QUOTA", "10")),
                                        "quotaDays": int(env("SEERR_QUOTA_DAYS", "7"))},
                              "tv": {"quotaLimit": int(env("SEERR_TV_QUOTA", "10")),
                                     "quotaDays": int(env("SEERR_QUOTA_DAYS", "7"))}}}
    if any(main.get(k) != v for k, v in want.items()):
        s.call("POST", "/settings/main", body=want)
        say("Seerr: sign-in through Jellyfin only, quotas, defaults")
    net = s.get("/settings/network")
    if not net.get("trustProxy"):
        # Caddy's X-Forwarded-For and -Proto: the real client in its log, the session cookie Secure. Read at start.
        s.call("POST", "/settings/network", body={"trustProxy": True})
        say("Seerr: trusts Caddy's forwarded client (RESTART: read at its start)")
    if chosen("ntfy") and os.environ.get("SEERR_NTFY_TOKEN"):
        apply_seerr_ntfy(s)
    if not http("GET", SEERR + "/api/v1/settings/public")[1].get("initialized"):
        s.call("POST", "/settings/initialize")
        s.call("POST", "/settings/jellyfin/sync", body={"start": True})
        say("Seerr: set up, its first look through Jellyfin's libraries started")


def apply_seerr_arr(s: Api, arr: Api, port: int, root: str, profile_name: str) -> None:
    """Radarr or Sonarr in Seerr: Recyclarr's profile (the first one until Recyclarr ran), the library's root, a search
    right away; Sonarr's series watch no new seasons. Seerr tests the connection before anything is saved."""
    kind, host, key = arr.name.lower(), arr.name.lower(), arr.headers["X-Api-Key"]
    profile = by_name(arr.get("/qualityprofile"), profile_name)
    want = {"name": arr.name, "hostname": host, "port": port, "apiKey": key, "useSsl": False, "baseUrl": "",
            "activeProfileId": profile.get("id"), "activeProfileName": profile.get("name"), "activeDirectory": root,
            "is4k": False, "isDefault": True, "externalUrl": site(kind), "syncEnabled": False, "preventSearch": False,
            "tagRequests": False, "tags": []}
    if kind == "radarr":
        want["minimumAvailability"] = "released"
    else:
        want.update({"seriesType": "standard", "animeSeriesType": "anime", "activeAnimeProfileId": profile.get("id"),
                     "activeAnimeProfileName": profile.get("name"), "activeAnimeDirectory": root, "animeTags": [],
                     "enableSeasonFolders": True, "monitorNewItems": "none"})
    have = s.get(f"/settings/{kind}")
    if have and not any(have[0].get(k) != v for k, v in want.items()):
        return
    s.call("POST", f"/settings/{kind}/test", body={"hostname": host, "port": port, "apiKey": key, "useSsl": False,
                                                  "baseUrl": ""})
    if not have:
        s.call("POST", f"/settings/{kind}", body=want)
        say(f"Seerr: {arr.name} ({profile.get('name')}, {root})")
    else:
        s.call("PUT", f"/settings/{kind}/{have[0]['id']}", body=want)
        say(f"Seerr: {arr.name} updated")


def apply_seerr_ntfy(s: Api) -> None:
    """Pending requests (a kid's, one over the quota) to ntfy's topic SEERR_NTFY_TOPIC, written with Seerr's own
    write-only token (ntfy/setup.sh token)."""
    want = {"enabled": True, "embedPoster": False, "types": 2,
            "options": {"url": site("ntfy"), "topic": env("SEERR_NTFY_TOPIC", "requests"), "authMethodToken": True,
                        "token": env("SEERR_NTFY_TOKEN"), "priority": 3}}
    have = s.get("/settings/notifications/ntfy")
    if not have.get("enabled") or have.get("types") != 2 or any(
            (have.get("options") or {}).get(k) != v for k, v in want["options"].items()):
        s.call("POST", "/settings/notifications/ntfy", body=want)
        say(f"Seerr: pending requests to ntfy ({want['options']['topic']})")


def names(key: str) -> set[str]:
    return {x.strip().lower() for x in env(key, "").split(",") if x.strip()}


def seerr_permissions(current: int, user: str, admins: set[str], family: set[str], kids: set[str]) -> int:
    """What Authentik's groups make of a Seerr user's permissions: admins admin; kids exactly Request (their requests
    wait for an admin, nothing else, whatever someone gave them); other family members Request and Auto-approve on
    top of what they have; anyone else as they are."""
    if user in admins:
        return ADMIN
    if user in kids:
        return REQUEST
    if user in family:
        return (current & ~ADMIN) | REQUEST | AUTO_APPROVE
    return current


def seerr_users() -> int:
    admins, family, kids = names("SEERR_ADMINS"), names("SEERR_FAMILY"), names("SEERR_KIDS")
    if not family:
        # Authentik's family is never empty (admins are in it): its groups weren't read, and kids would be no different
        raise Failed("Seerr: no family members given (SEERR_FAMILY): Authentik's groups weren't read")
    s = seerr()
    if not seerr_ready(s):
        raise Failed("Seerr: not set up yet (its own step: requests)")
    seerr_ours(s)
    have = {str(u.get("jellyfinUserId")) for u in s.get("/user?take=1000").get("results", [])}
    new = [u["id"] for u in s.get("/settings/jellyfin/users") if str(u.get("id")) not in have]
    if new:
        s.call("POST", "/user/import-from-jellyfin", body={"jellyfinUserIds": new})
        say(f"Seerr: {len(new)} of Jellyfin's users imported")
    for u in s.get("/user?take=1000").get("results", []):
        name = str(u.get("jellyfinUsername") or u.get("username") or "").lower()
        if u["id"] == 1 or not name:
            continue
        want = seerr_permissions(int(u.get("permissions") or 0), name, admins, family, kids)
        if want != u.get("permissions"):
            s.call("POST", f"/user/{u['id']}/settings/permissions", body={"permissions": want})
            what = ("admin" if want == ADMIN else "asks only" if want == REQUEST
                    else "approved automatically" if want & AUTO_APPROVE else str(want))
            say(f"Seerr: {name} → {what}")
    return 0


# ── apply, status ─────────────────────────────────────────────────────────

def apply() -> int:
    """Each app on its own: one that fails (unreachable, refusing) says why, and the others still get set up."""
    parts = [("qBittorrent", apply_qbittorrent)]
    if chosen("movies-tv"):
        parts += [(a[0].name, lambda a=a: apply_arr(*a)) for a in arrs()]
    if chosen("albums"):
        parts.append(("Lidarr", apply_lidarr))
    parts.append(("Prowlarr", apply_prowlarr))
    if chosen("byparr"):
        parts.append(("Byparr", lambda: apply_byparr(Api("Prowlarr", PROWLARR, env("PROWLARR_API_KEY"), "/api/v1"))))
    if chosen("subtitles"):
        parts.append(("Bazarr", apply_bazarr))
    if chosen("book-requests"):
        parts.append(("Bindery", apply_bindery))
    if chosen("requests"):
        parts.append(("Seerr", apply_seerr))
    failed = []
    for name, fn in parts:
        try:
            fn()
        except (Unreachable, Failed) as e:
            print(f"✗ {redact(str(e))}", file=sys.stderr, flush=True)
            failed.append(name)
    if failed:
        say(f"not set up: {', '.join(failed)}")
        return 1
    say("all set")
    return 0


def status() -> int:
    out: dict = {}

    def part(name, fn):
        try:
            out[name] = fn()
        except (Unreachable, Failed) as e:
            out[name] = {"error": redact(str(e))}

    def vpn():
        g = Api("gluetun", GLUETUN, env("GLUETUN_CONTROL_KEY"), "")
        ip, pf = g.get("/v1/publicip/ip"), g.get("/v1/portforward")
        return {"public_ip": ip.get("public_ip", ""), "country": ip.get("country", ""), "port": pf.get("port", 0)}

    def qbittorrent():
        q = qbt()
        prefs = q.get("/app/preferences")
        return {"listen_port": prefs.get("listen_port"), "interface": prefs.get("current_network_interface"),
                "upnp": prefs.get("upnp"), "save_path": prefs.get("save_path"),
                "whitelist": (prefs.get("bypass_auth_subnet_whitelist")
                              if prefs.get("bypass_auth_subnet_whitelist_enabled") else ""),
                "categories": sorted(q.get("/torrents/categories"))}

    def prowlarr():
        p = Api("Prowlarr", PROWLARR, env("PROWLARR_API_KEY"), "/api/v1")
        return {"apps": sorted(a.get("implementation") for a in p.get("/applications")),
                "indexers": len(p.get("/indexer")),
                "proxies": sorted(x.get("implementation") for x in p.get("/indexerproxy"))}

    def arr(a, root):
        def get():
            clients = [c for c in a.get("/downloadclient") if c.get("implementation") == "QBittorrent"]
            item = {"root": any(r.get("path") == root and r.get("accessible") for r in a.get("/rootfolder")),
                    "client": bool(clients and clients[0].get("enable")),
                    "indexers": len(a.get("/indexer")),
                    "profiles": sorted(q.get("name", "") for q in a.get("/qualityprofile"))}
            if a.name == "Sonarr":
                series = a.get("/series")
                item["monitor_new_seasons"] = sum(1 for s in series if s.get("monitorNewItems", "all") != "none")
                item["series"] = len(series)
            elif a.name == "Radarr":
                item["movies"] = len(a.get("/movie"))
                item["monitored_collections"] = sum(1 for c in a.get("/collection") if c.get("monitored"))
            else:
                artists = a.get("/artist")
                item["monitor_new_albums"] = sum(1 for x in artists if x.get("monitorNewItems", "all") != "none")
                item["artists"] = len(artists)
                item["tags_written"] = a.get("/config/metadataprovider").get("writeAudioTags") != "no"
            return item
        return get

    def subtitles():
        s = bazarr().get("/system/settings")
        g = s.get("general", {})
        return {"sonarr": bool(g.get("use_sonarr")), "radarr": bool(g.get("use_radarr")),
                "languages": sorted(x.get("code2") for x in bazarr().get("/system/languages") if x.get("enabled")),
                "providers": sorted(g.get("enabled_providers") or []),
                "signed_in": bazarr_signed_in()}

    def books():
        b = bindery()
        if not bindery_ready(b):
            return {"ready": False}
        st = http("GET", BINDERY + "/api/v1/auth/status")[1]
        sso = next(p for p in b.get("/auth/oidc/providers") if p.get("id") == "authentik")
        return {"ready": True, "setup_open": bool(isinstance(st, dict) and st.get("setupRequired")),
                "sso": (sso.get("status") or {}).get("state", ""),
                "roots": sorted(r.get("path") for r in b.get("/rootfolder")),
                "client": any(c.get("type") == "qbittorrent" and c.get("enabled") for c in b.get("/downloadclient")),
                "prowlarr": bool(b.get("/prowlarr"))}

    def requests():
        s = seerr()
        pub = http("GET", SEERR + "/api/v1/settings/public")[1]
        if not seerr_ready(s):
            return {"ready": False, "initialized": bool(isinstance(pub, dict) and pub.get("initialized"))}
        main = s.get("/settings/main")
        try:
            seerr_ours(s)
            ours = True
        except Claimed:
            ours = False
        return {"ready": True, "ours": ours, "initialized": bool(pub.get("initialized")),
                "local_login": main.get("localLogin"),
                "radarr": bool(s.get("/settings/radarr")), "sonarr": bool(s.get("/settings/sonarr")),
                "users": s.get("/user?take=1").get("pageInfo", {}).get("results", 0)}

    def locked():
        """What a neighbour on these networks gets from each app without its credentials (this script is one): its
        login or a refusal, never the app. Caddy's signed-in clients are the only ones let in without."""
        probes = {"qbittorrent": (QBT + "/api/v2/app/version", (401, 403)),
                  "prowlarr": (PROWLARR + "/initialize.json", (302, 401))}
        if chosen("movies-tv"):
            probes.update(radarr=(RADARR + "/initialize.json", (302, 401)),
                          sonarr=(SONARR + "/initialize.json", (302, 401)))
        if chosen("albums"):
            probes["lidarr"] = (LIDARR + "/initialize.json", (302, 401))
        if chosen("subtitles"):
            probes["bazarr"] = (BAZARR + "/", (401,))
        if chosen("book-requests"):
            probes["bindery"] = (BINDERY + "/api/v1/book", (401, 403))
        if chosen("requests"):
            probes["seerr"] = (SEERR + "/api/v1/settings/main", (401, 403))
        res = {}
        for name, (url, refused) in probes.items():
            try:
                code, _, hdrs = http("GET", url)
            except Unreachable:
                continue   # not started (yet): nothing to reach
            res[name] = code in refused and (code != 302 or "login" in hdrs.get("Location", ""))
        return res

    part("vpn", vpn)
    part("qbittorrent", qbittorrent)
    part("prowlarr", prowlarr)
    part("locked", locked)
    if chosen("movies-tv"):
        part("radarr", arr(radarr(), "/data/Movies"))
        part("sonarr", arr(sonarr(), "/data/TV Shows"))
    if chosen("albums"):
        part("lidarr", arr(lidarr(), "/data/Music"))
    if chosen("subtitles"):
        part("bazarr", subtitles)
    if chosen("book-requests"):
        part("bindery", books)
    if chosen("requests"):
        part("seerr", requests)
    print(json.dumps(out, sort_keys=True))
    return 0


# ── unmonitor-new-seasons, guard ──────────────────────────────────────────

def unmonitor_new_seasons() -> int:
    a = sonarr()
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


COMMANDS = {"init": init, "apply": apply, "status": status, "unmonitor-new-seasons": unmonitor_new_seasons,
            "bindery-init": bindery_init, "seerr-init": seerr_init, "seerr-users": seerr_users}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd in COMMANDS and len(argv) == 2:
            return COMMANDS[cmd]()
        if cmd == "guard" and len(argv) == 3:
            return guard(float(argv[2]))
    except (Unreachable, Failed) as e:
        print(f"✗ {redact(str(e))}", file=sys.stderr)
        return 1
    except Exception as e:   # noqa: BLE001 - said without its traceback, which could hold a secret (a header value)
        print(f"✗ {type(e).__name__}: {redact(str(e))}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
