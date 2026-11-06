#!/usr/bin/env bash
# ~/homelab/ntfy/setup.sh: ntfy's users, access and the n8n token (RUNBOOK.md step 7g).
# ntfy denies everything to everyone (NTFY_AUTH_DEFAULT_ACCESS=deny-all); this makes the exceptions in
# its user.db (volume ntfy-data, in the backup). Idempotent; secrets are never printed or put on a
# command line.
#
#   ./ntfy/setup.sh n8n-token    user n8n (random password nobody needs), write-only on topic bills,
#                                its token → NTFY_N8N_TOKEN in .env (kept if it still works)
#   ./ntfy/setup.sh user NAME    your own user: asks for its password (twice) in this terminal,
#                                then read-only on topic bills (the phone app signs in with it)
#   ./ntfy/setup.sh status       users and what they may read/write (no tokens shown)
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=.env
TOPIC=bills

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

ntfy() { docker compose exec -T ntfy ntfy "$@" </dev/null; }
has_user() { ntfy user list 2>&1 | grep -qx "user $1 (.*"; }

n8n_token() {
  if ! has_user n8n; then
    # A password is required; nobody uses it (n8n signs in with the token)
    NTFY_PASSWORD="$(openssl rand -base64 36 | tr -d '\n/+=')" docker compose exec -T -e NTFY_PASSWORD ntfy ntfy user add n8n >/dev/null
    echo "✓ ntfy user n8n"
  fi
  ntfy access n8n "$TOPIC" write-only >/dev/null
  echo "✓ n8n may write to topic $TOPIC (and nothing else)"
  local tok
  tok="$(env_get NTFY_N8N_TOKEN)"
  if [[ -n "$tok" ]] && ntfy token list n8n 2>/dev/null | grep -qFf <(printf '%s\n' "$tok"); then
    echo "✓ NTFY_N8N_TOKEN in .env still valid"
    return 0
  fi
  tok="$(ntfy token add --label=n8n n8n 2>&1 | grep -o 'tk_[a-z0-9]*' | head -n1)"
  [[ -n "$tok" ]] || { echo "✗ ntfy made no token" >&2; return 1; }
  env_set NTFY_N8N_TOKEN "$tok"
  echo "✓ new token for n8n → NTFY_N8N_TOKEN in .env (an ntfy credential in n8n)"
}

user() {
  local name=${1:?usage: ntfy/setup.sh user NAME}
  [[ "$name" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo "✗ a name of letters, digits, . _ -" >&2; return 1; }
  if ! has_user "$name"; then
    # ntfy asks for the password itself (needs this terminal)
    docker compose exec ntfy ntfy user add "$name"
  fi
  ntfy access "$name" "$TOPIC" read-only >/dev/null
  echo "✓ $name may read topic $TOPIC"
}

status() {
  ntfy user list 2>&1 | grep -v '^$'
}

case "${1:-}" in
  n8n-token) n8n_token ;;
  user) shift; user "$@" ;;
  status) status ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
