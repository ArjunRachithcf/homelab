"""The docs (plan Phase 6): docs/components.md is current, every relative link resolves, CLAUDE.md
imports the instance file, and an instance's own files stay out of git."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").rglob("*.md")), ROOT / "setup-app/README.md"]


class Docs(unittest.TestCase):
    def test_components_md_current(self):
        out = subprocess.run([sys.executable, str(ROOT / "lib/setup/engine.py"), "docs"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, (ROOT / "docs/components.md").read_text(), "run: python3 lib/setup/engine.py docs > docs/components.md")

    def test_links(self):
        for doc in DOCS:
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text()):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                self.assertTrue((doc.parent / target).exists(), f"{doc.relative_to(ROOT)} → {target}")

    def test_instance(self):
        self.assertIn("\n@INSTANCE.md\n", (ROOT / "CLAUDE.md").read_text())
        # Per machine, not in git: a clone mustn't come with someone else's instance instructions
        self.assertIn("\n/INSTANCE.md\n", (ROOT / ".gitignore").read_text())
        self.assertTrue((ROOT / "docs/instance/INSTANCE.example.md").is_file())
        # An instance's own repository is checked out at instance/ (docs/instance/README.md)
        for line in ("/instance/", "/RUNBOOK.md"):
            self.assertIn(f"\n{line}\n", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
