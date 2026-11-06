#!/usr/bin/env bash
# ~/homelab/authentik/setup-ssh.sh: Authentik side of SSH-via-LDAP, account hardening and each component's
# sign-in. Run as yourself (docker group) from anywhere; idempotent, never prints secrets. The logic is
# lib/authentik.py (the commands: ./authentik/setup-ssh.sh help); what runs inside Authentik is
# lib/authentik_tasks.py.
# shellcheck source=../lib/need-bash.sh
source "$(dirname "$0")/../lib/need-bash.sh"   # bash 4.4+ (macOS: Homebrew's)
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=../lib/portable.sh
source lib/portable.sh   # a python3 with tomllib
exec python3 lib/authentik.py "$@"
