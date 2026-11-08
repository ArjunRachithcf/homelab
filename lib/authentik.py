#!/usr/bin/env python3
"""~/homelab/lib/authentik.py: Authentik side of SSH-via-LDAP, account hardening and each component's
sign-in (run through ./authentik/setup-ssh.sh, as yourself, docker group). Idempotent; never prints secrets.

  setup-ssh.sh               everything below, in order (first run)
  setup-ssh.sh hardening     re-apply hardening, family and the chosen apps (after every Authentik upgrade)
  setup-ssh.sh component ID… each chosen component's own blueprint (authentik/<ID>.yaml)
  setup-ssh.sh family        family sign-up, the brand's wording (family.yaml) + jellyfin, photos, music
  setup-ssh.sh apps          the chosen ones of audiobooks, bookmarks, budget, pdf, adblock (7d)
  setup-ssh.sh files         the chosen ones of reading, files, share (RUNBOOK.md 7e)
  setup-ssh.sh family-apps   the chosen ones of Chores, Things, Family tree, Notes, Wiki, News, Games, Places (7f)
  setup-ssh.sh kid USER      USER into group kids, out of paperless-users (RUNBOOK.md 7e)
  setup-ssh.sh members GROUP the active users of GROUP, one username a line (Seerr's permissions, downloads-wizard.sh)
  setup-ssh.sh app           the family app's browser sign-in (family-app.yaml, RUNBOOK.md "Family app")
  setup-ssh.sh terminal      = component terminal: Termix's OIDC app, admins only (RUNBOOK.md 7e)
  setup-ssh.sh n8n           = component n8n: forward-auth, admins only (RUNBOOK.md 7h)
  setup-ssh.sh add-key       append public keys (one per line on stdin, the comment as title) to your user
  setup-ssh.sh remove-key FP… remove keys by fingerprint (SHA256:…, as list-keys shows them)
  setup-ssh.sh list-keys     show the keys stored on your user (type, fingerprint, title)
  (The home page's SSH keys sheet does the same over NetBird: authentik/ssh-sso.yaml, lib/ssh_keys.py.)
  setup-ssh.sh status        MFA devices, groups, phone, outpost token present
  setup-ssh.sh cert          (re)make the outpost certificate, export it to ssh/ldap-ssh.pem

"Everything": rename akadmin → ADMIN_USER, cert ldap-ssh, blueprints hardening.yaml and ssh-ldap.yaml,
list additions, CA → ssh/ldap-ssh.pem, outpost token → .env, start authentik-ldap-ssh. RUNBOOK.md step 10
and "Hardening". What runs inside Authentik is lib/authentik_tasks.py.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "lib/setup"))
import engine  # noqa: E402
import render  # noqa: E402
import ssh_keys  # noqa: E402

ENV_FILE = ROOT / ".env"
TASKS = ROOT / "lib/authentik_tasks.py"
WORKER = "authentik-worker"
# The commands that apply a fixed set of components (each only when chosen)
GROUPS = {
    "family": ["jellyfin", "photos", "music"],
    "apps": ["audiobooks", "bookmarks", "budget", "pdf", "adblock"],
    "files": ["reading", "files", "share"],
    "family-apps": ["chores", "things", "family-tree", "notes", "wiki", "news", "games", "places"],
}
SECRET_LINE = re.compile(r"password|token", re.I)


class Failed(Exception):
    """A step failed; its message (starting with ✗) is the last thing shown."""


def env() -> dict[str, str]:
    # Read each time: all() writes into .env between steps
    return render.read_env(ENV_FILE)


def admin() -> str:
    name = os.environ.get("SSH_USER") or env().get("ADMIN_USER", "")
    if not name:
        raise Failed('✗ ADMIN_USER missing in .env (section "Your instance" in .env.example)')
    return name


def docker(*args: str, extra_env: dict[str, str] | None = None, check: bool = True,
           capture: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["docker", "compose", *args], cwd=ROOT, text=True, capture_output=capture,
                       env={**os.environ, **(extra_env or {})})
    if check and r.returncode != 0:
        raise Failed(f"✗ docker compose {args[0]} failed")
    return r


def ak(task: str, **args) -> list[str]:
    """Run TASK of lib/authentik_tasks.py in ak shell; its OUT: lines, without the prefix."""
    code = TASKS.read_text(encoding="utf-8") + "\nrun()\n"
    r = docker("exec", "-T", "-e", "AK_TASK", WORKER, "ak", "shell", "-c", code,
               extra_env={"AK_TASK": json.dumps({"task": task, "args": args})}, check=False)
    lines = (r.stdout + r.stderr).splitlines()
    if r.returncode != 0:
        for line in [x for x in lines if not x.startswith("OUT:") and not SECRET_LINE.search(x)][-15:]:
            print(line, file=sys.stderr)
        raise Failed(f"✗ Authentik task {task} failed")
    return [x[4:] for x in lines if x.startswith("OUT:")]


def say(task: str, **args) -> None:
    for line in ak(task, **args):
        print(line)


def env_set(key: str, value: str) -> None:
    """KEY=value into .env, replaced or appended (mode 600); the value is never shown."""
    engine.upsert_env(ENV_FILE, {key: value})
    ENV_FILE.chmod(0o600)


# ── Blueprints ──────────────────────────────────────────────────────────────────────────────────

def apply_blueprint(rel: str, skip_missing: str | None = None) -> bool:
    """Render authentik/<file> from .env (__KEY__ placeholders, never a secret: lib/render.py), copy it
    into the worker and apply it. The secrets it reads (!Env NAME) are passed from .env as the worker's
    environment; one missing is an error, or with skip_missing=ID only skips (False)."""
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    e = env()
    names = sorted(set(re.findall(r"!Env ([A-Z][A-Z0-9_]*)", text)))
    for n in names:
        if not e.get(n):
            if skip_missing:
                print(f"· {skip_missing}.yaml skipped (no {n} yet)")
                return False
            raise Failed(f"✗ {n} missing in .env")
    try:
        rendered = render.render(text, render.values(e), rel)
    except SystemExit as err:
        print(err, file=sys.stderr)
        raise Failed(f"✗ blueprint {rel} not rendered") from None
    name = f"homelab-{path.name}"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(rendered)
    try:
        os.chmod(f.name, 0o644)   # ak runs as Authentik's own uid; no secrets in it (!Env)
        docker("cp", f.name, f"{WORKER}:/blueprints/{name}")
    finally:
        os.unlink(f.name)
    flags = [x for n in names for x in ("-e", n)]
    r = docker("exec", "-T", *flags, WORKER, "ak", "apply_blueprint", name,
               extra_env={n: e[n] for n in names}, check=False)
    # /blueprints belongs to root; the copied file must go, or Authentik keeps discovering it
    docker("exec", "-T", "-u", "root", WORKER, "rm", "-f", f"/blueprints/{name}")
    log = r.stdout + r.stderr
    if r.returncode != 0 or "invalid" in log.lower():
        for line in [x for x in log.splitlines() if not SECRET_LINE.search(x)][-20:]:
            print(line)
        raise Failed(f"✗ blueprint {rel} failed")
    print(f"✓ applied {rel}")
    return True


def app_components(comps: dict[str, dict]) -> list[str]:
    """The components with their own authentik/<id>.yaml, which hardening applies. Not these: core's
    (hardening, family, family-app: below), ssh-ldap.yaml (all) and jellyfin-ldap.yaml (by hand, 7b).
    They only need core's objects (family.yaml), so their order doesn't matter."""
    return [c for c, d in comps.items() if f"{c}.yaml" in d["blueprints"]]


def component(c: str, skip_missing: bool = False) -> None:
    """C's blueprint when C is chosen (COMPONENTS in .env; none set = all), then what its
    setup/components/<C>.toml asks of Authentik beyond it ([authentik]: providers onto the embedded
    outpost, you into groups; list additions a blueprint would replace)."""
    if not (ROOT / f"authentik/{c}.yaml").is_file():
        raise Failed(f"✗ no authentik/{c}.yaml")
    comps = engine.components()
    if c not in engine.current(comps, env())[0]:
        return
    if not apply_blueprint(f"authentik/{c}.yaml", skip_missing=c if skip_missing else None):
        return
    extra = comps[c].get("authentik", {})
    if extra.get("outpost"):
        say("on_outpost", providers=extra["outpost"])
    if extra.get("admin_groups"):
        say("join_groups", name=admin(), groups=extra["admin_groups"])


def family() -> None:
    """family.yaml (core): sign-up groups + notice mail, wording/templates, shared mappings, Home forward-auth."""
    apply_blueprint("authentik/family.yaml")
    e = env()
    if not (e.get("BRAND") and e.get("BRAND_SLUG")):
        raise Failed("✗ BRAND / BRAND_SLUG missing in .env")
    say("brand", brand=e["BRAND"], slug=e["BRAND_SLUG"],
        home_url=f"https://home.{e.get('DOMAIN', '')}:{e.get('NETBIRD_PROXY_PORT') or '9443'}")
    say("on_outpost", providers=["Provider for Home"])


def hardening() -> None:
    apply_blueprint("authentik/hardening.yaml")
    say("list_additions", name=admin())
    family()
    apply_blueprint("authentik/family-app.yaml")
    # Each chosen app; one whose secrets aren't in .env yet (its wizard writes them) is skipped
    for c in app_components(engine.components()):
        component(c, skip_missing=True)


# ── SSH via LDAP ────────────────────────────────────────────────────────────────────────────────

def make_cert() -> None:
    """Self-signed (issuer = subject) certificate for the SSH outpost. Not Authentik's own generator: it
    writes "authentik <version>" as issuer, which OpenSSL/SSSD can't verify."""
    if "ok" in ak("cert_state"):
        print("✓ certificate ldap-ssh exists (self-signed)")
        return
    with tempfile.TemporaryDirectory() as d:
        key, cert = Path(d, "key.pem"), Path(d, "cert.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-days", "3650", "-subj", "/CN=ldap.homelab.internal/O=homelab",
                        "-addext", "subjectAltName=DNS:ldap.homelab.internal", "-addext", "extendedKeyUsage=serverAuth",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        say("set_cert", cert=cert.read_text(), key=key.read_text())
    # the outpost loads its certificate at start
    if docker("ps", "-q", "authentik-ldap-ssh", check=False).stdout.strip():
        docker("restart", "authentik-ldap-ssh", check=False)


def export_ca() -> None:
    pem = ROOT / "ssh/ldap-ssh.pem"
    pem.write_text("".join(line + "\n" for line in ak("export_ca")))
    if subprocess.run(["openssl", "x509", "-in", str(pem), "-noout", "-subject", "-enddate"],
                      capture_output=True).returncode != 0:
        raise Failed("✗ ssh/ldap-ssh.pem is not a certificate")
    print("✓ CA written to ssh/ldap-ssh.pem (public certificate)")


def outpost_token() -> None:
    tok = next(iter(ak("outpost_token")), "")
    if not tok:
        raise Failed("✗ no token for outpost ldap-ssh")
    env_set("AK_LDAP_SSH_OUTPOST_TOKEN", tok)
    print("✓ outpost token saved to .env (AK_LDAP_SSH_OUTPOST_TOKEN)")


def clean_keys(text: str) -> list[str]:
    """The public key lines of pasted text: control characters out (an arrow key while pasting leaves
    ESC sequences behind), trailing space off."""
    text = re.sub(r"[^\t\n\x20-\x7e]", "", text)
    lines = (re.sub(r"\[[0-9;]*[A-Za-z]", "", line).rstrip() for line in text.splitlines())
    return [line for line in lines if re.match(r"(ssh-|sk-|ecdsa-)", line)]


def keys_changed() -> None:
    """After a change: the SSH outpost reloads its users (saving its provider: seconds, no restart), SSSD follows
    within a minute (ssh/sssd.conf), and the job ssh-keys mails what changed and updates the home page's sheet."""
    try:
        say("ldap_refresh")
    except Failed:
        if docker("restart", "authentik-ldap-ssh", check=False).returncode == 0:
            print("✓ outpost restarted instead")
    trigger = ROOT / "ops/keys/refresh"
    if trigger.parent.is_dir():
        trigger.write_text("command line\n")
        print("✓ SSH takes the change within a minute; you'll get a mail")


def add_keys() -> None:
    """The same checks as the SSH keys sheet (lib/ssh_keys.py); each line stored rebuilt, its comment as title"""
    keys = clean_keys(sys.stdin.read())
    if not keys:
        raise Failed("✗ no public key lines on stdin")
    lines = []
    for k in keys:
        try:
            kind, blob = ssh_keys.parse(k)
        except ssh_keys.Refused as err:
            raise Failed(f"✗ {err}") from None
        lines.append(ssh_keys.stored_line(kind, blob, ssh_keys.cli_title(" ".join(k.split()[2:]))))
    say("add_keys", name=admin(), keys=lines)
    keys_changed()


def remove_keys(fps: list[str]) -> None:
    bad = [f for f in fps if not ssh_keys.FINGERPRINT.fullmatch(f)]
    if not fps or bad:
        raise Failed("✗ usage: setup-ssh.sh remove-key SHA256:… (the fingerprints list-keys shows)")
    say("remove_keys", name=admin(), fps=fps)
    keys_changed()


def everything() -> None:
    if not env().get("SSH_LDAP_BIND_PASSWORD"):
        pw = re.sub(r"[/+=]", "", base64.b64encode(secrets.token_bytes(36)).decode())
        env_set("SSH_LDAP_BIND_PASSWORD", pw)
        print("✓ generated SSH_LDAP_BIND_PASSWORD in .env")
    say("rename_admin", name=admin())
    make_cert()
    hardening()
    apply_blueprint("authentik/ssh-ldap.yaml")
    say("list_additions", name=admin(), ssh_admins_only=True)   # ssh-admins exists now
    export_ca()
    outpost_token()
    docker("up", "-d", "authentik-ldap-ssh", capture=False)
    print("✓ done. Next: keys (add-key), then: sudo ./ssh/setup-host.sh")


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("all", [])
    try:
        if cmd == "all":
            everything()
        elif cmd == "hardening":
            hardening()
        elif cmd == "app":
            apply_blueprint("authentik/family-app.yaml")
        elif cmd == "component":
            for c in rest:
                component(c)
        elif cmd in ("terminal", "n8n"):
            component(cmd)
        elif cmd in GROUPS:
            if cmd == "family":
                family()
            for c in GROUPS[cmd]:
                component(c)
        elif cmd == "kid":
            if not rest:
                print("usage: setup-ssh.sh kid USERNAME", file=sys.stderr)
                return 2
            say("kid", name=rest[0])
        elif cmd == "members":
            if len(rest) != 1:
                print("usage: setup-ssh.sh members GROUP", file=sys.stderr)
                return 2
            say("members", group=rest[0])
        elif cmd == "add-key":
            add_keys()
        elif cmd == "remove-key":
            remove_keys(rest)
        elif cmd == "list-keys":
            say("list_keys", name=admin())
        elif cmd == "status":
            say("status", name=admin())
        elif cmd == "cert":
            make_cert()
            export_ca()
        else:
            print(__doc__.split("\n\n", 1)[1].rstrip())
            return 2
    except Failed as err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
