#!/usr/bin/env bash
# ~/homelab/immich/setup.sh: configure Immich over its API (RUNBOOK.md step 7c).
# Talks to https://photos.<domain> through Caddy on the NetBird IP; Immich has no host port.
# Secrets come from .env and are never printed or put on a command line.
#
#   ./immich/setup.sh            local break-glass admin + its API key (first run), then SSO,
#                                VAAPI, external URL
#   ./immich/setup.sh sso-only   after your SSO login works: straight to Authentik, no passwords
#   ./immich/setup.sh status     what's set
#
# Everything after the first run uses IMMICH_API_KEY (the break-glass admin's key), so it still
# works once password login is off. Break-glass if SSO breaks:
#   docker compose exec immich-server immich-admin enable-password-login
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=.env

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed "s/^'\(.*\)'$/\1/"; }
# env_set KEY VALUE: replace or append, value never shown (as in authentik/setup-ssh.sh)
env_set() {
  KEY="$1" VAL="$2" python3 - "$ENV_FILE" <<'EOF'
import os, sys
p, k, v = sys.argv[1], os.environ["KEY"], os.environ["VAL"]
lines = open(p).read().splitlines()
out, done = [], False
for l in lines:
    if l.startswith(k + "="):
        if not done:
            out.append(f"{k}={v}"); done = True
    else:
        out.append(l)
if not done:
    out.append(f"{k}={v}")
open(p, "w").write("\n".join(out) + "\n")
EOF
  chmod 600 "$ENV_FILE"
}

DOMAIN=$(env_get DOMAIN)
NB_IP=$(env_get NETBIRD_IP)
PORT=$(env_get NETBIRD_PROXY_PORT); PORT=${PORT:-9443}
HOST="photos.$DOMAIN"
BRAND=$(env_get BRAND); BRAND=${BRAND:-Home}
AUTH=   # one header line: "Authorization: Bearer …" or "x-api-key: …"

# api METHOD PATH [body]: with a 3rd argument, the JSON body is read from stdin.
# The auth header goes in via fd 3, so it never shows up in `ps`.
api() {
  curl -sS --fail-with-body -m 30 --resolve "$HOST:443:$NB_IP" -X "$1" \
    -H 'Content-Type: application/json' -H @/dev/fd/3 \
    "https://$HOST/api$2" ${3:+--data-binary @-} 3<<<"$AUTH"
}

use_key() {
  local key; key=$(env_get IMMICH_API_KEY)
  [[ -n "$key" ]] || { echo "✗ IMMICH_API_KEY missing in .env: run ./immich/setup.sh first" >&2; exit 1; }
  AUTH="x-api-key: $key"
}

# patch JQ_FILTER [--arg …]: GET the system config, apply the filter, PUT it back
patch() {
  local filter="$1"; shift
  api GET /system-config | jq "$@" "$filter" | api PUT /system-config body >/dev/null
}

init() {
  api GET /server/ping | jq -e '.res == "pong"' >/dev/null || { echo "✗ Immich not reachable" >&2; exit 1; }
  local creds; creds=$(jq -n --arg e "$(env_get IMMICH_ADMIN_EMAIL)" --arg p "$(env_get IMMICH_ADMIN_PASSWORD)" \
    '{email: $e, password: $p, name: "Break-glass admin"}')
  if [[ "$(api GET /server/config | jq -r .isInitialized)" != true ]]; then
    api POST /auth/admin-sign-up body <<<"$creds" >/dev/null
    echo "✓ created local admin $(env_get IMMICH_ADMIN_EMAIL) (password in .env)"
  fi
  if [[ -z "$(env_get IMMICH_API_KEY)" ]]; then
    local token key
    token=$(api POST /auth/login body <<<"$(jq '{email, password}' <<<"$creds")" | jq -r .accessToken)
    [[ -n "$token" && "$token" != null ]] || { echo "✗ admin login failed" >&2; exit 1; }
    AUTH="Authorization: Bearer $token"
    key=$(api POST /api-keys body <<<'{"name": "homelab setup.sh", "permissions": ["all"]}' | jq -r .secret)
    [[ -n "$key" && "$key" != null ]] || { echo "✗ couldn't create an API key" >&2; exit 1; }
    env_set IMMICH_API_KEY "$key"
    echo "✓ API key for the local admin saved in .env (IMMICH_API_KEY)"
  fi
  use_key
  # Fail loudly if Immich renamed a setting (upgrade), instead of PUTting junk
  api GET /system-config | jq -e '.oauth | has("roleClaim") and has("autoLaunch") and has("issuerUrl")' >/dev/null \
    || { echo "✗ unexpected system-config shape; check the Immich release notes" >&2; exit 1; }
  patch '
    .oauth.enabled = true
    | .oauth.issuerUrl = $issuer
    | .oauth.clientId = "immich"
    | .oauth.clientSecret = $secret
    | .oauth.scope = "openid email profile immich"
    | .oauth.buttonText = ($brand + " account")
    | .oauth.autoRegister = true
    | .oauth.roleClaim = "immich_role"
    | .oauth.mobileOverrideEnabled = false
    | .server.externalDomain = $ext
    | .ffmpeg.accel = "vaapi"
    | .ffmpeg.accelDecode = true' \
    --arg issuer "https://auth.$DOMAIN:$PORT/application/o/immich/" \
    --arg secret "$(env_get IMMICH_OIDC_CLIENT_SECRET)" \
    --arg ext "https://$HOST:$PORT" \
    --arg brand "$BRAND"
  echo "✓ SSO ($BRAND account), external URL https://$HOST:$PORT, VAAPI"
}

sso_only() {
  use_key
  patch '.oauth.autoLaunch = true | .passwordLogin.enabled = false'
  echo "✓ SSO only: login page goes straight to Authentik, password login off"
}

status() {
  use_key
  api GET /system-config | jq '{oauth: (.oauth | {enabled, issuerUrl, autoLaunch, autoRegister, roleClaim, buttonText}),
    passwordLogin: .passwordLogin.enabled, externalDomain: .server.externalDomain, ffmpeg: .ffmpeg.accel}'
}

case "${1:-init}" in
  init) init ;;
  sso-only) sso_only ;;
  status) status ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
