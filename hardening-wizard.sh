#!/usr/bin/env bash
#
# A wizard walks a human through a manual procedure, step by step.
# The wizard library (stage, say, ask, check, …; terminal or the setup app's protocol) is
# lib/wizard.sh; this file holds only this wizard's stages.

set -euo pipefail
# shellcheck source=lib/wizard.sh
source "$(dirname "$(readlink -f "$0")")/lib/wizard.sh"

# ──────────────────────────────────────────────────────────────────────────
# STAGES: author this section. One stage() per step the human takes.
# Replace the example below. Set TOTAL_STAGES to match the stages you write.
# Keep every stage inside run_wizard and the last line as is: bash then parses
# the whole file before the first prompt, so an edit mid-run can't break it.
# ──────────────────────────────────────────────────────────────────────────


# Homelab hardening + SSH via Authentik: the human-only parts of RUNBOOK.md step 10,
# step 9 (automatic backups) and "Hardening".
#   ./hardening-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./hardening-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail: never touch its mail records
NB_IP=$(_existing NETBIRD_IP)
SSH_USER=$(_existing ADMIN_USER || echo "$USER")
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
LDAP_IP=172.21.1.10
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

# ak_is KEY VALUE: a line of `authentik/setup-ssh.sh status` (totp=1, webauthn=0, …)
AK_STATUS=""
ak_refresh() { AK_STATUS=$(./authentik/setup-ssh.sh status 2>/dev/null || true); }
ak_val()  { sed -n "s/^$1=//p" <<<"$AK_STATUS"; }
ak_is()   { [[ "$(ak_val "$1")" == "$2" ]]; }
ak_min1() { [[ "$(ak_val "$1")" =~ ^[1-9] ]]; }

ldap_ssh_running() { [[ -n "$(docker compose ps --status running -q authentik-ldap-ssh)" ]]; }
ldaps_ok() {
  [[ -s ssh/ldap-ssh.pem ]] && timeout 10 openssl s_client -connect "$LDAP_IP:6636" \
    -servername ldap.homelab.internal -verify_hostname ldap.homelab.internal \
    -CAfile ssh/ldap-ssh.pem -verify_return_error </dev/null
}
sssd_keys()  { sss_ssh_authorizedkeys "$SSH_USER" 2>/dev/null | grep -q .; }
sshd_ok() {
  systemctl is-active -q sshd || return 1
  local a; a=$(ss -tlnH 'sport = :22' | awk '{print $4}')
  [[ "$a" == "$NB_IP:22" ]]
}
no_ldap_on_host() { ! ss -tlnH | awk '{print $4}' | grep -qE ':(389|636|3389|6636)$'; }
listens_on_nb_only() {
  local addrs; addrs=$(ss -tlnH | awk '{print $4}' | grep -E ":(22|80|443|8444|$(_existing NETBIRD_PROXY_PORT || echo 9443))\$" || true)
  [[ -n "$addrs" ]] && ! grep -vq "^$NB_IP:" <<<"$addrs"
}
headers_ok() {
  local h; h=$(curl -sI -m 10 --resolve "auth.$DOMAIN:443:$NB_IP" "https://auth.$DOMAIN/-/health/live/")
  grep -qi '^strict-transport-security: max-age=31536000' <<<"$h" && grep -qi '^x-content-type-options: nosniff' <<<"$h" \
    && ! grep -qi '^server:' <<<"$h"
}
nnp_all() {
  local c
  for c in $(docker compose ps -q); do
    docker inspect -f '{{.HostConfig.SecurityOpt}}' "$c" | grep -q no-new-privileges || return 1
  done
}
all_healthy() { ! docker compose ps --format '{{.Status}}' | grep -qiE 'unhealthy|restarting|exited'; }

# Over HTTPS: this network intercepts plain DNS to 1.1.1.1 and drops the AD (validated) flag.
dnssec_ok() {
  curl -s -m 10 -H 'accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=$1&type=SOA&do=1" \
    | grep -q '"AD":true'
}
caa_ok()    { dig +short CAA "$DOMAIN" @1.1.1.1 | grep -q 'issue "letsencrypt.org"'; }

timer_ok()      { systemctl is-enabled -q homelab-backup.timer && systemctl is-active -q homelab-backup.timer; }
backup_exists() { compgen -G 'backups/homelab-*.tar.age' >/dev/null; }
latest_backup() { ls -t backups/homelab-*.tar.age 2>/dev/null | head -n1; }

run_checks() {
  ak_refresh
  say "${BOLD}Authentik${RESET}"
  check "akadmin renamed to $SSH_USER (placeholder inactive)" ak_is akadmin_active no || true
  check "$SSH_USER has TOTP"                      ak_min1 totp || true
  check "$SSH_USER has a passkey"                 ak_min1 webauthn || true
  check "$SSH_USER has a phone number"            ak_is phone yes || true
  check "$SSH_USER in ssh-admins"                 ak_is ssh_admin yes || true
  check "SSH public key(s) in Authentik"          ak_min1 keys || true
  check "sign-up flow exists (phone on sign-up)"  enrollment_ok || true
  say "${BOLD}SSH${RESET}"
  check "outpost authentik-ldap-ssh running"      ldap_ssh_running || true
  check "LDAPS $LDAP_IP:6636, certificate valid"  ldaps_ok || true
  check "SSSD returns your keys"                  sssd_keys || true
  check "sshd active, only on $NB_IP:22"          sshd_ok || true
  check "no LDAP port on the host"                no_ldap_on_host || true
  say "${BOLD}Box${RESET}"
  check "22/80/443/8444/$(_existing NETBIRD_PROXY_PORT || echo 9443) only on $NB_IP" listens_on_nb_only || true
  check "LAN closed (zone homelab-lan, wt0 trusted, mesh guard)" lan_closed || true
  check "all containers healthy/running"          all_healthy || true
  check "no-new-privileges on every container"    nnp_all || true
  check "HSTS + nosniff, no Server header"        headers_ok || true
  say "${BOLD}DNS${RESET}"
  check "DNSSEC $DOMAIN"                          dnssec_ok "$DOMAIN" || true
  check "DNSSEC $ALIAS"                           dnssec_ok "$ALIAS" || true
  check "CAA $DOMAIN allows Let's Encrypt"        caa_ok || true
  say "${BOLD}Backups${RESET}"
  check "homelab-backup.timer enabled"            timer_ok || true
  check "an encrypted backup exists"              backup_exists || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_prereq() {
  stage "Prerequisite: public-access wizard" || return 0
  say "Sign-up and recovery flows come from ./public-wizard.sh. The phone field and the"
  say "strong-password rule attach to them, so they should exist first."
  if enrollment_ok && recovery_ok; then
    printf '  %s✓%s enrollment and recovery flows exist\n' "$GREEN" "$RESET"; pause; return
  fi
  warn "Not there yet. Best: Ctrl-C, run ./public-wizard.sh, then come back."
  note "Or continue now and later run: ./authentik/setup-ssh.sh hardening"
  confirm "Continue without them" || exit 0
  SKIPPED+=("after public-wizard.sh: ./authentik/setup-ssh.sh hardening")
}

stage_authentik() {
  stage "Authentik: SSH directory + hardening" || return 0
  ak_refresh
  if ak_is outpost yes && ak_is akadmin_active no && ldap_ssh_running; then
    say "Already set up."; skip_offer && return
  fi
  say "This runs ./authentik/setup-ssh.sh, which:"
  step "renames your Authentik admin ${BOLD}akadmin → $SSH_USER${RESET} (log in with that or your email;"
  step "  Paperless keeps its link; a disabled 'akadmin' placeholder stays behind)"
  step "creates cert ldap-ssh, group ssh-admins, bind account ldap-ssh, outpost ldap-ssh"
  step "applies hardening: phone number required, strong passwords (≥12, not leaked),"
  step "  12 h sessions, ${BOLD}passkey-only web login for admins${RESET}, failed-login alerts"
  step "saves SSH_LDAP_BIND_PASSWORD + AK_LDAP_SSH_OUTPOST_TOKEN to .env, starts the outpost"
  warn "Your next Authentik web login will ask you to register a passkey (next stage)."
  confirm "Run it now" || { SKIPPED+=("./authentik/setup-ssh.sh"); return; }
  ./authentik/setup-ssh.sh || warn "setup-ssh.sh reported an error (see above)"
  sleep 5; ak_refresh
  retry_check "renamed, outpost created"     bash -c "./authentik/setup-ssh.sh status | grep -qx outpost=yes" || true
  retry_check "outpost authentik-ldap-ssh running" ldap_ssh_running || true
  retry_check "LDAPS answers with our certificate"  ldaps_ok || true
  pause
}

stage_passkey() {
  stage "Your login: passkey + phone number" || return 0
  ak_refresh
  if ak_min1 webauthn && ak_is phone yes; then say "Passkey and phone already set."; skip_offer && return; fi
  say "Use a private browser window, so your current session isn't in the way:"
  open_url "https://auth.$DOMAIN/"
  step "Log in with your username or email + password + TOTP."
  step "You're asked to ${BOLD}register a passkey${RESET}. On this PC (no fingerprint/face reader, no"
  step "  Bluetooth for the phone QR): let the ${BOLD}Proton Pass extension${RESET} save it (allow it in private"
  step "  windows), or use a security key. Register more later: Settings → MFA Devices."
  step "You're asked for your ${BOLD}mobile number${RESET} once, with country code (e.g. $(_existing PHONE_EXAMPLE || echo "+44 7700 900123"))."
  note "TOTP stays on your account: SSH and Jellyfin apps use password;123456 with it."
  note "Locked out? On the PC's console: docker compose exec authentik-worker ak create_recovery_key 1 $SSH_USER"
  pause "Done both?"
  ak_refresh
  retry_check "passkey registered"   bash -c "./authentik/setup-ssh.sh status | grep -q '^webauthn=[1-9]'" || true
  retry_check "TOTP still present"   bash -c "./authentik/setup-ssh.sh status | grep -q '^totp=[1-9]'" || true
  retry_check "phone number stored"  bash -c "./authentik/setup-ssh.sh status | grep -qx phone=yes" || true
  pause
}

stage_keys() {
  stage "SSH public keys → your Authentik profile" || return 0
  ak_refresh
  if ak_min1 keys; then
    say "Keys already stored:"; ./authentik/setup-ssh.sh list-keys | sed 's/^/    /'
    confirm "Add more" || return 0
  fi
  say "One key per device, so a lost phone is revoked by deleting one line."
  say "${BOLD}Laptop${RESET} (with a FIDO2 key like a YubiKey: touch per login):"
  note "  ssh-keygen -t ed25519-sk -O resident -C $SSH_USER-laptop-yubikey"
  note "  or without: ssh-keygen -t ed25519 -a 100 -C $SSH_USER-laptop   (set a passphrase)"
  say "${BOLD}Phone${RESET} (Termius: Keychain → Generate key → Ed25519 → Export public key)."
  local keys=""
  ask_lines keys "Paste each ${BOLD}.pub${RESET} line (starts with ssh-ed25519 / sk-ssh-ed25519…), then an empty line:"
  if [[ -n "$keys" ]]; then
    printf '%s' "$keys" | ./authentik/setup-ssh.sh add-key || warn "add-key failed (see above)"
  fi
  note "Revoke later: Authentik admin → Directory → Users → $SSH_USER → Attributes → sshPublicKey."
  ak_refresh
  retry_check "at least one key stored" bash -c "./authentik/setup-ssh.sh status | grep -q '^keys=[1-9]'" || true
  pause
}

stage_netbird() {
  stage "NetBird: who may reach port 22" || return 0
  open_url "https://app.netbird.io/"
  step "Access Control → Policies: ${BOLD}admin-ssh${RESET} exists: source Admin → destination"
  step "  Plex-Server, TCP 22, not bidirectional, enabled (RUNBOOK step 3). Create it if not."
  step "Only your laptop/phone in group ${BOLD}Admin${RESET}; family devices only in Streams."
  note "Posture checks and peer approval need NetBird's Business plan: skip them on the free plan."
  step "Instead, now and then: Peers → delete any device you don't recognise."
  step "Peers → this PC ($PEER) → ${BOLD}Login expiration off${RESET} (so it never drops at night)."
  note "Labels may have moved slightly; the intent is what counts."
  pause "Done?"
  confirm "admin-ssh policy exists and only your devices are in Admin" || FAILED+=("NetBird admin-ssh policy")
  confirm "Login expiration is off for this PC" || SKIPPED+=("NetBird: login expiration off for $PEER")
}

stage_host() {
  stage "This PC: SSSD + sshd (needs sudo)" || return 0
  if sshd_ok && sssd_keys; then say "sshd already runs with Authentik keys."; skip_offer && return; fi
  say "Runs ${BOLD}sudo ./ssh/setup-host.sh${RESET}: installs SSSD, labels port 6636 for SELinux,"
  say "adds ldap.homelab.internal to /etc/hosts, writes /etc/sssd/sssd.conf (0600),"
  say "switches ${BOLD}only sshd's${RESET} PAM auth to pam_sss (console login and sudo unchanged),"
  say "installs the sshd drop-in, checks sshd -t, starts sshd on $NB_IP:22."
  say "It shows this PAM diff and asks before applying:"
  note "  -auth       substack     password-auth"
  note "  +auth       required     pam_sss.so"
  confirm "Run it now" || { SKIPPED+=("sudo ./ssh/setup-host.sh"); return; }
  sudo ./ssh/setup-host.sh || warn "setup-host.sh stopped (see above)"
  retry_check "SSSD returns your keys"         sssd_keys || true
  retry_check "sshd active, only on $NB_IP:22" sshd_ok || true
  pause
}

stage_test() {
  stage "Test SSH (keep this console logged in)" || return 0
  say "On the laptop, ~/.ssh/config:"
  note "  Host homelab"
  note "      HostName $NB_IP"
  note "      User $SSH_USER"
  note "      IdentityFile ~/.ssh/id_ed25519_sk     # or id_ed25519"
  step "${BOLD}ssh homelab${RESET} → key (touch it), then ${BOLD}Password:${RESET} <Authentik password>;<6-digit code>"
  step "Termius on the phone: host $NB_IP, user $SSH_USER, your key; answer the password prompt the same way."
  step "Try a ${BOLD}wrong code${RESET}: must be refused."
  step "From a Streams-only device: ${BOLD}nc -vz $NB_IP 22${RESET} must time out."
  note "If it fails: journalctl -u sshd -u sssd -n 50 · undo PAM: sudo cp /etc/pam.d/sshd.homelab-bak /etc/pam.d/sshd"
  local t
  for t in "laptop: ssh homelab works" "phone (Termius) works" "a wrong TOTP code is refused" \
           "a Streams-only device can't reach port 22"; do
    confirm "$t?" || FAILED+=("ssh: $t")
  done
  say "SELinux denials in the last 10 minutes (should be none):"
  sudo ausearch -m avc -ts recent 2>/dev/null | grep -E 'sssd|sshd' | tail -n 5 | sed 's/^/    /' || note "    none"
  pause
}

stage_dnssec() {
  stage "Infomaniak: DNSSEC on both zones" || return 0
  if dnssec_ok "$DOMAIN" && dnssec_ok "$ALIAS"; then say "DNSSEC already validates."; skip_offer && return; fi
  open_url "https://manager.infomaniak.com/"
  step "Domains → ${BOLD}$DOMAIN${RESET} → DNSSEC → Activate."
  step "Domains → ${BOLD}$ALIAS${RESET} → DNSSEC → Activate."
  note "This signs the zones; it changes no records. Proton Mail on $ALIAS works with DNSSEC."
  note "Validation (the 'ad' flag) can take up to a day while the DS record propagates."
  pause "Activated both?"
  retry_check "DNSSEC validates for $DOMAIN" dnssec_ok "$DOMAIN" || true
  retry_check "DNSSEC validates for $ALIAS"  dnssec_ok "$ALIAS" || true
  pause
}

stage_caa() {
  stage "Infomaniak: CAA on $DOMAIN (who may issue certificates)" || return 0
  if caa_ok; then say "CAA already allows Let's Encrypt."; skip_offer && return; fi
  local c apex_issuer cl_caa
  c=$(cluster)
  apex_issuer=$(echo | timeout 10 openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | sed 's/^issuer=//')
  cl_caa=$( [[ -n "$c" ]] && dig +short CAA "$c" @1.1.1.1 )
  say "Before adding anything:"
  note "  apex redirect cert issuer: ${apex_issuer:-<none found>}"
  note "  CAA on the NetBird cluster ${c:-?}: ${cl_caa:-<none>} (the wildcard CNAME makes"
  note "  *.${DOMAIN} answer with that one first; none = $DOMAIN's own CAA applies)"
  if [[ -n "$cl_caa" ]] && ! grep -q letsencrypt <<<"$cl_caa"; then
    warn "The cluster's CAA doesn't allow Let's Encrypt: Caddy renewals would fail. Skipping CAA."
    SKIPPED+=("CAA: cluster CAA blocks Let's Encrypt, ask NetBird"); pause; return
  fi
  if [[ -n "$apex_issuer" ]] && ! grep -qi "let's encrypt" <<<"$apex_issuer"; then
    warn "The apex redirect uses another CA. Add an extra 'issue' record for it too (its CAA domain)."
  fi
  open_url "https://manager.infomaniak.com/"
  step "Domains → $DOMAIN → DNS zone → Add record → CAA, at the apex ($DOMAIN):"
  step "  ${BOLD}0 issue \"letsencrypt.org\"${RESET}"
  step "  ${BOLD}0 issuewild \";\"${RESET}   (no wildcard certificates at all)"
  local mtasts
  mtasts=$(dig +short TXT "_mta-sts.$ALIAS" @1.1.1.1)
  if [[ -z "$mtasts" ]]; then
    note "Optional: the same two on $ALIAS (no MTA-STS record there). Touch nothing else in it."
  else
    warn "$ALIAS has an MTA-STS record: leave CAA off $ALIAS."
  fi
  pause "Added?"
  retry_check "CAA $DOMAIN allows Let's Encrypt" caa_ok || true
  pause
}

stage_backups() {
  stage "Automatic encrypted backups" || return 0
  say "Backups are encrypted to a key whose private half never touches this PC."
  say "${BOLD}On the laptop${RESET} (macOS: brew install age · Fedora: sudo dnf install age · Debian/Ubuntu: sudo apt install age):"
  note "  mkdir -p ~/.config/age && age-keygen -o ~/.config/age/homelab.key"
  note "  It prints: Public key: age1…   (the file holds AGE-SECRET-KEY-1…)"
  pause "Generated?"
  say "${BOLD}Proton Pass${RESET}: if the laptop dies, this is the only other copy."
  step "Proton Pass → New → Note, title ${BOLD}homelab backup age key${RESET}"
  step "Paste the whole ${BOLD}homelab.key${RESET} file (the AGE-SECRET-KEY-1… line incl. comments) → Save."
  step "Open the note again and check the AGE-SECRET-KEY line is complete."
  warn "Without this key every backup is unreadable. Nobody can recover it."
  until confirm "The private key is saved in Proton Pass and you checked it"; do
    warn "Please save it first; the backups are useless without it."
  done
  local r=""
  while :; do
    ask BACKUP_AGE_RECIPIENT "Paste the PUBLIC key (age1…):" || return 0
    r=$BACKUP_AGE_RECIPIENT
    [[ "$r" =~ ^age1[0-9a-z]{58}$ ]] && break
    warn "That doesn't look like an age public key (age1 + 58 characters). The secret key goes in Proton Pass, not here."
  done
  write_env BACKUP_AGE_RECIPIENT "$r"
  say "Installing age and the systemd timer (sudo):"
  command -v age >/dev/null || install_pkgs age
  ansible_timers || FAILED+=("ansible-playbook --tags timers")
  say "First run (a minute or two)…"
  sudo systemctl start homelab-backup.service || warn "backup failed: journalctl -u homelab-backup -n 30"
  retry_check "timer enabled (daily 03:30)" timer_ok || true
  retry_check "encrypted backup written"   backup_exists || true
  [[ -n "$(latest_backup)" ]] && note "  $(latest_backup)"
  say "${BOLD}Test a restore on the laptop${RESET} (proves the key works):"
  note "  scp homelab:homelab/$(latest_backup) ."
  note "  age -d -i ~/.config/age/homelab.key homelab-*.tar.age | tar -t | head"
  confirm "The test listed export/, authentik.sql.zst and .env" || FAILED+=("backup: test decrypt on laptop")
  say "Keep a copy off this PC now and then (laptop, external disk, Proton Drive)."
  local old; old=$(ls backups/authentik-*.sql ~/homelab-backup-*.tar.gz.gpg 2>/dev/null || true)
  if [[ -n "$old" ]] && backup_exists; then
    say "Older backups from the first setup:"; sed 's/^/    /' <<<"$old"
    if confirm "Delete backups/authentik-*.sql (an unencrypted DB dump)"; then
      rm -f backups/authentik-*.sql && printf '  %s✓%s deleted\n' "$GREEN" "$RESET"
    fi
  fi
  SKIPPED+=("copy backups/homelab-*.tar.age off this PC regularly")
  pause
}

stage_verify() {
  stage "Final checks" || return 0
  run_checks
  pause
}

run_wizard() {

TOTAL_STAGES=11

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab: SSH via Authentik + hardening"
stage_prereq
stage_authentik
stage_passkey
stage_keys
stage_netbird
stage_host
stage_test
stage_dnssec
stage_caa
stage_backups
stage_verify

for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
