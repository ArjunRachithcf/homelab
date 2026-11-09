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


class Recyclarr(unittest.TestCase):
    def config(self, **extra):
        env = render.read_env(ROOT / "tests/fixtures/full.env") | extra
        return configure.outputs(env)[ROOT / "recyclarr/configs/recyclarr.yml"].decode()

    def test_the_guides_own_without_choices(self):
        text = self.config()
        self.assertIn("# sizes: TRaSH Guides' (MOVIE_SIZE_MB_PER_MIN empty)", text)
        self.assertIn("# sizes: TRaSH Guides' (EPISODE_SIZE_MB_PER_MIN empty)", text)
        self.assertNotIn("qualities:", text)
        self.assertNotIn("custom_formats:", text)
        self.assertNotIn("__", text)

    def test_sizes_and_x265_in_each_section(self):
        text = self.config(MOVIE_SIZE_MB_PER_MIN="10/40/70", EPISODE_SIZE_MB_PER_MIN="7/20/35", DOWNLOADS_PREFER_X265="true")
        radarr, sonarr = text.split("\nsonarr:\n")
        for section, line, cf, profile in (
                (radarr, "{name: Bluray-1080p, min: 10, preferred: 40, max: 70}",
                 "dc98083864ea246d05a42df0d05f81cc", "d1d67249d3890e49bc12e275d989a7e9"),
                (sonarr, "{name: WEBDL-720p, min: 7, preferred: 20, max: 35}",
                 "47435ece6b99a0b477caf360e79ba0bb", "72dae194fc92bf828f32cde7744e51a1")):
            self.assertEqual(section.count("        - {name: "), len(configure.SIZED))
            self.assertIn(line, section)
            # the sizes under quality_definition, the scores under the instance, after its profile
            self.assertLess(section.index("quality_definition:"), section.index("      qualities:"))
            self.assertLess(section.index("      qualities:"), section.index("    media_management:"))
            self.assertIn(f"      - trash_ids: [{cf}]   # x265 (HD)", section)
            self.assertIn(f"          - trash_id: {profile}\n            score: {configure.X265_SCORE}", section)
            self.assertLess(section.index("    quality_profiles:"), section.index("    custom_formats:"))

    def test_sizes_must_rise(self):
        for bad in ("40/10/70", "10/40", "big", "10/40/70/90"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                self.config(MOVIE_SIZE_MB_PER_MIN=bad)


if __name__ == "__main__":
    unittest.main()
