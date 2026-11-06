"""ops/server.py: only allowed names (ops/access.py), only from the home page, only POST."""
import http.client
import importlib.util
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class OpsServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPS_REGISTRY"] = str(ROOT / "jobs/jobs.toml")
        os.environ["OPS_ORIGINS"] = "https://home.example.org:9443"
        os.environ["OPS_COMPONENTS_DIR"] = str(ROOT / "setup/components")
        os.environ["OPS_COMPONENTS"] = "core,mesh,paperless,backups"
        spec = importlib.util.spec_from_file_location("ops_server", ROOT / "ops/server.py")
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.m.ORIGINS = {"https://home.example.org:9443"}
        cls.m.TRUSTED = {"127.0.0.1"}
        cls.tmp = tempfile.mkdtemp()
        cls.m.REQUESTS = cls.tmp
        cls.m.DATA = cls.tmp
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.m.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1])
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_actions_from_registry(self):
        actions = {"backup", "clean-backups", "clean-bins", "clean-docker"}
        self.assertLessEqual(actions, self.m.JOBS)
        # Jobs the homelab user may start, of the chosen components only
        self.assertIn("run-backup", self.m.JOBS)
        self.assertNotIn("run-drift", self.m.JOBS)        # root job, not polkit_start
        self.assertNotIn("run-photos-copy", self.m.JOBS)  # photos isn't chosen
        self.assertIn("logs-job-drift", self.m.JOBS)
        # Restarts: the chosen components' services, never core's
        self.assertIn("restart-paperless", self.m.JOBS)
        self.assertNotIn("restart-caddy", self.m.JOBS)
        self.assertNotIn("restart-authentik-server", self.m.JOBS)
        self.assertNotIn("restart-jellyfin", self.m.JOBS)
        self.assertIn("logs-app-paperless", self.m.REPORTS)
        self.assertIn("console", self.m.REPORTS)

    def test_only_caddy(self):
        good = {"X-Homelab-Ops": "1", "Origin": "https://home.example.org:9443"}
        self.m.TRUSTED = {"172.21.1.11"}
        try:
            self.assertEqual(self.call("GET", "/ops/data/console.json"), 403)
            self.assertEqual(self.call("POST", "/ops/run/backup", good), 403)
        finally:
            self.m.TRUSTED = {"127.0.0.1"}
        self.assertEqual(self.call("GET", "/ops/data/console.json"), 200)

    def test_refusals(self):
        good = {"X-Homelab-Ops": "1", "Origin": "https://home.example.org:9443"}
        self.assertEqual(self.call("POST", "/ops/run/rm-rf", good), 404)
        self.assertEqual(self.call("POST", "/ops/run/restart-caddy", good), 404)
        self.assertEqual(self.call("POST", "/ops/run/restart-..%2fx", good), 404)
        self.assertEqual(self.call("GET", "/ops/data/logs-app-caddy.json"), 404)
        self.assertEqual(self.call("POST", "/ops/run/backup", {"Origin": good["Origin"]}), 403)
        self.assertEqual(self.call("POST", "/ops/run/backup", {**good, "Origin": "https://evil.example"}), 403)
        self.assertEqual(self.call("POST", "/ops/run/backup", {**good, "Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.call("GET", "/ops/data/secrets.json"), 404)
        self.assertFalse(os.listdir(self.tmp))

    def test_queue(self):
        good = {"X-Homelab-Ops": "1", "Origin": "https://home.example.org:9443"}
        self.assertEqual(self.call("POST", "/ops/run/clean-bins", good), 202)
        self.assertIn("clean-bins", os.listdir(self.tmp))
        os.remove(os.path.join(self.tmp, "clean-bins"))

    def test_actor(self):
        good = {"X-Homelab-Ops": "1", "Origin": "https://home.example.org:9443"}
        self.assertEqual(self.call("POST", "/ops/run/restart-paperless", {**good, "X-Authentik-Username": "anna"}), 202)
        f = os.path.join(self.tmp, "restart-paperless")
        self.assertEqual(Path(f).read_text(), "anna\n")
        os.remove(f)
        # Anything odd in the username becomes "?" (it only goes into a mail subject)
        self.assertEqual(self.call("POST", "/ops/run/restart-paperless", {**good, "X-Authentik-Username": "a b;rm"}), 202)
        self.assertEqual(Path(f).read_text(), "?\n")
        os.remove(f)


if __name__ == "__main__":
    unittest.main()
