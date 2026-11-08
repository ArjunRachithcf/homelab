#!/usr/bin/env python3
"""~/homelab/lib/ssh_keys.py: the SSH keys in Authentik's `sshPublicKey`, for the home page's SSH keys sheet.

What a key must be is the same here as in the flows' policy (authentik/ssh-sso.yaml, which runs inside Authentik and
can't import this file); tests/unit/test_ssh_keys.py runs the same cases through both.

  python3 lib/ssh_keys.py   the job ssh-keys (homelab-ssh-keys.{service,timer,path}, RUNBOOK.md step 10):
                            what SSH accepts now (sss_ssh_authorizedkeys, no root needed), Authentik's list when that
                            changed, when the sheet asked (ops/keys/refresh) or after an hour, and when each key was
                            last used (sshd's "publickey" lines, LogLevel VERBOSE in ssh/10-homelab.conf; kept, as the
                            journal may not go back far). Writes ops/data/ssh-keys.json; when the keys changed, has
                            the outpost reload them and mails NOTIFY_TO what changed, when and from where.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
         "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")
POINT = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
TITLE = re.compile(r"[A-Za-z0-9 ._@()'+,-]{1,50}")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
NOT_A_KEY = "That isn't a public key: paste the line from the .pub file."
# sshd at LogLevel VERBOSE: "Partial publickey for USER from IP port N ssh2: ED25519 SHA256:…" (the key, before the
# code) and "Accepted publickey …" (a key alone, if ever allowed)
USED = re.compile(r"^(?:Partial|Accepted) publickey for (\S+) from (\S+) port \d+ ssh2: \S+ (SHA256:[A-Za-z0-9+/]{43})")
AUTHENTIK_EVERY = 3600


class Refused(ValueError):
    """A key (or title) this homelab won't store; the message says why, never what was pasted."""


def fields(blob: bytes) -> list[bytes] | None:
    out, i = [], 0
    while i < len(blob):
        if len(blob) - i < 4:
            return None
        n = struct.unpack(">I", blob[i:i + 4])[0]
        i += 4
        if n > len(blob) - i:
            return None
        out.append(blob[i:i + n])
        i += n
    return out


def decoded(b64: str) -> bytes | None:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def blob_of(line: str) -> bytes | None:
    """The key of a stored line (whatever its comment), or None"""
    p = line.split() if isinstance(line, str) else []
    return decoded(p[1]) if len(p) >= 2 else None


def fingerprint(blob: bytes) -> str:
    """As ssh-keygen -l shows it"""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def check(kind: str, blob: bytes | None) -> str | None:
    """None if SSH here takes this key, else why not"""
    f = fields(blob) if blob else None
    if not f or f[0] != kind.encode():
        return NOT_A_KEY
    if kind == "ssh-ed25519" and len(f) == 2 and len(f[1]) == 32:
        return None
    if kind == "sk-ssh-ed25519@openssh.com" and len(f) == 3 and len(f[1]) == 32:
        return None
    if kind.startswith("ecdsa-sha2-") and len(f) == 3 and f[1] == kind[11:].encode() \
            and len(f[2]) == POINT.get(kind[11:]) and f[2][:1] == b"\x04":
        return None
    if kind == "sk-ecdsa-sha2-nistp256@openssh.com" and len(f) == 4 and f[1] == b"nistp256" \
            and len(f[2]) == 65 and f[2][:1] == b"\x04":
        return None
    if kind == "ssh-rsa" and len(f) == 3:
        if int.from_bytes(f[2], "big").bit_length() >= 3072:
            return None
        return "RSA keys need 3072 bits or more; an ed25519 key is better anyway."
    return NOT_A_KEY


def parse(text: str) -> tuple[str, bytes]:
    """(kind, key) of one pasted public key line, in the policy's order of checks; Refused otherwise"""
    if "PRIVATE KEY" in text or "Private-Lines" in text:
        raise Refused("That's a private key: never paste it anywhere. Paste the line from the .pub file.")
    if "BEGIN SSH2 PUBLIC KEY" in text:
        raise Refused("Paste the OpenSSH form: ssh-keygen -i -f key.pub turns this one into it.")
    lines = [x for x in text.splitlines() if x.strip()]
    if len(lines) != 1:
        raise Refused("One key at a time: the single line from the .pub file.")
    p = lines[0].split()
    if len(p) < 2:
        raise Refused(NOT_A_KEY)
    if p[0] not in KINDS:
        if "-cert-" in p[0]:
            raise Refused("A certificate won't do here: paste the key itself, the line from the .pub file.")
        if any(c in p[0] for c in "=,\""):
            raise Refused("Just the key, with nothing in front of it.")
        raise Refused("SSH here takes ed25519, ecdsa and RSA keys of 3072 bits or more, also on a security key.")
    blob = decoded(p[1])
    why = check(p[0], blob)
    if why:
        raise Refused(why)
    return p[0], blob


def title_of(text: str) -> str:
    t = " ".join(str(text or "").split())
    if not TITLE.fullmatch(t):
        raise Refused("Title: up to 50 letters, digits and spaces, and . _ @ ( ) ' + , -")
    return t


def stored_line(kind: str, blob: bytes, title: str) -> str:
    """The line Authentik keeps: rebuilt from the key, never the pasted text"""
    return f"{kind} {base64.b64encode(blob).decode()} {title}"


def cli_title(comment: str) -> str:
    """A pasted line's comment as its title (the command line has no title field)"""
    t = " ".join(re.sub(r"[^A-Za-z0-9 ._@()'+,-]", " ", comment).split())[:50].strip()
    return t or "key"


def describe(line: str) -> dict | None:
    """What the sheet shows of a stored line"""
    p = line.split() if isinstance(line, str) else []
    blob = blob_of(line)
    f = fields(blob) if blob else None
    if not f:
        return None
    kind = p[0]
    label = {"ssh-ed25519": "ED25519", "sk-ssh-ed25519@openssh.com": "ED25519 security key",
             "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA security key"}.get(kind)
    if kind.startswith("ecdsa-sha2-"):
        label = "ECDSA P-" + kind.removeprefix("ecdsa-sha2-nistp")
    if kind == "ssh-rsa" and len(f) == 3:
        label = f"RSA {int.from_bytes(f[2], 'big').bit_length()}"
    return {"title": " ".join(p[2:]) or "no title", "type": label or kind, "fingerprint": fingerprint(blob)}


# ── The job ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def read_env() -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "lib"))
    import render  # noqa: PLC0415
    return render.read_env(Path(os.environ.get("ENV_FILE") or ROOT / ".env"))


def accepted(user: str) -> list[str] | None:
    """The fingerprints SSH accepts for USER now (SSSD's view), or None when SSSD can't say"""
    try:
        r = subprocess.run(["sss_ssh_authorizedkeys", user], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return sorted({fingerprint(b) for b in map(blob_of, r.stdout.splitlines()) if b})


def journal_uses(cursor: str | None) -> tuple[dict[str, dict], str | None]:
    """{fingerprint: {time, from}} of sshd's key lines since CURSOR (the last 30 days the first time), new cursor"""
    args = ["journalctl", "-u", "sshd", "-o", "json", "--no-pager", "--output-fields=MESSAGE,__REALTIME_TIMESTAMP"]
    args += [f"--after-cursor={cursor}"] if cursor else ["--since=-30d"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}, cursor
    uses, last = {}, cursor
    for raw in r.stdout.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        last = e.get("__CURSOR") or last
        m = USED.match(str(e.get("MESSAGE") or ""))
        if m:
            t = int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000
            if t >= uses.get(m[3], {}).get("time", 0):
                uses[m[3]] = {"time": t, "from": m[2], "user": m[1]}
    return uses, last


def write_json(path: Path, obj: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def ak(task: str, **args) -> list[str]:
    """A task of lib/authentik_tasks.py in Authentik's worker (lib/authentik.py's ak(), imported only here: it
    imports this file)"""
    sys.path.insert(0, str(ROOT / "lib"))
    import authentik as cli  # noqa: PLC0415
    try:
        return cli.ak(task, **args)
    except cli.Failed as err:
        raise RuntimeError(str(err)) from None


def mail(user: str, added: list[dict], removed: list[dict], event: dict | None) -> None:
    what = []
    for k in added:
        what.append(f"added:   “{k['title']}”, {k['type']}, {k['fingerprint']}")
    for k in removed:
        what.append(f"removed: “{k['title']}”, {k['type']}, {k['fingerprint']}")
    head = "SSH key " + ("added" if added and not removed else "removed" if removed and not added else "changes")
    lines = [f"The SSH keys of {user} on this homelab changed:", "", *what, ""]
    if event and event.get("by") == "authentik-shell":
        lines.append(f"When: {event.get('time', '?')}, on this PC's command line (setup-ssh.sh).")
    elif event:
        lines.append(f"When: {event.get('time', '?')}, from {event.get('client_ip') or 'an unknown address'}"
                     f"{', signed in as ' + event['by'] if event.get('by') else ''} (Authentik's event log has it too).")
    else:
        lines.append("No change to the account in Authentik's event log in the last half hour, so no address is known.")
    lines += ["", f"Not you? Remove the key on the home page (SSH keys, over NetBird) or with "
              f"./authentik/setup-ssh.sh remove-key FINGERPRINT, then change your password."]
    subprocess.run([str(ROOT / "notify.sh"), "send", f"Homelab: {head} for {user}"], input="\n".join(lines) + "\n",
                   text=True, capture_output=True, timeout=120, check=False)


def refresh() -> int:
    # first: homelab-ssh-keys.path starts this as long as the file is there
    trigger = ROOT / "ops/keys/refresh"
    asked = trigger.exists()
    trigger.unlink(missing_ok=True)
    env = read_env()
    user = os.environ.get("SSH_USER") or env.get("ADMIN_USER", "")
    if not user:
        print("✗ ADMIN_USER missing in .env", file=sys.stderr)
        return 1
    state_file = Path(os.environ.get("SSH_KEYS_STATE") or Path.home() / ".local/state/homelab/ssh-keys.json")
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    now = int(time.time())
    ssh = accepted(user)
    old_keys = state.get("keys")
    authentik, event = state.get("authentik"), None
    if asked or authentik is None or ssh != state.get("accepted") or now - state.get("authentik_at", 0) >= AUTHENTIK_EVERY:
        try:
            got = json.loads(ak("keys", name=user)[0])
        except (RuntimeError, IndexError, ValueError) as err:
            print(f"✗ Authentik's list of keys: {err}", file=sys.stderr)
            return 1
        authentik, event = got["keys"], got.get("event")
        state["authentik"], state["authentik_at"] = authentik, now
    keys = [d for d in map(describe, authentik) if d]
    fps = {k["fingerprint"] for k in keys}
    if old_keys is not None and fps != {k["fingerprint"] for k in old_keys}:
        added = [k for k in keys if k["fingerprint"] not in {o["fingerprint"] for o in old_keys}]
        removed = [o for o in old_keys if o["fingerprint"] not in fps]
        try:
            ak("ldap_refresh")   # the outpost reloads its users in seconds; SSSD follows within a minute
        except RuntimeError as err:
            print(f"· the outpost wasn't asked to reload: {err}", file=sys.stderr)
        mail(user, added, removed, event)
        print(f"✓ {len(added)} added, {len(removed)} removed: mailed")
    uses, state["cursor"] = journal_uses(state.get("cursor"))
    used = state.setdefault("used", {})
    for fp, u in uses.items():
        if u["user"].lower() == user.lower() and u["time"] >= used.get(fp, {}).get("time", 0):
            used[fp] = {"time": u["time"], "from": u["from"]}
    # a removed key's date goes with it: added again, it's new again
    seen = {fp: t for fp, t in state.get("added", {}).items() if fp in fps}
    state["added"] = seen
    for k in keys:
        # first seen here; the keys there were before the first run show no date
        seen.setdefault(k["fingerprint"], now if old_keys is not None else None)
        k["added"] = seen[k["fingerprint"]]
        k["last_used"] = used.get(k["fingerprint"], {}).get("time")
        k["accepted"] = None if ssh is None else k["fingerprint"] in ssh
    state["keys"], state["accepted"] = keys, ssh
    write_json(ROOT / "ops/data/ssh-keys.json", {
        "time": now, "user": user, "keys": keys,
        # removed in Authentik, but SSH still takes them until SSSD's cache (a minute) runs out
        "still_accepted": sorted(set(ssh or []) - fps)})
    write_json(state_file, state, 0o600)
    print(f"✓ {len(keys)} key(s) of {user}, SSH accepts {'?' if ssh is None else len(ssh)}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["refresh"]):
        sys.exit(__doc__)
    sys.exit(refresh())
