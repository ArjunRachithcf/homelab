"""lib/setup/run.py: a wizard over the protocol, answers in, messages out, the secret only inward."""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
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


# Asks once from inside (as sudo's askpass would), then waits until the test's outsider has tried
PEER_WIZARD = """#!/usr/bin/env bash
pw=$("$SUDO_ASKPASS" "inside:") && echo "inside len=${{#pw}}"
for _ in $(seq 1 200); do [ -e '{go}' ] && break; sleep 0.05; done
"""


class AskpassPeer(unittest.TestCase):
    """Only the running wizard's own processes get an answer: another program of the same user's that finds the
    socket gets nothing, and the front end is neither asked nor left unaware"""

    def test_outsider_refused(self):
        got = {}
        with tempfile.TemporaryDirectory() as d:
            go = Path(d) / "go"
            w = Path(d) / "w.sh"
            w.write_text(PEER_WIZARD.format(go=go))
            w.chmod(0o755)
            out = io.StringIO()
            pr = run.Protocol(out=out, inp=io.StringIO(json.dumps({"answer": "hunter2"}) + "\n"))

            def outsider():  # this test's own process: not in the wizard's session
                try:
                    for _ in range(500):
                        if pr.wizard_sid:
                            break
                        time.sleep(0.01)
                    data = b""
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(10)
                        s.connect(pr.sock_path)
                        try:
                            s.sendall(b"outside:\n")
                            while chunk := s.recv(4096):
                                data += chunk
                        except (ConnectionResetError, BrokenPipeError):
                            pass  # hung up on before its prompt was read: refused
                    got["data"] = data
                except Exception as e:  # noqa: BLE001 (shown in the assertion below)
                    got["error"] = repr(e)
                finally:
                    go.touch()

            orig = run.script
            run.script = lambda name: w
            t = threading.Thread(target=outsider)
            t.start()
            try:
                code = pr.run("fake", [])
            finally:
                run.script = orig
                t.join(10)
                pr.close()
        raw = out.getvalue()
        msgs = [json.loads(line) for line in raw.split("\n") if line]
        self.assertEqual(code, 0, raw)
        self.assertEqual(got.get("data"), b"", got)
        self.assertEqual([m["prompt"] for m in msgs if m["t"] == "sudo"], ["inside:"])
        self.assertIn("inside len=7", [m.get("text") for m in msgs if m["t"] == "output"])
        self.assertTrue(any(m["t"] == "say" and m.get("style") == "warn" and "refused" in m["text"] for m in msgs), raw)
        self.assertNotIn("hunter2", raw)


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
