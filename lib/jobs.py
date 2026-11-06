#!/usr/bin/env python3
"""~/homelab/lib/jobs.py: the job registry (jobs/jobs.toml) for scripts, Ansible and ./homelab.

    jobs.py units [--env F]   JSON list of every unit, in install order (Ansible: timers_units); with
                              --env, only the jobs of the components in COMPONENTS (setup/components/)
    jobs.py polkit [--env F]  JSON list of the services the homelab user may start (the polkit rule)
    jobs.py locks        the lock names, space-separated (ansible/live-safe.sh)
    jobs.py actions      the home page's action names, one per line (ops-run.sh, tests)
    jobs.py list         a table for people
    jobs.py status [--env F]  last run, result and next run of every job (systemctl); with --env, only the
                              jobs of the chosen components, as the console (./homelab status)
    jobs.py unit JOB     the job's service unit (./homelab run / logs)
    jobs.py script JOB   the job's script and arguments (lib/platform/launchd-run.sh)
    jobs.py field JOB K  one field of a job
    jobs.py plists --env F --out DIR [--repo R]
                         macOS: a launchd job (org.homelab.<job>.plist) per job of the chosen components
                         that runs there, from its calendar (ansible/site-macos.yml installs them)
    jobs.py json         the whole registry
Python 3.11+ (tomllib), standard library only.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "jobs/jobs.toml"


def load(path: Path = REGISTRY) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def enabled(reg: dict, env_path: str | None) -> dict:
    """The jobs of the chosen components: COMPONENTS in env_path, each component's `jobs`. No file or
    no COMPONENTS (an install from before the setup engine) = every job."""
    if not env_path or not Path(env_path).is_file():
        return reg["jobs"]
    comps = None
    for line in Path(env_path).read_text().splitlines():
        if line.startswith("COMPONENTS="):
            comps = line.split("=", 1)[1].strip().strip("'\"")
    if not comps:
        return reg["jobs"]
    on: set[str] = set()
    for f in sorted((ROOT / "setup/components").glob("*.toml")):
        if f.stem in comps.split(","):
            with open(f, "rb") as fh:
                on.update(tomllib.load(fh).get("jobs", []))
    return {k: v for k, v in reg["jobs"].items() if k in on}


def launchd_calendar(cal: str) -> dict:
    """A systemd OnCalendar of this registry as launchd keys: StartCalendarInterval (a dict or a list of
    them) or StartInterval. Only the forms jobs.toml uses; anything else is an error, not a guess."""
    if cal == "hourly":
        return {"StartCalendarInterval": {"Minute": 0}}
    m = re.fullmatch(r"\*:(\d+)/(\d+)", cal)  # every N minutes from minute M
    if m:
        return {"StartInterval": int(m[2]) * 60}
    m = re.fullmatch(r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?\*-\*-\* (\*|\d\d|\d\d/\d+):(\d\d):00", cal)
    if not m:
        raise ValueError(f"no launchd form for OnCalendar={cal!r}")
    day, hour, minute = m[1], m[2], int(m[3])
    base = {"Minute": minute}
    if day:
        base["Weekday"] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].index(day)
    if hour == "*":
        return {"StartCalendarInterval": base}
    if "/" in hour:
        start, step = map(int, hour.split("/"))
        return {"StartCalendarInterval": [dict(base, Hour=h) for h in range(start, 24, step)]}
    return {"StartCalendarInterval": dict(base, Hour=int(hour))}


# GNU tools first, as on Linux: the scripts use stat -c, date -d, find -printf (Homebrew's g-prefixed
# tools under their own names); then Homebrew, then the system
MAC_PATH = ":".join([f"{b}/opt/{f}/libexec/gnubin" for b in ("/opt/homebrew", "/usr/local")
                     for f in ("coreutils", "findutils", "gnu-sed", "grep")]
                    + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def plists(reg: dict, jobs: dict, repo: str, home: str) -> dict[str, bytes]:
    """file name → plist, for every job that runs on macOS and has a trigger (a calendar or the ops inbox)"""
    out = {}
    for jid, j in jobs.items():
        if not j.get("macos", True) or jid == "notify":
            continue
        label = f"org.homelab.{jid}"
        d = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", f"{repo}/lib/platform/launchd-run.sh", jid],
            "WorkingDirectory": repo,
            "EnvironmentVariables": {"PATH": MAC_PATH, "HOME": home},
            "StandardOutPath": f"{home}/Library/Logs/homelab/{label}.launchd.log",
            "StandardErrorPath": f"{home}/Library/Logs/homelab/{label}.launchd.log",
            "ProcessType": "Background",
            "LowPriorityIO": True,
        }
        if j["calendar"]:
            d.update(launchd_calendar(j["calendar"]))
        elif any(u.endswith(".path") for u in j["units"]):
            # homelab-ops.path's PathExistsGlob: launchd keeps it running while the inbox isn't empty
            d["QueueDirectories"] = [f"{repo}/ops/requests"]
        else:
            continue
        out[f"{label}.plist"] = plistlib.dumps(d, sort_keys=True)
    return out


def service(job: dict) -> str:
    return next(u for u in job["units"] if u.endswith(".service"))


def trigger(job: dict) -> str | None:
    return next((u for u in job["units"] if u.endswith((".timer", ".path"))), None)


def show(unit: str, props: list[str]) -> dict[str, str]:
    """systemctl show, as a dict; empty when systemctl isn't there (macOS, containers)."""
    try:
        out = subprocess.run(["systemctl", "show", unit, "-p", ",".join(props)],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def launchd_state(jid: str) -> dict[str, str]:
    """macOS: a job's launchd state and its log's last run (lib/platform/launchd-run.sh), in the
    systemctl-show keys states() reads; empty when the job isn't loaded"""
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/org.homelab.{jid}"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    running = re.search(r"^\s*state = running", out.stdout, re.M)
    last = ""
    log = Path.home() / "Library/Logs/homelab" / f"org.homelab.{jid}.log"
    code = "0"
    if log.exists():
        ends = [m for m in re.finditer(r"^── (\S+ \S+) \S+: exit (\d+)$", log.read_text(errors="replace"), re.M)]
        if ends:
            last, code = ends[-1][1], ends[-1][2]
    return {"ActiveState": "active" if running else "inactive", "Result": "success" if code == "0" else f"exit-code {code}",
            "ExecMainExitTimestamp": last}


def states(reg: dict, jobs: dict | None = None) -> list[dict]:
    """Each job's state from systemd (macOS: launchd): id, title, when, guide, state (running | failed |
    ok | unknown), detail, last (finished), next, polkit_start. ./homelab status and console-report.py."""
    out = []
    mac = sys.platform == "darwin"
    for jid, job in (jobs if jobs is not None else reg["jobs"]).items():
        if jid == "notify" or (mac and not job.get("macos", True)):
            continue
        if mac:
            s = launchd_state(jid)
            state, detail = ("unknown", "not loaded (ansible/site-macos.yml)") if not s else (
                ("running", "running") if s["ActiveState"] == "active" else
                ("failed", f"failed ({s['Result']})") if s["Result"] != "success" else ("ok", "ok"))
            out.append({"id": jid, "title": job["title"], "when": job["when"], "guide": job["guide"], "state": state,
                        "detail": detail, "last": s.get("ExecMainExitTimestamp", ""), "next": "",
                        "polkit_start": job["polkit_start"]})
            continue
        s = show(service(job), ["ActiveState", "Result", "ExecMainExitTimestamp", "ExecMainStartTimestamp"])
        t = trigger(job)
        tp = show(t, ["NextElapseUSecRealtime", "LastTriggerUSec", "ActiveState"]) if t else {}
        if not s:
            state, detail = "unknown", "unknown (no systemd)"
        elif s.get("ActiveState") in ("active", "activating"):
            state, detail = "running", "running"
        elif s.get("Result") not in ("success", "", None) or s.get("ActiveState") == "failed":
            state, detail = "failed", f"failed ({s.get('Result')})"
        else:
            state, detail = "ok", "ok"
        if t and tp and tp.get("ActiveState") not in ("active", None):
            state, detail = "failed", detail + f", {t.rsplit('.', 1)[1]} {tp.get('ActiveState')}"
        nxt = (tp.get("NextElapseUSecRealtime") or "") if t and t.endswith(".timer") else ""
        out.append({"id": jid, "title": job["title"], "when": job["when"], "guide": job["guide"],
                    "state": state, "detail": detail, "last": s.get("ExecMainExitTimestamp") or "",
                    "next": nxt, "polkit_start": job["polkit_start"]})
    return out


def status(reg: dict, color: bool, jobs: dict | None = None) -> int:
    red, green, dim, reset = ("\033[31m", "\033[32m", "\033[2m", "\033[0m") if color else ("", "", "", "")
    rows, bad = [], 0
    for j in states(reg, jobs):
        # A failed timer on a healthy service counts too (the detail names it)
        mark = {"unknown": dim, "failed": red}.get(j["state"], green)
        bad += j["state"] == "failed"
        rows.append((j["id"], f"{mark}{j['detail']}{reset}", j["last"] or "never", j["next"] or j["when"]))
    w = max((len(r[0]) for r in rows), default=3)
    print(f"{'job':<{w}}  {'state':<26} {'last finished':<30} next")
    for jid, st, last_s, nxt_s in rows:
        pad = 26 + (len(st) - len(st.replace(red, "").replace(green, "").replace(dim, "").replace(reset, "")))
        print(f"{jid:<{w}}  {st:<{pad}} {last_s:<30} {nxt_s}")
    return 1 if bad else 0


def main() -> None:
    reg = load()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    env = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--env" else None
    jobs = reg["jobs"]
    if cmd == "units":
        print(json.dumps([u for j in enabled(reg, env).values() for u in j["units"]]))
    elif cmd == "polkit":
        print(json.dumps([service(j) for j in enabled(reg, env).values() if j["polkit_start"]]))
    elif cmd == "locks":
        seen: list[str] = []
        for j in jobs.values():
            if j["lock"] and j["lock"] not in seen:
                seen.append(j["lock"])
        print(" ".join(seen))
    elif cmd == "actions":
        print("\n".join(reg.get("actions", {})))
    elif cmd == "json":
        print(json.dumps(reg, indent=2))
    elif cmd == "unit":
        if len(sys.argv) < 3 or sys.argv[2] not in jobs:
            sys.exit(f"unknown job; one of: {', '.join(jobs)}")
        print(service(jobs[sys.argv[2]]))
    elif cmd in ("script", "field"):
        if len(sys.argv) < 3 or sys.argv[2] not in jobs:
            sys.exit(f"unknown job; one of: {', '.join(jobs)}")
        j = jobs[sys.argv[2]]
        v = j["script"] if cmd == "script" else j[sys.argv[3]]
        print(json.dumps(v) if isinstance(v, bool) else v)
    elif cmd == "plists":
        args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
        out = Path(args["--out"])
        out.mkdir(parents=True, exist_ok=True)
        repo = args.get("--repo", str(ROOT))
        for name, data in plists(reg, enabled(reg, args.get("--env")), repo, os.environ.get("HOME", "")).items():
            f = out / name
            if not f.exists() or f.read_bytes() != data:
                f.write_bytes(data)
                print(f"wrote {name}")
    elif cmd == "list":
        w = max(map(len, jobs))
        for jid, j in jobs.items():
            print(f"{jid:<{w}}  {j['when']:<36} {j['title']}")
    elif cmd == "status":
        sys.exit(status(reg, sys.stdout.isatty() and not os.environ.get("NO_COLOR"), enabled(reg, env)))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
