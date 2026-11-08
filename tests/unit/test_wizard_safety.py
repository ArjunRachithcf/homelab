"""Wizard steps that could destroy something, run in a copy with the disk tools stubbed (DOCKER_HOST to nowhere):
- immich:disk never offers the wipe for a disk that already holds the photo library and is only unmounted, and its
  check knows the photos disk also behind an encryption mapping;
- the Memos check asks with validateOnly, so it can't create a user (an admin, on an empty Memos)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUBS = {
    # $PHOTOS isn't mounted: no source, and findmnt's own "not found" status
    "findmnt": "#!/bin/sh\nexit 1\n",
    # The disk: -nP lists its devices' UUID and LABEL ($LSBLK_PAIRS); a whole disk for -dno TYPE
    "lsblk": "#!/bin/sh\ncase \"$*\" in *-nP*) printf '%s\\n' \"$LSBLK_PAIRS\" ;; *TYPE*) echo disk ;; esac\n",
    "sudo": "#!/bin/sh\necho \"sudo $*\" >> \"$STUB_LOG\"\nexit 1\n",
    "docker": "#!/bin/sh\necho \"docker $*\" >> \"$STUB_LOG\"\n",
    "sleep": "#!/bin/sh\n",
    # the wizard's closing checks: no network here, so no waiting on timeouts
    "curl": "#!/bin/sh\nexit 7\n",
    "dig": "#!/bin/sh\nexit 9\n",
    "systemctl": "#!/bin/sh\nexit 1\n",
}


def disk_stage(pairs: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as d:
        box = Path(d) / "box"
        box.mkdir()
        shutil.copy(ROOT / "immich-wizard.sh", box)
        shutil.copytree(ROOT / "lib", box / "lib")
        (box / ".env").write_text(
            f"DOMAIN=example.org\nDOMAIN_ALIAS=example.net\nNETBIRD_IP=100.64.0.5\nIMMICH_UPLOAD_DIR={d}/photos\n"
            "PHOTOS_DISK=/dev/disk/by-id/nvme-EXAMPLE_0001\nPHOTOS_UUID=0000-1111\nCOMPONENTS=core,mesh,photos,second-disk\n"
            f"PHOTOS_COPY_DIR={d}/photos-copy\n")
        bin_ = Path(d) / "bin"
        bin_.mkdir()
        for name, body in STUBS.items():
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        log = Path(d) / "calls.log"
        # HOME too: the closing checks run the proton-drive CLIs under $HOME (the real session otherwise)
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOME=d, HOMELAB_UI="protocol", HOMELAB_STAGES="disk",
                   HOMELAB_STAGES_TOTAL="1", TERM="dumb", STUB_LOG=str(log), DOCKER_HOST="unix:///nonexistent",
                   LSBLK_PAIRS=pairs)
        r = subprocess.run(["bash", str(box / "immich-wizard.sh")], input="\n" * 5,
                           capture_output=True, text=True, env=env, timeout=60)
        return r.stdout + r.stderr, log.read_text() if log.exists() else ""


class ImmichDisk(unittest.TestCase):
    def test_the_librarys_disk_is_never_offered_for_the_wipe(self):
        for pairs in ('UUID="" LABEL=""\nUUID="0000-1111" LABEL=""',       # PHOTOS_UUID
                      'UUID="" LABEL=""\nUUID="9999-9999" LABEL="photos"'):  # the label immich:disk gives it
            out, calls = disk_stage(pairs)
            self.assertIn("already holds the photo library", out, pairs)
            self.assertIn("sudo mount -a", out)
            self.assertNotRegex(out, r'"t":"danger"|can\'t be undone', pairs)
            self.assertNotIn("sudo", calls, "nothing ran with sudo")

    def test_another_disk_goes_on_to_the_checks(self):
        # Not the library: the stage carries on (here to the by-id check, since the stub disk isn't a block device)
        out, _ = disk_stage('UUID="" LABEL=""\nUUID="2222-3333" LABEL="data"')
        self.assertNotIn("already holds the photo library", out)
        self.assertIn("Set PHOTOS_DISK", out)


class DiskCheck(unittest.TestCase):
    """immich:disk's check: $PHOTOS mounted from the disk's first partition, directly or through an encryption
    (dm-crypt) mapping on it, writable, and in fstab"""

    def disk_ok(self, source, chain, in_fstab=True, writable=True):
        fn = re.search(r"^disk_ok\(\).*?; \}$", (ROOT / "immich-wizard.sh").read_text(), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as d:
            photos = Path(d) / "photos"
            photos.mkdir()
            if not writable:
                photos.chmod(0o555)
            fstab = Path(d) / "fstab"
            fstab.write_text(f"/srv/nvme1/photos {photos if in_fstab else '/srv/other'} none bind,nofail 0 0\n")
            bin_ = Path(d) / "bin"
            bin_.mkdir()
            # findmnt: the mount's source; readlink: the by-id link of the disk's first partition; lsblk -s: the
            # devices from that source down to the disk
            stubs = {"findmnt": f"#!/bin/sh\necho '{source}'\n", "readlink": "#!/bin/sh\necho /dev/nvme1n1p1\n",
                     "lsblk": "#!/bin/sh\nprintf '%s\\n' " + " ".join(f"'{x}'" for x in chain) + "\n"}
            for name, body in stubs.items():
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", PHOTOS=str(photos),
                       DISK="/dev/disk/by-id/nvme-EXAMPLE_0001", FSTAB=str(fstab), DOCKER_HOST="unix:///nonexistent")
            r = subprocess.run(["bash", "-c", fn + "\ndisk_ok"], env=env, capture_output=True, text=True, timeout=30)
            photos.chmod(0o755)
            return r.returncode == 0

    def test_the_partition_itself(self):
        self.assertTrue(self.disk_ok("/dev/nvme1n1p1[/photos]", ["/dev/nvme1n1p1", "/dev/nvme1n1"]))

    def test_through_an_encryption_mapping_on_it(self):
        self.assertTrue(self.disk_ok("/dev/mapper/photos[/photos]",
                                     ["/dev/mapper/photos", "/dev/nvme1n1p1", "/dev/nvme1n1"]))

    def test_anything_else_isnt_the_photos_disk(self):
        self.assertFalse(self.disk_ok("/dev/nvme0n1p3[/root/srv/photos]", ["/dev/nvme0n1p3", "/dev/nvme0n1"]))
        self.assertFalse(self.disk_ok("", []))   # not mounted
        self.assertFalse(self.disk_ok("/dev/nvme1n1p1[/photos]", ["/dev/nvme1n1p1", "/dev/nvme1n1"], in_fstab=False))
        if os.geteuid():
            self.assertFalse(self.disk_ok("/dev/nvme1n1p1[/photos]", ["/dev/nvme1n1p1", "/dev/nvme1n1"],
                                          writable=False))


class MemosCheck(unittest.TestCase):
    def test_the_probe_only_validates(self):
        src = (ROOT / "family-wizard.sh").read_text()
        probe = re.search(r"^memos_pw_shut\(\) \{\n(.*?)^\}", src, re.S | re.M).group(1)
        self.assertIn("CreateUser", probe)
        self.assertIn('"validateOnly":true', probe)


if __name__ == "__main__":
    unittest.main()
