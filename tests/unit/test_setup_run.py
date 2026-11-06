"""lib/setup/run.py: a wizard over the protocol, answers in, messages out, the secret only inward."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/setup"))
import run  # noqa: E402

WIZARD = f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT}/lib/wizard.sh
ENV_FILE=$(dirname "$0")/.env
TOTAL_STAGES=1
stage_only() {{
  stage "Only"
  echo "a command's own output"
  ask DOMAIN "Domain:"
  ask_secret SMTP_TOKEN "SMTP token:"
  write_env SMTP_TOKEN "$SMTP_TOKEN"
  confirm "Go on?" || exit 3
  ask_lines KEYS "Keys:"
  echo "keys: $(printf '%s' "$KEYS" | wc -l)"
}}
stage_only
finish
"""


class SetupRun(unittest.TestCase):
    def go(self, answers):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d) / "fake-wizard.sh"
            w.write_text(WIZARD)
            w.chmod(0o755)
            orig = run.script
            run.script = lambda name: w
            try:
                out = io.StringIO()
                inp = io.StringIO("".join(json.dumps({"answer": a}) + "\n" for a in answers))
                pr = run.Protocol(out=out, inp=inp)
                code = pr.run("fake", [])
                pr.close()
            finally:
                run.script = orig
            env = (Path(d) / ".env").read_text() if (Path(d) / ".env").exists() else ""
        return code, [json.loads(l) for l in out.getvalue().splitlines()], out.getvalue(), env

    def test_flow(self):
        code, msgs, raw, env = self.go(["example.org", "tok-SECRET-42", "y", "ssh-ed25519 A", "ssh-ed25519 B", ""])
        self.assertEqual(code, 0)
        kinds = [m["t"] for m in msgs]
        self.assertEqual(kinds, ["start", "stage", "output", "input", "secret", "wrote", "confirm", "lines", "output", "finish", "exit"])
        self.assertEqual(msgs[8]["text"], "keys: 2")
        self.assertTrue(all(m["wizard"] == "fake" for m in msgs))
        self.assertEqual(msgs[2]["text"], "a command's own output")
        self.assertNotIn("tok-SECRET-42", raw)
        self.assertIn("SMTP_TOKEN='tok-SECRET-42'", env)

    def test_no_and_eof(self):
        code, msgs, _, _ = self.go(["example.org", "x", "n"])
        self.assertEqual((code, msgs[-1]), (3, {"t": "exit", "wizard": "fake", "code": 3}))
        code, msgs, _, _ = self.go(["example.org"])  # the front end goes away at the secret
        self.assertNotEqual(code, 0)
        self.assertEqual(msgs[-1]["t"], "exit")


if __name__ == "__main__":
    unittest.main()


SUDO_WIZARD = """#!/usr/bin/env bash
set -euo pipefail
sudo whoami
ansible-playbook -K site.yml --tags x
"""
# The real sudo and ansible-playbook, as far as the shims know: they ask the askpass like the real ones
FAKE_SUDO = """#!/usr/bin/env bash
[ "$1" = -A ] || { echo "no -A: $*"; exit 1; }
pw=$("$SUDO_ASKPASS" "[sudo] password for you:") || exit 1
echo "sudo $* len=${#pw}"
"""
FAKE_ANSIBLE = """#!/usr/bin/env bash
echo "ansible $*"
for i in "$@"; do [ "$prev" = --become-password-file ] && pw=$("$i") && echo "become len=${#pw}"; prev=$i; done
"""


class Sudo(unittest.TestCase):
    def test_askpass_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "bin"
            fake.mkdir()
            for name, body in (("sudo", FAKE_SUDO), ("ansible-playbook", FAKE_ANSIBLE)):
                (fake / name).write_text(body)
                (fake / name).chmod(0o755)
            w = Path(d) / "w.sh"
            w.write_text(SUDO_WIZARD)
            w.chmod(0o755)
            orig, path = run.script, os.environ["PATH"]
            run.script = lambda name: w
            os.environ["PATH"] = f"{fake}:{path}"
            try:
                out = io.StringIO()
                inp = io.StringIO(json.dumps({"answer": "hunter2"}) + "\n" + json.dumps({"answer": "s3cret"}) + "\n")
                pr = run.Protocol(out=out, inp=inp)
                code = pr.run("fake", [])
                pr.close()
            finally:
                run.script, os.environ["PATH"] = orig, path
        raw = out.getvalue()
        msgs = [json.loads(line) for line in raw.split("\n") if line]
        self.assertEqual(code, 0, raw)
        self.assertEqual([m["t"] for m in msgs if m["t"] == "sudo"], ["sudo", "sudo"])
        self.assertEqual(msgs[1]["prompt"], "[sudo] password for you:")
        texts = [m.get("text") for m in msgs if m["t"] == "output"]
        self.assertIn("sudo -A whoami len=7", texts)
        self.assertTrue(any(t.startswith("ansible --become-password-file ") and t.endswith("site.yml --tags x") for t in texts), texts)
        self.assertIn("become len=6", texts)
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("s3cret", raw)


class Arguments(unittest.TestCase):
    def setUp(self):
        self.orig = run.tty
        run.tty = lambda *a: self.fail("a wizard was started")

    def tearDown(self):
        run.tty = self.orig

    def test_help_starts_nothing(self):
        out = io.StringIO()
        sys.stdout, orig = out, sys.stdout
        try:
            self.assertEqual(run.main(["--help"]), 0)
        finally:
            sys.stdout = orig
        self.assertIn("homelab setup WIZARD [--check]", out.getvalue())
        self.assertNotIn("Protocol", out.getvalue())

    def test_unknown_option_starts_nothing(self):
        err = io.StringIO()
        sys.stderr, orig = err, sys.stderr
        try:
            self.assertEqual(run.main(["files", "--chek"]), 2)
        finally:
            sys.stderr = orig
        self.assertIn("unknown option --chek", err.getvalue())


class ChosenStages(unittest.TestCase):
    def test_retired_and_unknown_ids(self):
        # A .env that still names a retired id (made up: none is retired today) and an id no component has
        import engine
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(engine.RETIRED, {"old-share": "share"}):
            env = Path(d) / ".env"
            env.write_text("COMPONENTS=core,mesh,paperless,files,old-share,gone\n")
            sel = run.chosen_stages(env)
        self.assertIn("sharry", sel["files"])
        self.assertIn("dns", sel["wizard"])
