#!/usr/bin/env python3
"""~/homelab/console-report.py: the console's overview (site/console/, RUNBOOK.md "Console").

Run by heartbeat.sh after ops-report.py (every 5 min), as the admin user, read-only towards
everything but ops/data/console.json. Python 3.11+ standard library only.

  time        when this ran
  jobs        every job of the chosen components: state, last and next run, its guide (lib/jobs.py)
  apps        every compose service of the chosen components: state, health, its component, and
              whether the console may restart it (ops/access.py)
  certs       days left on each mesh name's certificate (TLS to NETBIRD_IP:443, as a peer sees it)
  attention   what's red, each with the guide that says what to do (site/console/guides/<guide>.md)
  allowed     the request names the console may send (ops/access.py), for its buttons
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
sys.path.insert(0, str(HERE / "ops"))
import access  # noqa: E402
import jobs as registry  # noqa: E402
import render  # noqa: E402

DATA = HERE / "ops" / "data"
CERT_WARN_DAYS = 14
DISK_WARN = 0.10  # free share below which a disk is red


def components(chosen: list[str]) -> dict[str, dict]:
    out = {}
    for f in sorted((HERE / "setup/components").glob("*.toml")):
        if not chosen or f.stem in chosen:
            with open(f, "rb") as fh:
                out[f.stem] = tomllib.load(fh)
    return out


def containers() -> dict[str, dict]:
    """docker compose ps, by service; empty without Docker."""
    try:
        out = subprocess.run(["docker", "compose", "ps", "-a", "--format", "json"], cwd=HERE,
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # Compose prints one object per line (2.21+) or one array (older)
    rows = json.loads(out) if out.startswith("[") else [json.loads(x) for x in out.splitlines() if x.strip()]
    return {r["Service"]: r for r in rows}


def cert_days(ip: str, name: str) -> int | None:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname=name) as s:
            return int((ssl.cert_time_to_seconds(s.getpeercert()["notAfter"]) - time.time()) // 86400)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def report(env: dict[str, str]) -> dict:
    chosen = [c for c in env.get("COMPONENTS", "").split(",") if c]
    comps = components(chosen)
    reg = registry.load()
    allowed = access.allowed(reg, HERE / "setup/components", chosen)
    attention = []

    on_jobs = {j for c in comps.values() for j in c.get("jobs", [])}
    jobs = registry.states(reg, {k: v for k, v in reg["jobs"].items() if k in on_jobs})
    for j in jobs:
        if j["state"] == "failed":
            attention.append({"kind": "job", "id": j["id"], "title": f"{j['title']}: {j['detail']}",
                              "guide": j["guide"] or "job-failed"})

    ps = containers()
    apps = []
    for cid, c in comps.items():
        for svc in c.get("services", []):
            r = ps.get(svc, {})
            state, health = r.get("State", "unknown" if not ps else "missing"), r.get("Health", "")
            apps.append({"service": svc, "component": cid, "title": c.get("title", cid), "state": state,
                         "health": health, "status": r.get("Status", ""), "restart": svc in allowed["restart"]})
            if ps and (state != "running" or health == "unhealthy"):
                attention.append({"kind": "app", "id": svc, "title": f"{svc}: {health or state}",
                                  "guide": "app-down"})

    certs = []
    ip, domain = env.get("NETBIRD_IP", ""), env.get("DOMAIN", "")
    if ip and domain:
        for n in sorted({n for c in comps.values() for n in c.get("peer_names", [])}):
            days = cert_days(ip, f"{n}.{domain}")
            certs.append({"name": f"{n}.{domain}", "days": days})
            if days is None or days < CERT_WARN_DAYS:
                attention.append({"kind": "cert", "id": n, "guide": "certificate",
                                  "title": f"{n}.{domain}: " + ("no valid certificate" if days is None else f"expires in {days} days")})

    hb_file = HERE / "site/status/heartbeat.json"
    if hb_file.exists():
        hb = json.loads(hb_file.read_text())
        if (hb.get("outside") or {}).get("status") in ("down", "grace"):
            attention.append({"kind": "outside", "id": "outside", "title": "Not reachable from outside", "guide": "site-down"})
        for d in hb.get("disks", []):
            if d.get("total") and d["free"] / d["total"] < DISK_WARN:
                attention.append({"kind": "disk", "id": d["mount"], "guide": "disk-full",
                                  "title": f"{d.get('label') or d['mount']}: {d['free'] * 100 // d['total']}% free"})

    return {"time": int(time.time()), "jobs": jobs, "apps": apps, "certs": certs,
            "attention": attention, "allowed": access.names(allowed)}


def main() -> None:
    obj = report(render.read_env(HERE / ".env"))
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / ".console.tmp"
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.chmod(0o644)
    tmp.replace(DATA / "console.json")


if __name__ == "__main__":
    main()
