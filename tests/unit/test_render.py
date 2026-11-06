"""lib/render.py: placeholders, derived keys, refusals."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import render  # noqa: E402


class Render(unittest.TestCase):
    def v(self, **env):
        base = {"DOMAIN": "example.org", "DOMAIN_ALIAS": "example.net", "BRAND": "Smith", "BRAND_SLUG": "smith"}
        base.update(env)
        return render.values(base)

    def test_domains_re(self):
        self.assertEqual(render.domains_re("example.ch", "example.com"), r"example\.(ch|com)")
        self.assertEqual(render.domains_re("a.org", "b.net"), r"(a\.org|b\.net)")
        rx = re.compile(render.domains_re("my-home.org", "other.net"))
        self.assertTrue(rx.fullmatch("my-home.org") and rx.fullmatch("other.net"))
        self.assertFalse(rx.fullmatch("myXhome.org") or rx.fullmatch("my-homeXorg"))

    def test_derived(self):
        v = self.v(NETBIRD_PROXY_PORT="")
        self.assertEqual(v["PUBLIC_PORT"], "9443")
        self.assertEqual(v["APP_ID"], "org.example.family")
        self.assertEqual(self.v(ADMIN_USER="jane")["ADMIN_LOGIN"], "jane")
        self.assertEqual(self.v(ADMIN_USER="jane", ADMIN_LOGIN="JaneS")["ADMIN_LOGIN"], "JaneS")

    def test_render_and_refuse(self):
        v = self.v()
        self.assertEqual(render.render("https://home.__DOMAIN__:__PUBLIC_PORT__/", v, "t"), "https://home.example.org:9443/")
        with self.assertRaisesRegex(SystemExit, "unknown placeholder"):
            render.render("__AK_SECRET_KEY__", v, "t")
        with self.assertRaisesRegex(SystemExit, "empty in .env: ADMIN_NAME"):
            render.render("__ADMIN_NAME__", v, "t")
        # Python dunders and lowercase are not placeholders
        self.assertEqual(render.render("__init__ x__y", v, "t"), "__init__ x__y")

    def test_no_secret_allowed(self):
        for k in render.ALLOWED:
            self.assertNotRegex(k, "SECRET|PASS|TOKEN|KEY$|_KEY_")

    def test_read_env_like_scripts(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("A='x y'\nB=\"q\"\nC=1\nC=2\n# D=no\nE='it'\\''s'\n")
        env = render.read_env(Path(f.name))
        self.assertEqual((env["A"], env["B"], env["C"], env["E"]), ("x y", "q", "2", "it's"))
        self.assertNotIn("D", env)


if __name__ == "__main__":
    unittest.main()
