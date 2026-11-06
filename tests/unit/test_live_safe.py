"""ansible/live-safe.sh: busy while a homelab job runs or in the nightly window, idle otherwise; stub systemctl/date/pgrep."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Real systemctl's --state matches the LOAD, ACTIVE or SUB column; $UNITS holds "UNIT LOAD ACTIVE SUB DESCRIPTION" rows
SYSTEMCTL = """#!/bin/sh
[ "$1" = list-units ] || exit 0
for a in "$@"; do case $a in --state=*) states=${a#--state=} ;; esac; done
awk -v s=",$states," 'index(s, "," $2 ",") || index(s, "," $3 ",") || index(s, "," $4 ",")' "$UNITS"
"""
DATE = """#!/bin/sh
[ "$1" = +%H%M ] && { echo "$FAKE_HM"; exit 0; }
exec "$REAL_DATE" "$@"
"""
GUARD = "homelab-mesh-guard.service loaded active exited Homelab: the NetBird address answers only NetBird"


class LiveSafe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        for tool, body in (("systemctl", SYSTEMCTL), ("date", DATE), ("pgrep", "#!/bin/sh\nexit 1\n")):
            (bin_ / tool).write_text(body)
            (bin_ / tool).chmod(0o755)
        self.units = self.tmp / "units"
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", UNITS=str(self.units),
                        REAL_DATE=shutil.which("date"), FAKE_HM="1200")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def live_safe(self, *units, hm="1200"):
        self.units.write_text("".join(u + "\n" for u in units))
        self.env["FAKE_HM"] = hm
        r = subprocess.run(["bash", str(ROOT / "ansible/live-safe.sh"), str(self.tmp)],
                           capture_output=True, text=True, env=self.env, timeout=60)
        return r.returncode, r.stdout.strip()

    def test_idle_while_a_remain_after_exit_unit_is_active(self):
        self.assertEqual(self.live_safe(GUARD), (0, "idle"))

    def test_busy_while_a_job_runs_or_stops(self):
        rc, out = self.live_safe(GUARD, "homelab-backup.service loaded activating start Homelab backup")
        self.assertEqual((rc, out), (1, "busy: homelab-backup.service running"))
        rc, out = self.live_safe("homelab-offbox.service loaded deactivating stop-sigterm Homelab off-box copy")
        self.assertEqual((rc, out), (1, "busy: homelab-offbox.service running"))

    def test_drift_and_heartbeat_dont_count(self):
        # homelab-drift runs this script as its ExecCondition, so it is activating itself
        self.assertEqual(self.live_safe("homelab-drift.service loaded activating start Homelab drift check",
                                        "homelab-heartbeat.service loaded activating start Homelab heartbeat"), (0, "idle"))

    def test_nightly_window(self):
        self.assertEqual(self.live_safe(hm="0300"), (1, "busy: nightly window 02:00–05:30"))
        self.assertEqual(self.live_safe(hm="0530"), (0, "idle"))


if __name__ == "__main__":
    unittest.main()
