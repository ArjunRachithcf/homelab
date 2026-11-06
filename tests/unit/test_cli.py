"""./homelab: list, status (with a stub systemctl), refusals."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUB = """#!/bin/sh
# systemctl show UNIT -p …: backup failed, everything else fine
case "$2" in
  homelab-backup.service) printf 'ActiveState=failed\\nResult=exit-code\\nExecMainExitTimestamp=Tue 2026-10-27 09:30:05 UTC\\n' ;;
  *.timer|*.path) printf 'ActiveState=active\\nNextElapseUSecRealtime=Tue 2026-10-27 10:30:00 UTC\\n' ;;
  *) printf 'ActiveState=inactive\\nResult=success\\nExecMainExitTimestamp=Tue 2026-10-27 09:00:00 UTC\\n' ;;
esac
"""
# Every job fine, but OpenCloud's snapshot timer isn't installed: its component (files) is off
NOT_CHOSEN = """#!/bin/sh
case "$2" in
  homelab-files-local.timer) printf 'ActiveState=inactive\\n' ;;
  *.timer|*.path) printf 'ActiveState=active\\nNextElapseUSecRealtime=Tue 2026-10-27 10:30:00 UTC\\n' ;;
  *) printf 'ActiveState=inactive\\nResult=success\\nExecMainExitTimestamp=Tue 2026-10-27 09:00:00 UTC\\n' ;;
esac
"""


class Cli(unittest.TestCase):
    def run_cli(self, *args, path_prefix=None):
        env = dict(os.environ, NO_COLOR="1")
        if path_prefix:
            env["PATH"] = f"{path_prefix}:{env['PATH']}"
        return subprocess.run([str(ROOT / "homelab"), *args], capture_output=True, text=True, env=env, cwd=ROOT)

    def test_list(self):
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("backup", r.stdout)

    def test_status_reports_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "systemctl"
            p.write_text(STUB)
            p.chmod(0o755)
            r = self.run_cli("status", path_prefix=d)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"backup\s+failed \(exit-code\)")
        self.assertRegex(r.stdout, r"photos-local\s+ok")

    def test_status_lists_the_chosen_components_jobs(self):
        # As the console: a job whose component is off has no units, and that is no failure
        comps = ",".join(sorted(p.stem for p in (ROOT / "setup/components").glob("*.toml") if p.stem != "files"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "systemctl"
            p.write_text(NOT_CHOSEN)
            p.chmod(0o755)
            env = Path(d) / "env"
            env.write_text(f"COMPONENTS='{comps}'\n")
            run = dict(os.environ, NO_COLOR="1", PATH=f"{d}:{os.environ['PATH']}")
            chosen = subprocess.run([sys.executable, str(ROOT / "lib/jobs.py"), "status", "--env", str(env)],
                                    capture_output=True, text=True, env=run, cwd=ROOT)
            every = subprocess.run([sys.executable, str(ROOT / "lib/jobs.py"), "status"],
                                   capture_output=True, text=True, env=run, cwd=ROOT)
        self.assertEqual(chosen.returncode, 0, chosen.stdout + chosen.stderr)
        self.assertNotIn("files-local", chosen.stdout)
        self.assertRegex(chosen.stdout, r"backup\s+ok")
        self.assertEqual(every.returncode, 1, every.stdout + every.stderr)
        self.assertRegex(every.stdout, r"files-local\s+ok, timer inactive")

    def test_status_reads_the_env(self):
        for f in ("lib/platform/systemd.sh", "lib/platform/launchd.sh"):
            self.assertIn("jobs.py status --env .env", (ROOT / f).read_text(), f)

    def test_unknown(self):
        self.assertEqual(self.run_cli("nope").returncode, 2)
        self.assertNotEqual(self.run_cli("run", "no-such-job").returncode, 0)


if __name__ == "__main__":
    unittest.main()
