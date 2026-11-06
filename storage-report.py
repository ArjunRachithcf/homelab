#!/usr/bin/env python3
"""~/homelab/storage-report.py: what uses the disks, and what each cleanup would free (RUNBOOK.md 7e,
"Details"). Hourly by homelab-storage-report.timer (idle I/O; du on the photo library takes a while),
and by ops-run.sh after a cleanup. Read-only; writes ops/data/storage.json for the Disk sheet.

The cleanups are the fixed ones ops-run.sh knows (never free-form):
  clean-docker   images no container uses, older than 7 days, and the build cache
  clean-bins     the second copies' recycle bins (…-copy/deleted/<date>) older than 7 days (kept 30)
  clean-backups  hourly archives older than 24 h thinned to one a day (backup.sh: 48 h)
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import jobs  # noqa: E402  (the job registry: the cleanups' titles)
DATA = HERE / "ops" / "data"
NVME1 = Path(os.environ.get("NVME1_DIR", "/srv/nvme1"))
PHOTOS_COPY = Path(os.environ.get("PHOTOS_COPY_DIR", "/srv/photos-copy"))
FILES_COPY = Path(os.environ.get("FILES_COPY_DIR", "/srv/files-copy"))
BIN_DAYS = 7
BACKUP_DAYS = 14  # backup.sh keeps one a day for 14 days


def run(*cmd, timeout=600):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def du(path):
    """Bytes under path on its own filesystem (0 when missing)."""
    if not Path(path).exists():
        return 0
    out = run("du", "-sxb", str(path), timeout=1800)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return 0


UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def human_bytes(s):
    m = re.match(r"([\d.]+)\s*([kKMGT]?B)", s or "")
    return int(float(m[1]) * UNITS[m[2]]) if m else 0


def docker():
    """Total on disk, and what clean-docker would free (images: an upper bound, layers are shared)."""
    total, build_cache = 0, 0
    for line in run("docker", "system", "df", "--format", "{{json .}}").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        total += human_bytes(row.get("Size"))
        if row.get("Type") == "Build Cache":
            build_cache = human_bytes(row.get("Reclaimable"))
    used = set(run("docker", "ps", "-aq", "--no-trunc").split())
    used_images = set(run("docker", "inspect", "-f", "{{.Image}}", *used).split()) if used else set()
    cutoff = time.time() - 7 * 86400
    images = 0
    ids = run("docker", "image", "ls", "-q", "--no-trunc").split()
    if ids:
        for line in run("docker", "image", "inspect", "-f", "{{.Id}} {{.Size}} {{.Created}}", *set(ids)).splitlines():
            iid, size, created = line.split(" ", 2)
            try:
                made = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            if iid not in used_images and made < cutoff:
                images += int(size)
    return total, images + build_cache


def bins():
    """Bytes in the copies' recycle bins older than BIN_DAYS."""
    cutoff = time.time() - BIN_DAYS * 86400
    total = 0
    for root in (PHOTOS_COPY / "deleted", FILES_COPY / "deleted"):
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    total += du(d)
    return total


def thin_backups():
    names = "\n".join(p.name for p in (HERE / "backups").glob("homelab-*.tar.age"))
    out = subprocess.run([str(HERE / "backup-retention.sh"), "24", str(BACKUP_DAYS)], input=names,
                         capture_output=True, text=True).stdout
    return sum((HERE / "backups" / n).stat().st_size for n in out.split() if (HERE / "backups" / n).exists())


def disk(mount):
    st = os.statvfs(mount)
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    return total, free


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    docker_total, docker_free = docker()

    sys_total, sys_free = disk("/")
    sys_used = sys_total - sys_free
    sys_areas = [
        ["Media", du("/srv/media")],
        ["Photos copy", du(PHOTOS_COPY)],
        ["Files copy", du(FILES_COPY)],
        ["Backups", du(HERE / "backups")],
        ["Docker", docker_total],
    ]
    # The rest: the system and home folders (du on /home overcounts on btrfs: shared and compressed blocks)
    sys_areas.append(["System and home folders", max(0, sys_used - sum(a[1] for a in sys_areas))])

    disks = [{"label": "System SSD", "mount": "/", "total": sys_total, "free": sys_free,
              "areas": [{"name": n, "bytes": b} for n, b in sys_areas]}]
    if NVME1.is_dir() and os.path.ismount(NVME1):
        n_total, n_free = disk(NVME1)
        n_areas = [["Photos", du(NVME1 / "photos")], ["Files", du(NVME1 / "files")]]
        n_areas.append(["Other", max(0, n_total - n_free - sum(a[1] for a in n_areas))])
        disks.append({"label": "Photos and files NVMe", "mount": str(NVME1), "total": n_total, "free": n_free,
                      "areas": [{"name": n, "bytes": b} for n, b in n_areas],
                      # Snapshots are reflinks: they share blocks with Files until a file changes
                      "note": "File snapshots share space with Files (reflinks)."})

    titles = {k: a["title"] for k, a in jobs.load()["actions"].items()}
    cleanups = [
        {"job": "clean-docker", "title": titles["clean-docker"],
         "detail": "Images no container uses, older than 7 days; they download again if needed.",
         "frees": docker_free},
        {"job": "clean-bins", "title": titles["clean-bins"],
         "detail": f"Photos and files deleted more than {BIN_DAYS} days ago, from {PHOTOS_COPY} and {FILES_COPY}. "
                   "Normally kept 30 days.",
         "frees": bins()},
        {"job": "clean-backups", "title": titles["clean-backups"],
         "detail": "Archives older than 24 hours thinned to one a day (normally after 48 hours).",
         "frees": thin_backups()},
    ]
    obj = {"generated": now, "disks": disks, "cleanups": cleanups}
    tmp = DATA / ".storage.tmp"
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.chmod(0o644)
    tmp.replace(DATA / "storage.json")


if __name__ == "__main__":
    main()
