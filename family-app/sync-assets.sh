#!/usr/bin/env bash
# Before every build (tauri.conf.json beforeBuildCommand): ./homelab configure writes the files that come
# from .env (tauri.conf.json, ui/index.html, the icons from BRANDING_DIR, site/mark.png); then the home
# page's shared assets (app logos, mark, font) are copied into ui/: one source, site/.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f ../.env ]]; then python3 ../lib/configure.py; else echo "sync-assets: no ../.env, using the files as they are" >&2; fi
rm -rf ui/apps ui/fonts
cp -r ../site/apps ui/apps
mkdir -p ui/fonts && cp ../site/fonts/inter.woff2 ../site/fonts/OFL-inter.txt ui/fonts/
cp ../site/mark.png ui/mark.png
