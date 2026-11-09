"""lib/setup/engine.py: resolve, plan, apply (secrets generated, never printed), export, stage selection."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/setup"))
sys.path.insert(0, str(ROOT / "lib"))
import engine  # noqa: E402
import render  # noqa: E402


def cli(*args, input=None):
    return subprocess.run([sys.executable, str(ROOT / "lib/setup/engine.py"), *args], capture_output=True, text=True, input=input)


class Engine(unittest.TestCase):
    comps = engine.components()

    def test_requires_and_gpu(self):
        r = engine.resolve(self.comps, ["terminal"], "none")
        self.assertIn("ssh-sso", r.enabled)
        self.assertIn("core", r.enabled)
        self.assertTrue(any("needs SSH" in n for n in r.notes))
        r = engine.resolve(self.comps, ["local-ai"], "none")
        self.assertTrue(any("NVIDIA" in e for e in r.errors))
        self.assertEqual(engine.resolve(self.comps, ["nope"], "nvidia").errors[0], "unknown component(s): nope")

    def test_retired_ids_read_as_their_successor(self):
        # A component merged into another (made up: none is retired today) still named in COMPONENTS
        with mock.patch.dict(engine.RETIRED, {"old-share": "share"}):
            on, guessed = engine.current(self.comps, {"COMPONENTS": "core,mesh,files,share,old-share"})
            self.assertEqual((on, guessed), (["core", "mesh", "files", "share"], False))
            self.assertEqual(engine.current(self.comps, {"COMPONENTS": "core,old-share"})[0], ["core", "share"])
            r = engine.resolve(self.comps, ["old-share"], "none")
            self.assertEqual(r.errors, [])
            self.assertIn("share", r.enabled)
            self.assertNotIn("old-share", r.enabled)
        for old, new in engine.RETIRED.items():
            self.assertNotIn(old, self.comps)
            self.assertIn(new, self.comps)

    def test_platforms(self):
        fam = engine.preset("family", self.comps)
        self.assertEqual(engine.resolve(self.comps, fam, "nvidia", "fedora").errors, [])
        errs = engine.resolve(self.comps, fam, "nvidia", "arch").errors
        self.assertEqual(len(errs), 2)  # ssh-sso and plex
        self.assertTrue(any("Plex" in e and "arch" in e for e in errs))
        self.assertEqual(len(engine.resolve(self.comps, fam, "nvidia", "ubuntu").errors), 1)  # ssh-sso
        self.assertEqual(engine.resolve(self.comps, engine.preset("minimal", self.comps), "none", "arch").errors, [])

    def test_apply_minimal(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text((ROOT / ".env.example").read_text())
            ans = Path(d) / "answers.toml"
            ans.write_text('preset = "minimal"\nadd = ["music"]\n[options]\nADMIN_USER = "jane"\nADMIN_NAME = "Jane Smith"\n'
                           'BACKUP_AGE_RECIPIENT = "age1qqq"\nDOMAIN = "smith.example"\n')
            r = cli("apply", "--answers", str(ans), "--env", str(env), "--gpu", "none")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            v = render.read_env(env)
            self.assertEqual(v["COMPONENTS"], "core,mesh,paperless,media-library,music,photos,backups")
            self.assertEqual(v["COMPOSE_PROFILES"], "paperless,music,photos")
            self.assertEqual((v["ADMIN_USER"], v["DOMAIN"]), ("jane", "smith.example"))
            for k in ("AK_SECRET_KEY", "PAPERLESS_SECRET_KEY", "NAVIDROME_PASSWORD_KEY", "IMMICH_DB_PASSWORD"):
                self.assertGreater(len(v[k]), 20, k)
                self.assertNotIn(v[k], r.stdout + r.stderr, k)
            self.assertTrue(v["IMMICH_DB_PASSWORD"].isalnum())
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            # Applying again changes no secret
            before = env.read_text()
            self.assertEqual(cli("apply", "--answers", str(ans), "--env", str(env), "--gpu", "none").returncode, 0)
            self.assertEqual(render.read_env(env)["AK_SECRET_KEY"], v["AK_SECRET_KEY"])
            self.assertEqual(env.read_text(), before)
            # Export: the choice and the non-secret options, never a secret
            out = cli("export", "--env", str(env)).stdout
            self.assertIn('ADMIN_USER = "jane"', out)
            self.assertNotIn(v["AK_SECRET_KEY"], out)
            self.assertIn('"music"', out)

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("")
            ans = Path(d) / "a.toml"
            ans.write_text('preset = "minimal"\n[options]\nDOMAIN = "not a domain"\nAK_SECRET_KEY = "x"\nNOPE = "1"\n')
            r = cli("apply", "--answers", str(ans), "--env", str(env), "--gpu", "none")
            self.assertEqual(r.returncode, 1)
            self.assertIn("DOMAIN: not a valid domain", r.stdout)
            self.assertIn("AK_SECRET_KEY: a secret", r.stdout)
            self.assertIn("NOPE: not an option", r.stdout)
            self.assertEqual(env.read_text(), "")  # nothing written

    def test_sizes(self):
        opt = {"type": "sizes"}
        for good in ("", "10/40/70", "7.5/20/35", "20/20/20"):
            self.assertIsNone(engine.check_value(opt, good), good)
        self.assertIn("each at least the one before", engine.check_value(opt, "40/10/70") or "")
        for bad in ("10/40", "big", "10 /40/70", "-1/2/3"):
            self.assertEqual(engine.check_value(opt, bad), "not a valid sizes", bad)

    def test_plan_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text((ROOT / ".env.example").read_text())
            before = env.read_text()
            cli("plan", "--preset", "family", "--env", str(env), "--gpu", "nvidia")
            self.assertEqual(env.read_text(), before)

    def test_stage_selection_in_wizards(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d) / "w.sh"
            w.write_text(f"""set -euo pipefail
source {ROOT}/lib/wizard.sh
TOTAL_STAGES=3
stage_a() {{ stage "A" || return 0; say "in a"; }}
stage_b() {{ stage "B" || return 0; say "in b"; }}
stage_c() {{ stage "C" || return 0; say "in c"; }}
stage_a; stage_b; stage_c
""")
            env = dict(os.environ, HOMELAB_UI="protocol", HOMELAB_STAGES="a,c", HOMELAB_STAGES_TOTAL="2")
            out = subprocess.run(["bash", str(w)], capture_output=True, text=True, env=env).stdout
            self.assertIn('"id":"a","n":1,"of":2', out)
            self.assertIn('"id":"c","n":2,"of":2', out)
            self.assertNotIn("in b", out)

    def test_infer_is_everything(self):
        # an install from before the setup engine never had an opt-in component (added since, only ever chosen)
        self.assertEqual(set(engine.infer(self.comps, {})), {c for c, d in self.comps.items() if not d.get("opt_in")})
        self.assertTrue({"downloads", "movies-tv"} <= {c for c, d in self.comps.items() if d.get("opt_in")})


if __name__ == "__main__":
    unittest.main()


class Json(unittest.TestCase):
    """--json for front ends (the setup app, the MCP server): no secret value, ever"""

    def test_catalogue_hides_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("COMPONENTS=core,mesh\nDOMAIN=example.org\nAK_SECRET_KEY='very-SECRET'\n")
            r = cli("components", "--json", "--env", str(env))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("very-SECRET", r.stdout)
        import json
        d = json.loads(r.stdout)
        self.assertEqual(d["chosen"], ["core", "mesh"])
        core = next(c for c in d["components"] if c["id"] == "core")
        opt = {o["key"]: o for o in core["options"]}
        self.assertEqual((opt["AK_SECRET_KEY"]["set"], opt["AK_SECRET_KEY"]["value"]), (True, None))
        self.assertEqual(opt["DOMAIN"]["value"], "example.org")
        self.assertEqual({p["id"] for p in d["presets"]}, {"minimal", "family", "everything"})
        # Every stage's title, for the app's list of steps: the domain filled in, nothing of the shell left
        titles = d["stage_titles"]
        self.assertEqual(set(titles), {f"{w}:{s}" for w, f in engine.WIZARDS.items()
                                       for s in re.findall(r"^stage_([a-z_]+)\(\)", (ROOT / f).read_text(), re.M)})
        self.assertEqual(titles["wizard:dns"], "DNS records at Infomaniak (runbook step 2)")
        self.assertEqual(titles["hardening:caa"], "Infomaniak: CAA on example.org (who may issue certificates)")
        self.assertEqual(titles["family:start"], "Start …")
        self.assertFalse([t for t in titles.values() if "$" in t or not t])

    def test_every_wizard_is_registered(self):
        # The engine's list, the runner's, ./homelab check's loop and the setup app's own copy (main.rs): a wizard
        # missing from one is a part the app can't start or a check nobody runs
        want = list(engine.WIZARDS)
        run = re.search(r"^WIZARDS = (\[.*\])$", (ROOT / "lib/setup/run.py").read_text(), re.M).group(1)
        self.assertEqual(json.loads(run), want)
        rust = re.search(r"const WIZARDS: &\[&str\] = &\[(.*?)\];", (ROOT / "setup-app/src-tauri/src/main.rs").read_text())
        self.assertEqual(re.findall(r'"([a-z]+)"', rust.group(1)), want)
        loop = re.search(r"^  for w in (wizard public-wizard .*); do$", (ROOT / "homelab").read_text(), re.M).group(1).split()
        self.assertEqual(loop, [engine.WIZARDS[w].removesuffix(".sh") for w in want])
        app = re.search(r"var WIZ = \{(.*?)\};", (ROOT / "setup-app/ui/app.js").read_text()).group(1)
        self.assertEqual(re.findall(r"(\w+): \"", app), want)
        for f in engine.WIZARDS.values():
            self.assertTrue(os.access(ROOT / f, os.X_OK), f)

    def test_plan_says_what_it_switches(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("COMPONENTS=core,mesh,paperless,photos\n")
            r = cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", "--platform", "fedora",
                    input='components = ["core", "mesh", "paperless", "backups"]\n')
            fresh = cli("plan", "--answers", "-", "--json", "--env", "/dev/null", "--gpu", "none",
                        input='components = ["core", "mesh", "paperless"]\n')
        p, f = json.loads(r.stdout), json.loads(fresh.stdout)
        self.assertEqual((p["switch_on"], p["switch_off"]), (["backups"], ["photos"]))
        self.assertIn("containers keep running", p["switch_off_means"])
        self.assertEqual((f["switch_on"], f["switch_off"], f["switch_off_means"]), ([], [], ""))  # a new machine

    def test_switch_baseline(self):
        import json
        ans = 'components = ["core", "mesh", "paperless"]\n'
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            # A new .env (as .env.example has them): nothing to switch off, and apply says nothing still runs
            env.write_text("COMPONENTS=\nAK_SECRET_KEY=\n")
            new = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout)
            applied = json.loads(cli("apply", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout.strip().splitlines()[-1])
            # An install from before the engine (Authentik's key, no COMPONENTS) ran everything
            env.write_text("AK_SECRET_KEY=x\n")
            old = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout)
            # COMPONENTS with an id no component has: left out (retired ids: test_retired_ids_read_as_their_successor)
            env.write_text("COMPONENTS=core,mesh,paperless,gone\nAK_SECRET_KEY=x\n")
            ren = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout)
        self.assertEqual((new["switch_off"], new["switch_on"]), ([], []))
        self.assertEqual((applied["still_running"], applied["stop_command"]), ([], ""))
        self.assertIn("photos", old["switch_off"])
        self.assertNotIn("paperless", old["switch_off"])
        self.assertEqual(ren["switch_off"], [])

    def test_same_components(self):
        import json
        ans = 'components = ["core", "mesh", "paperless"]\n'
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("")
            w = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout)["write"]
            env.write_text(f"COMPONENTS={w['COMPONENTS']}\nCOMPOSE_PROFILES={w['COMPOSE_PROFILES']}\nAK_SECRET_KEY=x\n")
            same = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none", input=ans).stdout)
            more = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none",
                                  input='components = ["core", "mesh", "paperless", "photos"]\n').stdout)
            # Chosen as .env says, but its COMPONENTS lacks what the plan adds (core): not "without changes"
            env.write_text(f"COMPONENTS=mesh,paperless\nCOMPOSE_PROFILES={w['COMPOSE_PROFILES']}\nAK_SECRET_KEY=x\n")
            lack = json.loads(cli("plan", "--answers", "-", "--json", "--env", str(env), "--gpu", "none",
                                  input='components = ["mesh", "paperless"]\n').stdout)
        self.assertTrue(same["same_components"])
        self.assertFalse(more["same_components"])
        self.assertFalse(lack["same_components"])

    def test_title_with_quotes(self):
        self.assertEqual(engine.stage_titles({})["public:recovery"], 'Authentik: "Forgot password?" via email')

    def test_plan_from_stdin(self):
        import json
        r = subprocess.run([sys.executable, str(ROOT / "lib/setup/engine.py"), "plan", "--answers", "-", "--json", "--env", "/dev/null", "--gpu", "none"],
                           input='components = ["paperless"]\n[options]\nDOMAIN = "bad domain"\n', capture_output=True, text=True)
        d = json.loads(r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertIn("paperless", d["enabled"])
        self.assertEqual(d["invalid"][0]["key"], "DOMAIN")
        self.assertFalse(d["applied"])

    def test_secret_from_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("COMPONENTS=core,mesh\n")
            run = lambda key, value: subprocess.run([sys.executable, str(ROOT / "lib/setup/engine.py"), "secret", key, "--env", str(env)],
                                                    input=value + "\n", capture_output=True, text=True)
            r = run("INFOMANIAK_API_TOKEN", "tok-SECRET")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("tok-SECRET", r.stdout + r.stderr)
            self.assertIn("INFOMANIAK_API_TOKEN='tok-SECRET'", env.read_text())
            self.assertEqual(run("DOMAIN", "x").returncode, 2)            # not a secret
            self.assertEqual(run("PAPERLESS_SECRET_KEY", "x").returncode, 2)  # not a chosen component's
            self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)
