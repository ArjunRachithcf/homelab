"""The setup app: its window end to end (Playwright, Tauri mocked; tests/setup-app/ui.test.js) on the
real engine and a protocol session recorded here through lib/setup/run.py and lib/wizard.sh, plus its
Rust side's configuration (the commands the window may call)."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/setup"))
import run  # noqa: E402

# Every kind of step a wizard has, through the real library
WIZARD = f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT}/lib/wizard.sh
ENV_FILE=$(dirname "$0")/.env
TOTAL_STAGES=2
ok() {{ true; }}
stage_first() {{
  stage "First things"
  say "Plain text."
  say "With ${{BOLD}}bold${{RESET}} and ${{YELLOW}}yellow${{RESET}} words."
  printf '  %sAll set.%s\n' "$GREEN" "$RESET"
  step "A step."
  note "A note."
  warn "A warning."
  open_url https://example.org/docs
  echo "a command's own output"
  check "something in place" ok || true
  ask DOMAIN "Your domain:"
  ask_secret SMTP_TOKEN "SMTP token:"
  write_env SMTP_TOKEN "$SMTP_TOKEN"
  confirm "Go on?" || exit 3
}}
stage_second() {{
  stage "Second things"
  ask_lines KEYS "Public keys, one per line:"
  echo "keys: $(printf '%s' "$KEYS" | wc -l)"
  danger "This erases the disk" WIPE || exit 4
  in_terminal "It asks for its own password" proton-drive auth login
  sudo true
}}
stage_first
stage_second
finish
"""
FAKE_SUDO = """#!/usr/bin/env bash
pw=$("$SUDO_ASKPASS" "[sudo] password:") || exit 1
echo "sudo ok"
"""


def record() -> list[str]:
    """A protocol session, as run.py sends it, answered like the UI test will answer it"""
    with tempfile.TemporaryDirectory() as d:
        w = Path(d) / "w.sh"
        w.write_text(WIZARD)
        w.chmod(0o755)
        fake = Path(d) / "bin"
        fake.mkdir()
        (fake / "sudo").write_text(FAKE_SUDO)
        (fake / "sudo").chmod(0o755)
        answers = ["example.org", "hunter2-SECRET", "y", "ssh-ed25519 AAA one", "ssh-ed25519 BBB two", "", "WIPE", "", "sudo-PW-9"]
        orig, path = run.script, os.environ["PATH"]
        run.script = lambda name: w
        os.environ["PATH"] = f"{fake}:{path}"
        try:
            out = io.StringIO()
            pr = run.Protocol(out=out, inp=io.StringIO("".join(json.dumps({"answer": a}) + "\n" for a in answers)))
            code = pr.run("wizard", [])
            pr.send({"t": "done", "failed": [] if code == 0 else ["wizard"]})
            pr.close()
        finally:
            run.script, os.environ["PATH"] = orig, path
    lines = [line for line in out.getvalue().split("\n") if line]
    assert code == 0, lines
    return lines


def node_playwright() -> bool:
    return bool(shutil.which("node")) and subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


class Session(unittest.TestCase):
    def test_recording_has_every_kind(self):
        kinds = {json.loads(line)["t"] for line in record()}
        self.assertLessEqual({"start", "stage", "say", "link", "output", "check", "input", "secret", "wrote", "confirm",
                              "lines", "danger", "terminal", "sudo", "finish", "exit", "done"}, kinds)


class Commands(unittest.TestCase):
    """main.rs, build.rs and the capability name the same commands (an IPC call outside them fails)"""

    def test_same_commands(self):
        main = (ROOT / "setup-app/src-tauri/src/main.rs").read_text()
        handler = re.search(r"generate_handler!\[(.*?)\]", main, re.S).group(1)
        handler = {w.strip() for w in handler.split(",") if w.strip()}
        build = (ROOT / "setup-app/src-tauri/build.rs").read_text()
        built = set(re.findall(r'"([a-z_]+)"', build.split(".commands(&[", 1)[1].split("])", 1)[0]))
        cap = json.loads((ROOT / "setup-app/src-tauri/capabilities/default.json").read_text())
        allowed = {p[len("allow-"):].replace("-", "_") for p in cap["permissions"] if p.startswith("allow-")}
        self.assertEqual(handler, built)
        self.assertEqual(handler, allowed)
        ui = (ROOT / "setup-app/ui/app.js").read_text() + (ROOT / "setup-app/ui/assistant.js").read_text()
        used = set(re.findall(r'invoke\("([a-z_]+)"', ui))
        self.assertIn("assistant_send", used)
        self.assertLessEqual(used, handler)

    def test_bootstrap_is_the_repo_one(self):
        self.assertIn('include_str!("../../../setup/bootstrap.sh")', (ROOT / "setup-app/src-tauri/src/main.rs").read_text())


@unittest.skipUnless(node_playwright(), "needs node with playwright")
class Window(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("")
            sess = Path(d) / "session.json"
            sess.write_text(json.dumps({"lines": record()}))
            r = subprocess.run(["node", str(ROOT / "tests/setup-app/ui.test.js"), str(ROOT), str(env), str(sess),
                                os.environ.get("SETUP_APP_SHOTS", "")], capture_output=True, text=True, timeout=180, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            out = json.loads(r.stdout.strip().splitlines()[-1])
            written = env.read_text()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["presets"], 3)
        self.assertTrue(out["applyDisabledBefore"])
        self.assertIn("DOMAIN", out["missing"])
        # The answers file: the choice and the plain options, never a secret
        self.assertIn('components = ["core", "mesh", "paperless", "photos", "backups"]', out["answersFile"])
        self.assertIn('DOMAIN = "example.org"', out["answersFile"])
        self.assertNotIn("tok-SECRET-1", out["answersFile"])
        # Applied with the real engine; the typed secret went in through set_secret
        self.assertIn("COMPONENTS='core,mesh,paperless,photos,backups'", written)
        self.assertIn("INFOMANIAK_API_TOKEN='tok-SECRET-1'", written)
        self.assertEqual(out["sent"]["secrets"], ["INFOMANIAK_API_TOKEN"])
        # The wizard run: every answer as the protocol wants it
        self.assertEqual(out["sent"]["answers"], ["", "example.org", "hunter2-SECRET", "y", "ssh-ed25519 AAA one",
                                                  "ssh-ed25519 BBB two", "", "WIPE", "", "sudo-PW-9"][1:])
        self.assertEqual(out["sent"]["sudo"], [len("sudo-PW-9")])
        self.assertEqual(out["stages"], ["First things", "Second things"])
        self.assertIn("proton-drive auth login", out["terminal"])
        self.assertIn("keys: 2", out["feedText"])
        self.assertIn("All done.", out["feedText"])
        # The wizards' emphasis: rendered, never shown as marks
        self.assertIn("With bold and yellow words.", out["feedText"])
        self.assertEqual(out["marked"], ["bold", "yellow", "All set."])  # also in a plain output line
        self.assertNotIn("⟦", out["bodyText"])
        # The parts list: the plan's stages by wizard, titled; Start everything, then one step alone
        self.assertEqual(out["startAll"], {"wizards": [], "stages": None, "check": False})
        self.assertTrue(out["partTitles"][0].startswith("The machine"), out["partTitles"])
        self.assertEqual(out["firstStep"], "DNS records at Infomaniak (runbook step 2)")
        self.assertEqual(out["startOne"], {"wizards": ["wizard"], "stages": ["dns"], "check": False})
        self.assertTrue(out["toRunHidden"])  # a machine without .env has nothing to set up unchanged
        self.assertIn("nothing to change", out["applied"])  # configure's own words after Apply
        # Switching off what the machine has: the plan lists it, Apply asks first, "Keep them" writes nothing
        # (COMPONENTS above is still the minimal choice)
        self.assertIn("Switched off", out["planOff"])
        self.assertIn("Photos (Immich)", out["planOff"])
        self.assertIn("Photos (Immich)", out["offConfirm"])
        self.assertIn("containers keep running", out["offConfirm"])
        self.assertTrue(out["offConfirmHidden"])
        # The assistant: the key went to settings (the keychain), the change waited for Allow
        self.assertTrue(out["aiSettingsOpen"])
        self.assertEqual(out["sent"]["settings"]["key"], "sk-ant-TEST-KEY")
        self.assertIsNone(out["aiBeforeAllow"])
        self.assertEqual(out["sent"]["decided"], {"id": "apply-1", "allow": True})
        self.assertEqual(out["sent"]["asked"], "Set up documents and photos")
        for words in ("Worked out a plan", "Shall I save that?", "allowed", "Saved the choice", "Saved."):
            self.assertIn(words, out["aiLog"])
        # Secrets never shown
        for s in ("hunter2-SECRET", "sudo-PW-9", "tok-SECRET-1", "sk-ant-TEST-KEY"):
            self.assertNotIn(s, out["bodyText"])


if __name__ == "__main__":
    unittest.main()
