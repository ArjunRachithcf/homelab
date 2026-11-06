#!/usr/bin/env python3
"""~/homelab/ops-report.py: the data behind the home page's detail sheets (RUNBOOK.md 7e, "Details").

Run by heartbeat.sh after every heartbeat (every 5 min), as the admin, read-only towards everything but
ops/data/. Writes, each atomically:

  samples.jsonl       one line per heartbeat (ok, problems, outside status, free disk, backup time),
                      30 days kept
  outside.json        uptime per range (24 h at 5 min, 7 d hourly, 30 d every 4 h), a timeline,
                      incidents, healthchecks.io's flips when the read-only key may read them
  backups.json        archives in backups/ (time, size), runs of homelab-backup / homelab-offbox from
                      the journal, the next scheduled runs
  disk-history.json   free space per disk over each range

The sheets fetch these through ops/server.py (admins only). storage-report.py writes storage.json
(hourly, du is slow); ops-run.sh writes jobs.json.
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "ops" / "data"
SAMPLES = DATA / "samples.jsonl"
KEEP = 30 * 86400
# range → (span, bucket)
RANGES = {"24h": (86400, 300), "7d": (7 * 86400, 3600), "30d": (30 * 86400, 4 * 3600)}
SLOT = 300  # one heartbeat every 5 min


def env_get(key):
    val = ""
    for line in (HERE / ".env").read_text().splitlines():
        if line.startswith(key + "="):
            val = line.split("=", 1)[1]
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        val = val[1:-1]
    return val


def write(name, obj):
    tmp = DATA / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.chmod(0o644)
    tmp.replace(DATA / name)


def add_sample(now):
    hb = json.loads((HERE / "site" / "status" / "heartbeat.json").read_text())
    line = {
        "t": hb["time"],
        "ok": hb["ok"],
        "problems": hb["problems"],
        "outside": (hb.get("outside") or {}).get("status"),
        "disks": [{"mount": d["mount"], "free": d["free"], "total": d["total"]} for d in hb.get("disks", [])],
        "backup": hb.get("backup_time", 0),
    }
    with SAMPLES.open("a") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")
    samples = load_samples()
    # Rewrite without the old ones about once a day (the first line is over a day past the limit)
    if samples and samples[0]["t"] < now - KEEP - 86400:
        kept = [s for s in samples if s["t"] >= now - KEEP]
        tmp = DATA / ".samples.tmp"
        tmp.write_text("".join(json.dumps(s, separators=(",", ":")) + "\n" for s in kept))
        tmp.replace(SAMPLES)
        samples = kept
    return samples


def load_samples():
    out = []
    if SAMPLES.exists():
        for line in SAMPLES.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return sorted(out, key=lambda s: s["t"])


def flips():
    """healthchecks.io's own up/down changes, if the read-only key may read them (else None)."""
    key = env_get("HEARTBEAT_API_KEY_RO")
    if not key:
        return None
    try:
        def get(url):
            req = urllib.request.Request(url, headers={"X-Api-Key": key})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        checks = get("https://healthchecks.io/api/v3/checks/").get("checks", [])
        check = next((c for c in checks if c.get("name") == "homelab"), checks[0] if checks else None)
        ident = check and (check.get("uuid") or check.get("unique_key"))
        if not ident:
            return None
        raw = get(f"https://healthchecks.io/api/v3/checks/{ident}/flips/?seconds={KEEP}")
        return [{"t": f["timestamp"], "up": bool(f["up"])} for f in (raw if isinstance(raw, list) else raw.get("flips", []))]
    except Exception:
        return None


def flip_uptime(fl, start, now):
    """Share of [start, now] healthchecks.io saw the check up (None before its first flip)."""
    if not fl:
        return None
    events = sorted((int(datetime.fromisoformat(f["t"]).timestamp()), f["up"]) for f in fl)
    if events[0][0] > start and events[0][0] > now - 60:
        return None
    up_time, state, t = 0, None, start
    for at, up in events:
        if at <= start:
            state = up
            continue
        if state is None:  # the range starts before the first flip: count from that flip on
            t, state = at, up
            continue
        if state:
            up_time += at - t
        t, state = at, up
    if state:
        up_time += now - t
    known = now - (events[0][0] if events[0][0] > start else start)
    return round(up_time / known, 4) if known > 0 else None


def outside(samples, now):
    by_slot = {s["t"] // SLOT: s for s in samples}
    fl = flips()
    ranges = {}
    for name, (span, bucket) in RANGES.items():
        start = now - span
        slots = range(start // SLOT + 1, now // SLOT + 1)
        known = [by_slot[i] for i in slots if i in by_slot]
        ok = sum(1 for s in known if s["ok"])
        state = {}
        for s in samples:
            if s["t"] >= start:
                b = s["t"] - s["t"] % bucket
                state[b] = "fail" if (not s["ok"] or state.get(b) == "fail") else "ok"
        timeline = [[b, state.get(b, "none")] for b in range(start - start % bucket, now, bucket)]
        # healthchecks.io's flips know when the box was off; our own samples only what ran
        from_flips = flip_uptime(fl, start, now)
        ranges[name] = {"uptime": from_flips if from_flips is not None else round(ok / max(1, len(known)), 4),
                        "source": "healthchecks.io" if from_flips is not None else "heartbeat",
                        "timeline": timeline}

    # Incidents: runs of failed heartbeats, and gaps (no heartbeat: PC off, no internet, Docker down)
    incidents, cur = [], None
    prev_t = None
    for s in samples:
        if prev_t is not None and s["t"] - prev_t > 3 * SLOT:
            incidents.append({"start": prev_t + SLOT, "end": s["t"], "kind": "gap",
                              "problems": ["No heartbeat (PC off, no internet, or the timer stopped)"]})
        if not s["ok"]:
            if cur is None:
                cur = {"start": s["t"], "end": s["t"] + SLOT, "kind": "fail", "problems": []}
            cur["end"] = s["t"] + SLOT
            for p in s["problems"]:
                if p not in cur["problems"]:
                    cur["problems"].append(p)
        elif cur is not None:
            incidents.append(cur)
            cur = None
        prev_t = s["t"]
    if cur is not None:
        cur["ongoing"] = True
        incidents.append(cur)

    hb = json.loads((HERE / "site" / "status" / "heartbeat.json").read_text())
    return {
        "generated": now,
        "now": hb.get("outside"),
        "ok": hb.get("ok"),
        "ranges": ranges,
        "incidents": sorted(incidents, key=lambda i: i["start"], reverse=True)[:30],
        "flips": fl,
        "since": samples[0]["t"] if samples else now,
    }


def journal_runs():
    """Start, end, result of each homelab-backup / homelab-offbox run in the last 30 days."""
    start_id, done_id = "7d4958e842da4a758f6c1cdc7b36dcc5", "39f53479d3a045ac8e11786248231fbf"
    cmd = ["journalctl", "-o", "json", "--since", "-30d", "--no-pager",
           "_PID=1", "UNIT=homelab-backup.service", "+", "_PID=1", "UNIT=homelab-offbox.service"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    open_runs, runs = {}, []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        unit = e.get("UNIT", "")
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000
        kind = "local" if unit.startswith("homelab-backup") else "proton"
        if e.get("MESSAGE_ID") == start_id:
            open_runs[unit] = t
        elif e.get("MESSAGE_ID") == done_id and unit in open_runs:
            started = open_runs.pop(unit)
            runs.append({"kind": kind, "start": started, "end": t, "ok": e.get("JOB_RESULT") == "done"})
    for unit, started in open_runs.items():
        runs.append({"kind": "local" if unit.startswith("homelab-backup") else "proton",
                     "start": started, "end": None, "ok": None})
    return sorted(runs, key=lambda r: r["start"], reverse=True)


def next_runs():
    try:
        out = subprocess.run(["systemctl", "list-timers", "--all", "-o", "json",
                              "homelab-backup.timer", "homelab-offbox.timer"],
                             capture_output=True, text=True, timeout=10).stdout
        return {t["unit"].split(".")[0].removeprefix("homelab-"): (t.get("next") or 0) // 1_000_000
                for t in json.loads(out or "[]")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def backups(now):
    archives = []
    for p in (HERE / "backups").glob("homelab-*.tar.age"):
        m = re.fullmatch(r"homelab-(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})\.tar\.age", p.name)
        st = p.stat()
        t = int(time.mktime(time.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M"))) if m else int(st.st_mtime)
        archives.append({"t": t, "size": st.st_size})
    archives.sort(key=lambda a: a["t"])
    return {"generated": now, "archives": archives, "runs": journal_runs()[:60], "next": next_runs()}


def disk_history(samples, now):
    ranges = {}
    for name, (span, bucket) in RANGES.items():
        points = {}
        for s in samples:
            if s["t"] < now - span:
                continue
            b = s["t"] - s["t"] % bucket
            points[b] = s["disks"]  # the last sample of each bucket
        ranges[name] = [[b, [d["free"] for d in disks]] for b, disks in sorted(points.items())]
    mounts = samples[-1]["disks"] if samples else []
    return {"generated": now, "disks": [{"mount": d["mount"], "total": d["total"]} for d in mounts], "ranges": ranges}


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    samples = add_sample(now)
    write("outside.json", outside(samples, now))
    write("backups.json", backups(now))
    write("disk-history.json", disk_history(samples, now))


if __name__ == "__main__":
    main()
