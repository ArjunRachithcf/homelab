"""setup/bootstrap.sh: clones a ref, checks the commit, leaves an existing checkout alone."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIT = ["git", "-c", "user.email=t@example.org", "-c", "user.name=t"]


class Bootstrap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name) / "repo"
        cls.repo.mkdir()
        (cls.repo / "homelab").write_text("#!/bin/sh\n")
        (cls.repo / "homelab").chmod(0o755)
        for cmd in (["init", "-q"], ["add", "homelab"], ["commit", "-qm", "x"], ["tag", "v1"]):
            subprocess.run(GIT + cmd, cwd=cls.repo, check=True)
        cls.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cls.repo, capture_output=True, text=True).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def boot(self, *args, dest="dest"):
        d = Path(self.tmp.name) / dest
        r = subprocess.run(["bash", str(ROOT / "setup/bootstrap.sh"), "--repo", f"file://{self.repo}", "--dir", str(d), *args],
                           capture_output=True, text=True, timeout=60, env=dict(os.environ, HOME=self.tmp.name))
        return r, d

    def test_clone_and_verify(self):
        r, d = self.boot("--ref", "v1", "--commit", self.sha, dest="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((d / "homelab").is_file())
        self.assertEqual(r.stdout.strip().splitlines()[-1], f"HOMELAB_DIR={d}")
        # Again: the checkout is left alone
        r, _ = self.boot("--ref", "v1", dest="ok")
        self.assertIn("already a checkout", r.stdout)

    def test_wrong_commit_removed(self):
        r, d = self.boot("--ref", "v1", "--commit", "0" * 40, dest="bad")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not the expected", r.stderr)
        self.assertFalse(d.exists())

    def test_refusals(self):
        for args in (["--repo", "http://example.org/x"], ["--ref", "v1;rm"], ["--commit", "abc"]):
            r = subprocess.run(["bash", str(ROOT / "setup/bootstrap.sh"), "--repo", f"file://{self.repo}", *args],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
