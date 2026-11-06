#!/usr/bin/env python3
"""~/homelab/lib/configure.py: write the files that come from .env instead of git (./homelab configure).

    configure.py           render and copy, then list what changed
    configure.py --check   change nothing; exit 1 (and list them) when a file is missing or stale

What it writes (all gitignored, all derived; safe to run any time):
  authentik/templates/<BRAND_SLUG>/*        mail templates from authentik/templates-src/ (mounted at /templates)
  authentik/mail-logo.png                   BRANDING_DIR/mail-logo.png (the mails' inline logo, worker only)
  site/*.png, site/manifest.webmanifest     BRANDING_DIR/site/* and site-src/ (home page, Authentik's brand)
  site/apps.json                            site-src/apps.json with the chosen components' [[tiles]] (who sees what)
  family-app/src-tauri/icons/*              BRANDING_DIR/app/* (window, bundle and tray icons)
  family-app/src-tauri/tauri.conf.json      from tauri.conf.src.json (product name, bundle id)
  family-app/ui/index.html                  from family-app/ui-src/ (the welcome screen)
  caddy/enabled/{sites,status,authentik}/   the chosen components' caddy/components/<id>/*.caddy (others removed)
Authentik's blueprints are rendered separately, at apply time (authentik/setup-ssh.sh).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# (source folder, destination folder): every file in the source is rendered (__KEY__ → .env)
RENDER = [
    ("authentik/templates-src", "authentik/templates/{slug}"),
    ("site-src", "site"),
    ("family-app/ui-src", "family-app/ui"),
]
RENDER_FILES = [("family-app/src-tauri/tauri.conf.src.json", "family-app/src-tauri/tauri.conf.json")]
# Folders configure owns entirely: a file in them that it didn't write this time is removed
OWNED = ["caddy/enabled"]
# (file or folder inside BRANDING_DIR, destination): copied as they are
COPY = [
    ("mail-logo.png", "authentik/mail-logo.png"),
    ("site", "site"),
    ("app", "family-app/src-tauri/icons"),
]


# Who sees a tile (site/apps.json's variables: $family $kids $paperless $budget $admin)
AUDIENCE = {
    "family-and-kids": "or $family $kids",
    "family": "and (or $family $kids) (not $kids)",
    "paperless": "and (or $family $kids) $paperless (not $kids)",
    "budget": "and (or $family $kids) (not $kids) $budget",
    "signed-in": "not $kids",
    "admin": "and $admin (not $kids)",
    "operators": "and $operator (not $kids)",
}


def tiles(chosen: list[str]) -> str:
    """The app list of site/apps.json: each chosen component's [[tiles]], by order, each in its audience."""
    import tomllib
    found = []
    for f in sorted((ROOT / "setup/components").glob("*.toml")):
        if chosen and f.stem not in chosen:
            continue
        with open(f, "rb") as fh:
            found += tomllib.load(fh).get("tiles", [])
    q = lambda v: '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    out = []
    for t in sorted(found, key=lambda t: t["order"]):
        url = f'(printf {q(t["url"])} $u)' if "%s" in t["url"] else q(t["url"])
        pairs = [("id", q(t["id"])), ("name", q(t["name"]))] + ([("kids", q(t["kids"]))] if "kids" in t else []) + [
            ("desc", q(t["desc"])), ("url", url), ("icon", q(t["icon"])), ("status", q(t["status"])), ("group", q(t["group"]))]
        if t.get("netbird"):
            pairs.append(("netbird", "true"))
        out.append(f"{{{{- if {AUDIENCE[t['audience']]} }}}}")
        if t.get("comment"):
            out.append(f"  {{{{- /* {t['comment']} */ -}}}}")
        out.append("  {{- $apps = append $apps (dict " + " ".join(f'"{k}" {v}' for k, v in pairs) + ") -}}")
        out.append("{{- end }}")
    return "\n".join(out)


def outputs(env: dict[str, str]) -> dict[Path, bytes]:
    v = render.values(env)
    v["APP_TILES"] = tiles([c for c in env.get("COMPONENTS", "").split(",") if c])
    slug = v.get("BRAND_SLUG") or ""
    if not slug or not slug.replace("-", "").isalnum() or slug != slug.lower():
        raise SystemExit('configure: BRAND_SLUG must be set in .env: lowercase letters, digits, "-" (e.g. "smith")')
    branding = ROOT / (env.get("BRANDING_DIR") or "branding/default")
    out: dict[Path, bytes] = {}

    def rendered(src: Path, dst: Path) -> None:
        text = render.render(src.read_text(encoding="utf-8"), v, str(src.relative_to(ROOT)))
        out[dst] = text.encode("utf-8")

    for src_dir, dst_dir in RENDER:
        for src in sorted((ROOT / src_dir).iterdir()):
            rendered(src, ROOT / dst_dir.format(slug=slug) / src.name)
    for src, dst in RENDER_FILES:
        rendered(ROOT / src, ROOT / dst)
    chosen = [c for c in env.get("COMPONENTS", "").split(",") if c]
    for comp_dir in sorted((ROOT / "caddy/components").iterdir()):
        if chosen and comp_dir.name not in chosen:
            continue  # (no COMPONENTS: an install from before the setup engine, everything on)
        for f in sorted(comp_dir.glob("*.caddy")):
            out[ROOT / "caddy/enabled" / f.stem / f"{comp_dir.name}.caddy"] = f.read_bytes()
    for name, dst in COPY:
        src = branding / name
        if src.is_dir():
            for f in sorted(src.iterdir()):
                out[ROOT / dst / f.name] = f.read_bytes()
        elif src.is_file():
            out[ROOT / dst] = src.read_bytes()
        else:
            raise SystemExit(f"configure: {src.relative_to(ROOT)} missing (BRANDING_DIR in .env; see branding/README.md)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    env_path = Path(a.env)
    if not env_path.is_file():
        raise SystemExit(f"configure: {env_path} not found")
    env = render.read_env(env_path)
    stale = []
    # An install from before the setup engine ran everything: COMPONENTS = all, and COMPOSE_PROFILES
    # (without it compose would only see the core services), written once
    if not env.get("COMPONENTS"):
        stale.append(env_path / "COMPONENTS")
        if not a.check:
            sys.path.insert(0, str(ROOT / "lib/setup"))
            import engine
            comps = engine.components()
            on = engine.infer(comps, env)
            engine.upsert_env(env_path, {"COMPONENTS": ",".join(on), "COMPOSE_PROFILES": ",".join(engine.profiles(comps, on))})
            print(f"✓ configure: COMPONENTS = all {len(on)} (as before the setup engine; ./homelab setup --plan changes them)")
    for path, data in outputs(env).items():
        if path.is_file() and path.read_bytes() == data:
            continue
        stale.append(path)
        if not a.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_bytes(data)
            tmp.chmod(0o644)
            tmp.replace(path)
    # Files of components no longer chosen
    want = outputs(env) if env.get("COMPONENTS") else {}
    for d in OWNED:
        for f in sorted((ROOT / d).rglob("*")) if (ROOT / d).is_dir() else []:
            if f.is_file() and want and f not in want:
                stale.append(f)
                if not a.check:
                    f.unlink()
    rel = ["COMPONENTS in .env" if p.name == "COMPONENTS" else str(p.relative_to(ROOT)) for p in stale]
    if a.check:
        if rel:
            print("configure needed: " + ", ".join(rel))
            sys.exit(1)
        print("✓ configure: everything up to date")
    elif rel:
        print("✓ configure updated: " + ", ".join(rel))
        if any(r.startswith("caddy/enabled/") for r in rel):
            print("  Caddy's sites changed: docker compose up -d --force-recreate caddy")
        if "authentik/mail-logo.png" in rel:
            print("  new mail logo: docker compose up -d --force-recreate authentik-worker")
        if any(r.startswith("family-app/") for r in rel):
            print("  the family app changed: rebuild it (RUNBOOK.md \"Family app\")")
    else:
        print("✓ configure: nothing to change")


if __name__ == "__main__":
    main()
