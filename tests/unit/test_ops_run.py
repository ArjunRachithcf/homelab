"""ops-run.sh: runs only allowed names (ops/access.py), each with its fixed command; stub tools."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOGGER = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
case "$(basename "$0") $1" in
  "journalctl -u") echo "2026-10-27T09:30:00 backup.sh: line one"; echo "2026-10-27T09:30:01 backup.sh: line two" ;;
  "docker compose") [ "$2" = logs ] && echo "2026-10-27T09:30:00Z paperless says hi" ;;
esac
exit 0
"""


class OpsRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp / "repo"
        for rel in ["ops-run.sh", "ops/access.py", "jobs/jobs.toml", "lib/jobs.py"]:
            (t / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, t / rel)
        shutil.copytree(ROOT / "setup/components", t / "setup/components")
        (t / ".env").write_text("COMPONENTS=core,mesh,paperless,backups\n")
        (t / "notify.sh").write_text('#!/bin/sh\necho "notify $*" >> "$CALLS"; cat >> "$CALLS"\n')
        (t / "notify.sh").chmod(0o755)
        (t / "ops/requests").mkdir()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        for tool in ("docker", "systemctl", "journalctl"):
            (bin_ / tool).write_text(LOGGER)
            (bin_ / tool).chmod(0o755)
        self.repo, self.calls = t, self.tmp / "calls"
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOME=str(self.tmp), CALLS=str(self.calls))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_requests(self, **reqs):
        for name, who in reqs.items():
            (self.repo / "ops/requests" / name.replace("_", "-")).write_text(who)
        r = subprocess.run(["bash", str(self.repo / "ops-run.sh")], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list((self.repo / "ops/requests").iterdir()), [])
        return r.stdout, self.calls.read_text() if self.calls.exists() else ""

    def test_restart_mails_the_actor(self):
        out, calls = self.run_requests(restart_paperless="anna\n")
        self.assertIn("docker compose restart paperless", calls)
        self.assertIn("notify send Homelab: anna restarted paperless", calls)
        self.assertIn("restart-paperless: done", out)

    def test_refused(self):
        # core's services, components not chosen, root jobs, unknown names: nothing runs
        out, calls = self.run_requests(restart_caddy="anna", restart_jellyfin="anna", run_drift="anna", rm_rf="x")
        self.assertEqual(calls, "")
        self.assertEqual(out.count("ignored unknown request"), 4)

    def test_run_job(self):
        out, calls = self.run_requests(run_backup="alex")
        self.assertIn("systemctl start homelab-backup.service", calls)
        self.assertNotIn("notify", calls)

    def test_logs(self):
        self.run_requests(logs_job_backup="alex", logs_app_paperless="alex")
        j = json.loads((self.repo / "ops/data/logs-job-backup.json").read_text())
        self.assertEqual(j["name"], "logs-job-backup")
        self.assertEqual(len(j["lines"]), 2)
        a = json.loads((self.repo / "ops/data/logs-app-paperless.json").read_text())
        self.assertEqual(a["lines"], ["2026-10-27T09:30:00Z paperless says hi"])
        self.assertIn("journalctl -u homelab-backup.service -n 200", self.calls.read_text())

    def test_odd_actor(self):
        _, calls = self.run_requests(restart_paperless="a$(id) b\nsecond line")
        self.assertIn("notify send Homelab: aidb restarted paperless", calls)


if __name__ == "__main__":
    unittest.main()
