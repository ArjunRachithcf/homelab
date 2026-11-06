#!/usr/bin/env bash
# ~/homelab/tests/golden.sh [--env FILE --golden DIR] [--update]: what compose, Ansible, the blueprints,
# ./homelab configure and Caddy make of an instance (its .env, dummy secrets) must equal its golden folder.
# A change that moves a literal into .env passes only if the output stays byte-identical; a deliberate
# output change re-runs with --update and commits the new golden alongside it.
# Without --env: every instance here, tests/fixtures/NAME.env → tests/golden/NAME/. An instance's own
# (docs/instance/README.md): --env instance/tests/instance.env --golden instance/tests/golden.
#   compose.json  docker compose config, canonical (sorted keys; tests/compose_canon.py)
#   vars.json     every ansible/vars.yml key, resolved (tests/render.yml)
#   units/        the timers role's templates, rendered
#   firewall/     the firewall role's templates, rendered (LAN zone, policy, mesh guard)
#   authentik/    blueprints, rendered (comment lines left out)
#   configure/    every file ./homelab configure writes
#   caddy-matrix.txt  tests/caddy/matrix.py: what Caddy answers, request by request
# Needs docker compose (no daemon). Without ansible-playbook or caddy their parts are skipped with a note,
# and --update refuses (it would keep their old output).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=../lib/portable.sh
source lib/portable.sh   # a python3 with tomllib (lib/configure.py)
REPO=$PWD

usage() { echo "usage: tests/golden.sh [--env FILE --golden DIR] [--update]" >&2; exit 2; }
abs() { echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; }
ENV="" GOLDEN="" UPDATE=""
while (( $# )); do
  case $1 in
    --env) [[ -f ${2:-} ]] || usage; ENV=$(abs "$2"); shift 2 ;;
    --golden) [[ -n ${2:-} ]] || usage; mkdir -p "$(dirname "$2")"; GOLDEN=$(abs "$2"); shift 2 ;;
    --update) UPDATE=1; shift ;;
    *) usage ;;
  esac
done
[[ ( -n "$ENV" && -n "$GOLDEN" ) || -z "$ENV$GOLDEN" ]] || usage
caddy_here() { [[ -n "${CADDY:-}" ]] || command -v caddy >/dev/null; }
if [[ -n "$UPDATE" ]] && ! { command -v ansible-playbook >/dev/null && caddy_here; }; then
  echo "✗ --update needs ansible-playbook and caddy (2.11 + caddy-dns/infomaniak): without them it keeps their old output" >&2
  exit 2
fi
# Every instance: one run each (its own errexit and temporary folder)
if [[ -z "$ENV" ]]; then
  failed=0
  for f in tests/fixtures/*.env; do
    bash "$REPO/tests/golden.sh" --env "$f" --golden "tests/golden/$(basename "$f" .env)" ${UPDATE:+--update} || failed=1
  done
  exit $failed
fi

# One instance: everything rendered into a temporary folder, then compared with its golden (or replacing it)
name=$(basename "$GOLDEN")
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT

# A clean environment: compose lets shell variables (TZ!) win over --env-file. Canonical JSON, so the
# Compose version (key order, printed defaults) doesn't matter (tests/compose_canon.py)
env -i PATH="$PATH" HOME="$HOME" docker compose -p homelab --env-file "$ENV" config --format json \
  | python3 tests/compose_canon.py | sed "s#$REPO#\$REPO#g" >"$out/compose.json"

if command -v ansible-playbook >/dev/null; then
  ANSIBLE_CONFIG=$REPO/ansible.cfg ansible-playbook -i localhost, tests/render.yml \
    -e homelab_env="$ENV" -e out="$out" >"$out/ansible.log" 2>&1 \
    || { tail -n 20 "$out/ansible.log"; echo "✗ tests/render.yml failed ($name)" >&2; exit 1; }
  rm -f "$out/ansible.log"
else
  echo "· ansible-playbook not installed: vars.json, units/ and firewall/ not checked ($name)"
  mkdir -p "$out/units" "$out/firewall"; cp "$GOLDEN/vars.json" "$out/"; cp "$GOLDEN"/units/* "$out/units/"
  cp "$GOLDEN"/firewall/* "$out/firewall/"
fi

# Authentik (Phase 1b): blueprints rendered as setup-ssh.sh renders them (compared without comment lines)
mkdir -p "$out/authentik"
for f in authentik/*.yaml; do
  python3 lib/render.py --env "$ENV" "$f" - | grep -vE '^\s*#' >"$out/authentik/$(basename "$f")"
done
# Everything ./homelab configure writes (mail templates, logos, icons, manifest, family app), byte for byte
python3 -c '
import sys
from pathlib import Path
sys.path.insert(0, "lib")
import configure, render
out = Path(sys.argv[2]) / "configure"
for path, data in configure.outputs(render.read_env(Path(sys.argv[1]))).items():
    dst = out / path.relative_to(configure.ROOT)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
' "$ENV" "$out"

# Caddy (Phase 1c): the real Caddyfile with stub apps and a stub Authentik, every host × listener × path × user
if caddy_here; then
  python3 tests/caddy/matrix.py --env "$ENV" --out "$out/caddy-matrix.txt"
else
  echo "· caddy (2.11 + caddy-dns/infomaniak) not on PATH: caddy-matrix.txt not checked ($name)"
  cp "$GOLDEN/caddy-matrix.txt" "$out/"
fi

if [[ -n "$UPDATE" ]]; then
  rm -rf "$GOLDEN"; cp -r "$out" "$GOLDEN"; echo "✓ ${GOLDEN#"$REPO"/} updated: review git diff ${GOLDEN#"$REPO"/}"; exit 0
fi
if diff -ru "$GOLDEN" "$out"; then echo "✓ golden ($name): compose config, vars, units, blueprints and configure output unchanged"
else echo "✗ golden ($name): output differs (above). Deliberate? tests/golden.sh --update" >&2; exit 1; fi
