#!/usr/bin/env bash
# ~/homelab/ntfy/setup.sh: ntfy's users, access and the n8n token (RUNBOOK.md step 7g).
# ntfy denies everything to everyone (NTFY_AUTH_DEFAULT_ACCESS=deny-all); this makes the exceptions in
# its user.db (volume ntfy-data, in the backup). Idempotent; secrets are never printed or put on a
# command line.
#
#   ./ntfy/setup.sh token NAME TOPIC KEY   user NAME (random password nobody needs), write-only on TOPIC, its token
#                                          → KEY in .env (kept if it still works): an app that only sends
#   ./ntfy/setup.sh n8n-token              = token n8n bills NTFY_N8N_TOKEN (RUNBOOK.md 7h)
#   ./ntfy/setup.sh user NAME [TOPIC]      your own user: asks for its password (twice) in this terminal, then
#                                          read-only on TOPIC (default bills; the phone app signs in with it)
#   ./ntfy/setup.sh status                 users and what they may read/write (no tokens shown)
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

ntfy() { docker compose exec -T ntfy ntfy "$@" </dev/null; }
has_user() { ntfy user list 2>&1 | grep -qx "user $1 (.*"; }

name_ok() { [[ "$1" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo "✗ $2: letters, digits, . _ -" >&2; return 1; }; }

token() {
  local name=${1:-} topic=${2:-} key=${3:-}
  [[ -n $name && -n $topic && $key =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "usage: ntfy/setup.sh token NAME TOPIC KEY" >&2; return 2; }
  name_ok "$name" "a name" && name_ok "$topic" "a topic" || return 1
  if ! has_user "$name"; then
    # A password is required; nobody uses it (the app signs in with the token)
    NTFY_PASSWORD="$(openssl rand -base64 36 | tr -d '\n/+=')" docker compose exec -T -e NTFY_PASSWORD ntfy ntfy user add "$name" >/dev/null
    echo "✓ ntfy user $name"
  fi
  ntfy access "$name" "$topic" write-only >/dev/null
  echo "✓ $name may write to topic $topic (and nothing else)"
  local tok
  tok="$(env_get "$key")"
  if [[ -n "$tok" ]] && ntfy token list "$name" 2>/dev/null | grep -qFf <(printf '%s\n' "$tok"); then
    echo "✓ $key in .env still valid"
    return 0
  fi
  tok="$(ntfy token add --label="$name" "$name" 2>&1 | grep -o 'tk_[a-z0-9]*' | head -n1)"
  [[ -n "$tok" ]] || { echo "✗ ntfy made no token" >&2; return 1; }
  env_set "$key" "$tok"
  echo "✓ new token for $name → $key in .env"
}

user() {
  local name=${1:?usage: ntfy/setup.sh user NAME [TOPIC]} topic=${2:-bills}
  name_ok "$name" "a name" && name_ok "$topic" "a topic" || return 1
  if ! has_user "$name"; then
    # ntfy asks for the password itself (needs this terminal)
    docker compose exec ntfy ntfy user add "$name"
  fi
  ntfy access "$name" "$topic" read-only >/dev/null
  echo "✓ $name may read topic $topic"
}

status() {
  ntfy user list 2>&1 | grep -v '^$'
}

case "${1:-}" in
  token) shift; token "$@" ;;
  n8n-token) token n8n bills NTFY_N8N_TOKEN ;;
  user) shift; user "$@" ;;
  status) status ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
