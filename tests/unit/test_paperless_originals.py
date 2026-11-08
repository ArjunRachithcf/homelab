"""paperless/originals.py (Paperless' documents as plain files for Proton Drive) and their part of backup-photos.sh,
run in a copy with a stub Proton Drive CLI (and a stub date for Sunday's restore test; DOCKER_HOST to nowhere)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "paperless"))
import originals  # noqa: E402

# Users 1 and 2, three documents (the third without owner), and what else an export holds: archived PDFs,
# thumbnails, the manifest (password hashes!) and an old export name the manifest no longer has
MANIFEST = [
    {"model": "auth.user", "pk": 1, "fields": {"username": "alex", "password": "pbkdf2_sha256$secret-hash"}},
    {"model": "auth.user", "pk": 2, "fields": {"username": "sam", "password": "pbkdf2_sha256$another-hash"}},
    {"model": "documents.document", "pk": 10, "fields": {"owner": 1, "title": "Rent"},
     "__exported_file_name__": "2026-11-01 Landlord Rent.pdf", "__exported_archive_name__": "2026-11-01 Landlord Rent-archive.pdf",
     "__exported_thumbnail_name__": "2026-11-01 Landlord Rent-thumbnail.webp"},
    {"model": "documents.document", "pk": 11, "fields": {"owner": 2, "title": "Contract"},
     "__exported_file_name__": "2026-10-03 Employer Contract.docx",
     "__exported_thumbnail_name__": "2026-10-03 Employer Contract-thumbnail.webp"},
    {"model": "documents.document", "pk": 12, "fields": {"owner": None, "title": "Manual"},
     "__exported_file_name__": "2024-02-02 Manual.pdf"},
    {"model": "documents.document", "pk": 13, "fields": {"owner": 1, "title": "Escape"},
     "__exported_file_name__": "../escape.pdf"},
]


def make_export(d: Path) -> Path:
    e = d / "export"
    e.mkdir()
    (e / "manifest.json").write_text(json.dumps(MANIFEST))
    (e / "metadata.json").write_text("{}")
    for o in MANIFEST:
        for k in ("__exported_file_name__", "__exported_archive_name__", "__exported_thumbnail_name__"):
            if o.get(k) and "/" not in o[k]:
                (e / o[k]).write_text(f"content of {o[k]}")
    (e / "2026-11-01 Landlord Old title.pdf").write_text("an old export name")
    (d / "escape.pdf").write_text("outside the export")
    return e


class Originals(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.export = make_export(self.tmp)
        self.stage = self.tmp / "stage"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def files(self):
        return sorted(str(p.relative_to(self.stage)) for p in self.stage.rglob("*") if p.is_file())

    def test_each_original_by_owner_nothing_else(self):
        counts, skipped = originals.stage(self.export, self.stage)
        self.assertEqual(self.files(), ["paperless/alex/2026-11-01 Landlord Rent.pdf",
                                        "paperless/no owner/2024-02-02 Manual.pdf",
                                        "paperless/sam/2026-10-03 Employer Contract.docx"])
        self.assertEqual(counts, {"alex": 1, "sam": 1, "no owner": 1})
        self.assertEqual(skipped, ["../escape.pdf"])
        # hard links: no space, the very file
        self.assertEqual(os.stat(self.stage / "paperless/alex/2026-11-01 Landlord Rent.pdf").st_ino,
                         os.stat(self.export / "2026-11-01 Landlord Rent.pdf").st_ino)
        self.assertEqual(os.stat(self.stage).st_mode & 0o777, 0o700)
        for f in self.stage.rglob("*"):
            if f.is_file():
                self.assertNotIn("hash", f.read_text())

    def test_made_anew_each_time(self):
        originals.stage(self.export, self.stage)
        m = [o for o in MANIFEST if o.get("pk") != 11]
        (self.export / "manifest.json").write_text(json.dumps(m))
        originals.stage(self.export, self.stage)
        self.assertNotIn("paperless/sam/2026-10-03 Employer Contract.docx", self.files())

    def test_cli(self):
        r = subprocess.run([sys.executable, str(ROOT / "paperless/originals.py"), str(self.export), str(self.stage)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["✓ alex: 1 document(s)", "✓ no owner: 1 document(s)", "✓ sam: 1 document(s)"])
        self.assertIn("skipped (no plain file): ../escape.pdf", r.stderr)


# The stub Proton Drive CLI: a folder stands for Proton (REMOTE_ROOT); every call is logged
PD = r"""#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ["REMOTE_ROOT"])
a = sys.argv[1:]
with open(os.environ["PD_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:2] == ["filesystem", "upload"]:
    srcs, dest = [x for x in a[2:-1] if not x.startswith("-") and x not in ("create-new-revision", "merge")], a[-1]
    for s in srcs:
        s = Path(s)
        target = root / dest.lstrip("/") / s.name
        if s.is_dir():
            shutil.copytree(s, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, target)
elif a[:2] == ["filesystem", "download"]:
    src, dest = root / a[-2].lstrip("/"), Path(a[-1])
    if not src.is_file() or src.name == os.environ.get("PD_LOSE"):
        sys.exit(1)
    shutil.copy2(src, dest / src.name)
elif a[:2] == ["filesystem", "list"]:
    print("[]")
"""


class BackupPhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        box = self.box = self.tmp / "box"
        box.mkdir()
        for rel in ("backup-photos.sh", "backup-retention.sh"):
            shutil.copy2(ROOT / rel, box / rel)
        shutil.copytree(ROOT / "lib", box / "lib")
        shutil.copytree(ROOT / "paperless", box / "paperless")
        shutil.copytree(ROOT / "setup", box / "setup")
        (box / ".env").write_text("COMPONENTS=core,mesh,paperless,backups,offsite\n")
        make_export(box)
        (box / "backups").mkdir()
        home = self.home = self.tmp / "home"
        pd = home / ".local/opt/proton-drive/proton-drive"
        pd.parent.mkdir(parents=True)
        pd.write_text(PD)
        pd.chmod(0o755)
        self.remote = self.tmp / "proton"
        self.log = self.tmp / "pd.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # Sunday for the restore test (date +%u), anything else as usual
        real_date = shutil.which("date")
        (self.bin / "date").write_text(f'#!/bin/sh\n[ "$1" = "+%u" ] && {{ echo "${{FAKE_DAY:-1}}"; exit 0; }}\nexec {real_date} "$@"\n')
        (self.bin / "date").chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_it(self, mode, day="1", lose=""):
        env = dict(os.environ, HOME=str(self.home), REMOTE_ROOT=str(self.remote), PD_LOG=str(self.log), FAKE_DAY=day, PD_LOSE=lose,
                   PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_HOST="unix:///nonexistent")
        env.pop("STATE_DIRECTORY", None)
        r = subprocess.run(["bash", str(self.box / "backup-photos.sh"), mode], capture_output=True, text=True,
                           env=env, cwd=self.box, timeout=120)
        return r

    def proton(self):
        return sorted(str(p.relative_to(self.remote)) for p in self.remote.rglob("*") if p.is_file())

    def test_quick_run_takes_the_documents(self):
        r = self.run_it("quick")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.proton(), ["my-files/homelab/paperless/alex/2026-11-01 Landlord Rent.pdf",
                                         "my-files/homelab/paperless/no owner/2024-02-02 Manual.pdf",
                                         "my-files/homelab/paperless/sam/2026-10-03 Employer Contract.docx"])
        self.assertIn("Paperless documents → /my-files/homelab/paperless", r.stdout)
        # nothing of the manifest's
        for f in self.remote.rglob("*"):
            if f.is_file():
                self.assertNotIn("hash", f.read_text())

    def test_deletions_stay_in_proton_and_sunday_restore_test(self):
        self.run_it("quick")
        m = [o for o in MANIFEST if o.get("pk") != 12]
        (self.box / "export/manifest.json").write_text(json.dumps(m))
        r = self.run_it("all", day="7")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("my-files/homelab/paperless/no owner/2024-02-02 Manual.pdf", self.proton())
        self.assertIn("Paperless restore test: 2 file(s) identical in Proton.", r.stdout)
        # Proton losing one: the test says so and the run fails
        r = self.run_it("all", day="7", lose="2026-10-03 Employer Contract.docx")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("✗ Paperless restore test: 1 of 20 files missing or different in Proton", r.stderr)

    def test_not_without_paperless(self):
        (self.box / ".env").write_text("COMPONENTS=core,mesh,backups,offsite\n")
        r = self.run_it("quick")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("paperless", "".join(self.proton()))


if __name__ == "__main__":
    unittest.main()
