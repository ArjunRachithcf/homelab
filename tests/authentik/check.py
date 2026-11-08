"""tests/authentik/check.py: the SSH keys flows' cases against the test's real Authentik (tests/authentik/run.sh sets
up the project, the users and this environment). Each case one line, ✓ or ✗; exits 1 when any failed.

  python3 check.py WORK   (WORK: the test's folder: creds.json, the keys it makes, the LDAP client's image name)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORK = Path(sys.argv[1])
REPO = WORK / "repo"
CREDS = json.loads((WORK / "creds.json").read_text())
LDAP_IMAGE = os.environ["LDAP_CLIENT_IMAGE"]
NETWORK = os.environ["COMPOSE_PROJECT_NAME"] + "_internal"
NETBIRD, PUBLIC = "100.64.1.2", "203.0.113.7"
OVER_NETBIRD = "Adding and removing SSH keys works over NetBird."
used = {u: 0 for u in CREDS}
passed = failed = 0


def check(what, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {what}")
    else:
        failed += 1
        print(f"  ✗ {what}{': ' + str(detail)[:400] if detail else ''}")


def compose(*args, **kw):
    return subprocess.run(["docker", "compose", *args], cwd=REPO, text=True, capture_output=True, **kw)


def client(box, scenario, user, n_codes, **extra):
    """The scenario's result lines; a failed sign-in counts as a failed check and gives empty results"""
    k = used[user]
    used[user] += n_codes
    args = {"user": user, "password": CREDS[user]["password"], "codes": CREDS[user]["codes"][k:k + n_codes], **extra}
    r = compose("exec", "-T", box, "python3", "/client.py", scenario, json.dumps(args), timeout=600)
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode or not lines or not lines[0].get("ok"):
        check(f"{scenario}: signed in as {user}", False, (lines[0].get("trace") if lines else None) or r.stderr[-300:])
        return [{} for _ in range(8)]
    return lines[1:] or [{}]


def ak(code):
    """OUT: lines of Python run in Authentik's worker"""
    r = compose("exec", "-T", "authentik-worker", "ak", "shell", "-c", code, timeout=300)
    return [x[4:].strip() for x in (r.stdout + r.stderr).splitlines() if x.startswith("OUT:")]


def stored(user="sam"):
    return json.loads(ak(f"""
import json
from authentik.core.models import User
print("OUT:" + json.dumps(User.objects.get(username={user!r}).attributes.get("sshPublicKey", [])))
""")[0])


def keygen(d, kind, bits=None):
    k = Path(d, f"{kind}{bits or ''}")
    subprocess.run(["ssh-keygen", "-q", "-t", kind, *(["-b", str(bits)] if bits else []), "-N", "", "-C",
                    "made-by-the-test", "-f", str(k)], check=True, stdin=subprocess.DEVNULL)
    pub = k.with_suffix(".pub").read_text().strip()
    fp = subprocess.run(["ssh-keygen", "-lf", str(k.with_suffix(".pub"))], capture_output=True, text=True,
                        check=True).stdout.split()[1]
    return {"pub": pub, "blob": pub.split()[1], "fp": fp, "private": k.read_text()}


def rebuilt(key, title):
    return f"{key['pub'].split()[0]} {key['blob']} {title}"


def ldap(*args, bind="cn=ldap-ssh,ou=users,dc=ssh,dc=goauthentik,dc=io", password=None):
    env = dict(os.environ, PW=password or CREDS["_bind_password"])
    return subprocess.run(["docker", "run", "--rm", "--network", NETWORK, "-e", "PW", LDAP_IMAGE, "sh", "-c",
                           'ldapsearch -LLL -x -H ldap://authentik-ldap-ssh:3389 -D "$1" -w "$PW" '
                           '-o ldif-wrap=no -b ou=users,dc=ssh,dc=goauthentik,dc=io "$2" "$3"', "sh", bind, *args],
                          text=True, capture_output=True, env=env, timeout=120)


def main():
    tmp = tempfile.mkdtemp(dir=WORK)
    ed, ec, rsa, small = (keygen(tmp, "ed25519"), keygen(tmp, "ecdsa", 256), keygen(tmp, "rsa", 3072),
                          keygen(tmp, "rsa", 2048))
    ec384, ec521 = keygen(tmp, "ecdsa", 384), keygen(tmp, "ecdsa", 521)

    print("Adding (from a NetBird address, through the proxy)")
    cases = [("Laptop", ed["pub"], "/ssh-keys/done"),
             ("Work laptop", ec["pub"], "/ssh-keys/done"),
             ("Old server", rsa["pub"], "/ssh-keys/done"),
             ("private", ed["private"], "That's a private key: never paste it anywhere. Paste the line from the .pub file."),
             ("small", small["pub"], "RSA keys need 3072 bits or more; an ed25519 key is better anyway."),
             ("options", 'from="100.64.0.0/10" ' + ec384["pub"], "Just the key, with nothing in front of it."),
             ("two", ec384["pub"] + "\n" + ec521["pub"], "One key at a time: the single line from the .pub file."),
             ("again", ed["pub"].replace("made-by-the-test", "another comment"), "/ssh-keys/done"),
             ("<b>x</b>", ec521["pub"], "Title: up to 50 letters, digits and spaces, and . _ @ ( ) ' + , -")]
    out = client("proxy", "add", "sam", 1 + len(cases), xff=NETBIRD, keys=[[t, k] for t, k, _ in cases])
    check("one result per paste", len([x for x in out if x]) == len(cases), out)
    for (title, _, want), r in zip(cases, out):
        check(f"{title}: the passkey step first", r.get("first") == "ak-stage-authenticator-validate", r)
        got = r.get("to") if want.startswith("/") else (r.get("errors") or [None])[0]
        check(f"{title}: {want[:60]}", got == want, r)
    keys = stored()
    check("stored rebuilt from the key, the title as its comment",
          keys == [rebuilt(ed, "Laptop"), rebuilt(ec, "Work laptop"), rebuilt(rsa, "Old server")], keys)
    marker = ed["private"].splitlines()[1][:40]
    hits = ak(f"""
import json
from authentik.events.models import Event
print("OUT:", sum(1 for e in Event.objects.all() if {marker!r} in json.dumps(e.context, default=str)))
""")
    check("no event holds the pasted private key", hits == ["0"], hits)
    check("each change in Authentik's event log", int(ak("""
from authentik.events.models import Event
print("OUT:", Event.objects.filter(action="model_updated", context__model__model_name="user").count())
""")[0]) >= 3)

    print("Removing")
    r, *_ = client("proxy", "remove", "sam", 2, xff=NETBIRD, query="fp=" + ec["fp"].replace("+", "%2B").replace("/", "%2F"),
                  fp="SHA256:" + "A" * 43)
    fields = {f["field_key"]: f for f in (r.get("form") or {}).get("fields", [])}
    fp_field = fields.get("ssh_key_fp", {})
    check("the form lists your keys", [c["value"] for c in fp_field.get("choices") or []] == [ed["fp"], ec["fp"], rsa["fp"]], r)
    check("?fp= preselects that key", fp_field.get("initial_value") == ec["fp"], fp_field.get("initial_value"))
    check("a fingerprint not in the list refused", any("not a valid choice" in e for e in (r.get("result") or {}).get("errors", [])), r)
    r, *_ = client("proxy", "remove", "sam", 2, xff=NETBIRD, fp=ec["fp"])
    check("removed, back to the sheet", (r.get("result") or {}).get("to") == "/ssh-keys/done", r)
    check("that key gone, the others kept", stored() == [rebuilt(ed, "Laptop"), rebuilt(rsa, "Old server")])

    print("Only for ssh-admins on NetBird")
    r, *_ = client("proxy", "start", "sam", 1, xff=PUBLIC, slug="ssh-key-add")
    check("a public address refused", r.get("error_message") == OVER_NETBIRD, r)
    r, *_ = client("proxy", "cache-window", "sam", 2, xff=NETBIRD, public=PUBLIC, keys=[["cache", ec384["pub"]]])
    check("Authentik's cached pass lets the public address start", r.get("first") == "ak-stage-authenticator-validate", r)
    check("... and the form's own check refuses it", (r.get("result") or {}).get("errors") == [OVER_NETBIRD], r)
    r, *_ = client("neighbour", "start", "sam", 1, xff=NETBIRD, slug="ssh-key-add")
    check("a neighbour container claiming a NetBird address refused", r.get("error_message") == OVER_NETBIRD, r)
    r, *_ = client("proxy", "start", "other", 1, xff=NETBIRD, slug="ssh-key-add")
    check("not in ssh-admins refused", r.get("component") == "ak-stage-access-denied", r)
    out = client("proxy", "add", "sam", 2, xff=NETBIRD, no_csrf=True, keys=[["nocsrf", ec384["pub"]]])
    check("the form without the CSRF header refused", out and out[0].get("status") in (403, 500), out)
    check("... and nothing stored", stored() == [rebuilt(ed, "Laptop"), rebuilt(rsa, "Old server")])
    r, *_ = client("proxy", "two-tabs", "sam", 4, xff=NETBIRD, keys=[["Tab A", ec384["pub"]], ["Tab B", ec521["pub"]]])
    check("two tabs at the form, both saved", [x.get("to") for x in r.get("results", [])] == ["/ssh-keys/done"] * 2, r)
    check("... neither undid the other", stored()[-2:] == [rebuilt(ec384, "Tab A"), rebuilt(ec521, "Tab B")], stored())

    print("setup-ssh.sh and the SSH outpost")
    def served():
        """The keys the SSH outpost serves for sam (what SSSD would get), or why not"""
        r = ldap("(cn=sam)", "sshPublicKey")
        if r.returncode:
            return f"ldapsearch {r.returncode}: {r.stderr[-200:]}"
        return sorted(x.split(": ", 1)[1] for x in r.stdout.splitlines() if x.startswith("sshPublicKey: "))
    first = served()
    # (perhaps none yet: the flows don't reload the outpost, the host's job does)
    check("the outpost answers SSSD's search", isinstance(first, list), first)
    cli_env = dict(os.environ, SSH_USER="sam")
    r = subprocess.run([sys.executable, str(REPO / "lib/authentik.py"), "add-key"], input=ec["pub"] + "\n", text=True,
                       capture_output=True, env=cli_env, cwd=REPO, timeout=600)
    check("add-key", r.returncode == 0 and "outpost ldap-ssh reloads" in r.stdout, r.stdout + r.stderr)
    # the flows above don't reload the outpost (the host's job does): after add-key's reload it serves exactly
    # what Authentik holds
    want, t0, now = sorted(stored()), time.time(), first
    while time.time() - t0 < 60 and now != want:
        time.sleep(2)
        now = served()
    check(f"the outpost serves Authentik's keys within a minute ({time.time() - t0:.0f} s)", now == want, (now, want))
    r = subprocess.run([sys.executable, str(REPO / "lib/authentik.py"), "list-keys"], text=True, capture_output=True,
                       env=cli_env, cwd=REPO, timeout=600)
    check("list-keys shows fingerprints", ec["fp"] in r.stdout and ed["fp"] in r.stdout, r.stdout)
    r = subprocess.run([sys.executable, str(REPO / "lib/authentik.py"), "remove-key", ec["fp"], ec384["fp"]], text=True,
                       capture_output=True, env=cli_env, cwd=REPO, timeout=600)
    check("remove-key", r.returncode == 0 and stored() == [rebuilt(ed, "Laptop"), rebuilt(rsa, "Old server"),
                                                           rebuilt(ec521, "Tab B")], r.stdout + r.stderr)
    k = used["sam"]
    used["sam"] += 1
    r = ldap("(cn=sam)", "cn", bind="cn=sam,ou=users,dc=ssh,dc=goauthentik,dc=io",
             password=CREDS["sam"]["password"] + ";" + CREDS["sam"]["codes"][k])
    check("an LDAP bind as SSSD does it (password;code) still works", r.returncode == 0 and "cn: sam" in r.stdout,
          r.stderr[-200:])
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
