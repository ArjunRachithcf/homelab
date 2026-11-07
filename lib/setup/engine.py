#!/usr/bin/env python3
"""~/homelab/lib/setup/engine.py: the setup engine (plan Phase 2b). What gets set up is data:
setup/components/*.toml (one per app or feature) and setup/presets/*.toml; this file resolves a
choice into components, options, compose profiles and wizard stages.

    engine.py components [--env F]          every component, on or off (COMPONENTS in .env, else guessed)
    engine.py plan [CHOICE] [--env F]       what a choice would do; changes nothing
    engine.py apply [CHOICE] [--env F]      write COMPONENTS, COMPOSE_PROFILES, given options and generated
                                            secrets into .env (never prints a secret)
    engine.py export [--env F]              the current choice as an answers file (no secrets)
    engine.py stages [--env F]              the wizard stages of the chosen components, in order
    engine.py infer [--env F]               an install from before the engine: COMPONENTS = all, written
    engine.py docs                          docs/components.md, from the component files (tests check it's current)
    engine.py secret KEY [--env F]          one secret option of the chosen components, from the first line
                                            of stdin, into .env (the setup app; never on a command line)
CHOICE: --preset NAME | --answers FILE.toml (- = stdin), plus --add a,b --remove c,d, --gpu nvidia|none and
--platform fedora|debian|ubuntu|arch|macos|any (default: this machine; a component that doesn't run there is an error).
An answers file: preset = "…", add = […], remove = […], gpu = "…", [options] KEY = "value".
--json (components, plan, apply): one JSON object instead of text, for the setup app and the MCP
server. Secrets never appear in it: an option says only whether it is set.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import render  # noqa: E402

COMPONENTS_DIR = ROOT / "setup/components"
PRESETS_DIR = ROOT / "setup/presets"
# Wizards in RUNBOOK order; stages run in this order, then in file order within a wizard
WIZARDS = {"wizard": "wizard.sh", "public": "public-wizard.sh", "hardening": "hardening-wizard.sh",
           "immich": "immich-wizard.sh", "apps": "apps-wizard.sh", "music": "music-wizard.sh",
           "files": "files-wizard.sh", "family": "family-wizard.sh", "downloads": "downloads-wizard.sh"}
TYPES = {
    "domain": re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"),
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "port": re.compile(r"^([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$"),
    "int": re.compile(r"^[0-9]+$"),
    "bool": re.compile(r"^(true|false)$"),
    "tag": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    "path": re.compile(r"^[^\s'\"]+$"),
    # the first three numbers of a /24 (DOWNLOADS_NET, REQUESTS_NET); which ranges: check_value
    "net24": re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){2}$"),
}
# Where a net24 may lie: documentation and benchmarking ranges. Never a private one (Radarr, Sonarr, Lidarr and Prowlarr
# skip their own login for those, docker-compose.yml), never a real one (this PC could no longer reach it).
NET24_RANGES = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15")]
# LAN_ALLOW (firewall role): firewalld service names (0-AD, RH-Satellite-6, mdns: a letter somewhere, so
# a bare port is refused) or PORT[-PORT]/PROTO, spaces between
FIREWALL_WORD = re.compile(r"^((?=[^/]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9_.-]*|[0-9]{1,5}(-[0-9]{1,5})?/(tcp|udp))$")


# Components merged into another: an .env that still names one reads as its successor, and the next
# ./homelab setup --apply writes the new id (none at the moment)
RETIRED: dict[str, str] = {}


def renamed(ids: list[str]) -> list[str]:
    """IDS with every retired id replaced by its successor, in order, without duplicates."""
    out: list[str] = []
    for c in ids:
        c = RETIRED.get(c, c)
        if c not in out:
            out.append(c)
    return out


def components() -> dict[str, dict]:
    out = {}
    for f in sorted(COMPONENTS_DIR.glob("*.toml")):
        with open(f, "rb") as fh:
            out[f.stem] = tomllib.load(fh)
    return out


def preset(name: str, comps: dict[str, dict]) -> list[str]:
    f = PRESETS_DIR / f"{name}.toml"
    if not f.is_file():
        raise SystemExit(f"no preset {name}; one of: {', '.join(p.stem for p in sorted(PRESETS_DIR.glob('*.toml')))}")
    with open(f, "rb") as fh:
        p = tomllib.load(fh)
    return list(comps) if p["components"] == "all" else list(p["components"])


PLATFORMS = ["fedora", "debian", "ubuntu", "arch", "macos"]


def detect_platform() -> str:
    """This machine: fedora | debian | ubuntu | arch | macos, or "" when it's none of them"""
    if sys.platform == "darwin":
        return "macos"
    try:
        rel = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        return ""
    ids = [rel.get("ID", "").strip('"')] + rel.get("ID_LIKE", "").strip('"').split()
    for p in ("fedora", "ubuntu", "debian", "arch"):
        if p in ids:
            return p
    return ""


def detect_gpu() -> str:
    if Path("/dev/nvidia0").exists() or (shutil.which("nvidia-smi") and subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True).returncode == 0):
        return "nvidia"
    return "none"


@dataclass
class Resolution:
    enabled: list[str]
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def order(comps: dict[str, dict], ids) -> list[str]:
    """Core first, then the family preset's order, then the rest by name."""
    family = preset("family", comps)
    key = {c: (0 if comps[c].get("core") else 1, family.index(c) if c in family else 999, c) for c in ids}
    return sorted(ids, key=key.__getitem__)


def resolve(comps: dict[str, dict], chosen: list[str], gpu: str, platform: str | None = None) -> Resolution:
    """platform None: don't check where the components run (tests, an answers file for another machine)"""
    r = Resolution(enabled=[])
    chosen = renamed(chosen)
    unknown = [c for c in chosen if c not in comps]
    if unknown:
        r.errors.append(f"unknown component(s): {', '.join(unknown)}")
    want = {c for c in chosen if c in comps} | {c for c, d in comps.items() if d.get("core")}
    todo = list(want)
    while todo:
        c = todo.pop()
        for dep in comps[c]["requires"]:
            if dep not in want:
                want.add(dep)
                todo.append(dep)
                r.notes.append(f"{comps[c]['title']} needs {comps[dep]['title']}: added")
    for c in sorted(want):
        for x in comps[c]["conflicts"]:
            if x in want:
                r.errors.append(f"{comps[c]['title']} and {comps[x]['title']} can't both be on")
        if platform and platform not in comps[c].get("platforms", PLATFORMS):
            r.errors.append(f"{comps[c]['title']} isn't available on {platform} (only {', '.join(comps[c]['platforms'])}); leave it out")
        # macOS: Ollama runs natively on the Apple GPU (Metal), no NVIDIA card needed
        if comps[c]["gpu"] == "nvidia" and gpu != "nvidia" and platform != "macos":
            r.errors.append(f"{comps[c]['title']} needs an NVIDIA graphics card (none found); leave it out, "
                            "or pass --gpu nvidia if it's there")
        for s in comps[c]["suggests"]:
            if s not in want:
                r.notes.append(f"{comps[c]['title']} works well with {comps[s]['title']} (not chosen)")
    r.enabled = order(comps, want)
    return r


def profiles(comps: dict[str, dict], enabled: list[str], platform: str | None = None) -> list[str]:
    # macOS: no ollama container, Ollama runs natively (OLLAMA_URL)
    return [c for c in enabled if comps[c]["services"] and not comps[c].get("core")
            and not (platform == "macos" and c == "local-ai")]


def stages(comps: dict[str, dict], enabled: list[str]) -> list[str]:
    want = {s for c in enabled for s in comps[c]["stages"]}
    out = []
    for w, f in WIZARDS.items():
        for m in re.finditer(r"^stage_([a-z_]+)\(\)", (ROOT / f).read_text(), re.M):
            if f"{w}:{m.group(1)}" in want:
                out.append(f"{w}:{m.group(1)}")
    return out


def stage_titles(env: dict[str, str]) -> dict[str, str]:
    """{"wizard:id": title}, from each stage's `stage "…"` line. The domains and the public port are filled
    in from .env; anything else a wizard works out when it runs ($APP_LIST, $(…)) shows as …"""
    known = {"DOMAIN": env.get("DOMAIN", ""), "ALIAS": env.get("DOMAIN_ALIAS", "")}

    def fill(t: str) -> str:
        t = t.replace("$(pub_port)", env.get("NETBIRD_PROXY_PORT") or "9443")
        t = re.sub(r"\$\(\(.*?\)\)", "…", t)
        t = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "…", t)
        t = re.sub(r"\$\(.*", "…", t)  # cut short by a quote inside it
        t = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?|\$\{[^}]*\}", lambda m: known.get(m.group(1) or "") or "…", t)
        return t.strip()

    out = {}
    for w, f in WIZARDS.items():
        text = (ROOT / f).read_text()
        for m in re.finditer(r"^stage_([a-z_]+)\(\) \{\n(?:.*\n){0,3}?\s*stage \"((?:[^\"\\]|\\.)+)\"", text, re.M):
            out[f"{w}:{m.group(1)}"] = fill(re.sub(r"\\(.)", r"\1", m.group(2)))
    return out


def check_value(opt: dict, value: str) -> str | None:
    t = opt.get("type", "text")
    if t == "ip":
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return "not an IP address"
        # Caddy, sshd and AdGuard bind to it: 0.0.0.0 would be every interface, a public one the internet
        if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_global:
            return "not a mesh address (NetBird's are 100.64.0.0/10)"
        return None
    if t == "firewall":
        bad = [w for w in value.split() if not FIREWALL_WORD.match(w)]
        return f"not a firewalld service or PORT/PROTO: {' '.join(bad)}" if bad else None
    rx = TYPES.get(t)
    if rx and not rx.match(value):
        return f"not a valid {t}"
    if t == "net24" and not any(ipaddress.ip_network(f"{value}.0/24").subnet_of(r) for r in NET24_RANGES):
        return "not in a documentation or benchmarking range (192.0.2, 198.51.100, 203.0.113, 198.18–198.19)"
    return None


def options(comps: dict[str, dict], enabled: list[str]) -> dict[str, tuple[str, dict]]:
    return {k: (c, o) for c in enabled for k, o in comps[c].get("options", {}).items()}


@dataclass
class Plan:
    resolution: Resolution
    write: dict[str, str]                  # non-secret values to write
    generate: list[str]                    # secrets to generate
    missing: list[tuple[str, str, str]]    # (component, key, help): required, no value, nothing sets it
    later: list[tuple[str, str, str]]      # (component, key, stage): a wizard stage fills it in
    invalid: list[tuple[str, str]]         # (key, why)
    stages: list[str]
    profiles: list[str]
    switch_on: list[str] = field(default_factory=list)   # against installed() (none on a new .env)
    switch_off: list[str] = field(default_factory=list)
    same_components: bool = False                        # COMPONENTS and COMPOSE_PROFILES stay as .env has them


# What switching a component off does (./homelab configure, backup.sh, the jobs); the setup app says the same
SWITCH_OFF = ("they leave the home page, their sites stop once Caddy is recreated, and backups and timed jobs may leave "
              "them out; their containers keep running, with their data")


def make_plan(comps: dict[str, dict], env: dict[str, str], chosen: list[str], gpu: str,
              given: dict[str, str] | None = None, platform: str | None = None) -> Plan:
    res = resolve(comps, chosen, gpu, platform)
    given = given or {}
    opts = options(comps, res.enabled)
    write, gen, missing, later, invalid = {}, [], [], [], []
    for k, v in given.items():
        if k not in opts:
            invalid.append((k, "not an option of the chosen components"))
        elif opts[k][1].get("type") == "secret":
            invalid.append((k, "a secret: not taken from an answers file (generated, or asked by the wizard)"))
        elif (why := check_value(opts[k][1], str(v))):
            invalid.append((k, why))
        else:
            write[k] = str(v)
    for k, (c, o) in opts.items():
        have = env.get(k) or write.get(k)
        if have:
            continue
        if o.get("generate"):
            gen.append(k)
        elif platform == "macos" and o.get("macos_default") is not None:
            write[k] = os.path.expanduser(str(o["macos_default"]))
        elif o.get("default") is not None:
            write[k] = str(o["default"])
        elif o.get("set_by"):
            later.append((c, k, o["set_by"]))
        elif o.get("required"):
            missing.append((c, k, o.get("help", "")))
    # Two networks on one /24 (DOWNLOADS_NET, REQUESTS_NET): Docker refuses the second and Caddy, on all of them
    # whatever is chosen, wouldn't start. So every component's, defaults included
    seen: dict[str, str] = {}
    for c in comps.values():
        for k, o in c.get("options", {}).items():
            if o.get("type") == "net24":
                v = write.get(k) or env.get(k) or str(o.get("default", ""))
                if v in seen:
                    invalid.append((k, f"the same /24 as {seen[v]}: two of the stack's networks can't share one"))
                seen[v] = k
    prof = profiles(comps, res.enabled, platform)
    write["COMPONENTS"] = ",".join(res.enabled)
    write["COMPOSE_PROFILES"] = ",".join(prof)
    was = installed(comps, env)
    same = env.get("COMPONENTS") == write["COMPONENTS"] and env.get("COMPOSE_PROFILES") == write["COMPOSE_PROFILES"]
    return Plan(res, write, gen, missing, later, invalid, stages(comps, res.enabled), prof,
                [c for c in res.enabled if was and c not in was], [c for c in was if c not in res.enabled], same)


def upsert_env(path: Path, values: dict[str, str]) -> None:
    """KEY='value' lines (like the wizards' write_env): replaced in place or appended; mode 600."""
    sq = "'\\''"
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in values:
            if m.group(1) in done:
                continue
            out.append(f"{m.group(1)}='{values[m.group(1)].replace(chr(39), sq)}'")
            done.add(m.group(1))
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}='{v.replace(chr(39), sq)}'")
    old = os.umask(0o077)
    try:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        os.umask(old)


def generate(cmd: str) -> str:
    v = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True).stdout.strip()
    if not v:
        raise SystemExit("a secret generator printed nothing")
    return v


def infer(comps: dict[str, dict], env: dict[str, str]) -> list[str]:
    """An install from before the setup engine ran every compose service and had every Ansible flag
    on: that is all components but the opt-in ones (`opt_in`: added since, and only ever chosen).
    (Change it afterwards with ./homelab setup --plan / --apply.)"""
    return order(comps, [c for c, d in comps.items() if not d.get("opt_in")])


def current(comps: dict[str, dict], env: dict[str, str]) -> tuple[list[str], bool]:
    if env.get("COMPONENTS"):
        return renamed([c for c in env["COMPONENTS"].split(",") if c]), False
    return infer(comps, env), True


def installed(comps: dict[str, dict], env: dict[str, str]) -> list[str]:
    """What the machine runs now, against which a choice switches parts on and off: COMPONENTS; for an install
    from before the setup engine (Authentik's key, no COMPONENTS) everything; for a new .env nothing.
    Ids no component has any more are left out."""
    if not (env.get("COMPONENTS") or env.get("AK_SECRET_KEY")):
        return []
    return [c for c in current(comps, env)[0] if c in comps]


def choice(args, comps, env) -> tuple[list[str], str, dict[str, str]]:
    given: dict[str, str] = {}
    base: list[str] | None = None
    gpu = args.gpu
    if args.answers:
        if args.answers == "-":
            a = tomllib.loads(sys.stdin.read())
        else:
            with open(args.answers, "rb") as f:
                a = tomllib.load(f)
        if a.get("preset"):
            base = preset(a["preset"], comps)
        if a.get("components"):
            base = list(a["components"])
        base = (base or current(comps, env)[0]) + list(a.get("add", []))
        base = [c for c in base if c not in a.get("remove", [])]
        given = {k: str(v) for k, v in a.get("options", {}).items()}
        gpu = gpu or a.get("gpu")
    if args.preset:
        base = preset(args.preset, comps)
    chosen = base if base is not None else current(comps, env)[0]
    chosen += [c for c in (args.add or "").split(",") if c]
    chosen = [c for c in chosen if c not in (args.remove or "").split(",")]
    return chosen, gpu or env.get("GPU") or detect_gpu(), given


def show_plan(p: Plan, comps: dict[str, dict]) -> None:
    print("Components:")
    for c in p.resolution.enabled:
        print(f"  ✓ {c:<16} {comps[c]['title']}")
    off = [c for c in comps if c not in p.resolution.enabled]
    if off:
        print("Not chosen: " + ", ".join(off))
    if p.switch_on:
        print("Switched on: " + ", ".join(p.switch_on))
    if p.switch_off:
        print(f"Switched off: {', '.join(p.switch_off)} ({SWITCH_OFF})")
    for n in p.resolution.notes:
        print(f"  · {n}")
    for e in p.resolution.errors:
        print(f"  ✗ {e}")
    print("\n.env: would write " + (", ".join(sorted(p.write)) or "nothing"))
    if p.generate:
        print("Secrets to generate (never shown): " + ", ".join(p.generate))
    for k, why in p.invalid:
        print(f"  ✗ {k}: {why}")
    if p.missing:
        print("Still needed from you:")
        for c, k, h in p.missing:
            print(f"  ✗ {k} ({c}): {h}")
    if p.later:
        print("Filled in by the wizards: " + ", ".join(f"{k} ({s})" for _, k, s in p.later))
    print(f"\nWizard stages ({len(p.stages)}): " + " ".join(p.stages))


def catalogue(comps: dict[str, dict], env: dict[str, str]) -> dict:
    """Everything a front end needs to choose: components with their options (values of non-secrets
    only), presets, the GPU, the current choice and the wizard stages' titles."""
    on, guessed = current(comps, env)
    out = []
    for c, d in comps.items():
        opts = []
        for k, o in d.get("options", {}).items():
            secret = o.get("type") == "secret"
            opts.append({"key": k, "help": o.get("help", ""), "type": o.get("type", "text"),
                         "default": o.get("default"), "required": bool(o.get("required")),
                         "advanced": bool(o.get("advanced")), "generate": bool(o.get("generate")),
                         "set_by": o.get("set_by", ""), "set": bool(env.get(k)),
                         "value": None if secret else env.get(k)})
        out.append({"id": c, "title": d["title"], "description": d.get("description", ""),
                    "category": d.get("category", ""), "core": bool(d.get("core")), "requires": d["requires"],
                    "suggests": d["suggests"], "conflicts": d["conflicts"], "gpu": d["gpu"], "platforms": d["platforms"],
                    "on": c in on, "options": opts})
    presets = []
    for f in sorted(PRESETS_DIR.glob("*.toml")):
        with open(f, "rb") as fh:
            pr = tomllib.load(fh)
        presets.append({"id": f.stem, "title": pr.get("title", f.stem), "description": pr.get("description", ""),
                        "components": preset(f.stem, comps)})
    return {"components": out, "presets": presets, "chosen": on, "guessed": guessed,
            "gpu": env.get("GPU") or detect_gpu(), "platform": detect_platform(), "stage_titles": stage_titles(env)}


CATEGORY_TITLES = {"core": "Always there", "access": "Getting in", "apps": "Apps", "media": "Watch, listen, read",
                   "storage": "Disks", "backup": "Backups", "home": "Home"}


def docs_md(comps: dict[str, dict]) -> str:
    """docs/components.md: every component, by category, from setup/components/*.toml"""
    presets = {f.stem: preset(f.stem, comps) for f in sorted(PRESETS_DIR.glob("*.toml"))}
    out = ["# Components", "",
           "<!-- Generated by `python3 lib/setup/engine.py docs > docs/components.md` from setup/components/*.toml; "
           "tests/unit/test_docs.py checks it's current. Edit the component files, not this. -->", "",
           "Every app or feature is a component (`setup/components/<id>.toml`). Choose them with a preset and "
           "`./homelab setup --plan/--apply`, the setup app, or an assistant (docs/getting-started.md). "
           "What one needs comes along by itself.", "",
           "Presets: " + "; ".join(f"**{p}** ({len(c)} components)" for p, c in presets.items()) + ".", ""]
    cats: dict[str, list[str]] = {}
    for c, d in comps.items():
        cats.setdefault(d.get("category", ""), []).append(c)
    for cat in [c for c in CATEGORY_TITLES if c in cats] + [c for c in cats if c not in CATEGORY_TITLES]:
        out += [f"## {CATEGORY_TITLES.get(cat, cat)}", ""]
        for c in order(comps, cats[cat]):
            d = comps[c]
            out += [f"### {d['title']} (`{c}`)", "", d.get("description", ""), ""]
            facts = []
            if d.get("core"):
                facts.append("always on")
            if d["requires"]:
                facts.append("needs " + ", ".join(f"`{r}`" for r in d["requires"]))
            if d["suggests"]:
                facts.append("goes well with " + ", ".join(f"`{r}`" for r in d["suggests"]))
            if d["gpu"] != "none":
                facts.append("an NVIDIA card (macOS: the Apple GPU)")
            facts.append("runs on " + ", ".join(d["platforms"]))
            if d["services"]:
                facts.append("containers " + ", ".join(f"`{x}`" for x in d["services"]))
            if d["public_names"]:
                facts.append("public at " + ", ".join(f"`{x}.<domain>`" for x in d["public_names"]))
            if d["jobs"]:
                facts.append("jobs " + ", ".join(f"`{x}`" for x in d["jobs"]))
            out += ["- " + f for f in facts]
            opts = d.get("options", {})
            if opts:
                out += ["", "| Setting | | What it is |", "|---|---|---|"]
                for k, o in opts.items():
                    kind = "secret, " + ("generated" if o.get("generate") else "asked") if o.get("type") == "secret" else (
                        f"default `{o['default']}`" if o.get("default") is not None else ("needed" if o.get("required") else "optional"))
                    out.append(f"| `{k}` | {kind} | {o.get('help', '').replace('|', '/')} |")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def plan_json(p: Plan, opts: dict[str, tuple[str, dict]]) -> dict:
    secret = {k for k, (_, o) in opts.items() if o.get("type") == "secret"}
    return {"enabled": p.resolution.enabled, "notes": p.resolution.notes, "errors": p.resolution.errors,
            "write": {k: ("(set)" if k in secret else v) for k, v in p.write.items()},
            "generate": p.generate, "invalid": [{"key": k, "why": w} for k, w in p.invalid],
            "missing": [{"component": c, "key": k, "help": h} for c, k, h in p.missing],
            "later": [{"component": c, "key": k, "stage": s} for c, k, s in p.later],
            "stages": p.stages, "profiles": p.profiles, "switch_on": p.switch_on, "switch_off": p.switch_off,
            "switch_off_means": SWITCH_OFF if p.switch_off else "", "same_components": p.same_components}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["components", "plan", "apply", "export", "stages", "infer", "secret", "docs"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--preset")
    ap.add_argument("--answers")
    ap.add_argument("--add")
    ap.add_argument("--remove")
    ap.add_argument("--gpu", choices=["nvidia", "none"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--platform", choices=PLATFORMS + ["any"], help="where it will run (default: this machine; any: don't check)")
    a = ap.parse_args()
    comps = components()
    env_path = Path(a.env)
    env = render.read_env(env_path) if env_path.exists() else {}

    if a.cmd == "components" and a.json:
        print(json.dumps(catalogue(comps, env)))
        return 0
    if a.cmd == "components":
        on, guessed = current(comps, env)
        for c, d in comps.items():
            print(f"  {'✓' if c in on else '·'} {c:<16} {d['title']}")
        if guessed:
            print("(COMPONENTS not in .env yet: an install from before the setup engine runs everything; ./homelab configure writes it)")
        return 0
    if a.cmd == "docs":
        sys.stdout.write(docs_md(comps))
        return 0
    if a.cmd == "secret":
        opts = options(comps, current(comps, env)[0])
        if not a.key or a.key not in opts or opts[a.key][1].get("type") != "secret":
            print(f"✗ {a.key}: not a secret option of the chosen components", file=sys.stderr)
            return 2
        value = sys.stdin.readline().rstrip("\n")
        if not value:
            print(f"✗ {a.key}: empty", file=sys.stderr)
            return 2
        upsert_env(env_path, {a.key: value})
        print(f"✓ {a.key} set")
        return 0
    if a.cmd == "stages":
        print("\n".join(stages(comps, current(comps, env)[0])))
        return 0
    if a.cmd == "infer":
        if env.get("COMPONENTS"):
            print(f"✓ COMPONENTS already set: {env['COMPONENTS']}")
            return 0
        on = infer(comps, env)
        upsert_env(env_path, {"COMPONENTS": ",".join(on), "COMPOSE_PROFILES": ",".join(profiles(comps, on))})
        print(f"✓ wrote COMPONENTS ({len(on)}) and COMPOSE_PROFILES to {env_path}: {','.join(on)}")
        return 0
    if a.cmd == "export":
        on, _ = current(comps, env)
        opts = options(comps, on)
        print("# Answers for ./homelab setup --apply (no secrets: they're generated or asked again)")
        print(f"components = {on!r}".replace("'", '"'))
        print("\n[options]")
        for k, (_, o) in opts.items():
            if o.get("type") != "secret" and env.get(k):
                v = env[k].replace("\\", "\\\\").replace('"', '\\"')
                print(f'{k} = "{v}"')
        return 0

    chosen, gpu, given = choice(a, comps, env)
    platform = None if a.platform == "any" else (a.platform or detect_platform() or None)
    p = make_plan(comps, env, chosen, gpu, given, platform)
    opts = options(comps, p.resolution.enabled)
    if a.json:
        out = plan_json(p, opts)
    else:
        show_plan(p, comps)
    bad = bool(p.resolution.errors or p.invalid)
    if a.cmd == "plan" or bad:
        if a.json:
            print(json.dumps(dict(out, applied=False)))
        elif bad and a.cmd == "apply":
            print("\nNothing written: fix the ✗ above first.")
        return 1 if bad else 0
    values = dict(p.write)
    for k in p.generate:
        values[k] = generate(opts[k][1]["generate"])
    upsert_env(env_path, values)
    # Compose leaves a service whose profile went off running (not an orphan to it, even for up -d
    # --remove-orphans): AdGuard would keep port 53, Home Assistant 8123. Stopping is the person's call; their
    # data volumes stay either way. The same baseline as the plan's switch_off (nothing on a new .env).
    dropped = [c for c in p.switch_off if c in profiles(comps, installed(comps, env))]
    stop = ""
    if dropped:
        stop = ("docker compose " + " ".join(f"--profile {c}" for c in dropped) + " rm -s -f "
                + " ".join(s for c in dropped for s in comps[c]["services"]))
    if a.json:
        print(json.dumps(dict(out, applied=True, written=len(values), still_running=dropped, stop_command=stop)))
    else:
        print(f"\n✓ wrote {len(values)} value(s) to {env_path}" + (f" ({len(p.generate)} generated secrets)" if p.generate else ""))
        if dropped:
            print(f"! Switched off, but their containers still run: {', '.join(dropped)}. To stop and remove them"
                  f" (volumes stay):\n    {stop}")
        print("Next: ./homelab configure and ./homelab refresh-console, then ./homelab setup (the wizards for the stages above).")
    return 1 if p.missing else 0


if __name__ == "__main__":
    sys.exit(main())
