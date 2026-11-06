#!/usr/bin/env python3
"""~/homelab/lib/setup/mcp.py: the homelab's setup as an MCP server (stdio), for an AI assistant (plan Phase 3c).

Claude Code finds it through the repo's .mcp.json; the setup app's assistant starts it the same way
(on this machine or over SSH). Python 3.11+ standard library only. JSON-RPC 2.0, one message per line.

What an assistant may do, and what it may not:
  - read: the components and presets, a plan, the wizard stages, the jobs' state, checks, logs
  - change, each a tool the client asks the person about: apply a choice (.env; secrets generated on
    the machine, never shown), set one non-secret option, run one wizard stage and answer its questions
  - never: see or type a secret, the sudo password, or answer a `danger` step (a disk wipe). The stage
    stops there and the tool says which command the person runs themselves (./homelab setup WIZARD).
Every result is redacted: each secret value from .env (and the generated ones) becomes ‹KEY›. Every call
is appended to ~/.local/state/homelab/mcp-audit.log (tool, arguments, ok or error; redacted too).
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/setup"))
sys.path.insert(0, str(ROOT / "lib"))
import engine  # noqa: E402
import render  # noqa: E402

VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
ENV = Path(os.environ.get("HOMELAB_ENV", ROOT / ".env"))
AUDIT = Path(os.environ.get("HOMELAB_MCP_AUDIT", Path.home() / ".local/state/homelab/mcp-audit.log"))
WAIT = float(os.environ.get("HOMELAB_MCP_WAIT", "120"))  # seconds a stage runs before the tool returns
# What only a person may answer (lib/wizard.sh kinds)
PERSON_ONLY = {"secret": "a secret", "danger": "a step that destroys data", "sudo": "the sudo password"}
ASKS = {"pause", "confirm", "input", "lines", "terminal"} | set(PERSON_ONLY)
SECRETISH = re.compile(r"(PASS|SECRET|TOKEN|_KEY$|KEY_|PRIVATE)")
INSTRUCTIONS = """Sets up this homelab: choose components (list_components, plan, apply), then run the
wizard stages one by one (stages, run_stage, answer, poll) and check (run_check, status, logs).
Ask the person before apply, set_option and run_stage. Never ask the person to paste a secret or
password into the chat: when a stage needs one, it stops and names the command they run themselves."""


# ── Redaction ───────────────────────────────────────────────────────────
def secret_values() -> dict[str, str]:
    env = render.read_env(ENV) if ENV.exists() else {}
    comps = engine.components()
    secret_keys = {k for c in comps.values() for k, o in c.get("options", {}).items() if o.get("type") == "secret"}
    return {k: v for k, v in env.items() if v and len(v) >= 6 and (k in secret_keys or SECRETISH.search(k))}


def redact(obj, secrets: dict[str, str] | None = None):
    secrets = secret_values() if secrets is None else secrets
    if isinstance(obj, str):
        for k, v in sorted(secrets.items(), key=lambda kv: -len(kv[1])):
            obj = obj.replace(v, f"‹{k}›")
        return obj
    if isinstance(obj, list):
        return [redact(x, secrets) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    return obj


def audit(tool: str, args: dict, ok: bool, note: str = "") -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT, "a") as f:
            f.write(json.dumps(redact({"time": int(time.time()), "tool": tool, "args": args, "ok": ok, "note": note})) + "\n")
        os.chmod(AUDIT, 0o600)
    except OSError:
        pass


# ── The engine and the CLI ──────────────────────────────────────────────
def sh(argv: list[str], stdin: str = "", timeout: int = 600) -> tuple[int, str]:
    r = subprocess.run(argv, cwd=ROOT, input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def engine_json(cmd: str, answers: str | None = None) -> dict:
    argv = [sys.executable, str(ROOT / "lib/setup/engine.py"), cmd, "--json", "--env", str(ENV)]
    if answers is not None:
        argv += ["--answers", "-"]
    code, out = sh(argv, answers or "")
    line = next((x for x in reversed(out.splitlines()) if x.startswith("{")), None)
    if not line:
        raise ToolError(f"the engine said: {out.strip()[-500:]}")
    return json.loads(line)


def toml_str(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def answers_toml(a: dict) -> str:
    """An answers file from a tool's arguments; secret options are refused before this"""
    out = []
    if a.get("preset"):
        out.append(f"preset = {toml_str(a['preset'])}")
    if a.get("components"):
        out.append("components = [" + ", ".join(toml_str(c) for c in a["components"]) + "]")
    for k in ("add", "remove"):
        if a.get(k):
            out.append(f"{k} = [" + ", ".join(toml_str(c) for c in a[k]) + "]")
    if a.get("gpu"):
        out.append(f"gpu = {toml_str(a['gpu'])}")
    out.append("[options]")
    for k, v in (a.get("options") or {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            raise ToolError(f"odd option name {k!r}")
        out.append(f"{k} = {toml_str(v)}")
    return "\n".join(out) + "\n"


def refuse_secrets(options: dict) -> None:
    comps = engine.components()
    kinds = {k: o.get("type") for c in comps.values() for k, o in c.get("options", {}).items()}
    bad = [k for k in options if kinds.get(k) == "secret"]
    if bad:
        raise ToolError(f"{', '.join(bad)}: secrets are never set through the assistant. They're generated on the "
                        "machine, or the person types them in `./homelab setup` or the setup app.")


class ToolError(Exception):
    pass


# ── Wizard stages, one at a time ────────────────────────────────────────
class Stage:
    """A run of `./homelab setup WIZARD --stages=ID --protocol`, read by a thread into a queue"""
    RUN = [sys.executable, str(ROOT / "lib/setup/run.py")]  # tests put a stand-in here

    def __init__(self, wizard: str, stage: str):
        self.wizard, self.stage = wizard, stage
        self.p = subprocess.Popen([*self.RUN, "--protocol", wizard, f"--stages={stage}"],
                                  cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.q: queue.Queue = queue.Queue()
        self.asking: dict | None = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        assert self.p.stdout
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                self.q.put({"t": "output", "text": line.rstrip("\n")})
        self.q.put(None)

    def advance(self, wait: float) -> dict:
        """Messages until a question, the end, or `wait` seconds"""
        msgs, end, deadline = [], None, time.time() + wait
        while True:
            try:
                m = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if m is None:
                end = self.p.wait()
                break
            if m.get("t") in ("start", "banner"):
                continue
            if m.get("t") == "done":
                end = 0 if not m.get("failed") else 1
                continue
            if m.get("t") in ASKS:
                self.asking = m
                if m["t"] in PERSON_ONLY:
                    self.stop()
                    return {"messages": msgs, "state": "needs_person",
                            "why": f"This step needs {PERSON_ONLY[m['t']]}, which only the person gives.",
                            "person_runs": f"cd {ROOT} && ./homelab setup {self.wizard} --stages={self.stage}",
                            "question": {k: v for k, v in m.items() if k in ("t", "text", "prompt", "key", "word")}}
                return {"messages": msgs, "state": "question", "question": {k: v for k, v in m.items() if k != "wizard"}}
            msgs.append({k: v for k, v in m.items() if k != "wizard"})
        if end is not None:
            return {"messages": msgs, "state": "finished", "exit": end}
        return {"messages": msgs, "state": "running", "hint": "still working: call poll"}

    def answer(self, text: str) -> None:
        assert self.p.stdin
        kind = (self.asking or {}).get("t")
        lines = text.split("\n") + [""] if kind == "lines" else [text.replace("\n", " ")]
        for line in lines:
            self.p.stdin.write(json.dumps({"answer": line}) + "\n")
        self.p.stdin.flush()
        self.asking = None

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()


CURRENT: Stage | None = None


# ── Tools ───────────────────────────────────────────────────────────────
def t_list_components(a):
    c = engine_json("components")
    return {"chosen": c["chosen"], "gpu": c["gpu"], "presets": c["presets"],
            "components": [{k: x[k] for k in ("id", "title", "description", "category", "core", "requires", "suggests", "conflicts", "gpu", "on")}
                           | {"options": [o for o in x["options"]]} for x in c["components"]]}


def t_plan(a):
    refuse_secrets(a.get("options") or {})
    return engine_json("plan", answers_toml(a))


def t_apply(a):
    refuse_secrets(a.get("options") or {})
    r = engine_json("apply", answers_toml(a))
    if r.get("applied"):
        code, out = sh(["./homelab", "configure"])
        r["configure"] = {"exit": code, "output": out[-2000:]}
        if ENV.resolve() == (ROOT / ".env").resolve():  # the running console's backend reads this one's choice
            code, out = sh(["./homelab", "refresh-console"])
            r["console"] = {"exit": code, "output": out[-500:]}
    return r


def t_set_option(a):
    key, value = a["key"], str(a["value"])
    refuse_secrets({key: value})
    c = engine_json("components")
    return t_apply({"components": c["chosen"], "options": {key: value}})


def t_stages(a):
    code, out = sh([sys.executable, str(ROOT / "lib/setup/engine.py"), "stages", "--env", str(ENV)])
    stages = [s for s in out.split() if ":" in s]
    titles = engine.stage_titles(render.read_env(ENV) if ENV.exists() else {})
    return {"stages": [{"id": s, "title": titles.get(s, "")} for s in stages],
            "hint": "run_stage one at a time, in this order; a stage that is already done offers to skip"}


def t_run_stage(a):
    global CURRENT
    if CURRENT and CURRENT.p.poll() is None:
        raise ToolError(f"{CURRENT.wizard}:{CURRENT.stage} is still running: answer, poll or stop it first")
    w, _, s = a["stage"].partition(":")
    if w not in engine.WIZARDS or not re.fullmatch(r"[a-z_]+", s or ""):
        raise ToolError("stage is WIZARD:ID, from the stages tool")
    CURRENT = Stage(w, s)
    return CURRENT.advance(WAIT)


def t_answer(a):
    if not CURRENT or not CURRENT.asking:
        raise ToolError("no question is waiting")
    if CURRENT.asking["t"] in PERSON_ONLY:
        raise ToolError("only the person answers this one")
    CURRENT.answer(str(a.get("text", "")))
    return CURRENT.advance(WAIT)


def t_poll(a):
    if not CURRENT:
        raise ToolError("nothing is running")
    if CURRENT.asking:
        return {"messages": [], "state": "question", "question": CURRENT.asking}
    return CURRENT.advance(min(WAIT, float(a.get("wait", WAIT))))


def t_stop(a):
    if CURRENT:
        CURRENT.stop()
    return {"stopped": True}


def t_run_check(a):
    argv = ["./homelab", "setup"] + ([a["wizard"]] if a.get("wizard") else []) + ["--check", "--protocol"]
    if a.get("wizard") and a["wizard"] not in engine.WIZARDS:
        raise ToolError("unknown wizard")
    code, out = sh(argv, timeout=900)
    checks, wizard = [], ""
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("t") == "check":
            checks.append({"wizard": m.get("wizard"), "check": m["label"], "ok": m["ok"]})
    return {"exit": code, "failed": [c for c in checks if not c["ok"]], "passed": sum(c["ok"] for c in checks)}


def t_status(a):
    code, out = sh(["./homelab", "status"])
    return {"exit": code, "text": out}


def t_logs(a):
    job = a["job"]
    if not re.fullmatch(r"[a-z-]+", job):
        raise ToolError("a job id from status")
    code, out = sh(["./homelab", "logs", job])
    return {"exit": code, "text": out[-6000:]}


OBJ = {"type": "object", "additionalProperties": False}
CHOICE = {**OBJ, "properties": {
    "preset": {"type": "string", "description": "minimal | family | everything"},
    "components": {"type": "array", "items": {"type": "string"}, "description": "the whole choice (instead of preset)"},
    "add": {"type": "array", "items": {"type": "string"}}, "remove": {"type": "array", "items": {"type": "string"}},
    "gpu": {"type": "string", "enum": ["nvidia", "none"]},
    "options": {"type": "object", "additionalProperties": {"type": "string"}, "description": "non-secret .env options"}}}
RO = {"readOnlyHint": True, "openWorldHint": False}
TOOLS = {
    "list_components": (t_list_components, "Every component (app or feature) with its options (secret values hidden), the presets and the current choice.", OBJ, RO),
    "plan": (t_plan, "What a choice would do: components added, notes, what's still needed, the setup stages. Changes nothing.", CHOICE, RO),
    "apply": (t_apply, "Writes the choice and options into .env (generated secrets stay on the machine), runs ./homelab configure and gives a running console the new choice (./homelab refresh-console). Ask first.", CHOICE,
              {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}),
    "set_option": (t_set_option, "Sets one non-secret .env option of the chosen components. Ask first.",
                   {**OBJ, "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]},
                   {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}),
    "stages": (t_stages, "The wizard stages of the chosen components, in order (WIZARD:ID and title).", OBJ, RO),
    "run_stage": (t_run_stage, "Runs one stage. Returns its messages up to its first question (answer it with answer), its end, or after a while (poll). "
                  "Steps that need a secret, sudo or a destructive confirmation return needs_person with the command the person runs. Ask first.",
                  {**OBJ, "properties": {"stage": {"type": "string", "description": "WIZARD:ID from stages"}}, "required": ["stage"]},
                  {"readOnlyHint": False, "destructiveHint": False}),
    "answer": (t_answer, "Answers the running stage's question (confirm: y or n; pause/terminal: empty once the person did it; lines: one per line). "
               "Only what the person said or clearly wants.",
               {**OBJ, "properties": {"text": {"type": "string"}}, "required": ["text"]}, {"readOnlyHint": False, "destructiveHint": False}),
    "poll": (t_poll, "More of the running stage's messages.", {**OBJ, "properties": {"wait": {"type": "number"}}}, RO),
    "stop": (t_stop, "Stops the running stage (it can run again; finished steps are skipped).", OBJ, {"readOnlyHint": False, "destructiveHint": False}),
    "run_check": (t_run_check, "Every wizard's checks (or one wizard's), changes nothing: what passes and what fails.",
                  {**OBJ, "properties": {"wizard": {"type": "string"}}}, RO),
    "status": (t_status, "Every scheduled job: state, last and next run.", OBJ, RO),
    "logs": (t_logs, "A job's last log lines (secrets redacted).", {**OBJ, "properties": {"job": {"type": "string"}}, "required": ["job"]}, RO),
}


def tool_list() -> list[dict]:
    return [{"name": n, "title": n.replace("_", " ").capitalize(), "description": d, "inputSchema": s, "annotations": ann}
            for n, (_, d, s, ann) in TOOLS.items()]


def call(name: str, args: dict) -> dict:
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    try:
        out = redact(TOOLS[name][0](args or {}))
        audit(name, args, True)
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}], "structuredContent": out, "isError": False}
    except (ToolError, KeyError, subprocess.TimeoutExpired) as e:
        msg = redact(str(e) if not isinstance(e, KeyError) else f"missing argument {e}")
        audit(name, args, False, msg)
        return {"content": [{"type": "text", "text": msg}], "isError": True}


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # a notification (initialized, cancelled): nothing to answer
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        result = {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "homelab-setup", "version": "0.1.0"}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        result = call(params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg) if isinstance(msg, dict) else {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    if CURRENT:
        CURRENT.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
