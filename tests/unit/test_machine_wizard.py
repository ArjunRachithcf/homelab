"""wizard.sh (the machine), run in a copy with curl, docker, dig and sleep stubbed (DOCKER_HOST to nowhere):
- stage oidc on a box whose Paperless already signs in through Authentik: answering No to "skip" doesn't
  replace the sign-in without a second yes, a replacement gets today's URLs (HTTPS, the public port with public
  access), and a missing setting is never "already done";
- stage dns by the DNS's state: step 2's A records until the public cutover, none after it."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKING = '{"openid_connect":{"APPS":[{"client_id":"live-id","secret":"live-secret"}]}}'
STUBS = {
    # oidc_ok: Authentik's discovery answers (code_443 → curl -w '%{http_code}')
    "curl": "#!/bin/sh\necho 200\n",
    "docker": "#!/bin/sh\necho \"$*\" >> \"$STUB_LOG\"\n",
    "sleep": "#!/bin/sh\n",
    # Infomaniak's nameserver has the A records ($DIG_NS11); public DNS answers $DIG_PUBLIC (the wildcard's target)
    "dig": "#!/bin/sh\ncase \"$*\" in *@ns11.infomaniak.ch*) echo \"$DIG_NS11\" ;; *) echo \"$DIG_PUBLIC\" ;; esac\n",
}


def run(answers: list[str], components: str, stage: str = "oidc", oidc: str | None = WORKING,
        dig_ns11: str = "", dig_public: str = "", domain: str = "example.org") -> tuple[str, dict[str, str], str]:
    with tempfile.TemporaryDirectory() as d:
        box = Path(d) / "box"
        box.mkdir()
        shutil.copy(ROOT / "wizard.sh", box)
        shutil.copytree(ROOT / "lib", box / "lib")
        (box / ".env").write_text(
            f"DOMAIN={domain}\nDOMAIN_ALIAS=example.net\nNETBIRD_IP=100.64.0.5\nBRAND=Home\n"
            f"NETBIRD_PROXY_PORT=9443\nNETBIRD_PROXY_CLUSTER=eu1.proxy.example\nCOMPONENTS={components}\n"
            f"PAPERLESS_SSO_ONLY=true\n" + (f"PAPERLESS_OIDC_JSON='{oidc}'\n" if oidc else ""))
        bin_ = Path(d) / "bin"
        bin_.mkdir()
        for name, body in STUBS.items():
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        log = Path(d) / "docker.log"
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOMELAB_UI="protocol", HOMELAB_STAGES=stage,
                   HOMELAB_STAGES_TOTAL="1", TERM="dumb", STUB_LOG=str(log), DOCKER_HOST="unix:///nonexistent",
                   DIG_NS11=dig_ns11, DIG_PUBLIC=dig_public)
        r = subprocess.run(["bash", str(box / "wizard.sh")], input="".join(a + "\n" for a in answers),
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        values = {}
        for line in (box / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            values[k] = v.strip("'")
        return r.stdout, values, log.read_text() if log.exists() else ""


class PaperlessProvider(unittest.TestCase):
    def test_no_to_skip_still_asks_before_replacing(self):
        # banner, "skip?" no, "replace anyway?" no
        out, env, docker = run(["", "n", "n"], "core,mesh,public-access,paperless")
        self.assertIn("locks everyone out", out)
        self.assertEqual(env["PAPERLESS_OIDC_JSON"], WORKING)
        self.assertNotIn("up -d paperless", docker)

    def test_replacement_uses_https_and_the_public_port(self):
        out, env, docker = run(["", "n", "y", "new-id", "new-SECRET", ""], "core,mesh,public-access,paperless")
        app = json.loads(env["PAPERLESS_OIDC_JSON"])["openid_connect"]["APPS"][0]
        self.assertEqual((app["client_id"], app["secret"]), ("new-id", "new-SECRET"))
        self.assertEqual(app["settings"]["server_url"],
                         "https://auth.example.org:9443/application/o/paperless/.well-known/openid-configuration")
        self.assertIn("compose up -d paperless", docker)
        self.assertNotIn("new-SECRET", out)
        # RUNBOOK.md step 5: a public client, strict HTTPS callbacks on both domains (and the port), the app's
        for words in ("client type ⟦b⟧Public⟦/⟧", "https://paperless.example.net:9443/accounts/oidc/authentik/login/callback/",
                      "atlp://oidc-callback"):
            self.assertIn(words, out)
        self.assertNotIn("http://", out)

    def test_missing_setting_is_not_done(self):
        # The provider answers, but .env has no PAPERLESS_OIDC_JSON (e.g. a run stopped before it was written)
        out, env, _ = run(["", "", "", ""], "core,mesh,public-access,paperless", oidc=None)
        self.assertNotIn("Looks done already", out)
        self.assertIn("Paste the Client ID", out)
        self.assertNotIn("PAPERLESS_OIDC_JSON", env)  # nothing pasted, nothing written

    def test_without_public_access_no_port(self):
        _, env, _ = run(["", "n", "y", "id-2", "secret-2", ""], "core,mesh,paperless")
        app = json.loads(env["PAPERLESS_OIDC_JSON"])["openid_connect"]["APPS"][0]
        self.assertEqual(app["settings"]["server_url"],
                         "https://auth.example.org/application/o/paperless/.well-known/openid-configuration")


class Dns(unittest.TestCase):
    # example.invalid never resolves, so the names aren't there yet
    def test_before_the_cutover_the_records(self):
        # A fresh install, public access chosen: step 2's records until the public wizard's cutover replaces them
        out, _, _ = run(["", "", ""], "core,mesh,public-access,paperless", stage="dns", dig_ns11="100.64.0.5",
                        domain="example.invalid")
        self.assertIn("Source ⟦b⟧auth⟦/⟧", out)
        self.assertIn("Source ⟦b⟧paperless⟦/⟧", out)
        self.assertNotIn("jellyfin", out)  # not chosen
        self.assertIn('"ok":true', out)  # the records seen on ns11

    def test_after_the_cutover_no_records(self):
        out, _, _ = run(["", ""], "core,mesh,public-access,paperless,jellyfin", stage="dns",
                        dig_public="eu1.proxy.example.", domain="example.invalid")
        self.assertIn("went public", out)
        self.assertNotIn("Type A", out)


if __name__ == "__main__":
    unittest.main()
