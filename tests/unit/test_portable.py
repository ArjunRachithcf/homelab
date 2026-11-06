"""lib/portable.sh: a python3 3.11+ ahead of an older one, without moving it ahead of anything else in PATH."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(Path("/usr/bin/python3").exists() and subprocess.run(
    ["/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"]).returncode == 0,
    "needs a python3 3.11+ in /usr/bin")
class Portable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stubs, self.old = self.tmp / "stubs", self.tmp / "oldpy"
        for d, name, body in ((self.stubs, "docker", "echo stub"),
                              (self.old, "python3", "exit 1")):   # an activated env's python3 3.10
            d.mkdir()
            (d / name).write_text(f"#!/bin/sh\n{body}\n")
            (d / name).chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def resolve(self, path):
        r = subprocess.run(["bash", "-c", 'source lib/portable.sh; command -v python3; docker; echo "$PATH"'],
                           cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, PATH=path), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_stub_before_old_python_keeps_winning(self):
        # A test's stub docker first: the real docker in /usr/bin must not overtake it (it would reach
        # the live stack)
        py, docker, path = self.resolve(f"{self.stubs}:{self.old}:/usr/bin:/bin")
        self.assertEqual(py, "/usr/bin/python3")
        self.assertEqual(docker, "stub")
        self.assertEqual(path.split(":")[:3], [str(self.stubs), "/usr/bin", str(self.old)])

    def test_good_python_changes_nothing(self):
        path = f"{self.stubs}:/usr/bin:/bin"
        self.assertEqual(self.resolve(path)[2], path)


if __name__ == "__main__":
    unittest.main()
