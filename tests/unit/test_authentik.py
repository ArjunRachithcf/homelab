"""lib/authentik.py (authentik/setup-ssh.sh): which blueprints and Authentik tasks each command runs, with a
stub docker. DOCKER_HOST points nowhere, so even a real docker found by mistake can't reach a live stack."""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Records each call as JSON (argv, and the task of an ak shell call); answers like a happy worker
DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
rec = {"argv": ["<code>" if i and a[i - 1] == "-c" else x for i, x in enumerate(a)]}
if "AK_TASK" in a:
    rec["task"] = json.loads(os.environ["AK_TASK"])
# the variables passed with -e NAME (their values come from docker's own environment)
rec["env"] = {}
for i, x in enumerate(a):
    if x == "-e" and a[i + 1] != "AK_TASK":
        rec["env"][a[i + 1]] = os.environ.get(a[i + 1], "")
with open(os.environ["CALLS"], "a") as f:
    f.write(json.dumps(rec) + "\n")
task = rec.get("task", {}).get("task")
if task == "cert_state":
    print("OUT:ok")
elif task == "export_ca":
    for line in open(os.environ["CERT_PEM"]).read().splitlines():
        print("OUT:" + line)
elif task == "outpost_token":
    print("OUT:tok123")
elif task:
    print("ak shell noise"); print("OUT:✓ " + task)
"""


class Authentik(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        r = self.repo = self.tmp / "repo"
        for rel in ["lib/authentik.py", "lib/authentik_tasks.py", "lib/render.py", "lib/setup/engine.py"]:
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, r / rel)
        shutil.copytree(ROOT / "setup", r / "setup")
        shutil.copytree(ROOT / "authentik", r / "authentik", ignore=shutil.ignore_patterns("templates*", "*.png"))
        (r / "ssh").mkdir()
        self.env_file = r / ".env"
        self.env_file.write_text((ROOT / "tests/fixtures/full.env").read_text())
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "docker").write_text(DOCKER)
        (bin_ / "docker").chmod(0o755)
        self.calls = self.tmp / "calls"
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CALLS=str(self.calls),
                        DOCKER_HOST="unix:///nonexistent/docker.sock")
        self.env.pop("SSH_USER", None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def set_env(self, **kv):
        lines = [l for l in self.env_file.read_text().splitlines() if l.split("=", 1)[0] not in kv]
        self.env_file.write_text("\n".join(lines + [f"{k}={v}" for k, v in kv.items() if v is not None]) + "\n")

    def run_cmd(self, *args, stdin="", rc=0):
        r = subprocess.run([sys.executable, str(self.repo / "lib/authentik.py"), *args], input=stdin, text=True,
                           capture_output=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        calls = [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return r.stdout + r.stderr, calls

    @staticmethod
    def applied(calls):
        return [c["argv"][-1].removeprefix("homelab-").removesuffix(".yaml") for c in calls
                if c["argv"][-2:-1] == ["apply_blueprint"]]

    @staticmethod
    def tasks(calls):
        return [c["task"] for c in calls if "task" in c]

    def test_hardening_applies_only_the_chosen_components(self):
        self.set_env(COMPONENTS="core,mesh,wiki,music,budget,ntfy")
        out, calls = self.run_cmd("hardening")
        self.assertEqual(self.applied(calls), ["hardening", "family", "family-app", "budget", "music", "wiki"])
        t = self.tasks(calls)
        self.assertIn({"task": "on_outpost", "args": {"providers": ["Provider for Music"]}}, t)
        self.assertIn({"task": "join_groups", "args": {"name": "alex", "groups": ["budget"]}}, t)
        self.assertIn({"task": "on_outpost", "args": {"providers": ["Provider for Home"]}}, t)
        # Every copied blueprint is removed from the worker again
        cps = [c["argv"][-1].split(":/blueprints/")[1] for c in calls if c["argv"][1] == "cp"]
        rms = [c["argv"][-1].removeprefix("/blueprints/") for c in calls if "rm" in c["argv"]]
        self.assertEqual(cps, rms)

    def test_secrets_go_by_environment_never_argv(self):
        self.set_env(COMPONENTS="core,mesh,wiki", OUTLINE_OIDC_CLIENT_SECRET="s3cret-value")
        out, calls = self.run_cmd("component", "wiki")
        (apply,) = [c for c in calls if "apply_blueprint" in c["argv"]]
        self.assertEqual(apply["env"], {"OUTLINE_OIDC_CLIENT_SECRET": "s3cret-value"})
        self.assertNotIn("s3cret-value", json.dumps([c["argv"] for c in calls]))
        self.assertNotIn("s3cret-value", out)

    def test_missing_secret(self):
        self.set_env(COMPONENTS="core,mesh,wiki", OUTLINE_OIDC_CLIENT_SECRET=None)
        out, calls = self.run_cmd("component", "wiki", rc=1)
        self.assertIn("✗ OUTLINE_OIDC_CLIENT_SECRET missing in .env", out)
        self.assertEqual(self.applied(calls), [])
        # hardening skips it and goes on
        out, calls = self.run_cmd("hardening")
        self.assertIn("· wiki.yaml skipped (no OUTLINE_OIDC_CLIENT_SECRET yet)", out)
        self.assertNotIn("wiki", self.applied(calls))

    def test_component_not_chosen_does_nothing(self):
        self.set_env(COMPONENTS="core,mesh")
        out, calls = self.run_cmd("family-apps")
        self.assertEqual(calls, [])
        self.run_cmd("component", "nosuch", rc=1)

    def test_no_components_means_all(self):
        self.set_env(COMPONENTS=None)
        out, calls = self.run_cmd("files")
        self.assertEqual(self.applied(calls), ["reading", "files", "share"])

    def test_task_output_is_only_out_lines(self):
        out, calls = self.run_cmd("status")
        self.assertEqual(out, "✓ status\n")
        self.assertEqual(self.tasks(calls), [{"task": "status", "args": {"name": "alex"}}])

    def test_add_key_cleans_paste(self):
        if not shutil.which("ssh-keygen"):
            self.skipTest("no ssh-keygen")
        k = self.tmp / "k"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(k)], check=True)
        pub = (k.with_suffix(".pub")).read_text().strip()
        out, calls = self.run_cmd("add-key", stdin=f"junk\n\x1b[A{pub}  \r\n\n")
        (task,) = self.tasks(calls)
        self.assertEqual(task, {"task": "add_keys", "args": {"name": "alex", "keys": [pub]}})
        self.assertIn(["compose", "restart", "authentik-ldap-ssh"], [c["argv"] for c in calls])
        self.run_cmd("add-key", stdin="ssh-ed25519 notakey\n", rc=1)
        self.run_cmd("add-key", stdin="nothing here\n", rc=1)

    def test_everything_generates_the_bind_password_and_saves_the_token(self):
        if not shutil.which("openssl"):
            self.skipTest("no openssl")
        # export_ca checks what the worker hands back is a certificate: a real one
        cert = self.tmp / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-days", "1", "-subj", "/CN=test", "-keyout", str(self.tmp / "key.pem"), "-out", str(cert)],
                       check=True, capture_output=True)
        self.env["CERT_PEM"] = str(cert)
        self.set_env(SSH_LDAP_BIND_PASSWORD=None, AK_LDAP_SSH_OUTPOST_TOKEN=None)
        out, calls = self.run_cmd("all")
        self.assertEqual((self.repo / "ssh/ldap-ssh.pem").read_text(), cert.read_text())
        self.assertIn("ssh-ldap", self.applied(calls))
        env = self.env_file.read_text()
        self.assertRegex(env, r"(?m)^SSH_LDAP_BIND_PASSWORD='[A-Za-z0-9]{40,}'$")
        self.assertIn("AK_LDAP_SSH_OUTPOST_TOKEN='tok123'", env)
        self.assertEqual(oct(self.env_file.stat().st_mode & 0o777), "0o600")
        self.assertIn(["compose", "up", "-d", "authentik-ldap-ssh"], [c["argv"] for c in calls])
        self.assertNotIn("tok123", out)

    def test_usage(self):
        out, _ = self.run_cmd("nosuch", rc=2)
        self.assertIn("setup-ssh.sh hardening", out)
        self.run_cmd("kid", rc=2)

    def test_every_task_called_exists(self):
        tasks_src = (ROOT / "lib/authentik_tasks.py").read_text()
        defined = {n.name for n in ast.parse(tasks_src).body if isinstance(n, ast.FunctionDef)}
        host = ast.parse((ROOT / "lib/authentik.py").read_text())
        called = {n.args[0].value for n in ast.walk(host) if isinstance(n, ast.Call)
                  and getattr(n.func, "id", "") in ("ak", "say") and isinstance(n.args[0], ast.Constant)}
        self.assertTrue(called)
        self.assertLessEqual(called, defined)


if __name__ == "__main__":
    unittest.main()
