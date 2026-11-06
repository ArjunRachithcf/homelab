#!/usr/bin/env bash
# ~/homelab/ssh/setup-host.sh: host side of SSH via Authentik (RUNBOOK.md step 10).
# Run AFTER ./authentik/setup-ssh.sh, from ~/homelab:   sudo ./ssh/setup-host.sh
# Idempotent. Asks before changing PAM. Never prints the bind password.
#
# Installs SSSD (LDAPS to the Authentik outpost ldap-ssh at 172.21.1.10:6636), switches only
# /etc/pam.d/sshd's auth to pam_sss, installs the sshd drop-in, enables sshd.
# Console login, sudo, nsswitch and authselect stay untouched.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")/.."

LDAP_IP=172.21.1.10
LDAP_NAME=ldap.homelab.internal

env_get() { sed -n "s/^$1=//p" .env | tail -n1 | sed "s/^'\(.*\)'$/\1/"; }
SSH_USER="${SSH_USER:-$(env_get ADMIN_USER)}"
SSH_USER="${SSH_USER:-${SUDO_USER:?run with sudo from your own account, or set ADMIN_USER in .env}}"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

NB_IP=$(env_get NETBIRD_IP)
BIND_PW=$(env_get SSH_LDAP_BIND_PASSWORD)
[[ -n "$NB_IP" ]] || fail "NETBIRD_IP missing in .env"
[[ -n "$BIND_PW" ]] || fail "SSH_LDAP_BIND_PASSWORD missing: run ./authentik/setup-ssh.sh first"
[[ -s ssh/ldap-ssh.pem ]] || fail "ssh/ldap-ssh.pem missing: run ./authentik/setup-ssh.sh first"
id "$SSH_USER" >/dev/null || fail "no local user $SSH_USER"

echo "1/7 packages"
rpm -q sssd-ldap sssd-client sssd-tools >/dev/null 2>&1 || dnf -y -q install sssd-ldap sssd-client sssd-tools
ok "sssd-ldap sssd-client sssd-tools"

echo "2/7 SELinux: let sssd connect to 6636 (stays enforcing)"
if semanage port -l | awk '$1=="ldap_port_t"' | grep -qw 6636; then ok "6636 already ldap_port_t"
else semanage port -a -t ldap_port_t -p tcp 6636 2>/dev/null || semanage port -m -t ldap_port_t -p tcp 6636; ok "6636 → ldap_port_t"; fi

echo "3/7 name + CA for the outpost"
grep -qE "^${LDAP_IP}[[:space:]]+$LDAP_NAME\b" /etc/hosts || echo "$LDAP_IP $LDAP_NAME   # homelab: Authentik LDAP outpost for SSH" >> /etc/hosts
install -d -m 0755 /etc/sssd/pki
install -m 0644 ssh/ldap-ssh.pem /etc/sssd/pki/ldap-ssh.pem
restorecon -R /etc/sssd/pki
timeout 10 openssl s_client -connect "$LDAP_IP:6636" -servername "$LDAP_NAME" -verify_hostname "$LDAP_NAME" \
  -CAfile /etc/sssd/pki/ldap-ssh.pem -verify_return_error </dev/null >/dev/null 2>&1 \
  && ok "LDAPS $LDAP_NAME:6636 answers, certificate verified" \
  || fail "LDAPS to $LDAP_IP:6636 failed (docker compose ps authentik-ldap-ssh?)"

echo "4/7 sssd.conf"
tmp=$(mktemp)
BIND_PW="$BIND_PW" awk '{ gsub(/__SSH_LDAP_BIND_PASSWORD__/, ENVIRON["BIND_PW"]); print }' ssh/sssd.conf > "$tmp"
if getent group sssd >/dev/null; then install -m 0640 -o root -g sssd "$tmp" /etc/sssd/sssd.conf
else install -m 0600 -o root -g root "$tmp" /etc/sssd/sssd.conf; fi
rm -f "$tmp"
restorecon /etc/sssd/sssd.conf
sssctl config-check >/dev/null || fail "sssctl config-check failed"
systemctl enable sssd >/dev/null 2>&1
systemctl restart sssd
sss_cache -E 2>/dev/null || true
sleep 2
nkeys=$(sss_ssh_authorizedkeys "$SSH_USER" 2>/dev/null | grep -c . || true)
if [[ "$nkeys" -gt 0 ]]; then ok "sss_ssh_authorizedkeys $SSH_USER: $nkeys key(s) from Authentik"
else echo "  ⚠ no keys from Authentik yet (./authentik/setup-ssh.sh add-key). Continuing."; fi

echo "5/7 PAM (sshd only)"
new=$(mktemp)
sed -E 's/^auth[[:space:]]+substack[[:space:]]+password-auth$/auth       required     pam_sss.so/' /etc/pam.d/sshd > "$new"
if cmp -s "$new" /etc/pam.d/sshd; then
  grep -q '^auth.*pam_sss.so' /etc/pam.d/sshd && ok "already pam_sss" || fail "unexpected /etc/pam.d/sshd, not touching it"
else
  diff -u /etc/pam.d/sshd "$new" || true
  read -r -p "  Apply this change to /etc/pam.d/sshd? [y/N] " a
  [[ "$a" == [yY]* ]] || { rm -f "$new"; fail "PAM unchanged, stopping before sshd"; }
  [[ -e /etc/pam.d/sshd.homelab-bak ]] || cp -a /etc/pam.d/sshd /etc/pam.d/sshd.homelab-bak
  cat "$new" > /etc/pam.d/sshd       # keep owner, mode, SELinux label
  ok "pam_sss for sshd auth (backup: /etc/pam.d/sshd.homelab-bak)"
fi
rm -f "$new"

echo "6/7 sshd drop-in"
# sshd has never run here: create the missing host keys (sshd -t needs them)
ssh-keygen -A >/dev/null
restorecon -R /etc/ssh
sed -e "s/__NETBIRD_IP__/$NB_IP/" -e "s/__SSH_USER__/$SSH_USER/" ssh/10-homelab.conf > /etc/ssh/sshd_config.d/10-homelab.conf
chmod 0600 /etc/ssh/sshd_config.d/10-homelab.conf
restorecon /etc/ssh/sshd_config.d/10-homelab.conf
if ! sshd -t; then
  rm -f /etc/ssh/sshd_config.d/10-homelab.conf
  fail "sshd -t failed; drop-in removed, sshd untouched"
fi
ok "sshd -t passes"

echo "7/7 sshd"
systemctl enable sshd >/dev/null 2>&1
systemctl restart sshd
sleep 1
ss -tlnH 'sport = :22' | grep -qF "$NB_IP:22" || fail "sshd isn't listening on $NB_IP:22 (journalctl -u sshd)"
ss -tlnH 'sport = :22' | awk '{print $4}' | grep -qvx "$NB_IP:22" && fail "sshd listens beyond $NB_IP:22: $(ss -tlnH 'sport = :22' | awk '{print $4}' | xargs)"
ok "sshd on $NB_IP:22 only"

cat <<EOF

  Test from the laptop now, keeping this console logged in:
    ssh homelab      key from Authentik, then Password: <password>;<6-digit code>
  Undo PAM if needed:  sudo cp /etc/pam.d/sshd.homelab-bak /etc/pam.d/sshd
  Stop SSH entirely:   sudo systemctl disable --now sshd
EOF
