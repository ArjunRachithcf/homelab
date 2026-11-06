"""setup/components/*.toml and presets: complete, consistent with compose, the wizards and .env.example."""
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/setup"))
import engine  # noqa: E402

FIELDS = {"title", "description", "category", "core", "requires", "suggests", "conflicts", "gpu", "services",
          "public_names", "peer_names", "blueprints", "jobs", "ansible", "stages", "platforms"}
OPTION_FIELDS = {"help", "type", "default", "required", "generate", "advanced", "set_by", "macos_default"}
ENGINE_KEYS = {"COMPONENTS", "COMPOSE_PROFILES"}


class Components(unittest.TestCase):
    comps = engine.components()

    def test_fields(self):
        for c, d in self.comps.items():
            self.assertEqual(set(d) - {"options", "tiles", "authentik"}, FIELDS, c)
            self.assertLessEqual(set(d.get("authentik", {})), {"outpost", "admin_groups"}, c)
            for ref in d["requires"] + d["suggests"] + d["conflicts"]:
                self.assertIn(ref, self.comps, (c, ref))
            self.assertIn(d["gpu"], ("none", "nvidia"), c)
            self.assertLessEqual(set(d["platforms"]), set(engine.PLATFORMS), c)
            self.assertTrue(d["platforms"], c)
            for k, o in d.get("options", {}).items():
                self.assertLessEqual(set(o), OPTION_FIELDS, (c, k))
                self.assertIn(o.get("type", "text"), set(engine.TYPES) | {"ip", "text", "secret", "firewall"}, (c, k))
                if "default" in o and o.get("type") != "secret":
                    self.assertIsNone(engine.check_value(o, str(o["default"])), (c, k))
                self.assertFalse(o.get("type") == "secret" and "default" in o, (c, k))
            for b in d["blueprints"]:
                self.assertTrue((ROOT / "authentik" / b).is_file(), (c, b))

    def test_tiles(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import configure
        ids, orders = set(), set()
        for c, d in self.comps.items():
            for t in d.get("tiles", []):
                self.assertLessEqual({"order", "id", "name", "desc", "url", "icon", "status", "group", "audience"}, set(t), c)
                self.assertIn(t["audience"], configure.AUDIENCE, c)
                self.assertIn(t["group"], ("watch", "docs", "home", "life", "account", "admin"), c)
                self.assertNotIn(t["id"], ids)
                self.assertNotIn(t["order"], orders)
                ids.add(t["id"]); orders.add(t["order"])
                icon = t["icon"].lstrip("/")
                self.assertTrue((ROOT / "site" / icon).is_file() or (ROOT / "branding/default/site" / icon).is_file(), icon)
                status = (ROOT / "caddy/components" / c / "status.caddy")
                if t["status"] != "auth":
                    # core's status checks are in the Caddyfile itself
                    src = status.read_text() if status.exists() else (ROOT / "Caddyfile").read_text()
                    self.assertIn(f"import status {t['status']} ", src, c)

    def test_blueprints(self):
        # Every blueprint belongs to a component, and hardening applies exactly the components that
        # have their own authentik/<id>.yaml, so choosing one app sets up its sign-in and no other's
        used = [b for d in self.comps.values() for b in d["blueprints"]]
        self.assertEqual(len(used), len(set(used)), "a blueprint listed by two components")
        self.assertEqual(set(used), {p.name for p in (ROOT / "authentik").glob("*.yaml")})
        sys.path.insert(0, str(ROOT / "lib"))
        import authentik
        for c in authentik.GROUPS.values():
            self.assertLessEqual(set(c), set(authentik.app_components(self.comps)))
        # [authentik]: the providers and groups are made by the component's own blueprint
        for c, d in self.comps.items():
            extra = d.get("authentik", {})
            if extra:
                src = (ROOT / f"authentik/{c}.yaml").read_text()
                for name in extra.get("outpost", []) + extra.get("admin_groups", []):
                    self.assertRegex(src, rf"(?m)^\s+name: {re.escape(name)}$", (c, name))

    def test_kids_only_where_their_tiles_are(self):
        # Kids are in family too: an app whose tiles are for family alone must refuse them in Authentik
        # (mode all, kids negated), or the hidden tile is the only thing keeping them out
        kids = re.compile(r"group: !Find \[authentik_core\.group, \[name, kids\]\]\n\s+order: \d+\n\s+attrs:\n\s+negate: true")
        for c, d in self.comps.items():
            audiences = {t.get("audience") for t in d.get("tiles", [])}
            for bp in d["blueprints"]:
                src = (ROOT / "authentik" / bp).read_text()
                if "[name, family]]" not in src:
                    continue
                if audiences == {"family"}:
                    self.assertTrue(kids.search(src), f"{bp}: tiles are for family only, but kids may sign in")
                    self.assertNotIn("policy_engine_mode: any", src, bp)
                elif "family-and-kids" in audiences:
                    self.assertFalse(kids.search(src), f"{bp}: tiles are for kids too, but kids are refused")

    def test_caddy_files(self):
        dirs = {p.name for p in (ROOT / "caddy/components").iterdir()}
        self.assertLessEqual(dirs, set(self.comps))
        for c, d in self.comps.items():
            if d.get("core"):
                continue
            names = set(d["public_names"]) | set(d["peer_names"])
            if names:
                sites = (ROOT / "caddy/components" / c / "sites.caddy").read_text()
                for n in names:
                    self.assertIn(f"{n}.{{$DOMAIN}}", sites, (c, n))
            for n in d["public_names"]:
                self.assertIn(f"{n}.{{$DOMAIN}}:8444", (ROOT / "caddy/components" / c / "sites.caddy").read_text(), (c, n))

    def test_every_key_has_one_owner(self):
        keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M)) - ENGINE_KEYS
        owners: dict[str, str] = {}
        for c, d in self.comps.items():
            for k in d.get("options", {}):
                self.assertNotIn(k, owners, f"{k} in {c} and {owners.get(k)}")
                owners[k] = c
        self.assertEqual(keys, set(owners))

    @unittest.skipUnless(shutil.which("docker"), "docker compose not installed")
    def test_every_service_has_one_component_and_its_profile(self):
        env = dict(os.environ, COMPOSE_PROFILES="*")
        cfg = json.loads(subprocess.run(["docker", "compose", "--env-file", str(ROOT / "tests/fixtures/full.env"), "config",
                                         "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=True,
                                        env=env).stdout)
        services = cfg["services"]
        owner = {}
        for c, d in self.comps.items():
            for s in d["services"]:
                self.assertNotIn(s, owner, s)
                owner[s] = c
        self.assertEqual(set(owner), set(services))
        for s, c in owner.items():
            want = [] if self.comps[c].get("core") else [c]
            self.assertEqual(services[s].get("profiles", []), want, s)

    def test_every_stage_has_an_owner(self):
        owned = {s for d in self.comps.values() for s in d["stages"]}
        all_stages = set(engine.stages(self.comps, list(self.comps)))
        self.assertEqual(owned, all_stages)

    def test_every_job_has_one_component(self):
        with open(ROOT / "jobs/jobs.toml", "rb") as f:
            jobs = set(tomllib.load(f)["jobs"])
        owned = [j for d in self.comps.values() for j in d["jobs"]]
        self.assertEqual(sorted(owned), sorted(jobs))

    def test_presets_resolve(self):
        for p in sorted((ROOT / "setup/presets").glob("*.toml")):
            r = engine.resolve(self.comps, engine.preset(p.stem, self.comps), "nvidia")
            self.assertEqual(r.errors, [], p.stem)

    def test_each_component_alone(self):
        for c in self.comps:
            r = engine.resolve(self.comps, [c], "nvidia")
            self.assertEqual(r.errors, [], c)
            self.assertIn(c, r.enabled)
            for e in r.enabled:
                for dep in self.comps[e]["requires"]:
                    self.assertIn(dep, r.enabled, (c, e, dep))

    def test_the_example_runs_everything(self):
        # tests/fixtures/full.env (tests/golden/full/): every component, in the order the engine resolves them
        env = (ROOT / "tests/fixtures/full.env").read_text()
        comps = re.search(r"^COMPONENTS=(.*)$", env, re.M).group(1).split(",")
        self.assertEqual(comps, engine.resolve(self.comps, engine.preset("everything", self.comps), "nvidia").enabled)
        self.assertEqual(set(comps), set(self.comps))
        profiles = re.search(r"^COMPOSE_PROFILES=(.*)$", env, re.M).group(1)
        self.assertEqual(profiles, ",".join(engine.profiles(self.comps, comps, "fedora")))

    def test_family_is_every_app(self):
        fam = engine.resolve(self.comps, engine.preset("family", self.comps), "nvidia").enabled
        self.assertEqual(set(fam), set(self.comps))


if __name__ == "__main__":
    unittest.main()
