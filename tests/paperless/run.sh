#!/usr/bin/env bash
# tests/paperless/run.sh: paperless/family.py (wizard.sh stage paperless_family) in a real Paperless, the image and the
# service of docker-compose.yml with its Redis, in a copy of this tree, in compose project homelab-pltest (a docker
# wrapper refuses the live `homelab`; the network on 10.233.0.0/16, not the live 172.21.0.0/16). Proven
# (tests/paperless/check.py):
#   - the group paperless-users with a family member's rights, every active account in it (not a disabled one); a
#     member's own UI works, no admin rights;
#   - PAPERLESS_FAMILY_SHARING=false: the admin's documents stay his; true: the workflow, and the documents already
#     there shared once (view and edit, never delete); a document made private stays so when the script runs again;
#     a new document through Paperless' own consumer comes out shared; false again: the workflow off, shares kept;
#   - a new sign-in joins the group (PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS); wizard.sh's check agrees.
# Needs docker, openssl and the internet (the images). CI: .github/workflows/paperless.yml.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
src=$PWD
work=$(mktemp -d)

# docker for this test only: never the live project or its volumes and networks
mkdir -p "$work/bin"
real_docker=$(command -v docker)
cat > "$work/bin/docker" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in homelab|homelab_*|*[:=/]homelab_*) echo "docker wrapper: refused '\$a' (the live project)" >&2; exit 99 ;; esac
done
exec "$real_docker" "\$@"
EOF
chmod +x "$work/bin/docker"

# This tree (with what isn't committed yet) in a folder of its own: Paperless' ./consume and ./export are there
mkdir "$work/repo"
git ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$work/repo"
cat > "$work/override.yml" <<'EOF'
networks:
  internal:
    ipam: !override
      config:
        - subnet: 10.233.0.0/16
          ip_range: 10.233.0.0/24
EOF
export PATH="$work/bin:$PATH" COMPOSE_PROJECT_NAME=homelab-pltest COMPOSE_FILE="docker-compose.yml:$work/override.yml"
unset COMPOSE_PROFILES
verified=0
cleanup() {
  # KEEP=1: the project stays up to look into (then: docker compose -p homelab-pltest down -v in its folder)
  if [[ -n ${KEEP:-} ]]; then echo "· kept: $work (COMPOSE_FILE=$COMPOSE_FILE)"; return; fi
  if ((verified)); then (cd "$work/repo" && docker compose down -v --remove-orphans >/dev/null 2>&1) || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# The example instance with fresh secrets, Paperless chosen
cd "$work/repo"
python3 - .env "$src/tests/fixtures/full.env" "$(openssl rand -hex 32)" "$(openssl rand -hex 16)" <<'PY'
import re, sys
out, base, secret, admin_pw = sys.argv[1:]
text = open(base).read()
values = {"COMPONENTS": "core,mesh,paperless", "COMPOSE_PROFILES": "paperless", "PAPERLESS_SECRET_KEY": secret,
          "PAPERLESS_ADMIN_PASSWORD": admin_pw}
for k, v in values.items():
    text, n = re.subn(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
    if not n:
        text += f"\n{k}={v}\n"
open(out, "w").write(text)
PY
chmod 600 .env
mkdir -p consume export
[[ $(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])') == homelab-pltest ]] \
  || { echo "✗ not project homelab-pltest" >&2; exit 1; }
verified=1

echo "· Paperless and its Redis (first start: migrations)"
docker compose up -d --no-deps paperless-redis paperless >/dev/null 2>&1
healthy() { [[ $(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q paperless)" 2>/dev/null) == healthy ]]; }
for _ in $(seq 1 90); do healthy && break; sleep 5; done
healthy || { docker compose logs --tail 40 paperless >&2; echo "✗ Paperless never got healthy" >&2; exit 1; }

echo "· the cases"
python3 "$src/tests/paperless/check.py" "$work/repo"
