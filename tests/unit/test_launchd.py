"""macOS (plan Phase 5): launchd jobs from the registry, the run wrapper, the engine's macOS plan."""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "lib/setup"))
import engine  # noqa: E402
import jobs  # noqa: E402


class Calendar(unittest.TestCase):
    def test_forms(self):
        c = jobs.launchd_calendar
        self.assertEqual(c("hourly"), {"StartCalendarInterval": {"Minute": 0}})
        self.assertEqual(c("*-*-* *:30:00"), {"StartCalendarInterval": {"Minute": 30}})
        self.assertEqual(c("*-*-* 04:00:00"), {"StartCalendarInterval": {"Hour": 4, "Minute": 0}})
        self.assertEqual(c("Sun *-*-* 02:30:00"), {"StartCalendarInterval": {"Weekday": 0, "Hour": 2, "Minute": 30}})
        self.assertEqual(c("*-*-* 00/4:50:00")["StartCalendarInterval"][1], {"Hour": 4, "Minute": 50})
        self.assertEqual(len(c("*-*-* 00/4:50:00")["StartCalendarInterval"]), 6)
        self.assertEqual(c("*:0/5"), {"StartInterval": 300})
        with self.assertRaises(ValueError):
            c("Mon..Fri 10:00")

    def test_every_registry_calendar(self):
        for jid, j in jobs.load()["jobs"].items():
            if j["calendar"]:
                jobs.launchd_calendar(j["calendar"])  # no error


class Plists(unittest.TestCase):
    def setUp(self):
        reg = jobs.load()
        self.out = jobs.plists(reg, reg["jobs"], "/Users/sam/homelab", "/Users/sam")

    def test_which(self):
        names = {n[len("org.homelab."):-len(".plist")] for n in self.out}
        self.assertNotIn("drift", names)          # Linux only
        self.assertNotIn("photos-local", names)
        self.assertNotIn("notify", names)         # runs inside launchd-run.sh
        self.assertNotIn("photos-copy", names)    # mountpoint, /srv paths
        self.assertNotIn("heartbeat", names)      # ip addr show wt0 (NetBird is utun on macOS)
        self.assertLessEqual({"backup", "ops", "storage-report"}, names)

    def test_content(self):
        d = plistlib.loads(self.out["org.homelab.backup.plist"])
        self.assertEqual(d["ProgramArguments"], ["/bin/bash", "/Users/sam/homelab/lib/platform/launchd-run.sh", "backup"])
        self.assertEqual(d["StartCalendarInterval"], {"Minute": 30})
        self.assertTrue(d["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/opt/coreutils/libexec/gnubin:"))
        ops = plistlib.loads(self.out["org.homelab.ops.plist"])
        self.assertEqual(ops["QueueDirectories"], ["/Users/sam/homelab/ops/requests"])

    @unittest.skipUnless(shutil.which("plutil"), "macOS only (plutil)")
    def test_plutil_lint(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data in self.out.items():
                (Path(d) / name).write_bytes(data)
                r = subprocess.run(["plutil", "-lint", str(Path(d) / name)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Wrapper(unittest.TestCase):
    """lib/platform/launchd-run.sh in a copy of the repo with a stand-in job"""

    def run_job(self, exit_code):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            for rel in ("lib/jobs.py", "lib/platform/launchd-run.sh"):
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / rel, repo / rel)
            (repo / "jobs").mkdir()
            (repo / "jobs/jobs.toml").write_text(
                '[jobs.backup]\ntitle = "B"\ndescription = ""\nunits = ["homelab-backup.service"]\nscript = "fake.sh one two"\n'
                'run_as = "user"\ncalendar = ""\nwhen = ""\npolkit_start = true\nlock = ""\nnotify_on_failure = true\nguide = ""\nmacos = true\n')
            (repo / "fake.sh").write_text(f'#!/bin/sh\necho "fake ran: $*"\nexit {exit_code}\n')
            (repo / "fake.sh").chmod(0o755)
            (repo / "notify.sh").write_text('#!/bin/sh\necho "$@" > "$HOME/notified"\n')
            (repo / "notify.sh").chmod(0o755)
            r = subprocess.run(["bash", str(repo / "lib/platform/launchd-run.sh"), "backup"], capture_output=True, text=True,
                               env=dict(os.environ, HOME=d))
            log = (Path(d) / "Library/Logs/homelab/org.homelab.backup.log").read_text()
            notified = (Path(d) / "notified").read_text() if (Path(d) / "notified").exists() else ""
            return r.returncode, log, notified

    def test_ok(self):
        code, log, notified = self.run_job(0)
        self.assertEqual(code, 0)
        self.assertIn("fake ran: one two", log)
        self.assertRegex(log, r"── \S+ \S+ backup: exit 0")
        self.assertEqual(notified, "")

    def test_failure_mails(self):
        code, log, notified = self.run_job(3)
        self.assertEqual(code, 3)
        self.assertIn("backup: exit 3", log)
        self.assertEqual(notified.strip(), "failed homelab-backup.service")


class EngineOnMac(unittest.TestCase):
    comps = engine.components()

    def test_plan(self):
        chosen = engine.preset("minimal", self.comps) + ["local-ai", "music"]
        p = engine.make_plan(self.comps, {}, chosen, "none", None, "macos")
        self.assertEqual(p.resolution.errors, [])               # no NVIDIA card needed: Metal
        self.assertEqual(p.write["OLLAMA_URL"], "http://host.docker.internal:11434")
        self.assertEqual(p.write["MEDIA_DIR"], os.path.expanduser("~/homelab-data/media"))
        self.assertNotIn("local-ai", p.profiles)                # no ollama container
        self.assertIn("paperless", p.profiles)
        # The same choice on Linux: the container, /srv
        q = engine.make_plan(self.comps, {}, chosen, "nvidia", None, "fedora")
        self.assertNotIn("OLLAMA_URL", q.write)
        self.assertEqual(q.write["MEDIA_DIR"], "/srv/media")
        self.assertIn("local-ai", q.profiles)

    def test_not_on_mac(self):
        errs = engine.resolve(self.comps, ["plex", "home-assistant", "ssh-sso"], "none", "macos").errors
        self.assertEqual(len(errs), 3)


if __name__ == "__main__":
    unittest.main()
