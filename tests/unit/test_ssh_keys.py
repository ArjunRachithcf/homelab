"""SSH keys: lib/ssh_keys.py and the flows' policy in authentik/ssh-sso.yaml agree on every case below (built here,
so nothing key-like is in the tree), the policy writes only the one change and never shows a paste, the blueprint
asks for a passkey every time and only lets ssh-admins on NetBird in, and the job ssh-keys follows Authentik and SSH
(stub docker, sss_ssh_authorizedkeys, journalctl and notify.sh; DOCKER_HOST to nowhere)."""
import base64
import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import ssh_keys  # noqa: E402

BLUEPRINT = (ROOT / "authentik/ssh-sso.yaml").read_text()
NOT = "That isn't a public key: paste the line from the .pub file."
TITLE = "Title: up to 50 letters, digits and spaces, and . _ @ ( ) ' + , -"


def s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" + raw if raw[0] & 0x80 else raw


def cases() -> list[dict]:
    """Keys built as OpenSSH writes them (random, but well-formed), and what each must give"""
    r = random.Random(7)
    rnd = lambda n: bytes(r.randrange(256) for _ in range(n))  # noqa: E731
    rsa = lambda bits: s(b"ssh-rsa") + s(b"\x01\x00\x01") + s(mpint(r.getrandbits(bits) | (1 << (bits - 1)) | 1))  # noqa: E731
    blobs = {
        "ssh-ed25519": s(b"ssh-ed25519") + s(rnd(32)),
        "ecdsa-sha2-nistp256": s(b"ecdsa-sha2-nistp256") + s(b"nistp256") + s(b"\x04" + rnd(64)),
        "ecdsa-sha2-nistp384": s(b"ecdsa-sha2-nistp384") + s(b"nistp384") + s(b"\x04" + rnd(96)),
        "ecdsa-sha2-nistp521": s(b"ecdsa-sha2-nistp521") + s(b"nistp521") + s(b"\x04" + rnd(132)),
        "ssh-rsa": rsa(3072),
        "sk-ssh-ed25519@openssh.com": s(b"sk-ssh-ed25519@openssh.com") + s(rnd(32)) + s(b"ssh:"),
        "sk-ecdsa-sha2-nistp256@openssh.com": s(b"sk-ecdsa-sha2-nistp256@openssh.com") + s(b"nistp256")
                                              + s(b"\x04" + rnd(64)) + s(b"ssh:"),
    }
    out = []
    for kind, blob in blobs.items():
        title = {"ssh-rsa": "Arjun's server (old)", "ecdsa-sha2-nistp384": "Phone, Termius"}.get(kind, "Laptop")
        out.append({"name": kind, "text": f"{kind} {b64(blob)} me@laptop", "title": title, "ok": True,
                    "line": f"{kind} {b64(blob)} {title}", "blob": blob,
                    "fingerprint": "SHA256:" + b64(hashlib.sha256(blob).digest()).rstrip("=")})
    ed = out[0]["text"]
    out.append({**out[0], "name": "spaces, a CRLF, a messy title", "text": f"  {ed}  \r\n\n", "title": "  Laptop   two "})
    out[-1]["line"] = out[0]["line"].replace(" Laptop", " Laptop two")
    no = lambda name, text, message, title="Laptop": out.append(  # noqa: E731
        {"name": name, "text": text, "title": title, "ok": False, "message": message})
    no("RSA 2048", f"ssh-rsa {b64(rsa(2048))} x", "RSA keys need 3072 bits or more; an ed25519 key is better anyway.")
    no("private key", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE\n-----END OPENSSH " + "PRIVATE KEY-----\n",
       "That's a private key: never paste it anywhere. Paste the line from the .pub file.")
    no("PuTTY private key", "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nPrivate-Lines: 1\nAAAA\n",
       "That's a private key: never paste it anywhere. Paste the line from the .pub file.")
    no("SSH2 format", "---- BEGIN SSH2 PUBLIC KEY ----\nAAAA\n---- END SSH2 PUBLIC KEY ----",
       "Paste the OpenSSH form: ssh-keygen -i -f key.pub turns this one into it.")
    no("two keys", ed + "\n" + out[1]["text"], "One key at a time: the single line from the .pub file.")
    no("empty", "  \n ", "One key at a time: the single line from the .pub file.")
    no("options in front", 'from="100.64.0.0/10" ' + ed, "Just the key, with nothing in front of it.")
    no("restrict in front", 'restrict,command="/bin/sh" ' + ed, "Just the key, with nothing in front of it.")
    no("certificate", "ssh-ed25519-cert-v01@openssh.com AAAA x",
       "A certificate won't do here: paste the key itself, the line from the .pub file.")
    no("DSA", "ssh-dss AAAAB3NzaC1kc3M x", "SSH here takes ed25519, ecdsa and RSA keys of 3072 bits or more, also on a security key.")
    no("the type alone", "ssh-ed25519", NOT)
    no("not base64", "ssh-ed25519 ###notbase64###", NOT)
    no("type mismatch", f"ssh-ed25519 {b64(s(b'ssh-rsa') + s(bytes(32)))}", NOT)
    no("truncated", f"ssh-ed25519 {b64((s(b'ssh-ed25519') + s(bytes(32)))[:-3])}", NOT)
    no("a short ed25519 key", f"ssh-ed25519 {b64(s(b'ssh-ed25519') + s(bytes(31)))}", NOT)
    no("ecdsa on another curve", f"ecdsa-sha2-nistp256 {b64(s(b'ecdsa-sha2-nistp256') + s(b'nistp384') + s(bytes(65)))}", NOT)
    no("ecdsa, compressed point", f"ecdsa-sha2-nistp256 {b64(s(b'ecdsa-sha2-nistp256') + s(b'nistp256') + s(bytes([2]) + bytes(64)))}", NOT)
    no("title with markup", ed, TITLE, title="<b>Laptop</b>")
    no("title too long", ed, TITLE, title="x" * 51)
    no("no title", ed, TITLE, title="   ")
    return out


CASES = cases()


def block(after: str, key: str) -> str:
    """The `key: |` block (dedented) of the entry whose identifier line is AFTER"""
    i = BLUEPRINT.index(after)
    j = BLUEPRINT.index(f"{key}: |\n", i) + len(f"{key}: |\n")
    lines = []
    for line in BLUEPRINT[j:].splitlines():
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


def entry(identifier: str) -> str:
    """The text of the blueprint entry holding IDENTIFIER (e.g. "name: ssh-keys-passkey")"""
    parts = re.split(r"\n(?=  - model: )", BLUEPRINT)
    (hit,) = [p for p in parts if re.search(rf"^\s+{re.escape(identifier)}$", p, re.M)]
    return hit


class Parse(unittest.TestCase):
    def test_every_case(self):
        for c in CASES:
            with self.subTest(c["name"]):
                if c["ok"]:
                    kind, blob = ssh_keys.parse(c["text"])
                    self.assertEqual(ssh_keys.stored_line(kind, blob, ssh_keys.title_of(c["title"])), c["line"])
                    self.assertEqual(ssh_keys.fingerprint(blob), c["fingerprint"])
                    self.assertEqual(ssh_keys.describe(c["line"])["fingerprint"], c["fingerprint"])
                else:
                    with self.assertRaises(ssh_keys.Refused) as e:
                        kind, blob = ssh_keys.parse(c["text"])
                        ssh_keys.title_of(c["title"])
                    self.assertEqual(str(e.exception), c["message"])

    def test_fingerprint_is_ssh_keygens(self):
        if not shutil.which("ssh-keygen"):
            self.skipTest("no ssh-keygen")
        with tempfile.TemporaryDirectory() as d:
            for t in (["-t", "ed25519"], ["-t", "ecdsa", "-b", "384"], ["-t", "rsa", "-b", "3072"]):
                k = Path(d, "k")
                subprocess.run(["ssh-keygen", "-q", *t, "-N", "", "-f", str(k)], check=True, stdin=subprocess.DEVNULL)
                line = k.with_suffix(".pub").read_text()
                want = subprocess.run(["ssh-keygen", "-lf", str(k.with_suffix(".pub"))], capture_output=True, text=True,
                                      check=True).stdout.split()[1]
                self.assertEqual(ssh_keys.fingerprint(ssh_keys.parse(line)[1]), want)
                k.unlink()
                k.with_suffix(".pub").unlink()

    def test_describe(self):
        types_ = {c["name"]: ssh_keys.describe(c["line"])["type"] for c in CASES if c["ok"]}
        self.assertEqual(types_["ssh-rsa"], "RSA 3072")
        self.assertEqual(types_["ecdsa-sha2-nistp521"], "ECDSA P-521")
        self.assertEqual(types_["sk-ssh-ed25519@openssh.com"], "ED25519 security key")
        self.assertIsNone(ssh_keys.describe("ssh-ed25519"))
        self.assertEqual(ssh_keys.describe(CASES[0]["line"].rsplit(" ", 1)[0])["title"], "no title")

    def test_cli_title(self):
        self.assertEqual(ssh_keys.cli_title("me@laptop"), "me@laptop")
        self.assertEqual(ssh_keys.cli_title("<script>x</script>"), "script x script")
        self.assertEqual(ssh_keys.cli_title(""), "key")
        self.assertEqual(len(ssh_keys.cli_title("y" * 80)), 50)


# ── The policy, run as Authentik runs it: a function body, with Authentik's models stood in for ──────────────────


class FakeUser:
    def __init__(self, pk, lines):
        self.pk, self.attributes, self.saved, self.is_authenticated = pk, {"sshPublicKey": list(lines)}, [], True

    def save(self, update_fields=None):
        self.saved.append(update_fields)


class Authentik:
    """The modules the policy imports, for one user; `used` = {device class: minutes since its last use}"""

    def __init__(self, user, used=None, classes=("webauthn",)):
        self.user, self.locked = user, 0
        now = datetime.datetime.now(datetime.timezone.utc)
        used = {"webauthn": 1} if used is None else used

        def devices(cls):
            class Q:
                def __init__(self, ok):
                    self.ok = ok

                def exists(self):
                    return self.ok

            class M:
                class objects:  # noqa: N801
                    @staticmethod
                    def filter(user, last_used__gte):
                        m = used.get(cls)
                        return Q(user is self.user and m is not None and now - datetime.timedelta(minutes=m) >= last_used__gte)
            return M

        outer = self

        class Users:
            @staticmethod
            def select_for_update():
                outer.locked += 1
                return Users

            @staticmethod
            def get(pk):
                assert pk == outer.user.pk
                return outer.user

        class Stage:
            class objects:  # noqa: N801
                @staticmethod
                def filter(name):
                    assert name == "ssh-keys-passkey"
                    return types.SimpleNamespace(first=lambda: types.SimpleNamespace(device_classes=list(classes)))

        self.modules = {
            "django": types.ModuleType("django"), "django.db": types.ModuleType("django.db"),
            "django.utils": types.ModuleType("django.utils"),
            "django.utils.timezone": types.SimpleNamespace(now=lambda: now),
            "authentik": types.ModuleType("authentik"), "authentik.core": types.ModuleType("authentik.core"),
            "authentik.core.models": types.SimpleNamespace(User=types.SimpleNamespace(objects=Users)),
            "authentik.stages": types.ModuleType("authentik.stages"),
            "authentik.stages.authenticator_static": types.ModuleType("x"),
            "authentik.stages.authenticator_static.models": types.SimpleNamespace(StaticDevice=devices("static")),
            "authentik.stages.authenticator_totp": types.ModuleType("x"),
            "authentik.stages.authenticator_totp.models": types.SimpleNamespace(TOTPDevice=devices("totp")),
            "authentik.stages.authenticator_validate": types.ModuleType("x"),
            "authentik.stages.authenticator_validate.models": types.SimpleNamespace(AuthenticatorValidateStage=Stage),
            "authentik.stages.authenticator_webauthn": types.ModuleType("x"),
            "authentik.stages.authenticator_webauthn.models": types.SimpleNamespace(WebAuthnDevice=devices("webauthn")),
        }
        self.modules["django.db"].transaction = types.SimpleNamespace(atomic=contextlib.nullcontext)

    @contextlib.contextmanager
    def installed(self):
        saved = {k: sys.modules.get(k) for k in self.modules}
        sys.modules.update(self.modules)
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


def run_expression(code: str, **variables):
    """As Authentik's evaluator does: the expression is the body of a function given these names"""
    src = "def __expr(" + ", ".join(variables) + "):\n" + textwrap.indent(code, "    ")
    scope: dict = {}
    exec(compile(src, "ssh-sso.yaml", "exec"), scope)  # noqa: S102  (our own blueprint's code)
    return scope["__expr"](**variables)


class Policy(unittest.TestCase):
    CODE = block("name: ssh-key-change", "expression")

    def run_policy(self, data, user=None, ip="100.64.1.2", debug=False, session=None, used=None, http_user=None,
                   classes=("webauthn",)):
        user = user or FakeUser(7, [])
        ak = Authentik(user, used, classes)
        messages = []
        http = types.SimpleNamespace(user=http_user or user, session=session or {})
        req = types.SimpleNamespace(context={"prompt_data": data}, http_request=http, user=user, debug=debug)
        logger = types.SimpleNamespace(warning=lambda *a, **k: messages.append(("log", a, k)))
        with ak.installed():
            ok = run_expression(self.CODE, request=req, ak_message=messages.append, ak_client_ip=ipaddress.ip_address(ip),
                                ip_address=ipaddress.ip_address, ip_network=ipaddress.ip_network, ak_logger=logger)
        return ok, [m for m in messages if isinstance(m, str)], user, ak

    def test_every_case(self):
        for c in CASES:
            with self.subTest(c["name"]):
                ok, msgs, user, _ = self.run_policy({"ssh_key_title": c["title"], "ssh_key": c["text"]})
                self.assertIs(ok, c["ok"], msgs)
                if c["ok"]:
                    self.assertEqual(user.attributes["sshPublicKey"], [c["line"]])
                    self.assertEqual(user.saved, [["attributes"]])
                else:
                    self.assertEqual(msgs, [c["message"]])
                    self.assertEqual(user.saved, [])
                for m in msgs:   # never anything of the paste
                    for part in c["text"].split():
                        if len(part) > 12:
                            self.assertNotIn(part, m)

    def test_the_same_key_again_changes_nothing(self):
        c = CASES[0]
        user = FakeUser(7, [c["line"].replace(" Laptop", " old title")])
        ok, msgs, user, _ = self.run_policy({"ssh_key_title": "Laptop", "ssh_key": c["text"]}, user)
        self.assertTrue(ok)
        self.assertEqual(msgs, ["That key is on your account already."])
        self.assertEqual(user.saved, [])

    def test_twenty_at_most(self):
        user = FakeUser(7, [c["line"] for c in CASES if c["ok"]][:7] * 3)
        ok, msgs, _, _ = self.run_policy({"ssh_key_title": "x", "ssh_key": f"ssh-ed25519 {b64(s(b'ssh-ed25519') + s(bytes(32)))}"}, user)
        self.assertFalse(ok)
        self.assertEqual(msgs, ["20 keys at most: remove one first."])

    def test_remove_by_fingerprint_drops_every_line_of_that_key(self):
        a, b = CASES[0], CASES[1]
        user = FakeUser(7, [a["line"], b["line"], a["line"].replace(" Laptop", " copy"), "garbage"])
        ok, msgs, user, ak = self.run_policy({"ssh_key_fp": a["fingerprint"]}, user)
        self.assertTrue(ok, msgs)
        self.assertEqual(user.attributes["sshPublicKey"], [b["line"], "garbage"])
        self.assertEqual(ak.locked, 1)
        ok, msgs, user, _ = self.run_policy({"ssh_key_fp": a["fingerprint"]}, user)
        self.assertTrue(ok)
        self.assertEqual(msgs, ["That key wasn't on your account any more."])
        ok, msgs, _, _ = self.run_policy({"ssh_key_fp": "laptop"}, user)
        self.assertEqual((ok, msgs), (False, ["Pick one of your keys."]))

    def test_refusals_before_anything_else(self):
        add = {"ssh_key_title": "Laptop", "ssh_key": CASES[0]["text"]}
        for kw, msg in [({"ip": "203.0.113.7"}, "Adding and removing SSH keys works over NetBird."),
                        ({"ip": "172.21.1.11"}, "Adding and removing SSH keys works over NetBird."),
                        ({"session": {"authentik/impersonate/user": 1}}, "Not while impersonating someone."),
                        ({"http_user": FakeUser(8, [])}, "Sign in again, then try once more."),
                        ({"used": {"webauthn": 6}}, "Confirm with your passkey again: start over from the SSH keys sheet."),
                        ({"used": {"totp": 1}}, "Confirm with your passkey again: start over from the SSH keys sheet.")]:
            with self.subTest(kw):
                ok, msgs, user, _ = self.run_policy(add, **kw)
                self.assertEqual((ok, msgs, user.saved), (False, [msg], []))
        # this PC itself, and the test's swap of the step to one-time codes
        self.assertTrue(self.run_policy(add, ip="172.21.0.1")[0])
        self.assertTrue(self.run_policy(add, used={"static": 1}, classes=("static",))[0])

    def test_admin_ui_test_never_writes(self):
        ok, _, user, _ = self.run_policy({"ssh_key_title": "Laptop", "ssh_key": CASES[0]["text"]}, debug=True)
        self.assertTrue(ok)
        self.assertEqual((user.saved, user.attributes["sshPublicKey"]), ([], []))

    def test_never_raises(self):
        user = FakeUser(7, [])
        user.save = lambda update_fields=None: 1 / 0
        ok, msgs, _, _ = self.run_policy({"ssh_key_title": "Laptop", "ssh_key": CASES[0]["text"]}, user)
        self.assertEqual((ok, msgs), (False, ["Something went wrong; nothing was changed."]))


class Blueprint(unittest.TestCase):
    def test_the_passkey_every_time(self):
        e = entry("name: ssh-keys-passkey")
        for want in ("device_classes: [webauthn]", "webauthn_user_verification: required",
                     "not_configured_action: deny", "last_auth_threshold: seconds=0"):
            self.assertIn(want, e)

    def test_flows_ssh_admins_on_netbird(self):
        for slug, form in (("ssh-key-add", "add-form"), ("ssh-key-remove", "remove-form")):
            e = entry(f"slug: {slug}")
            for want in ("designation: stage_configuration", "authentication: require_authenticated",
                         "policy_engine_mode: all"):
                self.assertIn(want, e)
            fid = re.search(r"id: (\S+)", e)[1]
            binds = [p for p in re.split(r"\n(?=  - model: )", BLUEPRINT) if f"target: !KeyOf {fid}" in p]
            self.assertTrue(any("group: !KeyOf ssh-admins" in p for p in binds))
            self.assertTrue(any("policy: !KeyOf from-netbird" in p for p in binds))
            stages = [re.search(r"stage: !KeyOf (\S+)", p)[1] for p in binds if "flowstagebinding" in p]
            self.assertEqual(stages, ["passkey", form])   # nothing after the form: the flow ends, ?next= leads back
        self.assertNotIn("redirectstage", BLUEPRINT)
        self.assertIn("state: created", entry("name: ssh-admins"))

    def test_netbird_policy(self):
        code = block("name: ssh-keys-from-netbird", "expression")
        last = str(ipaddress.ip_network("100.64.0.0/10")[-2])   # the range's top end
        for ip, ok in (("100.64.1.2", True), (last, True), ("172.21.0.1", True), ("172.21.1.11", False),
                       ("203.0.113.7", False), ("10.0.0.5", False), ("::1", False)):
            msgs = []
            self.assertIs(run_expression(code, ak_client_ip=ipaddress.ip_address(ip), ak_message=msgs.append,
                                         ip_address=ipaddress.ip_address, ip_network=ipaddress.ip_network), ok, ip)

    def test_static_text_holds_no_user_data(self):
        for name in ("name: ssh-key-help", "name: ssh-key-remove-warning"):
            e = entry(name)
            self.assertNotIn("expression: true", e)
            self.assertNotIn("_expression", e)

    def test_choices_and_preselection(self):
        a, b = CASES[0], CASES[4]
        user = types.SimpleNamespace(attributes={"sshPublicKey": [a["line"], b["line"], "garbage", 7]})
        choices = run_expression(block("name: ssh-key-fingerprint", "placeholder"), user=user, request=None, prompt_context={})
        self.assertEqual(choices, [{"label": "Laptop · " + a["fingerprint"][:19] + "…", "value": a["fingerprint"]},
                                   {"label": "Arjun's server (old) · " + b["fingerprint"][:19] + "…", "value": b["fingerprint"]}])
        self.assertEqual(run_expression(block("name: ssh-key-fingerprint", "placeholder"),
                                        user=types.SimpleNamespace(attributes=None), request=None, prompt_context={}), [])
        initial = block("name: ssh-key-fingerprint", "initial_value")
        from urllib.parse import urlencode

        class QD(dict):   # Django's QueryDict, the part used
            def __init__(self, q):
                from urllib.parse import parse_qsl
                super().__init__(parse_qsl(q))
        django_http = types.SimpleNamespace(QueryDict=QD)
        sys.modules["django.http"], saved = django_http, sys.modules.get("django.http")
        try:
            http = types.SimpleNamespace(GET={"query": urlencode({"fp": a["fingerprint"], "next": "/ssh-keys/done"})})
            self.assertEqual(run_expression(initial, request=types.SimpleNamespace(http_request=http), user=None,
                                            prompt_context={}), a["fingerprint"])
            self.assertEqual(run_expression(initial, request=http, user=None, prompt_context={}), a["fingerprint"])
            self.assertEqual(run_expression(initial, request=types.SimpleNamespace(http_request=None), user=None,
                                            prompt_context={}), "")
        finally:
            sys.modules.pop("django.http")
            if saved:
                sys.modules["django.http"] = saved


# ── The job: lib/ssh_keys.py refresh with stub tools ─────────────────────────────────────────────────────────────

STUB = r"""#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["CALLS"], "a") as f:
    f.write(json.dumps({"tool": name, "argv": sys.argv[1:], "task": json.loads(os.environ.get("AK_TASK", "null")),
                        "stdin": sys.stdin.read() if name == "notify.sh" else None}) + "\n")
state = json.load(open(os.environ["STUB_STATE"]))
if name == "sss_ssh_authorizedkeys":
    print("\n".join(state["sss"]))
elif name == "journalctl":
    for e in state["journal"]:
        print(json.dumps(e))
elif name == "docker":
    task = json.loads(os.environ["AK_TASK"])["task"]
    if task == "keys":
        print("OUT:" + json.dumps({"user": "alex", "keys": state["authentik"], "event": state.get("event")}))
    else:
        print("OUT:✓ " + task)
"""


class Job(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        r = self.repo = self.tmp / "repo"
        for rel in ["lib/ssh_keys.py", "lib/authentik.py", "lib/authentik_tasks.py", "lib/render.py", "lib/setup/engine.py"]:
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, r / rel)
        shutil.copytree(ROOT / "setup", r / "setup")
        (r / ".env").write_text((ROOT / "tests/fixtures/full.env").read_text())
        (r / "ops/keys").mkdir(parents=True)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        for tool in ("docker", "sss_ssh_authorizedkeys", "journalctl"):
            (bin_ / tool).write_text(STUB)
            (bin_ / tool).chmod(0o755)
        (r / "notify.sh").write_text(STUB)
        (r / "notify.sh").chmod(0o755)
        self.calls, self.state = self.tmp / "calls", self.tmp / "stub.json"
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CALLS=str(self.calls),
                        STUB_STATE=str(self.state), DOCKER_HOST="unix:///nonexistent/docker.sock",
                        HOME=str(self.tmp / "home"), SSH_KEYS_STATE=str(self.tmp / "home/state.json"))
        self.env.pop("SSH_USER", None)
        self.a, self.b = CASES[0], CASES[1]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_job(self, authentik, sss, journal=(), event=None):
        self.state.write_text(json.dumps({"authentik": authentik, "sss": sss, "journal": list(journal), "event": event}))
        self.calls.unlink(missing_ok=True)
        r = subprocess.run([sys.executable, str(self.repo / "lib/ssh_keys.py")], capture_output=True, text=True,
                           env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = [json.loads(x) for x in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return json.loads((self.repo / "ops/data/ssh-keys.json").read_text()), calls

    def test_follows_authentik_and_ssh(self):
        a, b = self.a, self.b
        # first run: both keys, SSH takes one so far; no mail (nothing to compare with)
        out, calls = self.run_job([a["line"], b["line"]], [a["line"]])
        self.assertEqual([k["fingerprint"] for k in out["keys"]], [a["fingerprint"], b["fingerprint"]])
        self.assertEqual([k["accepted"] for k in out["keys"]], [True, False])
        self.assertEqual([k["added"] for k in out["keys"]], [None, None])
        self.assertEqual(out["user"], "alex")
        self.assertNotIn("notify.sh", [c["tool"] for c in calls])
        # nothing changed: Authentik isn't asked (no docker), SSH is
        out, calls = self.run_job([a["line"], b["line"]], [a["line"]])
        self.assertNotIn("docker", [c["tool"] for c in calls])
        # the sheet asks: Authentik is read; b was removed there, SSH still takes it for a minute; a mail, the outpost
        (self.repo / "ops/keys/refresh").write_text("alex\n")
        ev = {"time": "2026-11-08T10:00:00+00:00", "client_ip": "100.64.1.2", "by": "alex"}
        out, calls = self.run_job([a["line"]], [a["line"], b["line"]], event=ev)
        self.assertFalse((self.repo / "ops/keys/refresh").exists())
        self.assertEqual([k["fingerprint"] for k in out["keys"]], [a["fingerprint"]])
        self.assertEqual(out["still_accepted"], [b["fingerprint"]])
        tasks = [c["task"]["task"] for c in calls if c["tool"] == "docker"]
        self.assertEqual(tasks, ["keys", "ldap_refresh"])
        (mail,) = [c for c in calls if c["tool"] == "notify.sh"]
        self.assertEqual(mail["argv"], ["send", "Homelab: SSH key removed for alex"])
        self.assertIn(b["fingerprint"], mail["stdin"])
        self.assertIn("from 100.64.1.2, signed in as alex", mail["stdin"])
        # a key added: its date is now; used (VERBOSE "Partial publickey"), others' and other users' lines don't count
        (self.repo / "ops/keys/refresh").write_text("alex\n")
        j = [{"__CURSOR": "c1", "__REALTIME_TIMESTAMP": "1794000000000000",
              "MESSAGE": f"Partial publickey for alex from 100.64.1.9 port 50000 ssh2: ED25519 {a['fingerprint']}"},
             {"__CURSOR": "c2", "__REALTIME_TIMESTAMP": "1794000500000000",
              "MESSAGE": f"Partial publickey for mallory from 100.64.1.9 port 50001 ssh2: ECDSA {b['fingerprint']}"},
             {"__CURSOR": "c3", "__REALTIME_TIMESTAMP": "1794000600000000", "MESSAGE": "Accepted keyboard-interactive/pam for alex"}]
        out, calls = self.run_job([a["line"], b["line"]], [a["line"], b["line"]], journal=j)
        keys = {k["fingerprint"]: k for k in out["keys"]}
        self.assertEqual(keys[a["fingerprint"]]["last_used"], 1794000000)
        self.assertIsNone(keys[b["fingerprint"]]["last_used"])
        self.assertIsInstance(keys[b["fingerprint"]]["added"], int)
        self.assertIsNone(keys[a["fingerprint"]]["added"])
        (mail,) = [c for c in calls if c["tool"] == "notify.sh"]
        self.assertEqual(mail["argv"], ["send", "Homelab: SSH key added for alex"])
        self.assertIn("no address is known", mail["stdin"])
        # the journal is read on from its cursor
        _, calls = self.run_job([a["line"], b["line"]], [a["line"], b["line"]])
        (jc,) = [c for c in calls if c["tool"] == "journalctl"]
        self.assertIn("--after-cursor=c3", jc["argv"])
        # the state and the report: the report readable by ops, the state only by you
        self.assertEqual(oct(os.stat(self.tmp / "home/state.json").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.repo / "ops/data/ssh-keys.json").st_mode & 0o777), "0o644")

    def test_sssd_cant_say(self):
        (self.tmp / "bin/sss_ssh_authorizedkeys").write_text("#!/bin/sh\nexit 1\n")
        out, _ = self.run_job([self.a["line"]], [])
        self.assertEqual([k["accepted"] for k in out["keys"]], [None])


if __name__ == "__main__":
    unittest.main()
