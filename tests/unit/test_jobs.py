"""jobs/jobs.toml agrees with the hand-written units, the polkit rule, live-safe.sh and ops."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import jobs  # noqa: E402

TEMPLATES = ROOT / "ansible/roles/timers/templates"
REQUIRED = {"title", "description", "units", "script", "run_as", "calendar", "when", "polkit_start", "lock",
            "notify_on_failure", "guide", "macos"}


class Registry(unittest.TestCase):
    reg = jobs.load()

    def test_fields(self):
        for jid, j in self.reg["jobs"].items():
            self.assertEqual(set(j), REQUIRED, jid)
            self.assertIn(j["run_as"], ("user", "root"), jid)
            self.assertTrue(j["units"][0].endswith(".service"), jid)

    def test_every_template_is_a_unit_and_back(self):
        units = [u for j in self.reg["jobs"].values() for u in j["units"]]
        self.assertEqual(len(units), len(set(units)))
        on_disk = {p.name[:-3] for p in TEMPLATES.glob("homelab-*.j2")}
        self.assertEqual(set(units), on_disk)

    def test_units_match(self):
        for jid, j in self.reg["jobs"].items():
            svc = (TEMPLATES / (j["units"][0] + ".j2")).read_text()
            exec_start = re.search(r"^ExecStart=(.*)$", svc, re.M).group(1)
            self.assertTrue(exec_start.endswith("{{ homelab_dir }}/" + j["script"]), (jid, exec_start))
            self.assertEqual(bool(re.search(r"^OnFailure=homelab-notify@%n.service$", svc, re.M)), j["notify_on_failure"], jid)
            self.assertEqual(bool(re.search(r"^User={{ homelab_user }}$", svc, re.M)), j["run_as"] == "user", jid)
            timer = next((u for u in j["units"] if u.endswith(".timer")), None)
            if timer:
                cal = re.search(r"^OnCalendar=(.*)$", (TEMPLATES / (timer + ".j2")).read_text(), re.M).group(1)
                self.assertEqual(cal, j["calendar"], jid)
            else:
                self.assertEqual(j["calendar"], "", jid)

    def test_polkit_rule_uses_registry(self):
        self.assertIn("timers_polkit_units", (TEMPLATES / "50-homelab-backups.rules.j2").read_text())
        defaults = (ROOT / "ansible/roles/timers/defaults/main.yml").read_text()
        self.assertIn("jobs.py units", defaults)
        self.assertIn("jobs.py polkit", defaults)

    def test_locks_are_taken(self):
        for jid, j in self.reg["jobs"].items():
            if j["lock"]:
                script = ROOT / j["script"].split()[0]
                self.assertIn(f'homelab/{j["lock"]}.lock', script.read_text(), jid)
        self.assertIn("jobs.py\" locks", (ROOT / "ansible/live-safe.sh").read_text())

    def test_ops_actions(self):
        # Every name ops/access.py allows has its branch in ops-run.sh's run_job
        run = (ROOT / "ops-run.sh").read_text()
        for a in self.reg["actions"]:
            self.assertRegex(run, rf"(?m)^\s+{re.escape(a)}\)", a)
        for kind in ("run", "restart", "logs-job", "logs-app"):
            self.assertRegex(run, rf"(?m)^\s+{kind}-\*\)", kind)
        self.assertIn("is_allowed \"$name\"", run)
        cleanups = [a for a, v in self.reg["actions"].items() if v["cleanup"]]
        report = (ROOT / "storage-report.py").read_text()
        for a in cleanups:
            self.assertIn(f'"job": "{a}"', report)

    def test_guides_named(self):
        for jid, j in self.reg["jobs"].items():
            self.assertRegex(j["guide"], r"^[a-z-]*$", jid)


if __name__ == "__main__":
    unittest.main()
