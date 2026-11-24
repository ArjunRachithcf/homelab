#!/usr/bin/env python3
"""wishlist/draw.py: draw names for a gift exchange, beside Wishlist (wishlist/README.md "Draw names").

  plan EVENT.toml                      who takes part, whether a draw is possible; shows no pair
  draw EVENT.toml [--redraw]           draws, stores the pairs encrypted to BACKUP_AGE_RECIPIENT, mails each giver
                                       the one person they give to, with a link to that person's list
  resend EVENT.toml LOGIN --identity KEY   mails LOGIN their person again (decrypts with your age key; prints no pair)

Nobody sees the whole draw: not on the terminal, not in a log, not in a plaintext file. The pairs are kept in
~/.local/state/homelab/draws/<event>.age, readable only with the backup's private key (not on this box).

EVENT.toml (an instance's own file: household names stay out of the public repo; example: draw.example.toml):
  name = "Christmas 2026"
  budget = "CHF 50"                  # optional
  note = "Gifts on the 24th."        # optional
  participants = ["alice", "bob", "carol", "dave"]   # Authentik usernames, each signed in to Wishlist once
  exclude = [["alice", "bob"]]       # neither draws the other (couples)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "homelab/draws"
TRIES = 10000


class DrawError(Exception):
    pass


def env_get(key: str, env_file: Path = ROOT / ".env") -> str:
    value = ""
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def load_event(path: Path) -> dict:
    event = tomllib.loads(path.read_text())
    people = event.get("participants") or []
    if not event.get("name"):
        raise DrawError(f"{path}: name missing")
    if len(people) < 2 or len(set(people)) != len(people):
        raise DrawError(f"{path}: participants must be at least two different logins")
    for pair in event.get("exclude", []):
        if len(pair) != 2 or not set(pair) <= set(people):
            raise DrawError(f"{path}: exclude {pair}: two logins from participants")
    return event


def draw(people: list[str], exclude: list[list[str]], rng: random.Random) -> dict[str, str]:
    """giver -> receiver: nobody draws themselves or someone they're excluded with."""
    forbidden = {(p, p) for p in people}
    for a, b in exclude:
        forbidden |= {(a, b), (b, a)}
    for _ in range(TRIES):
        receivers = people[:]
        rng.shuffle(receivers)
        pairs = dict(zip(people, receivers))
        if not any((g, r) in forbidden for g, r in pairs.items()):
            return pairs
    raise DrawError("no draw is possible with these exclusions")


def people_in_wishlist() -> dict[str, dict]:
    out = subprocess.run([str(ROOT / "wishlist/setup.sh"), "people"], check=True, capture_output=True, text=True)
    return {p["username"]: p for p in json.loads(out.stdout.strip().splitlines()[-1])}


def check_people(event: dict, known: dict[str, dict]) -> None:
    missing = [p for p in event["participants"] if p not in known]
    if missing:
        raise DrawError(f"not signed in to Wishlist yet: {', '.join(missing)}")
    nolist = [p for p in event["participants"] if not known[p].get("list")]
    if nolist:
        raise DrawError(f"no list of their own in Wishlist: {', '.join(nolist)}")


def mail_body(event: dict, giver: dict, receiver: dict, base: str, brand: str) -> str:
    lines = [f"Hi {giver['name']},", "",
             f"{event['name']}: you're giving a gift to {receiver['name']}.", "",
             f"Their wish list: {base}/lists/{receiver['list']}"]
    if event.get("budget"):
        lines.append(f"Budget: {event['budget']}")
    if event.get("note"):
        lines += ["", event["note"]]
    lines += ["", "Keep it a secret. What you claim on their list stays hidden from them.", "", f"{brand}"]
    return "\n".join(lines) + "\n"


def send(to: str, subject: str, body: str) -> None:
    subprocess.run([str(ROOT / "notify.sh"), "send", subject], input=body, text=True, check=True,
                   env={**os.environ, "NOTIFY_TO": to}, stdout=subprocess.DEVNULL)


def store(path: Path, recipient: str, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    part = path.with_suffix(".age.part")
    subprocess.run(["age", "-r", recipient, "-o", str(part)], input=json.dumps(record), text=True, check=True)
    part.replace(path)


def load(path: Path, identity: Path) -> dict:
    out = subprocess.run(["age", "-d", "-i", str(identity), str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def mail_all(event: dict, pairs: dict[str, str], known: dict[str, dict], only: str | None = None) -> list[str]:
    base = f"https://gifts.{env_get('DOMAIN')}:{env_get('NETBIRD_PROXY_PORT') or '9443'}"
    brand = env_get("BRAND") or "Home"
    failed = []
    for giver, receiver in pairs.items():
        if only and giver != only:
            continue
        try:
            send(known[giver]["email"], f"{event['name']}: your draw",
                 mail_body(event, known[giver], known[receiver], base, brand))
        except subprocess.CalledProcessError:
            failed.append(giver)
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").add_argument("event", type=Path)
    d = sub.add_parser("draw")
    d.add_argument("event", type=Path)
    d.add_argument("--redraw", action="store_true", help="draw again although a draw exists (everyone gets a new mail)")
    r = sub.add_parser("resend")
    r.add_argument("event", type=Path)
    r.add_argument("login")
    r.add_argument("--identity", type=Path, required=True, help="the age private key (your laptop's)")
    a = ap.parse_args(argv)
    try:
        event = load_event(a.event)
        known = people_in_wishlist()
        check_people(event, known)
        saved = STATE / f"{a.event.stem}.age"
        if a.cmd == "plan":
            draw(event["participants"], event.get("exclude", []), random.SystemRandom())
            print(f"{event['name']}: {len(event['participants'])} people, {len(event.get('exclude', []))} exclusion(s);"
                  f" a draw is possible{' (one exists: ' + str(saved) + ')' if saved.exists() else ''}.")
            return 0
        if a.cmd == "draw":
            if saved.exists() and not a.redraw:
                raise DrawError(f"already drawn ({saved}); --redraw draws again, resend mails one person again")
            recipient = env_get("BACKUP_AGE_RECIPIENT")
            if not recipient.startswith("age1"):
                raise DrawError("BACKUP_AGE_RECIPIENT (age1…) missing in .env")
            pairs = draw(event["participants"], event.get("exclude", []), random.SystemRandom())
            store(saved, recipient, {"event": event["name"], "drawn": datetime.now(timezone.utc).isoformat(),
                                     "pairs": pairs})
            failed = mail_all(event, pairs, known)
        else:
            record = load(saved, a.identity)
            if a.login not in record["pairs"]:
                raise DrawError(f"{a.login} isn't in this draw")
            failed = mail_all(event, record["pairs"], known, only=a.login)
        sent = (len(event["participants"]) if a.cmd == "draw" else 1) - len(failed)
        print(f"{event['name']}: {sent} mail(s) sent; the draw is in {saved} (encrypted).")
        if failed:
            print(f"not sent: {', '.join(failed)} (resend LOGIN --identity KEY)", file=sys.stderr)
            return 1
        return 0
    except (DrawError, FileNotFoundError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as e:
        print(f"draw.py: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
