"""lib/wizard-checks.sh: backup_section_ok reads only the newest homelab-backup run, one section of it."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = {
    # An older run, from before ntfy's volume existed: still in the journal's last lines
    "old": "Apps (RUNBOOK.md 7e)…\n  ntfy-data: not created yet, skipped\nEncrypting → a\n",
    "new": "Apps (RUNBOOK.md 7d)…\n  audiobookshelf: ok\nApps (RUNBOOK.md 7e)…\n  sharry: ok\n"
           "Apps (RUNBOOK.md 7f)…\n  romm: not running, skipped\nEncrypting → b\n",
}
# systemctl show -p InvocationID --value homelab-backup.service → the newest run's id
SYSTEMCTL = '#!/bin/sh\nprintf "%s\\n" "$NEWEST"\n'
# journalctl _SYSTEMD_INVOCATION_ID=<id> … → that run's lines; anything else → every run
JOURNALCTL = r'''#!/bin/sh
for a; do case $a in _SYSTEMD_INVOCATION_ID=*) cat "$RUNS/${a#*=}"; exit 0 ;; esac; done
cat "$RUNS"/old "$RUNS"/new
'''


class BackupSection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "runs").mkdir()
        for k, v in RUNS.items():
            (self.tmp / "runs" / k).write_text(v)
        for name, body in (("systemctl", SYSTEMCTL), ("journalctl", JOURNALCTL)):
            (self.tmp / name).write_text(body)
            (self.tmp / name).chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def ok(self, title, bad, newest="new"):
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", RUNS=str(self.tmp / "runs"), NEWEST=newest)
        return subprocess.run(["bash", "-c", 'set -uo pipefail; source lib/wizard-checks.sh; backup_section_ok "$@"',
                               "_", title, bad], cwd=ROOT, env=env, timeout=30).returncode == 0

    def test_older_runs_do_not_count(self):
        self.assertTrue(self.ok("Apps (RUNBOOK.md 7e)", "not created yet|skipped"))
        self.assertFalse(self.ok("Apps (RUNBOOK.md 7e)", "not created yet|skipped", newest="old"))

    def test_only_its_own_section(self):
        self.assertFalse(self.ok("Apps (RUNBOOK.md 7f)", "not running|skipped"))
        self.assertTrue(self.ok("Apps (RUNBOOK.md 7d)", "not running|skipped"))
        self.assertTrue(self.ok("Apps (RUNBOOK.md 7e)", "not running|skipped"))

    def test_missing_section_or_run(self):
        self.assertFalse(self.ok("Apps (RUNBOOK.md 9z)", "x"))
        self.assertFalse(self.ok("Apps (RUNBOOK.md 7d)", "x", newest=""))


if __name__ == "__main__":
    unittest.main()
