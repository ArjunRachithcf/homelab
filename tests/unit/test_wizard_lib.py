"""lib/wizard.sh: the terminal output is the old library's, byte for byte; protocol mode speaks JSON
lines and never repeats a secret."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / "tests/unit/fixtures/wizard-lib-before.sh"
NEW = ROOT / "lib/wizard.sh"

# A small wizard using every helper both libraries have
BODY = r'''
TOTAL_STAGES=2
stage_one() {
  stage "First"
  say "plain"; step "a step"; note "a note"; warn "careful"
  pause "Press Enter…"
  ask NAME "Your name:"
  ask_secret TOKEN "A token (hidden):"
  write_env NAME "$NAME"
  write_env TOKEN "$TOKEN"
  confirm "Proceed?" && say "yes" || say "no"
  check "true works" true || true
  check "false fails" false || true
  retry_check "still false" false || true
}
stage_two() {
  stage "Second"
  skip_offer && say "skipped" || say "not skipped"
}
stage_one; stage_two
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
'''
ANSWERS = "\nJane\nsupersecret-123\ny\nn\nn\n"


def run(lib: Path, ui: str, answers: str):
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "w.sh"
        script.write_text(f"set -euo pipefail\nENV_FILE={d}/.env\nsource {lib}\n" + BODY)
        env = dict(os.environ, HOMELAB_UI=ui, TERM="dumb")
        r = subprocess.run(["bash", str(script)], input=answers, capture_output=True, text=True, env=env, cwd=d)
        env_text = (Path(d) / ".env").read_text() if (Path(d) / ".env").exists() else ""
        r.stdout = r.stdout.replace(d, "<d>")
        return r, env_text.replace(d, "<d>")


class WizardLib(unittest.TestCase):
    def test_tty_output_unchanged(self):
        old, old_env = run(OLD, "tty", ANSWERS)
        new, new_env = run(NEW, "tty", ANSWERS)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout.replace(str(NEW.parent), ""), old.stdout.replace(str(OLD.parent), ""))
        self.assertEqual(new_env, old_env)

    def test_protocol(self):
        r, env = run(NEW, "protocol", ANSWERS)
        self.assertEqual(r.returncode, 0, r.stderr)
        # split("\n"), not splitlines(): that one also breaks at the 0x1E marker itself
        lines = r.stdout.rstrip("\n").split("\n")
        msgs = [json.loads(l[1:]) for l in lines if l.startswith("\x1e")]
        others = [l for l in lines if not l.startswith("\x1e")]
        self.assertEqual(others, [])
        kinds = [m["t"] for m in msgs]
        self.assertEqual(kinds[:7], ["stage", "say", "say", "say", "say", "pause", "input"])
        self.assertEqual(msgs[0], {"t": "stage", "id": "one", "n": 1, "of": 2, "title": "First"})
        secret = next(m for m in msgs if m["t"] == "secret")
        self.assertEqual(secret["key"], "TOKEN")
        self.assertFalse(secret["current_set"])
        self.assertNotIn("supersecret-123", r.stdout)
        self.assertIn("TOKEN='supersecret-123'", env)
        checks = [(m["label"], m["ok"]) for m in msgs if m["t"] == "check"]
        self.assertEqual(checks, [("true works", True), ("false fails", False), ("still false", False)])
        self.assertEqual(msgs[-1]["t"], "finish")
        self.assertEqual(msgs[-1]["wrote"], ["NAME", "TOKEN"])

    def test_danger_needs_the_word(self):
        with tempfile.TemporaryDirectory() as d:
            for answer, rc in (("wipe\n", 0), ("yes\n", 1)):
                s = Path(d) / "w.sh"
                s.write_text(f"source {NEW}\ndanger 'Erases the disk' wipe")
                r = subprocess.run(["bash", str(s)], input=answer, capture_output=True, text=True,
                                   env=dict(os.environ, HOMELAB_UI="protocol"))
                self.assertEqual(r.returncode, rc)
                self.assertIn('"t":"danger"', r.stdout)


if __name__ == "__main__":
    unittest.main()
