"""lib/configure.py: what it writes, from the example instance (tests/fixtures/full.env) and from .env.example."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import configure  # noqa: E402
import render  # noqa: E402


class Configure(unittest.TestCase):
    def test_instance(self):
        out = configure.outputs(render.read_env(ROOT / "tests/fixtures/full.env"))
        rel = {str(p.relative_to(ROOT)) for p in out}
        for must in ["authentik/templates/example/base.html", "authentik/mail-logo.png", "site/mark.png",
                     "site/manifest.webmanifest", "family-app/src-tauri/tauri.conf.json", "family-app/ui/index.html",
                     "family-app/src-tauri/icons/tray.png"]:
            self.assertIn(must, rel)
        for path, data in out.items():
            if path.suffix in (".html", ".txt", ".json", ".webmanifest"):
                self.assertNotIn(b"__", data.replace(b"__init__", b""), path)

    def test_fresh_install_defaults(self):
        env = render.read_env(ROOT / ".env.example")
        out = configure.outputs(env)  # BRAND=Home, branding/default
        self.assertIn(ROOT / "authentik/templates/home/base.html", out)
        self.assertEqual(out[ROOT / "site/mark.png"], (ROOT / "branding/default/site/mark.png").read_bytes())

    def test_bad_slug(self):
        env = render.read_env(ROOT / "tests/fixtures/full.env") | {"BRAND_SLUG": "Not OK"}
        with self.assertRaises(SystemExit):
            configure.outputs(env)

    def test_branding_sets_complete(self):
        names = lambda d: sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())  # noqa: E731
        for d in (ROOT / "branding").iterdir():
            if d.is_dir():
                self.assertEqual(names(d), names(ROOT / "branding/default"), d.name)


if __name__ == "__main__":
    unittest.main()
