"""~/homelab/ops/access.py: what the console and the home page's sheets may ask for, by name.

One answer for three readers: ops/server.py (in the ops container: accepts only these names),
ops-run.sh (on the host: runs only these, each kind with one fixed command) and console-report.py
(which buttons the console shows). Python 3.11+ standard library only.

  actions          [actions] of the job registry (Back up now, the Disk sheet's cleanups)
  run-<job>        a job the homelab user may start (polkit_start in jobs/jobs.toml)
  logs-job-<job>   its last journal lines → ops/data/logs-job-<job>.json
  restart-<svc>    docker compose restart of an app's service (any chosen component but core: the
                   console can't outlive Caddy, Authentik or ops anyway)
  logs-app-<svc>   its last log lines → ops/data/logs-app-<svc>.json

Only the chosen components count (COMPONENTS in .env; empty = all, as before the setup engine).

  python3 ops/access.py names [--env .env]   every allowed request name, one per line
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def allowed(registry: dict, components_dir: Path, chosen: list[str]) -> dict[str, list[str]]:
    comps = {f.stem: _toml(f) for f in sorted(Path(components_dir).glob("*.toml"))}
    on = [c for c in comps if not chosen or c in chosen]
    jobs = [j for c in on for j in comps[c].get("jobs", [])]
    jobs = [j for j in registry["jobs"] if j in jobs and j != "notify"]
    services = [s for c in on if not comps[c].get("core") for s in comps[c].get("services", [])]
    return {
        "actions": list(registry.get("actions", {})),
        "run": [j for j in jobs if registry["jobs"][j]["polkit_start"]],
        "logs-job": jobs,
        "restart": services,
        "logs-app": services,
    }


def names(a: dict[str, list[str]]) -> list[str]:
    out = list(a["actions"])
    for kind in ("run", "logs-job", "restart", "logs-app"):
        out += [f"{kind}-{x}" for x in a[kind]]
    return out


def chosen_from_env(path: Path) -> list[str]:
    comps = ""
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith("COMPONENTS="):
                comps = line.split("=", 1)[1].strip().strip("'\"")
    return [c for c in comps.split(",") if c]


if __name__ == "__main__":
    if sys.argv[1:2] != ["names"]:
        sys.exit(__doc__)
    env = Path(sys.argv[3]) if sys.argv[2:3] == ["--env"] and len(sys.argv) > 3 else ROOT / ".env"
    print("\n".join(names(allowed(_toml(ROOT / "jobs/jobs.toml"), ROOT / "setup/components", chosen_from_env(env)))))
