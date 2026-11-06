"""The console: console-report.py's overview, its guides, and the page itself (Playwright, if Node has it)."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDES = ROOT / "site/console/guides"


def load_report():
    spec = importlib.util.spec_from_file_location("console_report", ROOT / "console-report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Guides(unittest.TestCase):
    def test_every_guide_exists(self):
        with open(ROOT / "jobs/jobs.toml", "rb") as f:
            reg = tomllib.load(f)
        named = {j["guide"] for j in reg["jobs"].values() if j["guide"]}
        named |= set(re.findall(r'"guide": "([a-z-]+)"', (ROOT / "console-report.py").read_text()))
        named |= set(re.findall(r'or "([a-z-]+)"', (ROOT / "console-report.py").read_text()))
        have = {p.stem for p in GUIDES.glob("*.md")}
        self.assertLessEqual(named, have)

    def test_index_lists_every_guide(self):
        idx = json.loads((GUIDES / "index.json").read_text())
        self.assertEqual(sorted(g["id"] for g in idx), sorted(p.stem for p in GUIDES.glob("*.md")))

    def test_links_between_guides_resolve(self):
        have = {p.name for p in GUIDES.glob("*.md")}
        for p in GUIDES.glob("*.md"):
            for target in re.findall(r"\]\(([a-z0-9-]+\.md)\)", p.read_text()):
                self.assertIn(target, have, p.name)

    def test_no_template_braces(self):
        # Caddy runs html and json through `templates`
        for p in [ROOT / "site/console/index.html", GUIDES / "index.json"]:
            self.assertNotIn("{{", p.read_text(), p)


class Report(unittest.TestCase):
    def test_attention(self):
        m = load_report()
        m.containers = lambda: {"paperless": {"Service": "paperless", "State": "exited", "Status": "Exited (1)"},
                                "ops": {"Service": "ops", "State": "running", "Health": ""}}
        m.cert_days = lambda ip, name: 5 if name.startswith("auth.") else 60
        m.registry.show = lambda unit, props: (
            {"ActiveState": "failed", "Result": "exit-code"} if unit == "homelab-backup.service"
            else {"ActiveState": "active"} if unit.endswith((".timer", ".path")) else {"ActiveState": "inactive", "Result": "success"})
        env = {"COMPONENTS": "core,mesh,paperless,backups", "NETBIRD_IP": "100.64.0.1", "DOMAIN": "example.org"}
        r = m.report(env)
        att = {(a["kind"], a["id"]): a["guide"] for a in r["attention"]}
        self.assertEqual(att[("job", "backup")], "backup-failed")
        self.assertEqual(att[("app", "paperless")], "app-down")
        self.assertEqual(att[("cert", "auth")], "certificate")
        # A service that isn't in `docker compose ps` at all is missing, hence red
        self.assertEqual(att[("app", "caddy")], "app-down")
        self.assertNotIn(("app", "ops"), att)
        self.assertIn("restart-paperless", r["allowed"])
        self.assertNotIn("restart-caddy", r["allowed"])
        self.assertEqual({a["service"] for a in r["apps"] if a["restart"]}, {"paperless", "paperless-redis", "paperless-gotenberg", "paperless-tika"})
        # Jobs of chosen components only
        self.assertNotIn("photos-copy", {j["id"] for j in r["jobs"]})

    def test_without_docker(self):
        m = load_report()
        m.containers = lambda: {}
        m.registry.show = lambda unit, props: {}
        r = m.report({"COMPONENTS": "core,mesh"})
        self.assertEqual(r["attention"], [])
        self.assertTrue(all(a["state"] == "unknown" for a in r["apps"]))


SAMPLE = {
    "time": 0, "allowed": ["run-backup", "logs-job-backup", "restart-paperless", "logs-app-paperless"],
    "jobs": [{"id": "backup", "title": "Backup", "when": "hourly", "guide": "backup-failed", "state": "failed",
              "detail": "failed (exit-code)", "last": "Tue 2026-10-27 09:30", "next": "", "polkit_start": True}],
    "apps": [{"service": "paperless", "component": "paperless", "title": "Documents", "state": "exited",
              "health": "", "status": "Exited (1)", "restart": True}],
    "certs": [{"name": "home.example.org", "days": 60}],
    "attention": [{"kind": "job", "id": "backup", "title": "Backup: failed", "guide": "backup-failed"},
                  {"kind": "app", "id": "paperless", "title": "paperless: exited", "guide": "app-down"}],
}
PAGE_TEST = r"""
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
const [root, sample, shot] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1000, height: 1400 } });
  const posted = [];
  let job = null;
  await page.route('https://home.test/**', async route => {
    const u = new URL(route.request().url());
    const p = u.pathname;
    if (p === '/console/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'site/console/index.html')) });
    if (p.startsWith('/console/guides/')) return route.fulfill({ contentType: p.endsWith('.json') ? 'application/json' : 'text/markdown', body: fs.readFileSync(path.join(root, 'site', p)) });
    if (p === '/ops/data/console.json') { const s = JSON.parse(sample); s.time = Math.floor(Date.now() / 1000) - 120; return route.fulfill({ json: s }); }
    if (p === '/ops/data/jobs.json') return route.fulfill({ json: job || {} });
    if (p.startsWith('/ops/data/logs-')) return route.fulfill({ json: { name: 'x', time: Math.floor(Date.now() / 1000), lines: ['line one', 'line two'] } });
    if (p.startsWith('/ops/run/') && route.request().method() === 'POST') {
      const name = p.slice(9); posted.push(name);
      job = { job: name, state: 'done', started: Math.floor(Date.now() / 1000), step: '', output: ['ok'] };
      return route.fulfill({ status: 202, json: { queued: name } });
    }
    return route.fulfill({ status: 404, body: '' });
  });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('https://home.test/console/');
  await page.waitForSelector('#attention .item');
  const out = {};
  out.attention = await page.locator('#attention .item').count();
  out.sub = await page.locator('#sub').textContent();
  out.guides = await page.locator('#guides .item').count();
  // A guide renders as markdown, text only
  await page.locator('#attention .item').first().getByRole('button', { name: 'What to do' }).click();
  await page.waitForSelector('#sheet-body .md h1');
  out.guideTitle = await page.locator('#sheet-body .md h1').textContent();
  out.guideLists = await page.locator('#sheet-body .md ol, #sheet-body .md ul').count();
  await page.keyboard.press('Escape');
  // Restart asks first, then posts, then shows the result
  await page.locator('#apps .item').first().getByRole('button', { name: 'Restart' }).click();
  out.confirm = await page.locator('#sheet-body p').first().textContent();
  out.postedBeforeConfirm = posted.length;
  await page.locator('#sheet-body').getByRole('button', { name: 'Restart' }).click();
  await page.waitForSelector('#sheet-body .good');
  out.posted = posted.slice();
  await page.keyboard.press('Escape');
  // Logs: posts logs-app-*, then shows the lines
  await page.locator('#apps .item').first().getByRole('button', { name: 'Logs' }).click();
  await page.waitForSelector('#sheet-body pre');
  out.logs = await page.locator('#sheet-body pre').textContent();
  await page.keyboard.press('Escape');
  if (shot) await page.screenshot({ path: shot, fullPage: true });
  out.errors = errors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def node_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@unittest.skipUnless(node_playwright(), "needs node with playwright")
class Page(unittest.TestCase):
    def test_render_and_actions(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "page.js"
            script.write_text(PAGE_TEST)
            r = subprocess.run(["node", str(script), str(ROOT), json.dumps(SAMPLE), os.environ.get("CONSOLE_SHOT", "")],
                               capture_output=True, text=True, timeout=120, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["attention"], 2)
        self.assertIn("Updated 2 min ago", out["sub"])
        self.assertEqual(out["guides"], len(list(GUIDES.glob("*.md"))))
        self.assertEqual(out["guideTitle"], "The backup failed")
        self.assertGreater(out["guideLists"], 0)
        self.assertIn("mail naming you", out["confirm"])
        self.assertEqual(out["postedBeforeConfirm"], 0)
        self.assertEqual(out["posted"], ["restart-paperless"])
        self.assertIn("line two", out["logs"])


if __name__ == "__main__":
    unittest.main()
