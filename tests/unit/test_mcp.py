"""lib/setup/mcp.py: the MCP server. JSON-RPC over stdio, tools; no secret in any result or the audit
log; secrets, sudo and danger steps refused (the stage stops and names the person's command)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = "tok-VERY-SECRET-123"

WIZARD = f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT}/lib/wizard.sh
ENV_FILE=$HOMELAB_TEST_ENV
TOTAL_STAGES=3
stage_ask() {{
  stage "Asks" || return 0
  echo "the token is {SECRET}"
  confirm "Go on?" || exit 3
  ask DOMAIN "Your domain:"
  echo "domain=$DOMAIN"
}}
stage_secret() {{
  stage "Secret" || return 0
  ask_secret SMTP_TOKEN "SMTP token:"
}}
stage_wipe() {{
  stage "Wipe" || return 0
  danger "This erases the disk" WIPE || exit 4
}}
stage_ask
stage_secret
stage_wipe
finish
"""
# Stands in for lib/setup/run.py: the same runner on the fake wizard
RUNNER = f"""import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / "lib/setup")!r})
import run
W = Path(sys.argv.pop(1))
run.script = lambda name: W
run.chosen_stages = lambda: None
sys.exit(run.main(sys.argv[1:]))
"""


class Mcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = d / ".env"
        self.env.write_text(f"COMPONENTS=core,mesh\nDOMAIN=example.org\nINFOMANIAK_API_TOKEN='{SECRET}'\n")
        self.audit = d / "audit.log"
        (d / "w.sh").write_text(WIZARD)
        (d / "w.sh").chmod(0o755)
        (d / "runner.py").write_text(RUNNER)
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, *calls, stand_in=False):
        """Runs the server with these requests; returns the responses by id"""
        msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        for i, (name, args) in enumerate(calls, 1):
            msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
        code = "import sys; sys.path.insert(0, %r); import mcp\n" % str(ROOT / "lib/setup")
        if stand_in:
            code += "mcp.Stage.RUN = [sys.executable, %r, %r]\n" % (str(self.d / "runner.py"), str(self.d / "w.sh"))
        code += "sys.exit(mcp.main())\n"
        env = dict(os.environ, HOMELAB_ENV=str(self.env), HOMELAB_MCP_AUDIT=str(self.audit), HOMELAB_MCP_WAIT="20",
                   HOMELAB_TEST_ENV=str(self.env))
        r = subprocess.run([sys.executable, "-c", code], input="".join(json.dumps(m) + "\n" for m in msgs),
                           capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.raw = r.stdout
        return {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}

    def test_handshake_and_tools(self):
        out = self.session()
        init = out[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        out = self.session(("nope", {}))
        self.assertTrue(out[1]["result"]["isError"])

    def test_tool_list(self):
        sys.path.insert(0, str(ROOT / "lib/setup"))
        import mcp
        names = {t["name"]: t for t in mcp.tool_list()}
        self.assertLessEqual({"list_components", "plan", "apply", "stages", "run_stage", "answer", "run_check", "logs"}, set(names))
        for t in names.values():
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", t["annotations"])
        # What changes something says so
        for n in ("apply", "set_option", "run_stage", "answer"):
            self.assertFalse(names[n]["annotations"]["readOnlyHint"], n)

    def test_no_secret_in_results(self):
        out = self.session(("list_components", {}), ("plan", {"preset": "minimal"}))
        self.assertFalse(out[1]["result"]["isError"])
        self.assertFalse(out[2]["result"]["isError"])
        self.assertNotIn(SECRET, self.raw)
        core = next(c for c in out[1]["result"]["structuredContent"]["components"] if c["id"] == "core")
        tok = next(o for o in core["options"] if o["key"] == "INFOMANIAK_API_TOKEN")
        self.assertEqual((tok["set"], tok["value"]), (True, None))

    def test_secrets_refused(self):
        out = self.session(("plan", {"options": {"INFOMANIAK_API_TOKEN": "x"}}), ("set_option", {"key": "AK_SECRET_KEY", "value": "x"}))
        for i in (1, 2):
            self.assertTrue(out[i]["result"]["isError"])
            self.assertIn("never set through the assistant", out[i]["result"]["content"][0]["text"])

    def test_stage_questions_and_redaction(self):
        out = self.session(("run_stage", {"stage": "wizard:ask"}), ("answer", {"text": "y"}), ("answer", {"text": "smith.example"}),
                           stand_in=True)
        first = out[1]["result"]["structuredContent"]
        self.assertEqual(first["state"], "question")
        self.assertEqual(first["question"]["t"], "confirm")
        texts = [m.get("text") for m in first["messages"]]
        self.assertIn("the token is ‹INFOMANIAK_API_TOKEN›", texts)
        self.assertEqual(out[2]["result"]["structuredContent"]["question"]["t"], "input")
        last = out[3]["result"]["structuredContent"]
        self.assertEqual(last["state"], "finished")
        self.assertIn("domain=smith.example", [m.get("text") for m in last["messages"]])
        self.assertNotIn(SECRET, self.raw)
        log = self.audit.read_text()
        self.assertIn('"tool": "run_stage"', log)
        self.assertNotIn(SECRET, log)

    def test_person_only_steps(self):
        out = self.session(("run_stage", {"stage": "wizard:secret"}), ("answer", {"text": "x"}),
                           ("stop", {}), ("run_stage", {"stage": "wizard:wipe"}), stand_in=True)
        r = out[1]["result"]["structuredContent"]
        self.assertEqual(r["state"], "needs_person", r)
        self.assertIn("./homelab setup wizard --stages=secret", r["person_runs"])
        self.assertTrue(out[2]["result"]["isError"])   # nothing to answer: it stopped
        w = out[4]["result"]["structuredContent"]
        self.assertEqual((w["state"], w["question"]["word"]), ("needs_person", "WIPE"))
        self.assertNotIn("SMTP_TOKEN=", self.env.read_text())


if __name__ == "__main__":
    unittest.main()
