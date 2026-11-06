#!/usr/bin/env python3
"""~/homelab/lib/render.py: fill __KEY__ placeholders from .env (plan Phase 1, "render step").

    render.py [--env FILE] SRC DST       render one file (DST "-" = stdout)
    render.py [--env FILE] --keys        print every placeholder value this .env gives (debug, no secrets used)

.env is read like the scripts and ansible/lookup_plugins/homelab_env.py read it: the last KEY= line
wins, one pair of surrounding quotes is removed. Only the keys in ALLOWED (and the derived ones) may
appear as placeholders, so a template can never pull a secret into a file. An unknown placeholder or
an empty value stops with an error instead of rendering a half-filled file.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Instance facts a rendered file may contain. Never add a secret here.
ALLOWED = {
    "DOMAIN", "DOMAIN_ALIAS", "NETBIRD_PROXY_PORT", "NETBIRD_IP",
    "ADMIN_USER", "ADMIN_LOGIN", "ADMIN_NAME", "NOTIFY_TO", "NETBIRD_PEER",
    "BRAND", "BRAND_SLUG", "SIGNIN_TITLE", "PHONE_EXAMPLE", "HOME_COUNTRY", "HOME_COUNTRY_NAME",
}
DEFAULTS = {"NETBIRD_PROXY_PORT": "9443"}
# What each value may look like. Values land unescaped in YAML scalars, Python strings inside Authentik
# expression policies, Django templates, HTML, JSON and CSS, so free text is letters, digits, spaces and
# . , + ( ) / - only: no quotes, braces, %, #, :, <, >, &, backslash or newline (BRAND={…} would be code
# Authentik runs). A key not listed here gets TEXT.
TEXT = re.compile(r"[\w .,+()/-]{1,80}")
FORMAT = {
    "DOMAIN": re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"),
    "DOMAIN_ALIAS": re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"),
    "NETBIRD_PROXY_PORT": re.compile(r"[0-9]{1,5}"),
    "NETBIRD_IP": re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}"),
    "ADMIN_USER": re.compile(r"[A-Za-z0-9._-]{1,64}"),
    "ADMIN_LOGIN": re.compile(r"[A-Za-z0-9._@+-]{1,150}"),
    "NOTIFY_TO": re.compile(r"[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,253}"),
    "NETBIRD_PEER": re.compile(r"[A-Za-z0-9._-]{1,63}"),
    "BRAND_SLUG": re.compile(r"[a-z0-9-]{1,40}"),
    "HOME_COUNTRY": re.compile(r"[A-Z]{2}"),
}
PLACEHOLDER = re.compile(r"__([A-Z][A-Z0-9_]*?)__")
LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = LINE.match(line.rstrip("\r"))
        if m:
            values[m.group(1)] = unquote(m.group(2))
    return values


def domains_re(a: str, b: str) -> str:
    """One regex for both domains, written the way the blueprints always had it: example.ch and
    example.com → example\\.(ch|com); unrelated names → (one\\.org|other\\.net)."""
    sa, sb = a.rsplit(".", 1), b.rsplit(".", 1)
    if len(sa) == len(sb) == 2 and sa[0] == sb[0]:
        return f"{re.escape(sa[0])}\\.({re.escape(sa[1])}|{re.escape(sb[1])})"
    return f"({re.escape(a)}|{re.escape(b)})"


def values(env: dict[str, str]) -> dict[str, str]:
    v = {k: env.get(k, "") or DEFAULTS.get(k, "") for k in ALLOWED}
    bad = sorted(k for k, x in v.items() if x and not FORMAT.get(k, TEXT).fullmatch(x))
    if bad:
        raise SystemExit("render.py: not allowed in .env: " + ", ".join(bad)
                         + " (free text: letters, digits, spaces and . , + ( ) / - only)")
    v["PUBLIC_PORT"] = v["NETBIRD_PROXY_PORT"]
    v["ADMIN_LOGIN"] = v["ADMIN_LOGIN"] or v["ADMIN_USER"]
    if v["DOMAIN"]:
        v["DOMAIN_RE"] = re.escape(v["DOMAIN"])
        # The family app's bundle id: the domain reversed (example.com → com.example.family)
        v["APP_ID"] = ".".join(reversed(v["DOMAIN"].lower().split("."))) + ".family"
        if v["DOMAIN_ALIAS"]:
            v["DOMAINS_RE"] = domains_re(v["DOMAIN"], v["DOMAIN_ALIAS"])
    return v


def render(text: str, v: dict[str, str], name: str) -> str:
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if not v.get(key):
            missing.add(key)
            return m.group(0)
        return v[key]

    out = PLACEHOLDER.sub(sub, text)
    if missing:
        unknown = sorted(k for k in missing if k not in v)
        empty = sorted(k for k in missing if k in v)
        msg = []
        if unknown:
            msg.append("unknown placeholder(s) " + ", ".join(unknown))
        if empty:
            msg.append("empty in .env: " + ", ".join(empty) + ' (section "Your instance" in .env.example)')
        raise SystemExit(f"render.py: {name}: " + "; ".join(msg))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(Path(__file__).resolve().parent.parent / ".env"))
    ap.add_argument("--keys", action="store_true")
    ap.add_argument("src", nargs="?")
    ap.add_argument("dst", nargs="?")
    a = ap.parse_args()
    v = values(read_env(Path(a.env)))
    if a.keys:
        for k in sorted(v):
            print(f"{k}={v[k]}")
        return
    if not (a.src and a.dst):
        ap.error("SRC and DST needed")
    out = render(Path(a.src).read_text(encoding="utf-8"), v, a.src)
    if a.dst == "-":
        sys.stdout.write(out)
    else:
        Path(a.dst).write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
