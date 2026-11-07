"""The downloads components (downloads, movies-tv; downloads-wizard.sh): their compose shape in tests/golden/full/,
downloads/setup.py against a stand-in for the apps' APIs, downloads/run.sh's project and secrets, downloads/guard.sh,
and the wizard's stages, run in a copy with their commands stubbed (DOCKER_HOST to nowhere). The real images:
tests/downloads/run.sh."""
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
SERVICES = {"gluetun", "qbittorrent", "prowlarr", "radarr", "sonarr", "recyclarr"}
SECRETS = ["VPN_WIREGUARD_PRIVATE_KEY", "GLUETUN_CONTROL_KEY", "QBITTORRENT_API_KEY", "DOWNLOADS_ADMIN_PASSWORD",
           "PROWLARR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"]


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

    def test_only_they_and_caddy_on_the_downloads_network(self):
        # Prowlarr's, Radarr's and Sonarr's API keys open them: no other app may reach them
        on = {n for n, s in self.c["services"].items() if "downloads" in (s.get("networks") or {})}
        self.assertIn("downloads", self.c["networks"])
        self.assertEqual(on, {"gluetun", "radarr", "sonarr", "recyclarr", "caddy"})
        for n in ("qbittorrent", "prowlarr"):
            self.assertEqual(self.svc(n)["network_mode"], "service:gluetun", n)
            self.assertEqual(self.svc(n)["healthcheck"]["test"], ["CMD-SHELL", "test -e /sys/class/net/eth0"], n)

    def test_caddy_keeps_its_default_network(self):
        nets = self.svc("caddy")["networks"]
        self.assertEqual((nets["internal"]["ipv4_address"], nets["internal"]["gw_priority"]), ("172.21.1.11", 1))
        self.assertFalse((nets["downloads"] or {}).get("gw_priority"))
        # configure's recreate hint would start them before their setup (downloads/run.sh init)
        self.assertFalse(SERVICES & set(self.svc("caddy").get("depends_on", {})))

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
        for n in ("prowlarr", "radarr", "sonarr"):
            self.assertTrue(any(t.startswith("/run:uid=") for t in self.svc(n)["tmpfs"]), n)
        for n in SERVICES - {"gluetun"}:
            conf = next(v for v in self.svc(n)["volumes"] if v["target"] == "/config")
            self.assertTrue(conf.get("volume", {}).get("nocopy"), n)   # downloads/run.sh init's owner stays

    def test_their_own_logins(self):
        for n in ("prowlarr", "radarr", "sonarr"):
            e = self.svc(n)["environment"]
            # case-sensitive; "External" would hand the API key to anyone who reaches the port (/initialize.json)
            self.assertEqual(e[f"{n.upper()}__AUTH__METHOD"], "Forms", n)
            self.assertEqual(e[f"{n.upper()}__AUTH__REQUIRED"], "Enabled", n)
        role = json.loads(self.svc("gluetun")["environment"]["HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE"])
        self.assertEqual(role, {"auth": "apikey", "apikey": example_value("GLUETUN_CONTROL_KEY")})

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


# ── downloads/setup.py against stand-ins ─────────────────────────────────

class Apps:
    """What setup.py talks to: qBittorrent, Radarr, Sonarr, Prowlarr, gluetun's control server, on one port
    (/qbt, /radarr, /sonarr, /prowlarr, /gluetun)."""

    def __init__(self, keys: dict[str, str]):
        self.keys, self.calls = keys, []
        self.qbt = {"prefs": {"save_path": "/downloads", "upnp": True, "listen_port": 12345, "lsd": True,
                              "current_network_interface": "", "max_ratio_act": 0},
                    "categories": {}, "torrents": []}
        arr = lambda: {"host": {"id": 1, "authenticationMethod": "forms", "username": "", "password": "",
                                "apiKey": "x", "instanceName": "kept"},
                       "rootfolders": [], "clients": [], "indexers": [], "profiles": [{"name": "Any"}],
                       "mm": {"id": 1, "copyUsingHardlinks": False, "fileDate": "none", "recycleBin": "",
                              "minimumFreeSpaceWhenImporting": 100, "downloadPropersAndRepacks": "preferAndUpgrade",
                              "autoUnmonitorPreviouslyDownloadedMovies": False,
                              "autoUnmonitorPreviouslyDownloadedEpisodes": False},
                       "series": [], "movies": [], "collections": []}
        self.arr = {"radarr": arr(), "sonarr": arr(), "prowlarr": arr()}
        self.arr["prowlarr"]["apps"] = []

    def handle(self, h, method: str):
        u = urllib.parse.urlsplit(h.path)
        app, _, path = u.path.lstrip("/").partition("/")
        path = "/" + path
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n).decode() if n else ""
        body = json.loads(raw) if raw.startswith(("{", "[")) else dict(urllib.parse.parse_qsl(raw))
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
        return self.servarr(h, app, method, path, body)

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

    def servarr(self, h, app, method, path, body):
        a = self.arr[app]
        if path == "/login":
            ok = a["host"]["username"] and body.get("username") == a["host"]["username"] \
                and body.get("password") == a["host"]["password"]
            return 302, "", {"Location": "/" if ok else "/login?returnUrl=%2F&loginFailed=true"}
        if h.headers.get("X-Api-Key") != self.keys[f"{app.upper()}_API_KEY"]:
            return 401, {}
        path = re.sub(r"^/api/v[13]", "", path)
        coll = {"/rootfolder": "rootfolders", "/downloadclient": "clients", "/applications": "apps",
                "/indexer": "indexers", "/qualityprofile": "profiles", "/series": "series", "/movie": "movies",
                "/collection": "collections"}
        if method == "GET":
            if path == "/config/host":
                return 200, dict(a["host"], password="")
            if path == "/config/mediamanagement":
                return 200, a["mm"]
            if path.endswith("/schema"):
                kind = path.split("/")[1]
                if kind == "downloadclient":
                    names = ["host", "port", "useSsl", "urlBase", "apiKey", "username", "password", "movieCategory",
                             "tvCategory", "initialState"]
                    return 200, [{"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
                                  "fields": [{"name": x, "value": None} for x in names]}]
                return 200, [{"implementation": impl, "configContract": f"{impl}Settings",
                              "fields": [{"name": x, "value": [2000] if x == "syncCategories" else None}
                                         for x in ("prowlarrUrl", "baseUrl", "apiKey", "syncCategories")]}
                             for impl in ("Radarr", "Sonarr", "Lidarr")]
            return 200, a[coll[path]]
        if path.startswith("/config/host/"):
            a["host"].update({k: body[k] for k in ("authenticationMethod", "username", "password")})
            return 202, body
        if path.startswith("/config/mediamanagement/"):
            a["mm"] = body
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
            a[coll[key]] = [body if x["id"] == int(i) else x for x in a[coll[key]]]
            return 202, body
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

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


KEYS = {"QBITTORRENT_API_KEY": "qbt_" + "a" * 28, "PROWLARR_API_KEY": "p" * 32, "RADARR_API_KEY": "r" * 32,
        "SONARR_API_KEY": "s" * 32, "GLUETUN_CONTROL_KEY": "g" * 32, "DOWNLOADS_ADMIN_PASSWORD": "correct horse battery"}


def run_setup(args: list[str], base: str = "", **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOST_UID=str(os.getuid()), HOST_GID=str(os.getgid()), ADMIN_USER="admin", MOVIES_TV="1", **KEYS)
    env.update(extra)
    if base:
        env.update({f"SETUP_{k}": f"{base}/{p}" for k, p in
                    (("QBT", "qbt"), ("PROWLARR", "prowlarr"), ("RADARR", "radarr"), ("SONARR", "sonarr"), ("GLUETUN", "gluetun"))})
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

    def test_apply_then_a_second_run_changes_nothing(self):
        r = run_setup(["apply"], self.base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        q = self.apps.qbt
        self.assertEqual(q["categories"], {"movies": {"name": "movies", "savePath": "/data/torrents/movies"},
                                           "tv": {"name": "tv", "savePath": "/data/torrents/tv"}})
        self.assertEqual({k: q["prefs"][k] for k in ("save_path", "upnp", "lsd", "current_network_interface",
                                                     "max_ratio", "max_seeding_time", "bypass_local_auth")},
                         {"save_path": "/data/torrents", "upnp": False, "lsd": False, "current_network_interface": "tun0",
                          "max_ratio": 2.0, "max_seeding_time": 14 * 1440, "bypass_local_auth": False})
        self.assertEqual(q["prefs"]["listen_port"], 12345)   # gluetun's, untouched
        for app, root, cat in (("radarr", "/data/Movies", ("movieCategory", "movies")),
                               ("sonarr", "/data/TV Shows", ("tvCategory", "tv"))):
            a = self.apps.arr[app]
            self.assertEqual(a["host"]["username"], "admin")
            self.assertEqual([r["path"] for r in a["rootfolders"]], [root])
            fields = {f["name"]: f["value"] for f in a["clients"][0]["fields"]}
            self.assertEqual((fields["host"], fields["port"], fields["apiKey"], fields[cat[0]], fields["username"]),
                             ("gluetun", 8080, KEYS["QBITTORRENT_API_KEY"], cat[1], ""))
            self.assertTrue(a["clients"][0]["removeCompletedDownloads"] and a["clients"][0]["removeFailedDownloads"])
            # the whole object went back: what setup.py doesn't set keeps its value
            self.assertEqual((a["mm"]["fileDate"], a["mm"]["copyUsingHardlinks"], a["mm"]["downloadPropersAndRepacks"]),
                             ("none", True, "doNotPrefer"))
        apps = {x["implementation"]: {f["name"]: f["value"] for f in x["fields"]} for x in self.apps.arr["prowlarr"]["apps"]}
        self.assertEqual(sorted(apps), ["Radarr", "Sonarr"])
        # what Radarr and Sonarr are told, as the downloads network sees it (not where this script reached them)
        self.assertEqual((apps["Radarr"]["prowlarrUrl"], apps["Radarr"]["baseUrl"], apps["Radarr"]["syncCategories"]),
                         ("http://gluetun:9696", "http://radarr:7878", [2000]))
        for k in ("DOWNLOADS_ADMIN_PASSWORD", "QBITTORRENT_API_KEY", "RADARR_API_KEY"):
            self.assertNotIn(KEYS[k], r.stdout + r.stderr, k)
        before = len(self.changes())
        r = run_setup(["apply"], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.changes()[before:], [])
        self.assertEqual(r.stdout.strip(), "all set")

    def test_downloads_alone_leaves_radarr_and_sonarr_be(self):
        r = run_setup(["apply"], self.base, MOVIES_TV="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.apps.calls if c[0] in ("radarr", "sonarr")])
        self.assertEqual(self.apps.arr["prowlarr"]["apps"], [])

    def test_status_says_no_secret(self):
        run_setup(["apply"], self.base)
        r = run_setup(["status"], self.base)
        d = json.loads(r.stdout)
        self.assertEqual((d["vpn"]["public_ip"], d["vpn"]["port"], d["qbittorrent"]["listen_port"]), ("198.51.100.7", 12345, 12345))
        self.assertEqual((d["qbittorrent"]["categories"], d["prowlarr"]["apps"]), (["movies", "tv"], ["Radarr", "Sonarr"]))
        self.assertTrue(d["radarr"]["root"] and d["sonarr"]["client"])
        for v in KEYS.values():
            self.assertNotIn(v, r.stdout)

    def test_unmonitor_new_seasons(self):
        self.apps.arr["sonarr"]["series"] = [{"id": 1, "monitorNewItems": "all", "monitored": True},
                                             {"id": 2, "monitorNewItems": "none", "monitored": True}]
        r = run_setup(["unmonitor-new-seasons"], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
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

    def test_unreachable_says_which(self):
        r = run_setup(["apply"], "http://127.0.0.1:9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ 127.0.0.1:9", r.stderr)

    def test_usage(self):
        self.assertEqual(run_setup(["nope"]).returncode, 2)


class Init(unittest.TestCase):
    def init(self, vol: Path, data: Path) -> subprocess.CompletedProcess:
        return run_setup(["init"], SETUP_VOL=str(vol), SETUP_DATA=str(data))

    def test_fresh_then_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            vol, data = Path(d) / "vol", Path(d) / "data"
            for v in ("qbittorrent-config", "radarr-config"):
                (vol / v).mkdir(parents=True)
            (data / "Movies").mkdir(parents=True)
            r = self.init(vol, data)
            self.assertEqual(r.returncode, 0, r.stderr)
            for rel in ("torrents/movies", "torrents/tv", "TV Shows"):
                self.assertTrue((data / rel).is_dir(), rel)
            conf = (vol / "qbittorrent-config/qBittorrent/config/qBittorrent.conf").read_text()
            web = dict(ln.split("=", 1) for ln in conf.split("[WebUI]\n")[1].split("[")[0].splitlines() if ln)
            self.assertEqual((web["Username"], web["APIKey"], web["HostHeaderValidation"], web["LocalHostAuth"], web["Port"]),
                             ("admin", KEYS["QBITTORRENT_API_KEY"], "false", "true", "8080"))
            # qBittorrent's format: "@ByteArray(base64 salt:base64 key)", PBKDF2-SHA512 with 100 000 rounds
            m = re.fullmatch(r'"@ByteArray\(([^:]+):([^)]+)\)"', web["Password_PBKDF2"])
            salt, key = base64.b64decode(m.group(1)), base64.b64decode(m.group(2))
            self.assertEqual((len(salt), len(key)), (16, 64))
            self.assertEqual(hashlib.pbkdf2_hmac("sha512", KEYS["DOWNLOADS_ADMIN_PASSWORD"].encode(), salt, 100_000, 64), key)
            self.assertIn("MigrationVersion=9999", conf)
            self.assertNotIn(KEYS["DOWNLOADS_ADMIN_PASSWORD"], conf + r.stdout)
            again = self.init(vol, data)
            self.assertEqual(again.stdout, "")   # same password: the hash stays, nothing written

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
case "$*" in
  "compose config --format json") echo '{"name": "homelab-dltest", "services": {}}' ;;
  "volume inspect homelab-dltest_qbittorrent-config"|"volume inspect homelab-dltest_prowlarr-config") ;;
  "volume inspect "*) exit 1 ;;
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
    def run_sh(self, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            b = stub_dir(Path(d))
            envf = Path(d) / ".env"
            envf.write_text("COMPONENTS=core,downloads,movies-tv\nHOST_UID=1000\nHOST_GID=1000\nADMIN_USER=admin\n"
                            "MEDIA_DIR=/srv/media\n" + "".join(f"{k}='{v}'\n" for k, v in KEYS.items()))
            env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", ENV_FILE=str(envf), STUB_LOG=str(Path(d) / "log"),
                       DOCKER_HOST="unix:///nonexistent")
            r = subprocess.run(["bash", str(ROOT / "downloads/run.sh"), *args], capture_output=True, text=True, env=env, timeout=30)
            log = Path(d) / "log"
            return r, log.read_text().splitlines() if log.exists() else []

    def test_the_project_decides_network_and_volumes(self):
        r, calls = self.run_sh("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertIn("--network homelab-dltest_downloads", run)
        self.assertNotIn(" homelab_", run)
        r, calls = self.run_sh("init")
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertIn("--network none", run)
        # only what compose made (the stub has two): one made here would lack compose's labels
        self.assertEqual(re.findall(r"-v (homelab-dltest_[a-z-]+):", run),
                         ["homelab-dltest_qbittorrent-config", "homelab-dltest_prowlarr-config"])
        self.assertIn("--cap-add CHOWN", run)

    def test_secrets_go_by_name(self):
        _, calls = self.run_sh("status")
        run = next(c for c in calls if c.startswith("docker run"))
        self.assertIn("-e QBITTORRENT_API_KEY ", run)
        for v in KEYS.values():
            self.assertNotIn(v, run)

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
        code, out, calls = self.guard(started={"gluetun": self.T1, "qbittorrent": self.T0, "prowlarr": self.T1})
        self.assertEqual(code, 0, out)
        self.assertIn("docker compose up -d --force-recreate --no-deps qbittorrent", calls)
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
    # The four apps' status as downloads/run.sh status reports it (gluetun's port is qBittorrent's)
    "run.sh": """echo "run.sh $*" >> "$STUB_LOG"
[ "$1" = status ] && echo '{"vpn": {"public_ip": "198.51.100.7", "port": 12345}, "qbittorrent": {"listen_port": 12345, "interface": "tun0", "upnp": false, "categories": ["movies", "tv"]}}'
exit 0
""",
    "docker": """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "inspect -f "*) echo healthy ;;
  "compose ps "*) echo id ;;
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


def wizard_stage(stage: str, answers: list[str], components="core,downloads", env_extra: str = "",
                 check: bool = False) -> tuple[int, list[dict], list[str], str, str]:
    with tempfile.TemporaryDirectory() as d:
        box = Path(d) / "box"
        for sub in ("downloads", "authentik", "ansible"):
            (box / sub).mkdir(parents=True)
        shutil.copy(ROOT / "downloads-wizard.sh", box)
        shutil.copytree(ROOT / "lib", box / "lib")
        (box / "downloads/run.sh").write_text(WIZARD_STUBS["run.sh"])
        (box / "authentik/setup-ssh.sh").write_text('#!/bin/sh\necho "setup-ssh.sh $*" >> "$STUB_LOG"\n')
        (box / "authentik/setup-ssh.sh").chmod(0o755)
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
                   DOCKER_HOST="unix:///nonexistent")
        args = ["bash", str(box / "downloads-wizard.sh")] + (["--check"] if check else [])
        r = subprocess.run(args, input="".join(a + "\n" for a in ([] if check else [""]) + answers),
                           capture_output=True, text=True, env=env, timeout=60)
        msgs = [json.loads(x[1:]) for x in r.stdout.split("\n") if x.startswith("\x1e")]
        calls = log.read_text().splitlines() if log.exists() else []
        return r.returncode, msgs, calls, (box / ".env").read_text(), r.stdout


class Wizard(unittest.TestCase):
    def test_secrets_into_env_and_never_shown(self):
        _, msgs, _, env, out = wizard_stage("secrets", [WG_KEY, "Switzerland", "a long enough pw", "a long enough pw"])
        self.assertIn(f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'", env)
        self.assertIn("DOWNLOADS_ADMIN_PASSWORD='a long enough pw'", env)
        self.assertIn("VPN_SERVER_COUNTRIES='Switzerland'", env)
        self.assertNotIn(WG_KEY, out)
        self.assertNotIn("a long enough pw", out)
        self.assertEqual([m["key"] for m in msgs if m["t"] == "secret"],
                         ["VPN_WIREGUARD_PRIVATE_KEY", "DOWNLOADS_ADMIN_PASSWORD", "DOWNLOADS_ADMIN_PASSWORD_AGAIN"])

    def test_a_malformed_key_and_a_mismatch_are_asked_again(self):
        _, msgs, _, env, _ = wizard_stage("secrets", ["PrivateKey = " + WG_KEY, WG_KEY, "", "short", "a long enough pw",
                                                      "a typo in it here", "a long enough pw", "a long enough pw"])
        self.assertEqual([m["key"] for m in msgs if m["t"] == "secret"].count("VPN_WIREGUARD_PRIVATE_KEY"), 2)
        self.assertIn(f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'", env)
        self.assertIn("DOWNLOADS_ADMIN_PASSWORD='a long enough pw'", env)

    def test_start_only_the_chosen(self):
        env = f"VPN_WIREGUARD_PRIVATE_KEY='{WG_KEY}'\n"
        _, _, calls, _, _ = wizard_stage("start", [], env_extra=env)
        docker = [c for c in calls if c.startswith(("docker compose", "run.sh"))]
        order = [c for c in docker if not c.startswith("docker compose ps")]
        self.assertEqual(order[:6], ["docker compose stop qbittorrent prowlarr",
                                     "docker compose create gluetun qbittorrent prowlarr",
                                     "run.sh init",
                                     "docker compose up -d gluetun",
                                     "docker compose up -d qbittorrent prowlarr",
                                     "docker compose up -d --force-recreate --no-deps caddy"])
        self.assertFalse([c for c in calls if re.search(r"\b(radarr|sonarr|recyclarr)\b", c)])
        _, _, calls, _, _ = wizard_stage("start", [], components="core,downloads,movies-tv", env_extra=env)
        self.assertIn("docker compose create gluetun qbittorrent prowlarr radarr sonarr recyclarr", calls)

    def test_start_wants_the_key_first(self):
        _, msgs, calls, _, _ = wizard_stage("start", [])
        self.assertFalse([c for c in calls if c.startswith("docker compose create")])
        self.assertIn("No VPN key", "\n".join(m.get("text", "") for m in msgs))

    def test_check_when_not_chosen(self):
        code, _, calls, _, out = wizard_stage("", [], components="core,mesh", check=True)
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in calls if c.startswith("docker")])


if __name__ == "__main__":
    unittest.main()
