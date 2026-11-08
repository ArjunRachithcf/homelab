#!/usr/bin/env python3
"""~/homelab/paperless/originals.py: Paperless' documents as plain files, for their copy in Proton Drive
(backup-photos.sh, RUNBOOK.md step 9), from backup.sh's hourly export.

  python3 paperless/originals.py EXPORT STAGE

Makes STAGE/paperless/<owner>/<the exported name> anew: each document's original, as it was scanned or uploaded, a
hard link into EXPORT (no space; a copy across filesystems), sorted by its owner's Paperless username ("no owner" for
none). Nothing else of the export: never its manifest, which holds the accounts' password hashes, nor the archived
PDFs or thumbnails (the encrypted backups have everything). A name that isn't a plain file name is skipped. One line
per owner. Proton keeps what Paperless no longer has: backup-photos.sh doesn't mirror deletions.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def stage(export: Path, stage_dir: Path) -> tuple[dict[str, int], list[str]]:
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    users = {o["pk"]: o["fields"]["username"] for o in manifest if o.get("model") == "auth.user"}
    tree = stage_dir / "paperless"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    os.chmod(stage_dir, 0o700)
    counts: dict[str, int] = {}
    skipped: list[str] = []
    for o in manifest:
        if o.get("model") != "documents.document":
            continue
        name = o.get("__exported_file_name__") or ""
        src = export / name
        if not name or Path(name).name != name or name.startswith(".") or not src.is_file():
            skipped.append(name or f"document {o.get('pk')}")
            continue
        owner = users.get(o["fields"].get("owner"), "no owner")
        if Path(owner).name != owner or owner.startswith("."):
            owner = "no owner"
        (tree / owner).mkdir(exist_ok=True)
        try:
            os.link(src, tree / owner / name)
        except OSError:
            shutil.copy2(src, tree / owner / name)
        counts[owner] = counts.get(owner, 0) + 1
    return counts, skipped


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    counts, skipped = stage(Path(argv[0]), Path(argv[1]))
    for owner, n in sorted(counts.items()):
        print(f"✓ {owner}: {n} document(s)")
    for name in skipped:
        print(f"· skipped (no plain file): {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
