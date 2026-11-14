"""The downloads components (downloads, movies-tv, albums, subtitles, book-requests, byparr, requests;
downloads-wizard.sh): their compose shape in tests/golden/full/, downloads/setup.py against a stand-in for the apps'
APIs, downloads/run.sh's project and secrets, downloads/guard.sh, and the wizard's stages, run in a copy with their
commands stubbed (DOCKER_HOST to nowhere). The real images: tests/downloads/run.sh."""
import base64
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "downloads/setup.py"
SERVICES = {"gluetun", "qbittorrent", "prowlarr", "byparr", "radarr", "sonarr", "recyclarr", "lidarr", "bazarr",
            "bindery", "seerr"}
SECRETS = ["VPN_WIREGUARD_PRIVATE_KEY", "GLUETUN_CONTROL_KEY", "QBITTORRENT_API_KEY", "QBITTORRENT_PASSWORD",
           "PROWLARR_API_KEY", "PROWLARR_PASSWORD", "RADARR_API_KEY", "RADARR_PASSWORD", "SONARR_API_KEY",
           "SONARR_PASSWORD", "LIDARR_API_KEY", "LIDARR_PASSWORD", "BAZARR_API_KEY", "BAZARR_AUTH", "BINDERY_API_KEY",
           "BINDERY_OIDC_CLIENT_SECRET", "SEERR_API_KEY", "SEERR_NTFY_TOKEN"]
ALL = "core,downloads,movies-tv,albums,subtitles,book-requests,byparr,requests,ntfy"


def example_value(key: str) -> str:
    return re.search(rf"^{key}=(.*)$", (ROOT / "tests/fixtures/full.env").read_text(), re.M).group(1)


def setup_module():
    spec = importlib.util.spec_from_file_location("downloads_setup", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compose_config() -> dict:
    """docker compose config for the full example (no daemon needed; DOCKER_HOST to nowhere all the same). Not the
    golden copy: tests/compose_canon.py drops a network attached without settings (its null)."""
    env = dict(os.environ, COMPOSE_PROFILES="*", DOCKER_HOST="unix:///nonexistent")
    out = subprocess.run(["docker", "compose", "--env-file", str(ROOT / "tests/fixtures/full.env"), "config", "--format", "json"],
                         cwd=ROOT, capture_output=True, text=True, check=True, env=env).stdout
    return json.loads(out)


@unittest.skipUnless(shutil.which("docker"), "docker compose not installed")
class Compose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = compose_config()

    def svc(self, name: str) -> dict:
        return self.c["services"][name]

    def test_nothing_published(self):
        for n in SERVICES:
            self.assertNotIn("ports", self.svc(n), n)

    def test_who_is_on_which_network(self):
        # The API keys open the apps: no other app may reach them, and the public Seerr reaches only Radarr, Sonarr
        # and Caddy
        def on(net):
            return {n for n, s in self.c["services"].items() if net in (s.get("networks") or {})}
        self.assertEqual(on("arr"), {"gluetun", "radarr", "sonarr", "recyclarr", "lidarr", "bazarr", "bindery", "caddy"})
        # not #8's "downloads": compose couldn't move that one to its range while its apps were on it
        self.assertFalse(on("downloads"))
        self.assertEqual(on("requests"), {"seerr", "radarr", "sonarr", "caddy"})
        for n in ("qbittorrent", "prowlarr", "byparr"):
            self.assertEqual(self.svc(n)["network_mode"], "service:gluetun", n)
            self.assertEqual(self.svc(n)["healthcheck"]["test"], ["CMD-SHELL", "test -e /sys/class/net/eth0"], n)

    def test_pinned_ranges_and_fixed_addresses(self):
        for net, prefix in (("arr", "198.51.100"), ("requests", "203.0.113")):
            self.assertEqual(self.c["networks"][net]["ipam"]["config"],
                             [{"subnet": f"{prefix}.0/24", "ip_range": f"{prefix}.128/25", "gateway": f"{prefix}.1"}])
            # IPv6's unique-local and link-local addresses would be local to Radarr and co
            self.assertIs(self.c["networks"][net]["enable_ipv6"], False, net)
        nets = self.svc("caddy")["networks"]
        self.assertEqual((nets["internal"]["ipv4_address"], nets["internal"]["gw_priority"]), ("172.21.1.11", 1))
        self.assertEqual((nets["arr"]["ipv4_address"], nets["arr"]["aliases"]),
                         ("198.51.100.11", [f"auth.{example_value('DOMAIN')}"]))
        self.assertEqual((nets["requests"]["ipv4_address"], sorted(nets["requests"]["aliases"])),
                         ("203.0.113.11", [f"jellyfin.{example_value('DOMAIN')}", f"ntfy.{example_value('DOMAIN')}"]))
        for n in ("arr", "requests"):
            self.assertFalse(nets[n].get("gw_priority"), n)
        self.assertEqual(self.svc("gluetun")["networks"]["arr"]["ipv4_address"], "198.51.100.10")
        # configure's recreate hint would start them before their setup (downloads/run.sh init, their own stages)
        self.assertFalse(SERVICES & set(self.svc("caddy").get("depends_on", {})))

    def test_authentik_is_the_only_login(self):
        caddy = {"downloads": "198.51.100.11", "requests": "203.0.113.11"}
        for n, trusted in (("prowlarr", ["downloads"]), ("radarr", ["downloads", "requests"]),
                           ("sonarr", ["downloads", "requests"]), ("lidarr", ["downloads"])):
            e = self.svc(n)["environment"]
            u = n.upper()
            # case-sensitive; "External" would hand the API key to anyone who reaches the port (/initialize.json)
            self.assertEqual(e[f"{u}__AUTH__METHOD"], "Forms", n)
            # their login except for local addresses: Caddy's forwarded client (a NetBird peer with TrustCgnat)
            self.assertEqual((e[f"{u}__AUTH__REQUIRED"], e[f"{u}__AUTH__TRUSTCGNATIPADDRESSES"]),
                             ("DisabledForLocalAddresses", "true"), n)
            self.assertEqual(e[f"{u}__SERVER__TRUSTEDNETWORKS"], ",".join(caddy[x] for x in trusted), n)
        # Prowlarr shares gluetun's loopback (local) with qBittorrent and Byparr: it listens on gluetun's address only,
        # and s6's readiness check asks there
        self.assertEqual(self.svc("prowlarr")["environment"]["PROWLARR__SERVER__BINDADDRESS"], "198.51.100.10")
        self.assertIn("/etc/s6-overlay/s6-rc.d/svc-prowlarr/data/check",
                      [v["target"] for v in self.svc("prowlarr")["volumes"]])
        self.assertEqual(self.svc("byparr")["environment"]["HOST"], "127.0.0.1")
        e = self.svc("bindery")["environment"]
        self.assertEqual((e["BINDERY_LOCAL_AUTH_ENABLED"], e["BINDERY_TRUSTED_PROXY"], e["BINDERY_COOKIE_SECURE"],
                          e["BINDERY_OIDC_ADMIN_GROUP"], e["BINDERY_OIDC_DEFAULT_ROLE"]),
                         ("false", "198.51.100.11", "always", "authentik Admins", "requester"))
        # Bazarr's Basic login: only Caddy sends it
        self.assertEqual(self.svc("caddy")["environment"]["BAZARR_AUTH"], example_value("BAZARR_AUTH"))
        role = json.loads(self.svc("gluetun")["environment"]["HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE"])
        self.assertEqual(role, {"auth": "apikey", "apikey": example_value("GLUETUN_CONTROL_KEY")})
        self.assertNotIn("External", json.dumps({n: self.svc(n).get("environment") for n in SERVICES}))

    def test_least_privilege(self):
        for n in SERVICES:
            s = self.svc(n)
            self.assertEqual(s.get("cap_drop"), ["ALL"], n)
            self.assertIn("no-new-privileges:true", s.get("security_opt", []), n)
            self.assertFalse(s.get("devices") or s.get("privileged"), n)
            if n == "gluetun":
                self.assertEqual(s["cap_add"], ["NET_ADMIN"])
                self.assertEqual(s["environment"]["WIREGUARD_IMPLEMENTATION"], "kernelspace")   # no /dev/net/tun
            else:
                self.assertFalse(s.get("cap_add"), n)
                self.assertEqual(s["user"], f"{example_value('HOST_UID')}:{example_value('HOST_GID')}", n)
        # Byparr's browser writes beside itself (/cache); LinuxServer's images need their /run
        for n in ("qbittorrent", "recyclarr", "bindery", "seerr"):
            self.assertTrue(self.svc(n).get("read_only"), n)
        for n in ("prowlarr", "radarr", "sonarr", "lidarr", "bazarr"):
            self.assertTrue(any(t.startswith("/run:uid=") for t in self.svc(n)["tmpfs"]), n)
        for n in SERVICES - {"gluetun", "byparr"}:
            conf = next(v for v in self.svc(n)["volumes"] if v["target"] in ("/config", "/app/config"))
            self.assertTrue(conf.get("volume", {}).get("nocopy"), n)   # downloads/run.sh init's owner stays

    def test_each_sees_only_its_folders(self):
        media = example_value("MEDIA_DIR")

        def mounts(n):
            return {v["target"]: v["source"] for v in self.svc(n).get("volumes", []) if v["type"] == "bind"}
        # Bazarr: Radarr's and Sonarr's paths, the .srt beside the video, nothing else of the media folder
        self.assertEqual(mounts("bazarr"), {"/data/Movies": f"{media}/Movies", "/data/TV Shows": f"{media}/TV Shows"})
        # Bindery: the downloads it imports (copies: two mounts) and the two libraries
        self.assertEqual(mounts("bindery"), {"/data/torrents/books": f"{media}/torrents/books",
                                             "/data/torrents/audiobooks": f"{media}/torrents/audiobooks",
                                             "/books": f"{media}/Books/Books",
                                             "/audiobooks": example_value("AUDIOBOOKS_DIR")})
        self.assertEqual(mounts("seerr"), {})
        self.assertEqual(mounts("byparr"), {})
        # Lidarr: the whole media folder, read-write (a hardlink from torrents/ into Music/ can't cross mounts)
        self.assertEqual(mounts("lidarr"), {"/data": media})

    def test_no_secret_on_a_command_line(self):
        # gluetun logs its settings, the port hook's command included: the key stays a variable there
        hook = self.svc("gluetun")["environment"]["VPN_PORT_FORWARDING_UP_COMMAND"].replace("$$", "$")
        self.assertIn('"$QBT_API_KEY"', hook)
        self.assertIn("WGETRC=", hook)
        self.assertNotIn("VPN_PORT_FORWARDING_DOWN_COMMAND", self.svc("gluetun")["environment"])
        for n in SERVICES:
            s = self.svc(n)
            text = json.dumps([s.get("command"), s.get("entrypoint"), s.get("healthcheck"),
                               s["environment"].get("VPN_PORT_FORWARDING_UP_COMMAND")])
            for k in SECRETS:
                self.assertNotIn(example_value(k), text, (n, k))


class KillSwitch(unittest.TestCase):
    def test_nothing_from_a_tunnel_leaves_by_eth0(self):
        # gluetun accepts an established connection's packets on any interface; while it restarts the tunnel they'd
        # go out by eth0 from the tunnel's address (seen on the box: 10.2.0.2 out of the LAN card). Its post-rules drop
        # every private and shared source there: tunnels' addresses, never eth0's (a documentation range, net24)
        import ipaddress
        sys.path.insert(0, str(ROOT / "lib/setup"))
        import engine
        lines = [ln for ln in (ROOT / "downloads/gluetun-post-rules.txt").read_text().splitlines() if ln.startswith("ip")]
        # Prowlarr from gluetun's loopback (a socket bound to 127.0.0.1): that address is local to it, no login
        self.assertIn("iptables -I INPUT 1 -i lo -s 127.0.0.0/8 -p tcp --dport 9696 -j DROP", lines)
        dropped = []
        for ln in lines:
            # only what gluetun reads back from its chains: interfaces, a source, a TCP port, DROP (no negation, no
            # other match, no REJECT: its reject-with is a field gluetun doesn't know)
            m = re.fullmatch(r"iptables -I (OUTPUT 1 -o eth0 -s ([0-9./]+)|INPUT 1 -i lo -s 127\.0\.0\.0/8 -p tcp --dport \d+) -j DROP", ln)
            self.assertTrue(m, ln)
            if m.group(2):
                dropped.append(ipaddress.ip_network(m.group(2)))
        self.assertEqual(sorted(map(str, dropped)), ["10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16"])
        for allowed in engine.NET24_RANGES:
            self.assertFalse(any(allowed.overlaps(d) for d in dropped), allowed)

    @unittest.skipUnless(shutil.which("docker"), "docker compose not installed")
    def test_gluetun_reads_them(self):
        gluetun = compose_config()["services"]["gluetun"]
        rules = next(v for v in gluetun["volumes"] if v["target"] == "/iptables/post-rules.txt")
        # (compose versions differ in the rest of the entry: create_host_path)
        self.assertEqual((rules["type"], rules["source"], rules.get("read_only"), rules["bind"]["selinux"]),
                         ("bind", str(ROOT / "downloads/gluetun-post-rules.txt"), True, "Z"))


class Ranges(unittest.TestCase):
    def test_only_documentation_or_benchmarking_ranges(self):
        # DOWNLOADS_NET, REQUESTS_NET: a private range would be "local" to Radarr and co (no login for a neighbour),
        # a real one this PC could no longer reach
        sys.path.insert(0, str(ROOT / "lib/setup"))
        import engine
        for v in ("198.51.100", "203.0.113", "192.0.2", "198.18.10", "198.19.255"):
            self.assertIsNone(engine.check_value({"type": "net24"}, v), v)
        for v in ("192.168.1", "10.0.0", "172.20.0", "100.64.0", "8.8.8", "127.0.0", "169.254.1", "198.51.100.0",
                  "256.1.1", "198.51", "192.0.02"):
            self.assertIsNotNone(engine.check_value({"type": "net24"}, v), v)
        # the stack's two networks on one /24: Docker refuses the second, and Caddy (on both) wouldn't start; also
        # when Seerr's component isn't chosen (its network is there all the same)
        comps = engine.components()
        for chosen in (["downloads", "requests"], ["downloads"]):
            p = engine.make_plan(comps, {}, chosen, "none", given={"DOWNLOADS_NET": "203.0.113"})
            self.assertIn("REQUESTS_NET", [k for k, _ in p.invalid], chosen)
        self.assertEqual(engine.make_plan(comps, {}, ["downloads", "requests"], "none").invalid, [])


# ── downloads/setup.py against stand-ins ─────────────────────────────────

def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class Apps:
    """What setup.py talks to: qBittorrent, Radarr, Sonarr, Lidarr, Prowlarr, Bazarr, Bindery, Seerr, gluetun's
    control server, on one port (/qbt, /radarr, …), each as far as setup.py uses it."""

    def __init__(self, keys: dict[str, str]):
        self.keys, self.calls = keys, []
        self.qbt = {"prefs": {"save_path": "/downloads", "upnp": True, "listen_port": 12345, "lsd": True,
                              "current_network_interface": "", "max_ratio_act": 0},
                    "categories": {}, "torrents": []}
        arr = lambda: {"host": {"id": 1, "authenticationMethod": "forms", "authenticationRequired": "disabledForLocalAddresses",
                                "username": "", "password": "", "apiKey": "x", "instanceName": "kept"},
                       "rootfolders": [], "clients": [], "indexers": [], "profiles": [{"id": 1, "name": "Any"}],
                       "mm": {"id": 1, "copyUsingHardlinks": False, "fileDate": "none", "recycleBin": "",
                              "minimumFreeSpaceWhenImporting": 100, "downloadPropersAndRepacks": "preferAndUpgrade",
                              "autoUnmonitorPreviouslyDownloadedMovies": False,
                              "autoUnmonitorPreviouslyDownloadedEpisodes": False,
                              "autoUnmonitorPreviouslyDownloadedTracks": False},
                       "series": [], "movies": [], "collections": [], "artists": [], "tags": [], "proxies": [],
                       "queue": [], "removed": []}
        self.arr = {"radarr": arr(), "sonarr": arr(), "prowlarr": arr(), "lidarr": arr()}
        self.arr["prowlarr"]["apps"] = []
        # what Recyclarr made (its profiles, TRaSH Guides')
        self.arr["radarr"]["profiles"].append({"id": 7, "name": "HD Bluray + WEB"})
        self.arr["sonarr"]["profiles"].append({"id": 8, "name": "WEB-1080p"})
        self.arr["lidarr"]["profiles"] = [{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}, {"id": 3, "name": "Standard"}]
        self.arr["lidarr"]["metadataprofiles"] = [{"id": 1, "name": "Standard"}]
        self.arr["lidarr"]["metaprovider"] = {"id": 1, "writeAudioTags": "newFiles", "scrubAudioTags": False, "embedCoverArt": True}
        self.arr["radarr"]["naming"] = {"id": 1, "renameMovies": False, "standardMovieFormat": "{Movie Title}"}
        self.arr["sonarr"]["naming"] = {"id": 1, "renameEpisodes": False, "standardEpisodeFormat": "{Series Title}"}
        self.arr["lidarr"]["naming"] = {"id": 1, "renameTracks": True, "artistFolderFormat": "{Artist Name}"}
        user, password = base64.b64decode(keys["BAZARR_AUTH"]).decode().split(":", 1)
        self.bazarr = {"auth": {"type": "basic", "username": user, "password": md5(password)},
                       "general": {"use_sonarr": False, "use_radarr": False, "enabled_providers": [],
                                   "serie_default_enabled": False, "serie_default_profile": "",
                                   "movie_default_enabled": False, "movie_default_profile": "", "use_embedded_subs": True},
                       "sonarr": {"ip": "127.0.0.1", "port": 8989, "base_url": "", "ssl": False, "apikey": "",
                                  "only_monitored": False},
                       "radarr": {"ip": "127.0.0.1", "port": 7878, "base_url": "", "ssl": False, "apikey": "",
                                  "only_monitored": False},
                       "analytics": {"enabled": True}, "languages": set(), "profiles": [], "posts": []}
        # Bindery after its first start: no provider, no account, its first-run setup open
        self.bindery = {"providers": [], "users": [], "roots": [], "settings": {}, "clients": [], "prowlarr": [], "synced": 0}
        # Seerr after its first start: nobody signed in yet
        self.seerr = {"users": [], "jellyfin": {"ip": "", "port": 8096, "useSsl": False, "urlBase": "", "externalHostname": "",
                                               "jellyfinForgotPasswordUrl": "", "libraries": []},
                      "jellyfin_users": [{"id": "jf-adult", "username": "Adult"}, {"id": "jf-kid", "username": "kid"},
                                         {"id": "jf-other", "username": "neighbour"}],
                      "jellyfin_admin": ("jfadmin", "the jellyfin admin password"),
                      "main": {"localLogin": True, "mediaServerLogin": True, "newPlexLogin": True, "defaultPermissions": 32,
                               "partialRequestsEnabled": True, "cacheImages": False, "versionCheck": True,
                               "applicationTitle": "Seerr", "applicationUrl": "", "defaultQuotas": {}},
                      "network": {"trustProxy": False}, "radarr": [], "sonarr": [], "ntfy": {"enabled": False},
                      "initialized": False}

    def handle(self, h, method: str):
        u = urllib.parse.urlsplit(h.path)
        app, _, path = u.path.lstrip("/").partition("/")
        path = "/" + path
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n).decode() if n else ""
        if raw.startswith(("{", "[")):
            body = json.loads(raw)
        elif app == "bazarr":
            body = urllib.parse.parse_qs(raw)
        else:
            body = dict(urllib.parse.parse_qsl(raw))
        query = dict(urllib.parse.parse_qsl(u.query))
        if method != "GET":
            self.calls.append((app, method, path, body))
        if app == "qbt":
            return self.qbittorrent(h, method, path, body, query)
        if app == "gluetun":
            if h.headers.get("X-Api-Key") != self.keys["GLUETUN_CONTROL_KEY"]:
                return 401, {}
            return 200, {"/v1/publicip/ip": {"public_ip": "198.51.100.7", "country": "Switzerland"},
                         "/v1/portforward": {"port": 12345, "ports": [12345]}}[path]
        if app == "bazarr":
            return self.bazarr_api(h, method, path, body)
        if app == "bindery":
            return self.bindery_api(h, method, path, body)
        if app == "seerr":
            return self.seerr_api(h, method, path, body, query)
        return self.servarr(h, app, method, path, body, query)

    def qbittorrent(self, h, method, path, body, query):
        if h.headers.get("Authorization") != f"Bearer {self.keys['QBITTORRENT_API_KEY']}":
            return 403, "Forbidden"
        q = self.qbt
        if path == "/api/v2/app/preferences":
            return 200, q["prefs"]
        if path == "/api/v2/app/setPreferences":
            q["prefs"].update(json.loads(body["json"]))
            return 200, ""
        if path == "/api/v2/torrents/categories":
            return 200, q["categories"]
        if path in ("/api/v2/torrents/createCategory", "/api/v2/torrents/editCategory"):
            q["categories"][body["category"]] = {"name": body["category"], "savePath": body["savePath"]}
            return 200, ""
        if path == "/api/v2/torrents/info":
            tag = query.get("tag")
            return 200, [t for t in q["torrents"] if not tag or tag in t["tags"].split(", ")]
        hashes = set(body.get("hashes", "").split("|"))
        for t in q["torrents"]:
            if t["hash"] in hashes:
                if path.endswith("/addTags"):
                    t["tags"] = ", ".join(filter(None, [t["tags"], body["tags"]]))
                elif path.endswith("/removeTags"):
                    t["tags"] = ", ".join(x for x in t["tags"].split(", ") if x and x != body["tags"])
                elif path.endswith("/stop"):
                    t["state"] = "stoppedDL"
                elif path.endswith("/start"):
                    t["state"] = "downloading"
        return 200, ""

    def servarr(self, h, app, method, path, body, query):
        a = self.arr[app]
        if path == "/login":
            ok = a["host"]["username"] and body.get("username") == a["host"]["username"] \
                and body.get("password") == a["host"]["password"]
            return 302, "", {"Location": "/" if ok else "/login?returnUrl=%2F&loginFailed=true"}
        if path == "/initialize.json":
            return 302, "", {"Location": "/login?returnUrl=%2Finitialize.json"}
        if h.headers.get("X-Api-Key") != self.keys[f"{app.upper()}_API_KEY"]:
            return 401, {}
        path = re.sub(r"^/api/v[13]", "", path)
        coll = {"/rootfolder": "rootfolders", "/downloadclient": "clients", "/applications": "apps",
                "/indexer": "indexers", "/qualityprofile": "profiles", "/series": "series", "/movie": "movies",
                "/collection": "collections", "/artist": "artists", "/tag": "tags", "/indexerproxy": "proxies",
                "/metadataprofile": "metadataprofiles"}
        if path == "/queue" and method == "GET":
            return 200, {"page": 1, "totalRecords": len(a["queue"]), "records": a["queue"]}
        if path.startswith("/queue/") and method == "DELETE":
            i = int(path.rsplit("/", 1)[1])
            a["queue"] = [r for r in a["queue"] if r["id"] != i]
            a["removed"].append((i, query))
            return 200, {}
        if method == "GET":
            if path == "/config/host":
                return 200, dict(a["host"], password="")
            if path == "/config/mediamanagement":
                return 200, a["mm"]
            if path == "/config/metadataprovider":
                return 200, a["metaprovider"]
            if path == "/config/naming":
                return 200, a["naming"]
            if path.endswith("/schema"):
                kind = path.split("/")[1]
                if kind == "downloadclient":
                    names = ["host", "port", "useSsl", "urlBase", "apiKey", "username", "password", "movieCategory",
                             "tvCategory", "musicCategory", "initialState"]
                    return 200, [{"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
                                  "fields": [{"name": x, "value": None} for x in names]}]
                if kind == "indexerproxy":
                    return 200, [{"implementation": "FlareSolverr", "configContract": "FlareSolverrSettings",
                                  "fields": [{"name": "host", "value": "http://localhost:8191/"},
                                             {"name": "requestTimeout", "value": 60}]}]
                return 200, [{"implementation": impl, "configContract": f"{impl}Settings",
                              "fields": [{"name": x, "value": ([3000, 3010, 3030, 3040] if impl == "Lidarr" else [2000])
                                          if x == "syncCategories" else None}
                                         for x in ("prowlarrUrl", "baseUrl", "apiKey", "syncCategories")]}
                             for impl in ("Radarr", "Sonarr", "Lidarr")]
            return 200, a[coll[path]]
        if path.startswith("/config/host/"):
            a["host"].update({k: body[k] for k in ("authenticationMethod", "username", "password")})
            return 202, body
        for p, key in (("/config/mediamanagement/", "mm"), ("/config/metadataprovider/", "metaprovider"),
                       ("/config/naming/", "naming")):
            if path.startswith(p):
                a[key] = body
                return 202, body
        if path == "/series/editor":
            for s in a["series"]:
                if s["id"] in body["seriesIds"]:
                    s["monitorNewItems"] = body["monitorNewItems"]
            return 202, {}
        if method == "POST":
            item = dict(body, id=len(a[coll[path]]) + 1, **({"accessible": True} if path == "/rootfolder" else {}))
            a[coll[path]].append(item)
            return 201, item
        if method == "PUT":
            key, _, i = path.rpartition("/")
            a[coll[key]] = [dict(body, id=int(i), **({"accessible": True} if key == "/rootfolder" else {}))
                            if x["id"] == int(i) else x for x in a[coll[key]]]
            return 202, body
        return 404, {}

    def bazarr_api(self, h, method, path, body):
        b = self.bazarr
        if path == "/":
            if not b["auth"]["type"]:   # no login at all: anyone in (its API key in the page)
                return 200, "<html>Bazarr</html>"
            auth = h.headers.get("Authorization", "")
            try:
                user, password = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return 401, "Unauthorized"
            ok = (user, md5(password)) == (b["auth"]["username"], b["auth"]["password"])
            return (200, "<html>Bazarr</html>") if ok else (401, "Unauthorized")
        if h.headers.get("X-API-KEY") != self.keys["BAZARR_API_KEY"]:
            return 401, {"message": "unauthorized"}
        if path == "/api/system/settings" and method == "GET":
            return 200, {k: b[k] for k in ("general", "sonarr", "radarr", "analytics")}
        if path == "/api/system/settings":
            b["posts"].append(body)
            for k, v in body.items():
                if k == "languages-enabled":
                    b["languages"] = set(v)
                elif k == "languages-profiles":
                    b["profiles"] = json.loads(v[0])
                elif k.startswith("settings-"):
                    _, section, key = k.split("-", 2)
                    val = v if key == "enabled_providers" else v[0]
                    val = {"true": True, "false": False}.get(val, val) if isinstance(val, str) else val
                    if section == "auth" and key == "password":
                        val = md5(val)
                    if isinstance(val, str) and val.isdigit():
                        val = int(val)
                    b[section][key] = val
            return 204, ""
        if path == "/api/system/languages":
            return 200, [{"code2": c, "enabled": c in b["languages"]} for c in ("de", "en", "fr")]
        if path == "/api/system/languages/profiles":
            return 200, b["profiles"]
        return 404, {}

    def bindery_api(self, h, method, path, body):
        b = self.bindery
        if path == "/api/v1/auth/status":
            return 200, {"setupRequired": not b["providers"] and not b["users"], "localAuthEnabled": False}
        if path == "/api/v1/book":
            return 401, {"error": "unauthorized"}
        if h.headers.get("X-Api-Key") != self.keys["BINDERY_API_KEY"]:
            return 401, {"error": "unauthorized"}
        path = path.removeprefix("/api/v1")
        if path == "/auth/users":
            return 200, b["users"]
        if path == "/auth/oidc/providers":
            if method == "PUT":
                if any(not p.get("client_secret") for p in body):
                    return 400, {"error": "client_secret required for new provider"}
                b["providers"] = [{k: v for k, v in p.items() if k != "client_secret"} | {"status": {"state": "ok"}}
                                  for p in body]
                return 200, {"ok": True, "count": len(body)}
            return 200, b["providers"]
        if path == "/rootfolder":
            if method == "POST":
                b["roots"].append({"id": len(b["roots"]) + 1, "path": body["path"]})
                return 201, b["roots"][-1]
            return 200, b["roots"]
        if path == "/setting":
            return 200, [{"key": k, "value": v} for k, v in b["settings"].items()]
        if path.startswith("/setting/"):
            b["settings"][path.removeprefix("/setting/")] = body["value"]
            return 200, {}
        if path == "/downloadclient":
            if method == "POST":
                b["clients"].append(dict(body, id=len(b["clients"]) + 1))
                return 201, b["clients"][-1]
            return 200, [{k: v for k, v in c.items() if k != "password"} for c in b["clients"]]
        if path.startswith("/downloadclient/"):
            i = int(path.split("/")[2])
            b["clients"] = [dict(body, id=i) if c["id"] == i else c for c in b["clients"]]
            return 200, body
        if path == "/prowlarr":
            if method == "POST":
                b["prowlarr"].append(dict(body, id=len(b["prowlarr"]) + 1))
                return 201, b["prowlarr"][-1]
            return 200, [{k: v for k, v in x.items() if k != "apiKey"} for x in b["prowlarr"]]
        if path.endswith("/sync"):
            b["synced"] += 1
            return 200, {"added": 0, "updated": 0, "removed": 0}
        return 404, {}

    def seerr_api(self, h, method, path, body, query):
        s = self.seerr
        path = path.removeprefix("/api/v1")
        if path == "/settings/public":
            return 200, {"initialized": s["initialized"]}
        if path == "/auth/jellyfin" and method == "POST":
            if s["users"]:
                return 500, {"message": "Jellyfin hostname already configured"}
            if (body.get("username"), body.get("password")) != s["jellyfin_admin"]:
                return 401, {"message": "Unauthorized"}
            s["jellyfin"].update(ip=body["hostname"], port=body["port"], useSsl=body["useSsl"], urlBase=body["urlBase"])
            s["users"].append({"id": 1, "jellyfinUsername": body["username"], "jellyfinUserId": "jf-admin", "permissions": 2})
            return 200, {"id": 1, "permissions": 2}
        if h.headers.get("X-Api-Key") != self.keys["SEERR_API_KEY"]:
            return 401, {"message": "unauthorized"}
        if not s["users"]:
            return 403, {"error": "You do not have permission to access this endpoint"}
        if path == "/user/1":
            return 200, s["users"][0]
        if path == "/user":
            return 200, {"pageInfo": {"results": len(s["users"])}, "results": s["users"]}
        if path == "/settings/jellyfin/users":
            return 200, s["jellyfin_users"]
        if path == "/user/import-from-jellyfin":
            for jid in body["jellyfinUserIds"]:
                ju = next(u for u in s["jellyfin_users"] if u["id"] == jid)
                s["users"].append({"id": len(s["users"]) + 1, "jellyfinUsername": ju["username"], "jellyfinUserId": jid,
                                   "permissions": s["main"]["defaultPermissions"]})
            return 201, {}
        m = re.fullmatch(r"/user/(\d+)/settings/permissions", path)
        if m:
            next(u for u in s["users"] if u["id"] == int(m.group(1)))["permissions"] = body["permissions"]
            return 200, {}
        if path == "/settings/jellyfin":
            if method == "POST":
                s["jellyfin"].update(body)
            return 200, s["jellyfin"]
        if path == "/settings/jellyfin/library/sync":
            s["jellyfin"]["libraries"] = [{"id": "lib-m", "name": "Movies", "type": "movie", "enabled": False},
                                          {"id": "lib-t", "name": "TV Shows", "type": "show", "enabled": False},
                                          {"id": "lib-x", "name": "Home videos", "type": "movie", "enabled": False}][:2]
            return 200, s["jellyfin"]["libraries"]
        if path.startswith("/settings/jellyfin/library/"):
            lib = next(x for x in s["jellyfin"]["libraries"] if x["id"] == path.rsplit("/", 1)[1])
            lib.update(body)
            return 200, lib
        m = re.fullmatch(r"/settings/(radarr|sonarr)(/test|/\d+)?", path)
        if m:
            kind, rest = m.group(1), m.group(2)
            if rest == "/test":
                return 200, {"profiles": [{"id": 1, "name": "Any"}, {"id": 7, "name": "HD Bluray + WEB"},
                                          {"id": 8, "name": "WEB-1080p"}], "rootFolders": []}
            if method == "POST":
                s[kind].append(dict(body, id=len(s[kind])))
                return 201, s[kind][-1]
            if method == "PUT":
                s[kind] = [dict(body, id=int(rest[1:]))]
                return 200, s[kind][0]
            return 200, s[kind]
        if path in ("/settings/main", "/settings/network"):
            key = path.rsplit("/", 1)[1]
            if method == "POST":
                s[key].update(body)
            return 200, dict(s[key], **({"apiKey": self.keys["SEERR_API_KEY"]} if key == "main" else {}))
        if path == "/settings/notifications/ntfy":
            if method == "POST":
                s["ntfy"] = body
            return 200, s["ntfy"]
        if path == "/settings/initialize":
            s["initialized"] = True
            return 200, {}
        if path == "/settings/jellyfin/sync":
            return 200, {"running": True}
        return 404, {}


def serve(apps: Apps) -> http.server.ThreadingHTTPServer:
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, method):
            r = apps.handle(self, method)
            code, data, hdrs = r if len(r) == 3 else (*r, {})
            out = (json.dumps(data) if isinstance(data, (dict, list)) else data).encode()
            self.send_response(code)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.reply("GET")

        def do_POST(self):
            self.reply("POST")

        def do_PUT(self):
            self.reply("PUT")

        def do_DELETE(self):
            self.reply("DELETE")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


KEYS = {"QBITTORRENT_API_KEY": "qbt_" + "a" * 28, "QBITTORRENT_PASSWORD": "qbt-password-for-bindery",
        "PROWLARR_API_KEY": "p" * 32, "PROWLARR_PASSWORD": "prowlarr-break-glass", "RADARR_API_KEY": "r" * 32,
        "RADARR_PASSWORD": "radarr-break-glass", "SONARR_API_KEY": "s" * 32, "SONARR_PASSWORD": "sonarr-break-glass",
        "LIDARR_API_KEY": "l" * 32, "LIDARR_PASSWORD": "lidarr-break-glass", "BAZARR_API_KEY": "b" * 32,
        "BAZARR_AUTH": base64.b64encode(b"homelab:bazarr-basic-password").decode(), "BINDERY_API_KEY": "d" * 64,
        "BINDERY_OIDC_CLIENT_SECRET": "bindery-oidc-client-secret", "SEERR_API_KEY": "e" * 48,
        "SEERR_NTFY_TOKEN": "tk_seerrntfytoken", "GLUETUN_CONTROL_KEY": "g" * 32}
JELLYFIN = {"JELLYFIN_ADMIN_USER": "jfadmin", "JELLYFIN_URL": "http://jellyfin:8096"}
PATHS = (("QBT", "qbt"), ("PROWLARR", "prowlarr"), ("RADARR", "radarr"), ("SONARR", "sonarr"), ("LIDARR", "lidarr"),
         ("BAZARR", "bazarr"), ("BINDERY", "bindery"), ("SEERR", "seerr"), ("GLUETUN", "gluetun"))


def run_setup(args: list[str], base: str = "", **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOST_UID=str(os.getuid()), HOST_GID=str(os.getgid()), ADMIN_USER="admin", COMPONENTS=ALL,
               DOMAIN="example.org", NETBIRD_PROXY_PORT="9443", BRAND="Example", BAZARR_LANGUAGES="en,de",
               LIDARR_QUALITY_PROFILE="Lossless", **KEYS, **JELLYFIN)
    if base:
        env.update({f"SETUP_{k}": f"{base}/{p}" for k, p in PATHS})
    env.update(extra)
    return subprocess.run([sys.executable, "-I", str(SETUP), *args], capture_output=True, text=True, env=env, timeout=60)


class Setup(unittest.TestCase):
    def setUp(self):
        self.apps = Apps(KEYS)
        self.srv = serve(self.apps)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def changes(self) -> list[tuple]:
        return [c for c in self.apps.calls if c[2] != "/login"]   # signing in to check the password changes nothing

    def ok(self, args, **extra) -> subprocess.CompletedProcess:
        r = run_setup(args, self.base, **extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def everything_set_up(self) -> str:
        """Bindery's and Seerr's own steps, then apply: what the wizard does. The output, for the secrets check."""
        out = self.ok(["bindery-init"]).stdout
        out += self.ok(["seerr-init"], JELLYFIN_ADMIN_PASSWORD="the jellyfin admin password").stdout
        r = self.ok(["apply"])
        return out + r.stdout + r.stderr

    def test_apply_then_a_second_run_changes_nothing(self):
        out = self.everything_set_up()
        q = self.apps.qbt
        self.assertEqual(q["categories"], {c: {"name": c, "savePath": f"/data/torrents/{c}"}
                                           for c in ("movies", "tv", "music", "books", "audiobooks")})
        self.assertEqual({k: q["prefs"][k] for k in ("save_path", "upnp", "lsd", "current_network_interface", "max_ratio",
                                                     "max_seeding_time", "bypass_local_auth",
                                                     "bypass_auth_subnet_whitelist_enabled", "bypass_auth_subnet_whitelist")},
                         {"save_path": "/data/torrents", "upnp": False, "lsd": False, "current_network_interface": "tun0",
                          "max_ratio": 2.0, "max_seeding_time": 14 * 1440, "bypass_local_auth": False,
                          "bypass_auth_subnet_whitelist_enabled": True, "bypass_auth_subnet_whitelist": "198.51.100.11/32"})
        self.assertEqual(q["prefs"]["listen_port"], 12345)   # gluetun's, untouched
        for app, root, cat in (("radarr", "/data/Movies", ("movieCategory", "movies")),
                               ("sonarr", "/data/TV Shows", ("tvCategory", "tv")),
                               ("lidarr", "/data/Music", ("musicCategory", "music"))):
            a = self.apps.arr[app]
            # each its own password: none shared
            self.assertEqual((a["host"]["username"], a["host"]["password"]), ("admin", KEYS[f"{app.upper()}_PASSWORD"]))
            self.assertEqual([r["path"] for r in a["rootfolders"]], [root])
            fields = {f["name"]: f["value"] for f in a["clients"][0]["fields"]}
            self.assertEqual((fields["host"], fields["port"], fields["apiKey"], fields[cat[0]], fields["username"]),
                             ("gluetun", 8080, KEYS["QBITTORRENT_API_KEY"], cat[1], ""))
            self.assertTrue(a["clients"][0]["removeCompletedDownloads"] and a["clients"][0]["removeFailedDownloads"])
            # the whole object went back: what setup.py doesn't set keeps its value
            self.assertEqual((a["mm"]["fileDate"], a["mm"]["copyUsingHardlinks"], a["mm"]["downloadPropersAndRepacks"]),
                             ("none", True, "doNotPrefer"))
        self.assertEqual(self.apps.arr["prowlarr"]["host"]["password"], KEYS["PROWLARR_PASSWORD"])
        # Lidarr: Lossless, nothing monitored by itself, the files as downloaded
        root = self.apps.arr["lidarr"]["rootfolders"][0]
        self.assertEqual((root["defaultQualityProfileId"], root["defaultMonitorOption"], root["defaultNewItemMonitorOption"]),
                         (2, "none", "none"))
        self.assertEqual((self.apps.arr["lidarr"]["metaprovider"]["writeAudioTags"],
                          self.apps.arr["lidarr"]["naming"]["renameTracks"]), ("no", False))
        # Radarr and Sonarr rename new imports (Jellyfin reads the standard names); the rest of the object is kept
        self.assertEqual(self.apps.arr["radarr"]["naming"], {"id": 1, "renameMovies": True, "standardMovieFormat": "{Movie Title}"})
        self.assertEqual(self.apps.arr["sonarr"]["naming"], {"id": 1, "renameEpisodes": True, "standardEpisodeFormat": "{Series Title}"})
        apps = {x["implementation"]: {f["name"]: f["value"] for f in x["fields"]} for x in self.apps.arr["prowlarr"]["apps"]}
        self.assertEqual(sorted(apps), ["Lidarr", "Radarr", "Sonarr"])
        # what the apps are told, as the downloads network sees them (not where this script reached them)
        self.assertEqual((apps["Radarr"]["prowlarrUrl"], apps["Radarr"]["baseUrl"], apps["Radarr"]["syncCategories"]),
                         ("http://gluetun:9696", "http://radarr:7878", [2000]))
        self.assertEqual(apps["Lidarr"]["syncCategories"], [3000, 3010, 3040])   # audiobooks (3030) are Bindery's
        # Byparr: Prowlarr's FlareSolverr proxy at its own loopback, only for the sites tagged cloudflare
        p = self.apps.arr["prowlarr"]
        proxy = p["proxies"][0]
        self.assertEqual(({f["name"]: f["value"] for f in proxy["fields"]}["host"], proxy["tags"]),
                         ("http://localhost:8191/", [next(t["id"] for t in p["tags"] if t["label"] == "cloudflare")]))
        # Bazarr: its languages, one profile, the defaults before Sonarr and Radarr, providers without an account
        bz = self.apps.bazarr
        self.assertEqual((bz["languages"], [i["language"] for i in bz["profiles"][0]["items"]]), ({"en", "de"}, ["en", "de"]))
        self.assertEqual((bz["general"]["use_sonarr"], bz["general"]["use_radarr"], bz["sonarr"]["apikey"],
                          bz["radarr"]["only_monitored"], bz["analytics"]["enabled"]),
                         (True, True, KEYS["SONARR_API_KEY"], True, False))
        first_use = next(i for i, post in enumerate(bz["posts"]) if "settings-general-use_sonarr" in post)
        defaults = next(i for i, post in enumerate(bz["posts"]) if "settings-general-serie_default_enabled" in post)
        self.assertLess(defaults, first_use)
        self.assertEqual(sorted(bz["general"]["enabled_providers"]), ["embeddedsubtitles", "gestdown", "yifysubtitles"])
        # Bindery: its sign-in (the secret only in the PUT), libraries, qBittorrent's login, Prowlarr, no author's
        # books all at once
        bd = self.apps.bindery
        self.assertEqual({k: bd["providers"][0][k] for k in ("id", "issuer", "client_id", "allowed_groups")},
                         {"id": "authentik", "issuer": "https://auth.example.org:9443/application/o/bindery/",
                          "client_id": "bindery", "allowed_groups": ["family"]})
        self.assertEqual([r["path"] for r in bd["roots"]], ["/books", "/audiobooks"])
        self.assertEqual((bd["settings"]["author.default_monitor_mode"], bd["settings"]["library.defaultRootFolderId"]),
                         ("none", "1"))
        self.assertEqual({k: bd["clients"][0][k] for k in ("host", "username", "password", "category", "categoryAudiobook")},
                         {"host": "gluetun", "username": "admin", "password": KEYS["QBITTORRENT_PASSWORD"],
                          "category": "books", "categoryAudiobook": "audiobooks"})
        self.assertEqual((bd["prowlarr"][0]["url"], bd["prowlarr"][0]["apiKey"]), ("http://gluetun:9696", KEYS["PROWLARR_API_KEY"]))
        # Seerr: Jellyfin as Seerr reaches it, Radarr's and Sonarr's profiles, sign-in only through Jellyfin
        se = self.apps.seerr
        self.assertEqual((se["jellyfin"]["ip"], se["jellyfin"]["externalHostname"]), ("jellyfin", "https://jellyfin.example.org:9443"))
        self.assertEqual([x["enabled"] for x in se["jellyfin"]["libraries"]], [True, True])
        self.assertEqual((se["radarr"][0]["activeProfileName"], se["radarr"][0]["activeDirectory"], se["sonarr"][0]["monitorNewItems"],
                          se["sonarr"][0]["activeProfileName"]), ("HD Bluray + WEB", "/data/Movies", "none", "WEB-1080p"))
        self.assertEqual((se["main"]["localLogin"], se["main"]["defaultPermissions"], se["main"]["defaultQuotas"]),
                         (False, 32, {"movie": {"quotaLimit": 10, "quotaDays": 7}, "tv": {"quotaLimit": 10, "quotaDays": 7}}))
        self.assertEqual((se["network"]["trustProxy"], se["initialized"]), (True, True))
        self.assertEqual((se["ntfy"]["types"], se["ntfy"]["options"]["topic"], se["ntfy"]["options"]["url"]),
                         (2, "requests", "https://ntfy.example.org:9443"))
        for k in KEYS:
            self.assertNotIn(KEYS[k], out, k)
        before = len(self.changes())
        r = self.ok(["apply"])
        # Bindery pulls Prowlarr's search sites on every run: the only call a second run makes
        self.assertEqual([c for c in self.changes()[before:] if not c[2].endswith("/sync")], [])
        self.assertEqual(r.stdout.strip(), "all set")

    def test_downloads_alone_leaves_the_others_be(self):
        self.ok(["apply"], COMPONENTS="core,downloads")
        self.assertFalse([c for c in self.apps.calls if c[0] in ("radarr", "sonarr", "lidarr", "bazarr", "bindery", "seerr")])
        self.assertEqual(self.apps.arr["prowlarr"]["apps"], [])
        self.assertEqual(self.apps.arr["prowlarr"]["proxies"], [])
        self.assertEqual(self.apps.qbt["categories"], {})

    def test_seerr_and_bindery_wait_for_their_own_steps(self):
        # apply never makes their first admin: that's their stage's, back to back with their start
        r = self.ok(["apply"])
        self.assertIn("Bindery: not set up yet", r.stdout)
        self.assertIn("Seerr: not set up yet", r.stdout)
        self.assertEqual((self.apps.bindery["providers"], self.apps.seerr["users"]), ([], []))
        self.assertFalse([c for c in self.apps.calls if c[0] in ("bindery", "seerr")])

    def test_seerr_and_bindery_not_started_yet(self):
        # their containers don't exist before their own steps: apply still sets up the rest, and says why not them
        r = run_setup(["apply"], self.base, SETUP_BINDERY="http://127.0.0.1:9", SETUP_SEERR="http://127.0.0.1:9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Bindery: not set up yet", r.stdout)
        self.assertIn("Seerr: not set up yet", r.stdout)

    def test_bazarr_without_any_login(self):
        # with none, Caddy's credentials work too (anyone's do): it must refuse a caller without them
        self.apps.bazarr["auth"] = {"type": None, "username": "", "password": ""}
        self.ok(["apply"])
        self.assertEqual(self.apps.bazarr["auth"]["type"], "basic")
        self.assertTrue(json.loads(self.ok(["status"]).stdout)["bazarr"]["signed_in"])

    def test_seerr_users_never_without_the_groups(self):
        self.everything_set_up()
        before = [dict(u) for u in self.apps.seerr["users"]]
        r = run_setup(["seerr-users"], self.base, SEERR_KIDS="kid")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SEERR_FAMILY", r.stderr)
        self.assertEqual(self.apps.seerr["users"], before)

    def test_bindery_another_sign_in(self):
        # a provider of someone else's: they'd keep signing in (its admin), whatever ours does
        self.ok(["bindery-init"])
        self.apps.bindery["providers"].append({"id": "theirs", "issuer": "https://idp.example.net/", "client_id": "x"})
        for cmd in (["bindery-init"], ["apply"]):
            r = run_setup(cmd, self.base)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("another sign-in: theirs", r.stderr)
        self.apps.bindery["providers"] = [dict(self.apps.bindery["providers"][0], issuer="https://idp.example.net/")]
        r = run_setup(["bindery-init"], self.base)
        self.assertEqual(r.returncode, 1)
        self.assertIn("points elsewhere", r.stderr)

    def test_the_jellyfin_password_on_stdin(self):
        env = dict(os.environ, HOST_UID=str(os.getuid()), HOST_GID=str(os.getgid()), COMPONENTS=ALL, DOMAIN="example.org",
                   JELLYFIN_ADMIN_PASSWORD_STDIN="1", **KEYS, **JELLYFIN,
                   **{f"SETUP_{k}": f"{self.base}/{p}" for k, p in PATHS})
        r = subprocess.run([sys.executable, "-I", str(SETUP), "seerr-init"], input="the jellyfin admin password\n",
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.apps.seerr["users"][0]["jellyfinUsername"], "jfadmin")

    def test_anything_unexpected_said_without_its_secret(self):
        # a key with a CRLF .env's \r: http.client refuses the header, its message names the value
        r = run_setup(["status"], self.base, QBITTORRENT_API_KEY=KEYS["QBITTORRENT_API_KEY"] + "\r")
        self.assertNotIn(KEYS["QBITTORRENT_API_KEY"], r.stdout + r.stderr)
        r = run_setup(["guard", "50"], self.base, QBITTORRENT_API_KEY=KEYS["QBITTORRENT_API_KEY"] + "\r")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ ValueError", r.stderr)
        self.assertNotIn(KEYS["QBITTORRENT_API_KEY"], r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bindery_claimed_by_someone_else(self):
        self.apps.bindery["users"] = [{"id": 1, "username": "mallory", "role": "admin"}]
        r = run_setup(["bindery-init"], self.base)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("someone set it up", r.stderr)
        self.assertEqual(self.apps.bindery["providers"], [])   # nothing of ours went to it
        self.assertNotIn(KEYS["BINDERY_OIDC_CLIENT_SECRET"], json.dumps(self.apps.calls))

    def test_bindery_init_once(self):
        self.ok(["bindery-init"])
        self.apps.bindery["users"] = [{"id": 1, "username": "admin", "role": "admin"}]   # signed in through Authentik
        before = len(self.apps.calls)
        self.ok(["bindery-init"])
        self.assertEqual(self.apps.calls[before:], [])

    def test_seerr_claimed_by_someone_else(self):
        # Someone signed in first, with their own Jellyfin: nothing of ours goes to it, the API keys least of all
        s = self.apps.seerr
        s["users"] = [{"id": 1, "jellyfinUsername": "mallory", "jellyfinUserId": "x", "permissions": 2}]
        s["jellyfin"]["ip"] = "evil.example"
        for cmd in ("seerr-init", "seerr-users"):
            r = run_setup([cmd], self.base, JELLYFIN_ADMIN_PASSWORD="the jellyfin admin password", SEERR_FAMILY="adult")
            self.assertEqual(r.returncode, 1, (cmd, r.stdout))
            self.assertIn("isn't ours", r.stderr)
        r = run_setup(["apply"], self.base)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not set up: Seerr", r.stdout)
        seerr_calls = json.dumps([c for c in self.apps.calls if c[0] == "seerr"])
        for k in ("RADARR_API_KEY", "SONARR_API_KEY", "SEERR_NTFY_TOKEN"):
            self.assertNotIn(KEYS[k], seerr_calls, k)

    def test_seerr_wrong_jellyfin_password(self):
        r = run_setup(["seerr-init"], self.base, JELLYFIN_ADMIN_PASSWORD="a typo")
        self.assertEqual(r.returncode, 1)
        self.assertIn("lock that account", r.stderr)
        self.assertNotIn("a typo", r.stdout + r.stderr)
        self.assertEqual(self.apps.seerr["users"], [])

    def test_seerr_users(self):
        self.everything_set_up()
        users = ("SEERR_ADMINS", "SEERR_FAMILY", "SEERR_KIDS")
        groups = dict(zip(users, ("arjun", "adult,kid,arjun", "kid")))
        self.ok(["seerr-users"], **groups)
        perms = {u["jellyfinUsername"]: u["permissions"] for u in self.apps.seerr["users"]}
        # user 1 (Jellyfin's admin) untouched; Adult case-insensitively an adult; the neighbour isn't family: as is
        self.assertEqual(perms, {"jfadmin": 2, "Adult": 32 | 128, "kid": 32, "neighbour": 32})
        before = len(self.apps.calls)
        self.ok(["seerr-users"], **groups)
        self.assertEqual(self.apps.calls[before:], [])

    def test_seerr_permissions(self):
        m = setup_module()
        admins, family, kids = {"arjun"}, {"arjun", "mum", "kid"}, {"kid"}
        self.assertEqual(m.seerr_permissions(32, "arjun", admins, family, kids), 2)
        self.assertEqual(m.seerr_permissions(2 | 16 | 128 | 8192, "kid", admins, family, kids), 32)   # whatever they had
        self.assertEqual(m.seerr_permissions(32 | 4194304, "mum", admins, family, kids), 32 | 128 | 4194304)
        self.assertEqual(m.seerr_permissions(2, "mum", admins, family, kids), 32 | 128)   # no longer an admin
        self.assertEqual(m.seerr_permissions(0, "stranger", admins, family, kids), 0)

    def test_status_says_no_secret(self):
        self.everything_set_up()
        r = self.ok(["status"])
        d = json.loads(r.stdout)
        self.assertEqual((d["vpn"]["public_ip"], d["vpn"]["port"], d["qbittorrent"]["listen_port"]), ("198.51.100.7", 12345, 12345))
        self.assertEqual((d["qbittorrent"]["categories"], d["prowlarr"]["apps"], d["prowlarr"]["proxies"]),
                         (["audiobooks", "books", "movies", "music", "tv"], ["Lidarr", "Radarr", "Sonarr"], ["FlareSolverr"]))
        self.assertTrue(d["radarr"]["root"] and d["sonarr"]["client"] and d["lidarr"]["root"])
        self.assertEqual((d["lidarr"]["tags_written"], d["bazarr"]["signed_in"], d["bindery"]["setup_open"], d["seerr"]["local_login"],
                          d["seerr"]["ours"]), (False, True, False, False, True))
        self.apps.seerr["users"][0]["jellyfinUsername"] = "mallory"
        self.assertFalse(json.loads(self.ok(["status"]).stdout)["seerr"]["ours"])
        # every app refuses a caller without its credentials (the fake Servarr: its login page)
        self.assertEqual(d["locked"], {"qbittorrent": True, "prowlarr": True, "radarr": True, "sonarr": True, "lidarr": True,
                                       "bazarr": True, "bindery": True, "seerr": True})
        for v in KEYS.values():
            self.assertNotIn(v, r.stdout)

    def test_redacted(self):
        # An app that echoes a secret in its error: the text says … instead
        m = setup_module()
        os.environ["RADARR_API_KEY"] = KEYS["RADARR_API_KEY"]
        try:
            self.assertEqual(m.redact(f"bad key {KEYS['RADARR_API_KEY']}!"), "bad key …!")
        finally:
            del os.environ["RADARR_API_KEY"]

    def test_unmonitor_new_seasons(self):
        self.apps.arr["sonarr"]["series"] = [{"id": 1, "monitorNewItems": "all", "monitored": True},
                                             {"id": 2, "monitorNewItems": "none", "monitored": True}]
        self.ok(["unmonitor-new-seasons"])
        self.assertEqual(self.changes(), [("sonarr", "PUT", "/api/v3/series/editor", {"seriesIds": [1], "monitorNewItems": "none"})])

    def test_guard(self):
        q = self.apps.qbt
        q["torrents"] = [{"hash": "a", "state": "downloading", "tags": ""}, {"hash": "b", "state": "stoppedUP", "tags": ""}]
        self.assertEqual(run_setup(["guard", "20"], self.base, DOWNLOADS_KEEP_FREE_PCT="15").returncode, 0)
        self.assertEqual(self.changes(), [])
        r = run_setup(["guard", "9.5"], self.base, DOWNLOADS_KEEP_FREE_PCT="15")
        self.assertEqual(r.returncode, 3, r.stderr)                   # stopped just now: the unit mails
        self.assertEqual([(t["state"], t["tags"]) for t in q["torrents"]], [("stoppedDL", "guard"), ("stoppedUP", "")])
        self.assertEqual(run_setup(["guard", "9"], self.base).returncode, 0)   # already stopped: no second mail
        self.assertEqual(run_setup(["guard", "17"], self.base).returncode, 0)  # not 5 points above yet
        self.assertEqual(q["torrents"][0]["state"], "stoppedDL")
        self.assertEqual(run_setup(["guard", "21"], self.base).returncode, 0)
        self.assertEqual([(t["state"], t["tags"]) for t in q["torrents"]], [("downloading", ""), ("stoppedUP", "")])

    def test_qbittorrent_never_downloads_programs(self):
        self.everything_set_up()
        prefs = self.apps.qbt["prefs"]
        self.assertIs(prefs["excluded_file_names_enabled"], True)
        names = prefs["excluded_file_names"].split("\n")
        for p in ("*.exe", "*.scr", "*.lnk", "*.bat", "*.msi", "*.vbs", "*.ps1"):
            self.assertIn(p, names)
        for keep in ("*.mkv", "*.mp4", "*.srt", "*.flac", "*.epub", "*.m4b", "*.nfo"):
            self.assertNotIn(keep, names)

    def test_fakes(self):
        def item(i, title, download_id, status="completed", said=()):
            return {"id": i, "title": title, "downloadId": download_id, "status": status, "indexer": "Site (Prowlarr)",
                    "statusMessages": [{"title": title, "messages": list(said)}] if said else []}
        q, sonarr, radarr = self.apps.qbt, self.apps.arr["sonarr"], self.apps.arr["radarr"]
        # nothing fake: nothing removed, exit 0
        q["torrents"] = [{"hash": "aa11", "size": 1_400_000_000, "total_size": 1_400_000_000, "tags": "", "state": "uploading"}]
        sonarr["queue"] = [item(1, "Show.S01E01.1080p.WEB-DL-GRP", "AA11")]
        r = run_setup(["fakes"], self.base)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(sonarr["removed"], [])
        q["torrents"] += [
            # every file excluded (it held only a program): qBittorrent wants 0 of its bytes
            {"hash": "bb22", "size": 0, "total_size": 1_154_028_818, "tags": "", "state": "stalledUP"},
            # still downloading, nothing said yet: left alone
            {"hash": "dd44", "size": 900_000_000, "total_size": 900_000_000, "tags": "", "state": "downloading"}]
        sonarr["queue"] += [
            item(2, "Show.S01E05.1080p.WEB-DL-NTb", "BB22"),
            item(3, "Show.S01E06.1080p.WEB-DL-NTb", "CC33", said=["Caution: Found executable file with extension: '.exe'"]),
            item(4, "Show.S01E07.1080p.WEB-DL-GRP", "DD44", status="downloading")]
        radarr["queue"] = [item(9, "Film.2026.1080p.WEB-DL-FAKE", "EE55",
                                said=["No files found are eligible for import in /data/torrents/movies/Film"])]
        r = run_setup(["fakes"], self.base)
        self.assertEqual(r.returncode, 3, r.stderr)   # removed some: the guard's unit mails
        self.assertEqual(sorted(i for i, _ in sonarr["removed"]), [2, 3])
        self.assertEqual([i for i, _ in radarr["removed"]], [9])
        for _, query in sonarr["removed"] + radarr["removed"]:
            # out of qBittorrent, never offered again, and the app searches for another
            self.assertEqual(query, {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"})
        self.assertEqual([x["id"] for x in sonarr["queue"]], [1, 4])
        for t in ("Show.S01E05.1080p.WEB-DL-NTb", "Show.S01E06.1080p.WEB-DL-NTb", "Film.2026.1080p.WEB-DL-FAKE"):
            self.assertIn(t, r.stdout)
        self.assertEqual(run_setup(["fakes"], self.base).returncode, 0)   # gone: no second mail

    def test_fakes_only_asks_the_chosen_apps(self):
        self.apps.arr["sonarr"]["queue"] = [{"id": 3, "title": "x", "downloadId": "CC33", "status": "completed",
                                             "statusMessages": [{"title": "x", "messages": ["Found executable file"]}]}]
        r = run_setup(["fakes"], self.base, COMPONENTS="core,downloads,albums")   # no movies-tv: Sonarr isn't asked
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.apps.arr["sonarr"]["removed"], [])

    def test_one_app_failing_leaves_the_others(self):
        r = run_setup(["apply"], self.base, SETUP_RADARR="http://127.0.0.1:9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ 127.0.0.1:9", r.stderr)
        self.assertIn("not set up: Radarr", r.stdout)
        self.assertTrue(self.apps.arr["sonarr"]["rootfolders"] and self.apps.qbt["categories"])

    def test_usage(self):
        self.assertEqual(run_setup(["nope"]).returncode, 2)


class Init(unittest.TestCase):
    def init(self, vol: Path, data: Path, **extra) -> subprocess.CompletedProcess:
        return run_setup(["init"], **{"SETUP_VOL": str(vol), "SETUP_DATA": str(data), "MEDIA_DIR": "/srv/media",
                                      "AUDIOBOOKS_DIR": "/srv/media/audiobooks", **extra})

    def test_fresh_then_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            for v in ("qbittorrent-config", "radarr-config", "bazarr-config"):
                (vol / v).mkdir(parents=True)
            (data / "Movies").mkdir(parents=True)
            r = self.init(vol, data)
            self.assertEqual(r.returncode, 0, r.stderr)
            for rel in ("torrents/movies", "torrents/tv", "TV Shows", "torrents/music", "Music", "torrents/books",
                        "torrents/audiobooks", "Books/Books", "audiobooks"):
                self.assertTrue((data / rel).is_dir(), rel)
            conf = (vol / "qbittorrent-config/qBittorrent/config/qBittorrent.conf").read_text()
            web = dict(ln.split("=", 1) for ln in conf.split("[WebUI]\n")[1].split("[")[0].splitlines() if ln)
            self.assertEqual((web["Username"], web["APIKey"], web["HostHeaderValidation"], web["LocalHostAuth"], web["Port"],
                              web["AuthSubnetWhitelistEnabled"], web["AuthSubnetWhitelist"]),
                             ("admin", KEYS["QBITTORRENT_API_KEY"], "false", "true", "8080", "true", "198.51.100.11/32"))
            # qBittorrent's format: "@ByteArray(base64 salt:base64 key)", PBKDF2-SHA512 with 100 000 rounds
            m = re.fullmatch(r'"@ByteArray\(([^:]+):([^)]+)\)"', web["Password_PBKDF2"])
            salt, key = base64.b64decode(m.group(1)), base64.b64decode(m.group(2))
            self.assertEqual((len(salt), len(key)), (16, 64))
            self.assertEqual(hashlib.pbkdf2_hmac("sha512", KEYS["QBITTORRENT_PASSWORD"].encode(), salt, 100_000, 64), key)
            self.assertIn("MigrationVersion=9999", conf)
            # Bazarr's sign-in before its first start: Basic, its key, no analytics (its own format: MD5 of the password)
            bazarr = (vol / "bazarr-config/config/config.yaml").read_text()
            self.assertEqual(bazarr, "analytics:\n  enabled: false\nauth:\n"
                             f"  apikey: '{KEYS['BAZARR_API_KEY']}'\n  password: '{md5('bazarr-basic-password')}'\n"
                             "  type: basic\n  username: 'homelab'\n")
            self.assertEqual((vol / "bazarr-config/config/config.yaml").stat().st_mode & 0o777, 0o600)
            for k in ("QBITTORRENT_PASSWORD", "BAZARR_AUTH"):
                self.assertNotIn(KEYS[k], conf + bazarr + r.stdout, k)
            again = self.init(vol, data)
            self.assertEqual(again.stdout, "")   # same password: the hash stays, nothing written

    def test_bazarrs_own_config_stays(self):
        # Only a fresh volume gets the seed: later changes go over its API (apply), which keeps the rest of the file
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            conf = vol / "bazarr-config/config/config.yaml"
            conf.parent.mkdir(parents=True)
            conf.write_text("auth:\n  type: form\n")
            self.assertEqual(self.init(vol, data).returncode, 0)
            self.assertEqual(conf.read_text(), "auth:\n  type: form\n")

    def test_existing_config_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            conf = vol / "qbittorrent-config/qBittorrent/config/qBittorrent.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("[BitTorrent]\nSession\\Port=6881\n\n[WebUI]\nPort=8080\nCSRFProtection=true\n\n[Meta]\nMigrationVersion=9999\n")
            r = self.init(vol, data)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = conf.read_text()
            self.assertTrue(text.startswith("[BitTorrent]\nSession\\Port=6881\n\n[WebUI]\nPort=8080\nCSRFProtection=true\n"
                                            "Username=admin\n"), text)
            self.assertTrue(text.endswith("\n\n[Meta]\nMigrationVersion=9999\n"), text)

    def test_only_the_chosen_ones_folders(self):
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            vol.mkdir()
            data.mkdir()
            self.assertEqual(self.init(vol, data, COMPONENTS="core,downloads").returncode, 0)
            self.assertEqual(sorted(p.name for p in data.iterdir()), ["torrents"])
            self.assertEqual(list((data / "torrents").iterdir()), [])

    def test_audiobooks_elsewhere(self):
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            vol.mkdir()
            data.mkdir()
            r = self.init(vol, data, COMPONENTS="core,downloads,book-requests", AUDIOBOOKS_DIR="/mnt/elsewhere/audiobooks")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("/mnt/elsewhere/audiobooks is outside the media folder", r.stdout)
            self.assertFalse((data / "mnt").exists())

    def test_ini_set(self):
        m = setup_module()
        self.assertEqual(m.ini_set("[A]\nx=1\n", "A", {"x": "2", "y": "3"}), "[A]\nx=2\ny=3\n")
        self.assertEqual(m.ini_set("[A]\nx=1\n\n[B]\n", "A", {"y": "3"}), "[A]\nx=1\ny=3\n\n[B]\n")
        self.assertEqual(m.ini_set("[A]\nx=1\n", "B", {"y": "3"}), "[A]\nx=1\n[B]\ny=3\n")
        self.assertTrue(m.qbt_hash_ok(m.qbt_hash("pw"), "pw"))
        self.assertFalse(m.qbt_hash_ok(m.qbt_hash("pw"), "other"))


# ── downloads/run.sh and downloads/guard.sh with docker stubbed ────────────

DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
[ "$1" = start ] && [ "$2" = -ai ] && echo "stdin $(cat)" >> "$STUB_LOG"
case "$*" in
  "network connect "*) [ -z "$NET_FAIL" ] || exit 1 ;;
  "compose config --format json") echo '{"name": "homelab-dltest", "services": {}}' ;;
  "volume inspect homelab-dltest_qbittorrent-config"|"volume inspect homelab-dltest_prowlarr-config") ;;
  "volume inspect homelab-dltest_seerr-config") ;;
  "volume inspect "*) exit 1 ;;
  "create "*) echo cid ;;
  "compose ps -q --status running "*) for s in $RUNNING; do [ "$s" = "${6}" ] && echo "id-$s"; done ;;
  "compose ps -q "*) echo "id-$4" ;;
  "inspect -f {{.State.StartedAt}} "*) eval "echo \$STARTED_${4#id-}" ;;
  "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{end}} "*) eval "echo \$HEALTH_${4#id-}" ;;
esac
exit 0
"""


def stub_dir(d: Path, **stubs: str) -> Path:
    b = d / "bin"
    b.mkdir()
    for name, body in {"docker": DOCKER, **stubs}.items():
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    return b


class Wrapper(unittest.TestCase):
    def run_sh(self, *args: str, components: str = ALL, **extra) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            b = stub_dir(Path(d))
            envf = Path(d) / ".env"
            envf.write_text(f"COMPONENTS={components}\nHOST_UID=1000\nHOST_GID=1000\nADMIN_USER=admin\n"
                            "MEDIA_DIR=/srv/media\nJELLYFIN_ADMIN_PASSWORD=from-the-env-file\n"
                            + "".join(f"{k}='{v}'\n" for k, v in KEYS.items()))
            env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", ENV_FILE=str(envf), STUB_LOG=str(Path(d) / "log"),
                       DOCKER_HOST="unix:///nonexistent", **extra)
            r = subprocess.run(["bash", str(ROOT / "downloads/run.sh"), *args], capture_output=True, text=True, env=env, timeout=30)
            log = Path(d) / "log"
            return r, log.read_text().splitlines() if log.exists() else []

    def test_the_project_decides_networks_and_volumes(self):
        r, calls = self.run_sh("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next(c for c in calls if c.startswith("docker create"))
        self.assertIn("--network homelab-dltest_arr", run)
        self.assertNotIn(" homelab_", run)
        # Seerr's network too, when requests is chosen
        self.assertIn("docker network connect homelab-dltest_requests cid", calls)
        self.assertIn("docker start -a cid", calls)
        _, calls = self.run_sh("apply", components="core,downloads,movies-tv")
        self.assertFalse([c for c in calls if "network connect" in c])
        _, calls = self.run_sh("seerr-users")
        self.assertIn("--network homelab-dltest_requests", next(c for c in calls if c.startswith("docker create")))
        r, calls = self.run_sh("init")
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertIn("--network none", run)
        # only what compose made (the stub has three): one made here would lack compose's labels
        self.assertEqual(re.findall(r"-v (homelab-dltest_[a-z-]+):", run),
                         ["homelab-dltest_qbittorrent-config", "homelab-dltest_prowlarr-config", "homelab-dltest_seerr-config"])
        self.assertIn("--cap-add CHOWN", run)

    def test_secrets_go_by_name(self):
        _, calls = self.run_sh("seerr-init", JELLYFIN_ADMIN_PASSWORD="typed once")
        run = next(c for c in calls if c.startswith("docker create"))
        for k in ("QBITTORRENT_API_KEY", "BAZARR_AUTH", "SEERR_API_KEY"):
            self.assertIn(f"-e {k} ", run, k)
        for v in list(KEYS.values()) + ["typed once"]:
            self.assertNotIn(v, run)
        # the typed password: on stdin, in no container's environment (docker inspect)
        self.assertNotIn("-e JELLYFIN_ADMIN_PASSWORD ", run)
        self.assertIn("-e JELLYFIN_ADMIN_PASSWORD_STDIN=1", run)
        self.assertIn("stdin typed once", calls)
        # not --rm (it races docker start -ai for the exit code): removed by run.sh itself
        self.assertNotIn("--rm", run)
        self.assertEqual(calls[-1], "docker rm -f cid")

    def test_the_jellyfin_password_only_from_the_caller(self):
        # never from .env (it's never stored): without the caller's, none goes in
        _, calls = self.run_sh("seerr-init")
        run = next(c for c in calls if c.startswith("docker create"))
        self.assertNotIn("JELLYFIN_ADMIN_PASSWORD", run)
        self.assertNotIn("from-the-env-file", "\n".join(calls))
        self.assertIn("docker start -a cid", calls)

    def test_a_failed_network_leaves_no_container(self):
        # never started, --rm wouldn't take it: with every key in its environment
        r, calls = self.run_sh("status", NET_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("docker rm -f cid", calls)
        self.assertFalse([c for c in calls if c.startswith("docker start")])

    def test_a_crlf_env(self):
        with tempfile.TemporaryDirectory() as d:
            probe = 'case "$1" in create) env | grep "^QBITTORRENT_API_KEY=" >> "$STUB_LOG" ;; esac\nexit 0\n'
            b = stub_dir(Path(d), docker=DOCKER.rsplit("exit 0\n", 1)[0] + probe)
            envf = Path(d) / ".env"
            envf.write_bytes(b"COMPONENTS=core,downloads\r\nQBITTORRENT_API_KEY='qbt_crlf'\r\n")
            env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", ENV_FILE=str(envf), STUB_LOG=str(Path(d) / "log"),
                       DOCKER_HOST="unix:///nonexistent")
            subprocess.run(["bash", str(ROOT / "downloads/run.sh"), "status"], capture_output=True, env=env, timeout=30)
            self.assertIn("QBITTORRENT_API_KEY=qbt_crlf", (Path(d) / "log").read_text().splitlines())

    def test_usage(self):
        r, calls = self.run_sh("guard")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(calls, [])


class Guard(unittest.TestCase):
    def guard(self, *, components="core,downloads", running="gluetun qbittorrent prowlarr", started=None, health=None,
              free_pct=50, run_code=0) -> tuple[int, str, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            box = Path(d) / "box"
            (box / "downloads").mkdir(parents=True)
            shutil.copy(ROOT / "downloads/guard.sh", box / "downloads")
            shutil.copytree(ROOT / "lib", box / "lib")
            (box / "downloads/run.sh").write_text(f'echo "run.sh $*" >> "$STUB_LOG"; exit {run_code}\n')
            (box / ".env").write_text(f"COMPONENTS={components}\nMEDIA_DIR=/srv/media\nDOWNLOADS_KEEP_FREE_PCT=15\n")
            df = f'#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted"\necho "/dev/x 1000 0 {free_pct * 10} 0% /"\n'
            b = stub_dir(Path(d), df=df)
            log = Path(d) / "log"
            env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", STUB_LOG=str(log), RUNNING=running,
                       DOCKER_HOST="unix:///nonexistent")
            for s, t in (started or {}).items():
                env[f"STARTED_{s}"] = t
            for s, h in (health or {}).items():
                env[f"HEALTH_{s}"] = h
            r = subprocess.run(["bash", str(box / "downloads/guard.sh")], capture_output=True, text=True, env=env, timeout=30)
            return r.returncode, r.stdout + r.stderr, log.read_text().splitlines() if log.exists() else []

    T0, T1 = "2026-11-07T09:00:00.5Z", "2026-11-07T10:00:00.25Z"

    def test_not_chosen(self):
        self.assertEqual(self.guard(components="core,mesh")[0:3:2], (0, []))

    def test_back_into_gluetun_after_its_restart(self):
        code, out, calls = self.guard(running="gluetun qbittorrent prowlarr byparr",
                                      started={"gluetun": self.T1, "qbittorrent": self.T0, "prowlarr": self.T1, "byparr": self.T0})
        self.assertEqual(code, 0, out)
        self.assertIn("docker compose up -d --force-recreate --no-deps qbittorrent byparr", calls)
        code, out, calls = self.guard(started={"gluetun": self.T0, "qbittorrent": self.T1, "prowlarr": self.T1},
                                      health={"prowlarr": "unhealthy"})
        self.assertIn("docker compose up -d --force-recreate --no-deps prowlarr", calls)
        self.assertFalse([c for c in calls if "recreate" in c and "qbittorrent" in c])

    def test_disk(self):
        same = {"gluetun": self.T0, "qbittorrent": self.T1, "prowlarr": self.T1}
        code, out, calls = self.guard(started=same, free_pct=9, run_code=3)
        self.assertEqual(code, 1, out)                          # stopped them just now: one mail
        self.assertIn("run.sh guard 9.0", calls)
        self.assertEqual(self.guard(started=same, free_pct=9, run_code=0)[0], 0)   # already stopped
        code, out, _ = self.guard(started=same, free_pct=50, run_code=1)
        self.assertEqual(code, 0, out)                          # qBittorrent broken, disk roomy: no mail each 10 min
        self.assertEqual(self.guard(started=same, free_pct=9, run_code=1)[0], 1)   # broken while the disk fills: mail
        _, _, calls = self.guard(running="gluetun", started={"gluetun": self.T0})
        self.assertFalse([c for c in calls if c.startswith("run.sh")])   # qBittorrent stopped on purpose


# ── the wizard's stages under the protocol ───────────────────────────────

WIZARD_STUBS = {
    # The apps' status as downloads/run.sh status reports it (gluetun's port is qBittorrent's); SEERR and BINDERY in
    # the environment say whether their first step ran. A failing first step: FAIL_INIT
    "run.sh": """echo "run.sh $* JFPW=${JELLYFIN_ADMIN_PASSWORD:+set}" >> "$STUB_LOG"
case "$1" in
  status) echo "{\\"vpn\\": {\\"public_ip\\": \\"198.51.100.7\\", \\"port\\": 12345}, \\"qbittorrent\\": {\\"listen_port\\": 12345,
    \\"interface\\": \\"tun0\\", \\"upnp\\": false, \\"categories\\": [\\"movies\\", \\"tv\\"]},
    \\"seerr\\": {\\"ready\\": ${SEERR:-false}}, \\"bindery\\": {\\"ready\\": ${BINDERY:-false}}}" ;;
  seerr-init|bindery-init) [ -z "$FAIL_INIT" ] || exit 1 ;;
esac
exit 0
""",
    "docker": """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "inspect -f "*) echo healthy ;;
  "compose ps "*) echo id ;;
  "compose config --format json") echo '{"name": "homelab-dltest"}' ;;
  "network inspect "*) exit 1 ;;
esac
exit 0
""",
    "curl": """#!/bin/sh
case "$*" in *cdn-cgi/trace*) echo "ip=203.0.113.5" ;; *) printf 302 ;; esac
""",
    "sudo": "#!/bin/sh\necho \"sudo $*\" >> \"$STUB_LOG\"\n",
    "sleep": "#!/bin/sh\nexit 0\n",
    "ss": "#!/bin/sh\nexit 0\n",
    "systemctl": "#!/bin/sh\nexit 3\n",
    "dig": "#!/bin/sh\nexit 1\n",
    "xdg-open": "#!/bin/sh\nexit 0\n",
}
WG_KEY = "Z29sZGVuLXdpcmVndWFyZC1wcml2YXRlLWtleS0wMDA="
# What makes, starts, stops or sets up something (the wizard's closing checks only look: status, ps, wget)
CHANGES = re.compile(r"^(docker compose (create|up|stop|restart)|run\.sh (?!status))")


def wizard_stage(stage: str, answers: list[str], components="core,downloads", env_extra: str = "",
                 check: bool = False, **environ) -> tuple[int, list[dict], list[str], str, str]:
    with tempfile.TemporaryDirectory() as d:
        box = Path(d) / "box"
        for sub in ("downloads", "authentik", "ansible", "ntfy"):
            (box / sub).mkdir(parents=True)
        shutil.copy(ROOT / "downloads-wizard.sh", box)
        shutil.copytree(ROOT / "lib", box / "lib")
        (box / "downloads/run.sh").write_text(WIZARD_STUBS["run.sh"])
        for script in ("authentik/setup-ssh.sh", "ntfy/setup.sh"):
            (box / script).write_text(f'#!/bin/sh\necho "{Path(script).name} $*" >> "$STUB_LOG"\n'
                                      '[ "$1" = members ] && [ "$2" = "$MEMBERS_FAIL" ] && exit 1\n'
                                      '[ "$1" = members ] && [ "$2" = kids ] && echo kid\nexit 0\n')
            (box / script).chmod(0o755)
        (box / "ansible/live-safe.sh").write_text("exit 0\n")
        (box / ".env").write_text(f"DOMAIN=example.org\nDOMAIN_ALIAS=example.net\nNETBIRD_IP=100.64.0.5\nADMIN_USER=admin\n"
                                  f"COMPONENTS={components}\n{env_extra}")
        b = Path(d) / "bin"
        b.mkdir()
        for name, body in WIZARD_STUBS.items():
            if name != "run.sh":
                (b / name).write_text(body)
                (b / name).chmod(0o755)
        log = Path(d) / "calls.log"
        env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", HOME=d, USER="admin", HOMELAB_UI="protocol",
                   HOMELAB_STAGES=stage, HOMELAB_STAGES_TOTAL="1", TERM="dumb", STUB_LOG=str(log),
                   DOCKER_HOST="unix:///nonexistent", **environ)
        args = ["bash", str(box / "downloads-wizard.sh")] + (["--check"] if check else [])
        r = subprocess.run(args, input="".join(a + "\n" for a in ([] if check else [""]) + answers),
                           capture_output=True, text=True, env=env, timeout=60)
        msgs = [json.loads(x[1:]) for x in r.stdout.split("\n") if x.startswith("\x1e")]
        calls = log.read_text().splitlines() if log.exists() else []
        if (box / "site/status/requests-ready").exists():   # Caddy would let anyone reach Seerr
            calls.append("marker requests-ready")
        return r.returncode, msgs, calls, (box / ".env").read_text(), r.stdout


class Wizard(unittest.TestCase):
    def test_secrets_only_the_vpn(self):
        # No password to choose: Authentik's sign-in is the one people use, the apps' own are generated
        _, msgs, _, env, out = wizard_stage("secrets", [WG_KEY, "Switzerland"])
        self.assertIn(f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'", env)
        self.assertIn("VPN_SERVER_COUNTRIES='Switzerland'", env)
        self.assertNotIn(WG_KEY, out)
        self.assertEqual([m["key"] for m in msgs if m["t"] == "secret"], ["VPN_WIREGUARD_PRIVATE_KEY"])
        self.assertNotIn("PASSWORD", env)

    def test_a_malformed_key_is_asked_again(self):
        _, msgs, _, env, _ = wizard_stage("secrets", ["PrivateKey = " + WG_KEY, WG_KEY, ""])
        self.assertEqual([m["key"] for m in msgs if m["t"] == "secret"].count("VPN_WIREGUARD_PRIVATE_KEY"), 2)
        self.assertIn(f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'", env)

    def test_start_only_the_chosen(self):
        env = f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'\n"
        _, _, calls, _, _ = wizard_stage("start", [], env_extra=env)
        order = [c for c in calls if c.startswith(("docker compose", "run.sh"))
                 and not c.startswith(("docker compose ps", "docker compose config"))]
        self.assertEqual(order[:6], ["docker compose stop qbittorrent prowlarr",
                                     "docker compose create gluetun qbittorrent prowlarr",
                                     "run.sh init JFPW=",
                                     "docker compose up -d gluetun",
                                     "docker compose up -d qbittorrent prowlarr",
                                     "docker compose up -d --force-recreate --no-deps caddy"])
        self.assertFalse([c for c in calls if re.search(r"\b(radarr|sonarr|recyclarr|lidarr|bazarr|byparr)\b", c)])
        _, _, calls, _, _ = wizard_stage("start", [], components=ALL, env_extra=env)
        self.assertIn("docker compose create gluetun qbittorrent prowlarr byparr radarr sonarr recyclarr lidarr bazarr", calls)
        # Seerr and Bindery never here: their first sign-in makes their admin (their own stages)
        self.assertFalse([c for c in calls if CHANGES.match(c) and re.search(r"\b(seerr|bindery)\b", c)])

    def test_start_leaves_8s_network_behind(self):
        # #8's downloads network (its range not pinned): once Caddy and the apps have left it, it goes
        env = f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'\n"
        _, _, calls, _, _ = wizard_stage("start", [], env_extra=env)
        self.assertLess(calls.index("docker compose up -d --force-recreate --no-deps caddy"),
                        calls.index("docker network rm homelab-dltest_downloads"))

    def test_start_wants_the_key_first(self):
        _, msgs, calls, _, _ = wizard_stage("start", [])
        self.assertFalse([c for c in calls if c.startswith("docker compose create")])
        self.assertIn("No VPN key", "\n".join(m.get("text", "") for m in msgs))

    def test_a_stage_of_a_component_not_chosen(self):
        for stage in ("subtitles", "albums", "books", "requests", "requests_proxy", "library"):
            code, msgs, calls, _, _ = wizard_stage(stage, [])
            self.assertFalse([m for m in msgs if m["t"] == "stage"], stage)
            self.assertFalse([c for c in calls if CHANGES.match(c)], stage)

    def test_requests_back_to_back(self):
        _, msgs, calls, env, out = wizard_stage("requests", ["jfadmin", "the jellyfin admin password"], components=ALL)
        steps = [c for c in calls if CHANGES.match(c) or c.startswith(("setup.sh token", "marker"))]
        # the password only for Seerr's first sign-in (its init and everything else without it), then whether it's
        # ours, only then the marker that lets Caddy reach it
        self.assertEqual(steps[:7], ["docker compose create seerr", "run.sh init JFPW=", "docker compose up -d --no-deps seerr",
                                     "run.sh seerr-init JFPW=set", "run.sh seerr-init JFPW=",
                                     "setup.sh token seerr requests SEERR_NTFY_TOKEN", "run.sh apply JFPW="])
        self.assertIn("docker compose restart seerr", steps)
        self.assertIn("run.sh seerr-users JFPW=", calls)
        self.assertEqual(calls[-1], "marker requests-ready")
        # the typed password: to Seerr's first sign-in only, never into .env or the output
        self.assertIn("JELLYFIN_ADMIN_USER='jfadmin'", env)
        self.assertNotIn("the jellyfin admin password", env + out)
        self.assertEqual([m["key"] for m in msgs if m["t"] == "secret"], ["JELLYFIN_ADMIN_PASSWORD"])

    def test_requests_stops_seerr_when_its_first_step_fails(self):
        _, _, calls, _, _ = wizard_stage("requests", ["jfadmin", "wrong"], components=ALL, FAIL_INIT="1")
        self.assertIn("docker compose stop seerr", calls)
        self.assertFalse([c for c in calls if c.startswith("run.sh apply")])
        self.assertNotIn("marker requests-ready", calls)

    def test_requests_never_without_authentiks_groups(self):
        # a lookup that fails: no permissions set at all (kids are in family too, they'd count as adults)
        _, _, calls, _, _ = wizard_stage("requests", [], components=ALL, SEERR="true", MEMBERS_FAIL="kids")
        self.assertFalse([c for c in calls if c.startswith("run.sh seerr-users")])

    def test_requests_again_asks_nothing(self):
        # set up before: no password asked, nothing started over
        _, msgs, calls, _, _ = wizard_stage("requests", [], components=ALL, SEERR="true")
        self.assertFalse([m for m in msgs if m["t"] in ("secret", "input")])
        self.assertFalse([c for c in calls if c.startswith(("docker compose create", "run.sh seerr-init JFPW=set"))])
        self.assertIn("run.sh seerr-init JFPW=", calls)   # still ours?
        self.assertIn("run.sh seerr-users JFPW=", calls)
        self.assertIn("marker requests-ready", calls)

    def test_books_back_to_back(self):
        _, _, calls, _, _ = wizard_stage("books", [], components=ALL)
        steps = [c for c in calls if CHANGES.match(c)]
        self.assertEqual(steps[:5], ["docker compose create bindery", "run.sh init JFPW=", "docker compose up -d --no-deps bindery",
                                     "run.sh bindery-init JFPW=", "run.sh apply JFPW="])
        _, _, calls, _, _ = wizard_stage("books", [], components=ALL, FAIL_INIT="1")
        self.assertIn("docker compose stop bindery", calls)

    def test_check_when_not_chosen(self):
        code, _, calls, _, out = wizard_stage("", [], components="core,mesh", check=True)
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in calls if c.startswith("docker")])


if __name__ == "__main__":
    unittest.main()
