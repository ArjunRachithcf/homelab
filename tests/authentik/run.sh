#!/usr/bin/env bash
# tests/authentik/run.sh: the SSH keys flows (authentik/ssh-sso.yaml) in a real Authentik, the image of
# docker-compose.yml, set up by the real lib/authentik.py (`all`: hardening with the chosen components' blueprints,
# family, ssh-ldap.yaml, the certificate, the SSH outpost) in a copy of this tree, in compose project homelab-aktest
# (a docker wrapper refuses the live `homelab`; the network on 10.231.0.0/16, not the live 172.21.0.0/16, and the
# test's "proxy" container in Caddy's place). The passkey step is swapped for one-time codes (no browser here;
# tests/unit/test_ssh_keys.py checks the passkey settings). Proven (tests/authentik/check.py):
#   - adding: three kinds of key; a private key, RSA 2048, options in front, two lines and a bad title refused with
#     their messages; the same key again a no-op; stored rebuilt from the key, the title as its comment; no event
#     holding a pasted private key; each change in Authentik's event log;
#   - removing: the form lists your keys, ?fp= preselects one, a fingerprint not in the list is refused;
#   - only ssh-admins on NetBird: a public address refused, also within the 5 minutes Authentik caches its own check
#     of a flow; a neighbour container claiming a NetBird address refused; the form without its CSRF header refused;
#     two tabs at the form both saved;
#   - setup-ssh.sh add-key, list-keys, remove-key with the real tasks; the SSH outpost has a new key within a minute
#     (its provider saved, no restart); an LDAP bind as SSSD does it (password;code) still works, with Authentik
#     taking X-Forwarded-For from Caddy only.
# Needs docker, openssl, ssh-keygen and the internet (the images). CI: .github/workflows/authentik.yml.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
src=$PWD
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22
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

# This tree (with what isn't committed yet) in a folder of its own: lib/authentik.py writes .env and ssh/ldap-ssh.pem
mkdir "$work/repo"
git ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$work/repo"
cp tests/authentik/client.py "$work/client.py"
cat > "$work/override.yml" <<'EOF'
services:
  authentik-server:
    environment:
      AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS: 10.231.1.11/32
  authentik-worker:
    environment:
      AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS: 10.231.1.11/32
  authentik-ldap-ssh:
    networks: !override
      internal:
        ipv4_address: 10.231.1.10
  # Caddy's place: the one address Authentik takes X-Forwarded-For from
  proxy:
    image: docker.io/library/python:3.13.9-alpine3.22
    command: ["sleep", "infinity"]
    volumes: ["../client.py:/client.py:ro,z"]
    networks: {internal: {ipv4_address: 10.231.1.11}}
  # any other container
  neighbour:
    image: docker.io/library/python:3.13.9-alpine3.22
    command: ["sleep", "infinity"]
    volumes: ["../client.py:/client.py:ro,z"]
    networks: {internal: {ipv4_address: 10.231.1.12}}
networks:
  internal:
    ipam: !override
      config:
        - subnet: 10.231.0.0/16
          ip_range: 10.231.0.0/24
EOF
export PATH="$work/bin:$PATH" COMPOSE_PROJECT_NAME=homelab-aktest COMPOSE_FILE="docker-compose.yml:$work/override.yml"
unset COMPOSE_PROFILES
verified=0
cleanup() {
  # KEEP=1: the project stays up to look into (then: docker compose -p homelab-aktest down -v in its folder)
  if [[ -n ${KEEP:-} ]]; then echo "· kept: $work (COMPOSE_FILE=$COMPOSE_FILE)"; return; fi
  if ((verified)); then (cd "$work/repo" && docker compose down -v --remove-orphans >/dev/null 2>&1) || true; fi
  docker image rm -f homelab-aktest-ldapc:1 >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# The example instance with fresh secrets, only the SSH component chosen
cd "$work/repo"
rnd() { openssl rand -hex 24; }
python3 - .env "$src/tests/fixtures/full.env" "$(rnd)" "$(rnd)" <<'PY'
import re, sys
out, base, secret, pg = sys.argv[1:]
text = open(base).read()
values = {"COMPONENTS": "core,mesh,ssh-sso", "COMPOSE_PROFILES": "ssh-sso", "AK_SECRET_KEY": secret, "AK_PG_PASS": pg,
          "SSH_LDAP_BIND_PASSWORD": "", "AK_LDAP_SSH_OUTPOST_TOKEN": ""}
for k, v in values.items():
    text, n = re.subn(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
    if not n:
        text += f"\n{k}={v}\n"
open(out, "w").write(text)
PY
chmod 600 .env
./homelab configure >/dev/null
[[ $(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])') == homelab-aktest ]] \
  || { echo "✗ not project homelab-aktest" >&2; exit 1; }
verified=1

echo "· Authentik (first start: a few minutes)"
docker compose up -d authentik-db authentik-server authentik-worker proxy neighbour >/dev/null 2>&1
ready() { docker compose exec -T proxy python3 -c "import urllib.request as u; u.urlopen('http://authentik-server:9000/-/health/ready/', timeout=5)" >/dev/null 2>&1; }
for _ in $(seq 1 120); do ready && break; sleep 5; done
ready || { docker compose logs --tail 40 authentik-server authentik-worker >&2; echo "✗ Authentik never got ready" >&2; exit 1; }
flows() { docker compose exec -T authentik-worker ak shell -c 'from authentik.flows.models import Flow; print("OUT:", Flow.objects.filter(slug="default-authentication-flow").count())' 2>/dev/null | sed -n 's/^OUT: //p'; }
for _ in $(seq 1 60); do [[ $(flows) == 1 ]] && break; sleep 5; done

echo "· the sign-up and recovery flows (public-wizard.sh has you make them from Authentik's examples), then lib/authentik.py all"
for bp in example/flows-recovery-email-verification.yaml example/flows-enrollment-email-verification.yaml; do
  docker compose exec -T authentik-worker ak apply_blueprint "$bp" 2>&1 | grep -iq "invalid" && { echo "✗ $bp" >&2; exit 1; }
done
python3 lib/authentik.py all

echo "· the test's users (sam in ssh-admins, other not) and the passkey step swapped for one-time codes"
bind=$(python3 -c 'import sys; sys.path.insert(0, "lib"); import render; from pathlib import Path; print(render.read_env(Path(".env"))["SSH_LDAP_BIND_PASSWORD"])')
docker compose exec -T -e BIND="$bind" authentik-worker ak shell -c '
import json, os, secrets
from authentik.core.models import Group, User
from authentik.stages.authenticator_static.models import StaticDevice, StaticToken
from authentik.stages.authenticator_validate.models import AuthenticatorValidateStage
out = {"_bind_password": os.environ["BIND"]}
for name, member in (("sam", True), ("other", False)):
    u, _ = User.objects.get_or_create(username=name, defaults={"name": name, "email": name + "@example.org"})
    pw = secrets.token_urlsafe(18)
    u.set_password(pw)
    u.attributes["phone"] = "+15555550100"   # hardening asks for one after the code otherwise (a made-up number)
    u.save()
    if member:
        Group.objects.get(name="ssh-admins").users.add(u)
    d = StaticDevice.objects.create(user=u, name="test", confirmed=True)
    codes = [secrets.token_hex(4) for _ in range(80)]
    StaticToken.objects.bulk_create([StaticToken(device=d, token=c) for c in codes])
    out[name] = {"password": pw, "codes": codes}
AuthenticatorValidateStage.objects.filter(name="ssh-keys-passkey").update(device_classes=["static"])
open("/tmp/creds.json", "w").write(json.dumps(out))
print("OUT: ok")
' | grep -q "^OUT: ok"
umask 077
docker compose exec -T authentik-worker cat /tmp/creds.json > "$work/creds.json"
printf 'FROM %s\nRUN apk add --no-cache openldap-clients\n' "$PY_IMAGE" | docker build -q -t homelab-aktest-ldapc:1 - >/dev/null

echo "· the cases"
LDAP_CLIENT_IMAGE=homelab-aktest-ldapc:1 python3 "$src/tests/authentik/check.py" "$work"
