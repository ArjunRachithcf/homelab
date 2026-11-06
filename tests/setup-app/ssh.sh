#!/usr/bin/env bash
# tests/setup-app/ssh.sh: the setup app's SSH transport against a real, throwaway sshd (127.0.0.1,
# a high port, its own host key and an authorized key for this user). Needs sshd and ssh-keygen;
# skipped (exit 0) without them. Changes nothing outside a temp directory.
set -euo pipefail
cd "$(dirname "$0")/../../setup-app/src-tauri"
SSHD=$(command -v sshd || echo /usr/sbin/sshd)
if [[ ! -x "$SSHD" ]] || ! command -v ssh-keygen >/dev/null; then echo "· no sshd: SSH test skipped"; exit 0; fi
t=$(mktemp -d); trap 'kill "$(cat "$t/pid" 2>/dev/null)" 2>/dev/null || true; rm -rf "$t"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$t/host"
ssh-keygen -q -t ed25519 -N '' -f "$t/user"
cp "$t/user.pub" "$t/authorized_keys"; chmod 600 "$t/authorized_keys"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
cat > "$t/sshd_config" <<CONF
Port $port
ListenAddress 127.0.0.1
HostKey $t/host
PidFile $t/pid
AuthorizedKeysFile $t/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
StrictModes no
UsePAM no
CONF
mkdir -p /run/sshd 2>/dev/null || true
"$SSHD" -f "$t/sshd_config" -E "$t/log"
for _ in 1 2 3 4 5 6 7 8 9 10; do [[ -s "$t/pid" ]] && break; sleep 0.2; done
fp=$(ssh-keygen -lf "$t/host.pub" | awk '{print $2}')
SETUP_APP_SSH="127.0.0.1 $port $(id -un) $t/user $fp" cargo test --quiet ssh_round_trip -- --nocapture 2>&1 | tail -n 5
