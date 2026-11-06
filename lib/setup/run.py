#!/usr/bin/env python3
"""~/homelab/lib/setup/run.py: ./homelab setup, the wizards in order, for a person or a front end.

    homelab setup                     the chosen components' wizard stages (COMPONENTS in .env), in
                                      RUNBOOK order, in this terminal (no COMPONENTS: every stage)
    homelab setup WIZARD [--check]    one wizard (wizard, public, hardening, immich, apps, music, files, family)
    homelab setup --protocol [WIZARD] the same, spoken as JSON lines (the setup app, the MCP server)
    homelab setup WIZARD --stages=a,b only those stages of one wizard (ids: lib/wizard.sh, stage_<id>)
    homelab setup --plan | --apply | --export-answers [--preset P] [--answers F] [--add a,b]
                  [--remove c] [--gpu nvidia|none] [--json]
    homelab setup --set-secret KEY    one secret, from stdin (the setup app)
                                      choose components and options: lib/setup/engine.py

Protocol (one JSON object per line):
  out (to the front end): the wizards' messages as lib/wizard.sh sends them (t: banner, stage, say,
       link, pause, confirm, input, secret, lines, danger, terminal, wrote, check, finish), each with
       "wizard"; {"t":"output","wizard",…,"text"} for the commands' own output lines;
       {"t":"start","wizard"} and {"t":"exit","wizard","code"} around each wizard; {"t":"done","failed":[…]}.
  in (from the front end): {"answer": "…"} for every pause, confirm, input, secret, lines (one per
       line, then ""), danger, terminal and sudo message. Anything else is ignored.
A secret answer goes into the wizard's stdin and nowhere else: no message ever carries it.
sudo: the wizards' `sudo …` and `ansible-playbook -K` have no terminal here, so lib/setup/bin/ (first on
PATH) runs them with lib/setup/askpass.py, which asks through a private Unix socket; this sends
{"t":"sudo","wizard","prompt"} and passes the answer back to sudo alone. A front end may keep the
password in memory for the session and answer by itself (the setup app does).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# RUNBOOK order. The setup engine (Phase 2b-2) narrows this to the chosen components.
WIZARDS = ["wizard", "public", "hardening", "immich", "apps", "music", "files", "family"]
ASKS = {"pause", "confirm", "input", "secret", "lines", "danger", "terminal"}


def chosen_stages(env_file: Path = ROOT / ".env") -> dict[str, list[str]] | None:
    """{wizard: [stage ids]} of the chosen components, or None when .env has no COMPONENTS. A retired id
    reads as its successor (engine.RETIRED), an unknown one is left out."""
    sys.path.insert(0, str(ROOT / "lib/setup"))
    sys.path.insert(0, str(ROOT / "lib"))
    import engine
    import render
    env = render.read_env(env_file) if env_file.exists() else {}
    if not env.get("COMPONENTS"):
        return None
    comps = engine.components()
    out: dict[str, list[str]] = {}
    for st in engine.stages(comps, [c for c in engine.renamed([c for c in env["COMPONENTS"].split(",") if c]) if c in comps]):
        w, sid = st.split(":", 1)
        out.setdefault(w, []).append(sid)
    return out


def wizard_env(name: str, sel: dict[str, list[str]] | None) -> dict[str, str]:
    env = dict(os.environ)
    if sel is not None:
        env["HOMELAB_STAGES"] = ",".join(sel.get(name, []))
        env["HOMELAB_STAGES_TOTAL"] = str(len(sel.get(name, [])))
    return env


def script(name: str) -> Path:
    return ROOT / ("wizard.sh" if name == "wizard" else f"{name}-wizard.sh")


def tty(names: list[str], extra: list[str], sel=None) -> int:
    failed = []
    for n in names:
        rc = subprocess.call([str(script(n)), *extra], cwd=ROOT, env=wizard_env(n, sel))
        if rc:
            failed.append(n)
            if "--check" not in extra:
                print(f"\n{n}: stopped (exit {rc}). Fix what it says, then ./homelab setup {n}", file=sys.stderr)
                return rc
    return 1 if failed else 0


class Protocol:
    def __init__(self, out=sys.stdout, inp=sys.stdin):
        self.out, self.inp, self.lock = out, inp, threading.Lock()
        # One question to the front end at a time (a wizard's own, or sudo's through askpass)
        self.ask_lock = threading.Lock()
        self.wizard = ""
        self.dir = tempfile.mkdtemp(prefix="homelab-setup-")  # 0700
        self.sock_path = os.path.join(self.dir, "askpass")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.sock_path)
        self.sock.listen(4)
        threading.Thread(target=self._askpass, daemon=True).start()

    def close(self) -> None:
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _askpass(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                prompt = conn.recv(256).decode("utf-8", "replace").strip() or "sudo password"
                with self.ask_lock:
                    self.send({"t": "sudo", "wizard": self.wizard, "prompt": prompt})
                    a = self.answer()
                if a is not None:
                    try:
                        conn.sendall(a.replace("\n", "").encode() + b"\n")
                    except OSError:
                        pass

    def send(self, msg: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.out.flush()

    def answer(self) -> str | None:
        """The next {"answer": …} line from the front end; None at EOF."""
        for line in self.inp:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and isinstance(msg.get("answer"), str):
                return msg["answer"]
        return None

    def run(self, name: str, extra: list[str], sel=None) -> int:
        env = dict(wizard_env(name, sel), HOMELAB_UI="protocol", TERM="dumb")
        env.update(PATH=f"{ROOT / 'lib/setup/bin'}:{env.get('PATH', '')}", HOMELAB_ASKPASS_SOCK=self.sock_path,
                   SUDO_ASKPASS=str(ROOT / "lib/setup/askpass.py"))
        self.wizard = name
        p = subprocess.Popen([str(script(name)), *extra], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.send({"t": "start", "wizard": name})
        assert p.stdout and p.stdin
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("\x1e"):
                try:
                    msg = json.loads(line[1:])
                except json.JSONDecodeError:
                    msg = {"t": "output", "text": line[1:]}
            else:
                msg = {"t": "output", "text": line}
            msg["wizard"] = name
            self.send(msg)
            if msg["t"] in ASKS:
                # lines: one answer per line until an empty one (the wizard reads them all without a
                # word in between); everything else: one answer
                gone = False
                with self.ask_lock:
                    while True:
                        a = self.answer()
                        if a is None:  # the front end went away: stop the wizard
                            gone = True
                            break
                        try:
                            p.stdin.write((a.replace("\n", " ") + "\n").encode())
                            p.stdin.flush()
                        except BrokenPipeError:
                            gone = True
                            break
                        if msg["t"] != "lines" or a == "":
                            break
                if gone:
                    p.kill()
                    break
        code = p.wait()
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except BrokenPipeError:
                pass
        self.send({"t": "exit", "wizard": name, "code": code})
        return code


ENGINE = {"--plan": "plan", "--apply": "apply", "--export-answers": "export", "--set-secret": "secret"}


def main(argv: list[str]) -> int:
    if {"-h", "--help", "help"} & set(argv):
        print(__doc__.split("\n\n", 1)[1].split("\n\nProtocol", 1)[0])
        return 0
    for flag, cmd in ENGINE.items():
        if flag in argv:
            rest = [a for a in argv if a != flag]
            return subprocess.call([sys.executable, str(ROOT / "lib/setup/engine.py"), cmd, *rest])
    protocol = "--protocol" in argv
    only = [a.split("=", 1)[1] for a in argv if a.startswith("--stages=")]
    args = [a for a in argv if a != "--protocol" and not a.startswith("--stages=")]
    names = [a for a in args if not a.startswith("-")]
    extra = [a for a in args if a.startswith("-")]
    # The wizards know only --check: anything else would start them for real
    for x in extra:
        if x != "--check":
            print(f"unknown option {x}; see ./homelab setup --help", file=sys.stderr)
            return 2
    for n in names:
        if n not in WIZARDS:
            print(f"unknown wizard {n}; one of: {' '.join(WIZARDS)}", file=sys.stderr)
            return 2
    sel = chosen_stages()
    if only:
        # --stages=a,b with one wizard: just those stages of it (the MCP server runs one at a time)
        if len(names) != 1:
            print("--stages needs exactly one wizard", file=sys.stderr)
            return 2
        sel = dict(sel or {}, **{names[0]: [x for x in only[0].split(",") if x]})
    # With components chosen, a wizard none of whose stages they need is left out (named ones still run)
    names = names or [w for w in WIZARDS if sel is None or sel.get(w)]
    if not protocol:
        return tty(names, extra, sel)
    pr, failed = Protocol(), []
    for n in names:
        code = pr.run(n, extra, sel)
        if code:
            failed.append(n)
            if "--check" not in extra:
                break
    pr.send({"t": "done", "failed": failed})
    pr.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
