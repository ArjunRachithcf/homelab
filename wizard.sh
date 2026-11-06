#!/usr/bin/env bash
#
# A wizard walks a human through a manual procedure, step by step.
# The wizard library (stage, say, ask, check, …; terminal or the setup app's protocol) is
# lib/wizard.sh; this file holds only this wizard's stages.

set -euo pipefail
# shellcheck source=lib/wizard.sh
source "$(dirname "$(readlink -f "$0")")/lib/wizard.sh"
# shellcheck source=lib/components.sh
source "$(dirname "$(readlink -f "$0")")/lib/components.sh"   # component_on (the always-on units)

# ──────────────────────────────────────────────────────────────────────────
# STAGES: author this section. One stage() per step the human takes.
# Replace the example below. Set TOTAL_STAGES to match the stages you write.
# ──────────────────────────────────────────────────────────────────────────

# Homelab: the remaining RUNBOOK.md steps (1 and 4 are done).
#   ./wizard.sh            walk through every stage (re-runnable; finished stages can be skipped)
#   ./wizard.sh --check    only run the automatic checks, e.g. after a reboot

cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD; see RUNBOOK.md step 4."; exit 1; }

DOMAIN=$(_existing DOMAIN)
ALIAS=$(_existing DOMAIN_ALIAS || true)
NB_IP=$(_existing NETBIRD_IP)
FAILED=()
# The names this wizard's DNS step and checks cover: Authentik's, and Paperless' and Jellyfin's when chosen
NAMES=(auth)
component_on paperless && NAMES+=(paperless)
component_on jellyfin && NAMES+=(jellyfin)
NAMES_SHOWN=$(IFS=/; echo "${NAMES[*]}")

# ── Helpers for this wizard (the library is lib/wizard.sh) ────────────────

# already_done "what"  offers to skip a stage that looks finished.
already_done() {
  say "$1"
  confirm "Looks done already. Skip this stage?"
}

# names_here: the NAMES.$DOMAIN resolve to $NB_IP on this peer (NetBird's DNS zones, or step 2's A records)
names_here() { local h; for h in "${NAMES[@]}"; do resolves_to "$h.$DOMAIN" "$NB_IP" || return 1; done; }
# a_records_ok: the NAMES as A records → $NB_IP on Infomaniak's nameserver (step 2, until the public cutover)
a_records_ok() {
  local h
  for h in "${NAMES[@]}"; do
    dig +short A "$h.$DOMAIN" @ns11.infomaniak.ch | grep -qxF "$NB_IP" || return 1
  done
}
# cut_over: the public wizard's cutover is done: auth, paperless and jellyfin are the wildcard CNAME to the proxy
cut_over() { local h; for h in auth paperless jellyfin; do public_dns_ok "$h" || return 1; done; }
via_caddy() { # via_caddy HOST PATH EXPECTED_CODE: Caddy's HTTPS on the NetBird IP (port 80 only redirects)
  [[ "$(code_443 "$1.$DOMAIN" "$2")" == "$3" ]]
}
listens_on_nb_only() { # port 80 (and 22 if sshd runs) bound to the NetBird IP and nothing else
  local addrs; addrs=$(ss -tlnH | awk '{print $4}' | grep -E ':(22|80)$' || true)
  grep -qx "$NB_IP:80" <<<"$addrs" && ! grep -vq "^$NB_IP:" <<<"$addrs"
}
sshd_on_nb() { ss -tlnH | awk '{print $4}' | grep -qx "$NB_IP:22"; }
all_containers_up() {
  [[ "$(docker compose ps --status running -q | wc -l)" -ge 7 ]] && ! docker compose ps | grep -q unhealthy
}
nb_connected() { netbird status | grep -q 'Management: Connected'; }
oidc_ok() { via_caddy auth /application/o/paperless/.well-known/openid-configuration 200; }
# oidc_in_env: .env has Paperless' sign-in with a real client ID (not missing, not .env.example's CHANGE_ME)
oidc_in_env() {
  local j; j=$(_existing PAPERLESS_OIDC_JSON || true)
  [[ $j == *'"client_id":"'[!\"]* && $j != *'"client_id":"CHANGE_ME"'* ]]
}

run_checks() {
  say "${BOLD}This box${RESET}"
  # Only what the chosen components bring (the setup app shows a part's checks as one verdict)
  check "NetBird connected, IP $NB_IP"            nb_connected || true
  check "all 7 containers running, none unhealthy" all_containers_up || true
  check ":80 (and :22) listen only on $NB_IP"     listens_on_nb_only || true
  if component_on ssh-sso; then check "sshd listening on $NB_IP:22" sshd_on_nb || true; fi
  check "$NAMES_SHOWN.$DOMAIN → $NB_IP here"      names_here || true
  check "Authentik answers via Caddy"             via_caddy auth /-/health/live/ 200 || true
  if component_on paperless; then
    check "Authentik's Paperless provider exists" oidc_ok || true
    check "Paperless answers via Caddy"           via_caddy paperless / 302 || true
  fi
  if component_on jellyfin; then check "Jellyfin answers via Caddy" via_caddy jellyfin / 302 || true; fi
  if component_on plex; then
    check "Plex running"                          systemctl is-active --quiet plexmediaserver || true
    check "Plex answers on :32400"                curl -sf -m 5 -o /dev/null "http://$NB_IP:32400/identity" || true
  fi
}

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

# ── Stages ────────────────────────────────────────────────────────────────

TOTAL_STAGES=11

stage_dns() {
  stage "DNS records at Infomaniak (runbook step 2)" || return 0
  if names_here; then already_done "$NAMES_SHOWN.$DOMAIN already resolve to $NB_IP here." && return; fi
  if cut_over; then
    # After the public cutover the names are the wildcard CNAME to the NetBird proxy; an A record would hide it
    say "These names went public (the wildcard CNAME to the NetBird proxy): peers find them in NetBird's DNS"
    say "zones, which the part Public access sets up (step zones). No A records: they would hide the wildcard."
    pause
    return
  fi
  open_url "https://manager.infomaniak.com/"
  step "Domains → $DOMAIN → DNS zone → Add a record, for each name:"
  local h
  for h in "${NAMES[@]}"; do step "  Type A · Source ${BOLD}$h${RESET} · Target $NB_IP · TTL 1 hour"; done
  note "A private 100.x address in public DNS is fine: only NetBird peers can reach it. With public access, the"
  note "public wizard's cutover replaces these records with the wildcard once NetBird's zones work."
  pause "Press Enter once they're saved…"
  say "Checking Infomaniak's nameserver (up to 5 minutes)…"
  for _ in $(seq 1 30); do a_records_ok && break; sleep 10; done
  check "the records live on ns11.infomaniak.ch" a_records_ok || warn "Not visible yet. Check the spelling in the zone and re-run later."
  pause
}

stage_authentik_setup() {
  stage "Authentik: admin account, MFA, group (runbook step 4)" || return 0
  skip_offer && return
  warn "Whoever opens this page first becomes admin. It's only reachable over NetBird."
  open_url "https://auth.$DOMAIN/if/flow/initial-setup/"
  step "Set the admin email and a strong password (save it in your password manager)."
  step "Top right: your avatar → Settings → MFA Devices → enrol a passkey or TOTP."
  step "Admin interface → Directory → Groups → Create: name ${BOLD}paperless-users${RESET}."
  step "Open the group → Users → Add existing user → yourself."
  pause "Press Enter when all four are done…"
}

stage_netbird() {
  stage "NetBird: devices in, Default policy off (runbook step 3)" || return 0
  skip_offer && return
  open_url "https://app.netbird.io/peers"
  step "Install the NetBird app on your phone/laptop and sign in, so each shows up under Peers."
  step "Peers → this PC: groups include ${BOLD}Plex-Server${RESET}; Login expiration off."
  step "Your phone/laptop: add to ${BOLD}Streams${RESET}; your own laptop (and phone if you SSH from it) also to ${BOLD}Admin${RESET}."
  step "Access Control → Policies: check ${BOLD}streams-to-homelab${RESET} (TCP 80, 443, 32400$(component_on public-access && echo ", $(pub_port)")) and ${BOLD}admin-ssh${RESET} (TCP 22) are enabled (RUNBOOK.md step 3)."
  pause "Press Enter when devices and groups are set…"
  say "$(netbird status | grep 'Peers count' || true)"
  step "On your phone (NetBird on), open https://paperless.$DOMAIN. It should show the sign-in page."
  if confirm "Did it load on the phone?"; then
    step "Now disable the ${BOLD}Default${RESET} policy (Access Control → Policies → toggle off)."
    pause "Press Enter once it's off…"
    step "Reload https://paperless.$DOMAIN on the phone: it should still load (the Streams policy allows it)."
    confirm "Still loads?" || { warn "Turn Default back on and check the phone is in Streams and the PC in Plex-Server."; SKIPPED+=("NetBird: disable Default policy"); }
  else
    warn "Leave Default on for now. Check the phone's NetBird app is connected and DNS (stage 1) is live."
    SKIPPED+=("NetBird: phone reachability + disable Default policy")
  fi
  pause
}

stage_oidc() {
  stage "Authentik: Paperless provider → .env (runbook step 5)" || return 0
  if oidc_ok && oidc_in_env; then
    already_done "The Paperless provider exists and .env has its client ID." && return
    # Paperless signs in only through it (PAPERLESS_SSO_ONLY): a wrong client ID locks everyone out
    warn "This replaces Paperless's sign-in (PAPERLESS_OIDC_JSON) and restarts Paperless. With SSO only, a wrong"
    warn "client ID locks everyone out of Paperless (the way back: PAPERLESS_SSO_ONLY=false, docker compose up -d paperless)."
    confirm "Replace Paperless's sign-in anyway?" || return 0
  fi
  # The browser follows Authentik's URLs from server_url: with public access they carry the public port
  local port="" d
  component_on public-access && port=":$(pub_port)"
  open_url "https://auth.$DOMAIN/if/admin/#/core/applications"
  step "Applications → Create with provider (wizard):"
  step "  Application name ${BOLD}Paperless${RESET}, slug ${BOLD}paperless${RESET}"
  step "  Provider type ${BOLD}OAuth2/OpenID Provider${RESET}, client type ${BOLD}Public${RESET} (the Less Paper app signs in with PKCE, no secret)"
  step "  Redirect URIs, each ${BOLD}Strict${RESET}:"
  for d in "$DOMAIN" ${ALIAS:+"$ALIAS"}; do
    step "    https://paperless.$d/accounts/oidc/authentik/login/callback/"
    [[ -n $port ]] && step "    https://paperless.$d$port/accounts/oidc/authentik/login/callback/"
  done
  step "    atlp://oidc-callback   (the Less Paper app)"
  step "  Signing key: ${BOLD}authentik Self-signed Certificate${RESET}; scopes: the defaults (openid, email, profile)"
  step "  Bindings: bind group ${BOLD}paperless-users${RESET}"
  step "Before finishing, copy the Client ID and Client Secret (Authentik shows one for a public client too)."
  local id secret
  ask id "Paste the Client ID:"
  ask_secret secret "Paste the Client Secret (hidden):"
  if [[ -z $id || -z $secret ]]; then warn "Missing value; .env unchanged. Re-run this stage."; SKIPPED+=("Paperless OIDC client in .env"); pause; return; fi
  local json
  json=$(OIDC_ID="$id" OIDC_SECRET="$secret" AUTH="https://auth.$DOMAIN$port" BRAND="$(_existing BRAND || echo Home)" python3 -c '
import json, os
print(json.dumps({"openid_connect": {"OAUTH_PKCE_ENABLED": True, "APPS": [{
  "provider_id": "authentik", "name": os.environ["BRAND"] + " account",
  "client_id": os.environ["OIDC_ID"], "secret": os.environ["OIDC_SECRET"],
  "settings": {"server_url": os.environ["AUTH"] + "/application/o/paperless/.well-known/openid-configuration"}}]}},
  separators=(",", ":")))')
  write_env PAPERLESS_OIDC_JSON "$json"
  chmod 600 "$ENV_FILE"
  docker compose up -d paperless >/dev/null 2>&1
  check "Authentik publishes the paperless OpenID config" oidc_ok || warn "Check the slug is exactly 'paperless'."
  pause
}

stage_paperless_sso() {
  stage "Paperless: link your Authentik account (runbook step 6)" || return 0
  if [[ "$(_existing PAPERLESS_SSO_ONLY)" == true ]]; then already_done "Paperless is already SSO-only." && return; fi
  say "First log in with the local break-glass admin (user: $(_existing PAPERLESS_ADMIN_USER))."
  if [[ -n ${WAYLAND_DISPLAY:-} ]] && command -v wl-copy >/dev/null; then
    _existing PAPERLESS_ADMIN_PASSWORD | wl-copy
    note "Its password is now in your clipboard (it is never printed)."
  else
    note "No clipboard here. Set a password you know instead:"
    note "  docker compose exec paperless python3 manage.py changepassword admin"
    pause "Press Enter once you've set it…"
  fi
  open_url "https://paperless.$DOMAIN"
  step "Log in as admin → profile (top right) → Connect new social account → Authentik → approve."
  step "Log out, then log in with ${BOLD}Authentik${RESET} to confirm it works."
  if command -v wl-copy >/dev/null && [[ -n ${WAYLAND_DISPLAY:-} ]]; then pause "Press Enter when it works (clipboard gets cleared)…"; wl-copy --clear || true; fi
  if confirm "Did the Authentik login work? Switch Paperless to SSO-only"; then
    write_env PAPERLESS_SSO_ONLY true
    chmod 600 "$ENV_FILE"
    docker compose up -d paperless >/dev/null 2>&1
    note "Locked out later? Set PAPERLESS_SSO_ONLY=false in .env and run: docker compose up -d paperless"
  else
    SKIPPED+=("Paperless: switch to SSO-only once the Authentik login works")
  fi
  pause
}

stage_plex() {
  stage "Plex: install natively and claim (runbook step 7)" || return 0
  if systemctl is-active --quiet plexmediaserver; then already_done "Plex is installed and running." && return; fi
  if [[ "$(distro_family)" != fedora ]]; then
    # Debian/Ubuntu: Plex's apt repo comes from Ansible (ansible/roles/plex/tasks/Debian.yml)
    say "Ansible adds Plex's apt repo (signed with PlexSign.v2.key) and installs plexmediaserver."
    in_terminal "ansible-playbook asks for your sudo password" ansible-playbook -K ansible/site.yml --tags plex
  else
  say "Adds Plex's official RPM repo (signed with PlexSign.v2.key) and installs plexmediaserver."
  confirm "Install Plex now? (needs sudo)" || { SKIPPED+=("Plex install"); return; }
  printf '[PlexRepo]\nname=PlexRepo\nbaseurl=https://downloads.plex.tv/repo/rpm/$basearch/\nenabled=1\ngpgcheck=1\ngpgkey=https://downloads.plex.tv/plex-keys/PlexSign.v2.key\n' \
    | sudo tee /etc/yum.repos.d/plex.repo >/dev/null
  sudo dnf -y install plexmediaserver
  sudo systemctl enable --now plexmediaserver
  fi
  check "Plex running" systemctl is-active --quiet plexmediaserver || true
  local found; found=$(find ~/Downloads ~/Videos -maxdepth 3 -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.m4v' -o -iname '*.mov' \) 2>/dev/null | wc -l)
  note "Media goes in /srv/media, e.g. /srv/media/Movies and /srv/media/TV ($found video files sit in ~/Downloads and ~/Videos)."
  say "Claiming has to happen from this PC, so it opens on localhost:"
  open_url "http://127.0.0.1:32400/web"
  step "Sign in with your Plex account, name the server, add libraries from /srv/media/…"
  step "Settings → Remote Access: ${BOLD}disable${RESET}."
  step "Settings → Network (Show Advanced): Custom server access URLs: ${BOLD}http://$NB_IP:32400${RESET}"
  step "Same page, LAN Networks: ${BOLD}100.64.0.0/10${RESET} → Save."
  note "Then test playback on the phone over mobile data; it should count as local."
  pause "Press Enter when Plex is claimed and configured…"
}

stage_jellyfin() {
  stage "Jellyfin: GPU, setup wizard, proxy (runbook step 7b)" || return 0
  skip_offer && return
  local subnet; subnet=$(docker network inspect homelab_internal -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || echo "?")
  open_url "https://jellyfin.$DOMAIN"
  step "Run the setup wizard: admin user, then a library with folder ${BOLD}/media${RESET} (subfolders /media/Movies …)."
  step "Dashboard → Networking → Known proxies: ${BOLD}$subnet${RESET} → Save."
  step "Dashboard → Users: one password user per family member (TV apps can use Quick Connect)."
  note "The GPU: RENDER_DEVICE in .env is mapped to /dev/dri/renderD128 in the container; Dashboard → Playback →"
  note "Transcoding → VAAPI with that device."
  pause "Press Enter when Jellyfin is set up…"
}

stage_backups() {
  stage "Backups: automatic and encrypted (runbook step 9)" || return 0
  say "backup.sh runs hourly and after every change: one archive in ~/homelab/backups, encrypted with age to"
  say "BACKUP_AGE_RECIPIENT; the private key is not on this box. The part Sign-in and backups (step backups)"
  say "sets it up, Photos the copies to Proton Drive."
  [[ "$(uname -s)" == Darwin ]] || check "homelab-backup.timer on" systemctl is-active --quiet homelab-backup.timer || true
  check "an encrypted archive in backups/" compgen -G 'backups/homelab-*.tar.age' || true
  note "Keep the age private key on your laptop and in Proton Pass: without it no archive can be read."
  pause
}

stage_ssh() {
  stage "SSH for you only (runbook step 10)" || return 0
  say "SSH takes your Authentik sign-in: the key in your Authentik profile, then password;code (RUNBOOK.md step 10)."
  say "The part Sign-in and backups sets it up (steps keys, host, test); ~/.ssh/authorized_keys isn't read"
  say "(AuthorizedKeysFile none), and Ansible installs sshd's drop-in."
  check "sshd listening on $NB_IP:22 only" listens_on_nb_only || true
  check "sshd's drop-in in place" test -f /etc/ssh/sshd_config.d/10-homelab.conf || true
  pause
}

stage_always_on() {
  stage "Always on: no sleep, services at boot, security updates (runbook step 11)" || return 0
  skip_offer && return
  say "1) Never suspend or hibernate:"
  note "   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target"
  confirm "Apply?" && sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  # sshd only with SSH via Authentik (its NetBird-only drop-in, Fedora); Plex only when it's chosen and
  # installed. A distro sshd enabled here would listen on every interface.
  local units=(netbird docker)
  component_on ssh-sso && [[ "$(distro_family)" == fedora ]] && units+=(sshd)
  component_on plex && systemctl list-unit-files plexmediaserver.service >/dev/null 2>&1 && units+=(plexmediaserver)
  say "2) Start at boot: ${units[*]}"
  confirm "Apply?" && sudo systemctl enable "${units[@]}"
  if [[ "$(distro_family)" == debian ]]; then
    say "3) Automatic security updates, never an automatic reboot: unattended-upgrades (its defaults)."
    if confirm "Apply?"; then
      install_pkgs unattended-upgrades
      printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
      check "unattended-upgrades on" grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades || true
    fi
  elif [[ "$(distro_family)" != fedora ]]; then
    say "3) Updates: $(distro_family) has no unattended updates here; update by hand now and then (not 02:00–05:30)."
  else
  say "3) Automatic security updates, never an automatic reboot. Writes /etc/dnf/automatic.conf:"
  note "   [commands] upgrade_type = security · apply_updates = yes · reboot = never"
  if confirm "Apply?"; then
    sudo dnf -y install dnf5-plugin-automatic
    if [[ -e /etc/dnf/automatic.conf ]]; then
      warn "/etc/dnf/automatic.conf already exists, so it was left as is; set those three values in its [commands] section."
    else
      printf '[commands]\nupgrade_type = security\napply_updates = yes\nreboot = never\n' | sudo tee /etc/dnf/automatic.conf >/dev/null
    fi
    sudo systemctl enable --now dnf5-automatic.timer
    check "dnf5-automatic.timer active" systemctl is-active --quiet dnf5-automatic.timer || true
  fi
  fi
  say "4) In the BIOS/UEFI (next time you reboot into it):"
  step "Set ${BOLD}Restore on AC power loss: Power On${RESET}."
  pause
}

stage_verify() {
  stage "Verify, then reboot test (runbook step 8)" || return 0
  run_checks
  printf '\n'
  say "${BOLD}From a Streams device on mobile data (Wi-Fi off), NetBird on:${RESET}"
  local t
  for t in "https://auth.$DOMAIN and https://paperless.$DOMAIN load; Paperless sends you to Authentik" \
           "Plex plays, and its dashboard shows the stream as local, not relayed" \
           "Jellyfin plays at https://jellyfin.$DOMAIN" \
           "Any other port, e.g. http://$NB_IP:9000, does NOT load" \
           "From a Streams-only device, ssh to $NB_IP is refused/times out"; do
    confirm "$t?" || FAILED+=("phone: $t")
  done
  if component_on public-access; then
    say "From outside NetBird only the public URLs answer (:$(pub_port), behind Authentik): the part Public access, step verify."
  else
    say "${BOLD}From a device NOT on NetBird:${RESET}"
    confirm "https://paperless.$DOMAIN times out?" || FAILED+=("off-NetBird device can reach Paperless")
  fi
  printf '\n'
  say "Last: reboot and re-check. When the PC is back up:"
  note "   sudo reboot"
  note "   ~/homelab/wizard.sh --check"
  pause
}

banner "Homelab: remaining setup on $DOMAIN ($NB_IP)"
stage_dns
stage_authentik_setup
stage_netbird
stage_oidc
stage_paperless_sso
stage_plex
stage_jellyfin
stage_backups
stage_ssh
stage_always_on
stage_verify

for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
