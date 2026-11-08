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


# Homelab Immich (photos.<domain>): the human-only parts of RUNBOOK.md step 7c + the Proton
# Drive part of step 9.
#   ./immich-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./immich-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
PHOTOS=$(_existing IMMICH_UPLOAD_DIR)   # /srv/photos
# Second NVMe, by its stable name (nvme0/nvme1 can swap between boots). PHOTOS_DISK in .env.
DISK=$(_existing PHOTOS_DISK || true)
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
# Both CLIs are installed and kept current by proton-drive/update.sh (Proton's version.json + SHA-512)
PD=$HOME/.local/opt/proton-drive/proton-drive
PDH=$HOME/.local/opt/proton-drive-homelab/proton-drive
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

# findmnt names a bind mount (RUNBOOK.md 7e: /srv/photos = <second disk>/photos) as /dev/…p1[/photos]
# $PHOTOS comes from the disk's first partition: directly, or through an encryption (dm-crypt) mapping on it, so lsblk
# -s walks from the mount's source down to the disk and the partition must be on that way
disk_ok()     { local src part; src=$(findmnt -n -o SOURCE "$PHOTOS"); src=${src%%\[*}; part=$(readlink -f "$DISK-part1")
                  [[ -n "$src" && -n "$part" ]] && lsblk -nsrpo NAME "$src" 2>/dev/null | grep -qxF "$part" \
                  && [[ -w "$PHOTOS" ]] && grep -q "[[:space:]]${PHOTOS}[[:space:]]" "${FSTAB:-/etc/fstab}"; }
# The disk already carries the photo filesystem: PHOTOS_UUID from .env, or the label photos that
# immich:disk gives it (lsblk reads udev's database: no sudo)
holds_library() { local uuid pairs; [[ -n "$DISK" ]] || return 1; uuid=$(_existing PHOTOS_UUID || true)
                  pairs=$(lsblk -nP -o UUID,LABEL "$DISK" 2>/dev/null) || return 1
                  grep -q 'LABEL="photos"' <<<"$pairs" || { [[ -n "$uuid" ]] && grep -qF "UUID=\"$uuid\"" <<<"$pairs"; }; }
immich_up()   { local s; for s in immich-server immich-machine-learning immich-redis immich-db; do
                  [[ -n "$(docker compose ps --status running -q "$s")" ]] || return 1; done; }
ping_443()    { curl -s -m 10 --resolve "photos.$DOMAIN:443:$NB_IP" "https://photos.$DOMAIN/api/server/ping" | grep -q pong; }
ping_8444()   { curl -s -m 10 --haproxy-protocol --resolve "photos.$DOMAIN:8444:$NB_IP" "https://photos.$DOMAIN:8444/api/server/ping" | grep -q pong; }
no_host_port() { ! ss -tlnH | awk '{print $4}' | grep -qE ':(2283|3003)$'; }
sso_on()      { ./immich/setup.sh status | jq -e '.oauth.enabled == true'; }
sso_only_on() { ./immich/setup.sh status | jq -e '.oauth.autoLaunch == true and .passwordLogin == false'; }
# The cluster must hand TLS through: the cert is Caddy's Let's Encrypt one, and Immich answers.
public_ok() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  echo | openssl s_client -connect "$ip:$(pub_port)" -servername "photos.$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | grep -q "Let's Encrypt" || return 1
  curl -s -m 10 --resolve "photos.$DOMAIN:$(pub_port):$ip" "https://photos.$DOMAIN:$(pub_port)/api/server/ping" | grep -q pong
}
pd_installed() { [[ -x "$PD" ]] && "$PD" version 2>/dev/null | grep -q "cli-drive@"; }
pd_session()   { PROTON_DRIVE_CREDENTIALS_STORE=pass "$PD" filesystem list / ; }
pd_backed_up() { PROTON_DRIVE_CREDENTIALS_STORE=pass "$PD" filesystem list /my-files/homelab | grep -q photos; }
timer_on()     { systemctl is-enabled homelab-photos-backup.timer && systemctl is-active homelab-photos-backup.timer; }
COPY=$(_existing PHOTOS_COPY_DIR || echo /srv/photos-copy)
copy_ready()   { [[ "$(stat -f -c %T "$COPY" 2>/dev/null)" == btrfs && -w "$COPY" ]]; }
copy_done()    { [[ -f "$COPY/current/library/.immich" ]]; }
local_timer_on() { systemctl is-enabled homelab-photos-local.timer && systemctl is-active homelab-photos-local.timer; }
pdh_installed() { [[ -x "$PDH" ]] && "$PDH" photo upload --help 2>/dev/null | grep -q -- --live-photos; }
pdh_session()   { PROTON_DRIVE_CREDENTIALS_STORE=pass "$PDH" photo timeline; }
family_album()  { PROTON_DRIVE_CREDENTIALS_STORE=pass "$PDH" album list | grep -q ' Family ('; }
update_timer_on() { systemctl is-enabled homelab-proton-drive-update.timer && systemctl is-active homelab-proton-drive-update.timer; }

run_checks() {
  say "${BOLD}This box${RESET}"
  check "$PHOTOS = second NVMe (fstab), writable"        disk_ok || true
  check "Immich containers running"                     immich_up || true
  check "Immich on no host port (2283/3003)"            no_host_port || true
  check "photos.$DOMAIN answers (NetBird path, :443)"    ping_443 || true
  check "photos.$DOMAIN answers on the public listener" ping_8444 || true
  check "Immich SSO on ($BRAND account)"               sso_on || true
  check "Immich SSO only (no passwords)"                sso_only_on || true
  check "NetBird zones photos.$DOMAIN / .$ALIAS → $NB_IP" zone_ok photos || true
  check "MX $ALIAS still Proton (as a peer sees it)"    mx_via_peer || true
  say "${BOLD}Public internet${RESET}"
  check "photos.* → $(cluster) (wildcard)"               public_dns_ok photos || true
  check "cluster passes photos TLS through to Immich"    public_ok || true
  say "${BOLD}Proton Drive${RESET}"
  check "proton-drive (official) installed"             pd_installed || true
  check "proton-drive signed in (pass store)"           pd_session || true
  check "/my-files/homelab/photos exists"                pd_backed_up || true
  check "homelab-photos-backup.timer enabled"            timer_on || true
  say "${BOLD}Local copy (primary SSD)${RESET}"
  check "$COPY on the primary SSD (btrfs), writable"   copy_ready || true
  check "$COPY/current holds the library"              copy_done || true
  check "homelab-photos-local.timer enabled (dedupe + copy)" local_timer_on || true
  say "${BOLD}Proton Photos${RESET}"
  check "patched proton-drive (Live Photos, thumbnails)" pdh_installed || true
  check "patched proton-drive signed in (own session)"   pdh_session || true
  check "album Family exists"                            family_album || true
  check "homelab-proton-drive-update.timer enabled"      update_timer_on || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_disk() {
  stage "Second NVMe: wipe → $PHOTOS" || return 0
  if disk_ok; then say "$PHOTOS is already the photos disk."; skip_offer && return; fi
  # The library's disk, only not mounted (slow at boot with nofail, an fstab put back): never the wipe
  if holds_library; then
    warn "$DISK already holds the photo library (filesystem \"photos\"); it just isn't mounted at $PHOTOS. Nothing is wiped."
    say "Mount what /etc/fstab lists: ${BOLD}sudo mount -a${RESET}, then check: ${BOLD}findmnt $PHOTOS${RESET}"
    FAILED+=("$PHOTOS isn't mounted, though its disk holds the library (sudo mount -a)")
    pause; return
  fi
  [[ -n "$DISK" && -b "$DISK" && "$DISK" == /dev/disk/by-id/* ]] \
    || { warn "Set PHOTOS_DISK in .env to the disk's /dev/disk/by-id/ name (ls -l /dev/disk/by-id/ | grep -v part)."; exit 1; }
  # A whole disk, not a partition, md or dm device (a by-id name can be any of them), and not the boot disk
  [[ "$(lsblk -dno TYPE "$DISK")" == disk ]] \
    || { warn "$DISK is a $(lsblk -dno TYPE "$DISK"), not a whole disk. Set PHOTOS_DISK to the disk's by-id name."; exit 1; }
  local m src
  for m in / /boot /boot/efi; do
    src=$(findmnt -nvo SOURCE "$m" 2>/dev/null) || continue
    if lsblk -nsrpo NAME,TYPE "$src" 2>/dev/null | awk '$2 == "disk" {print $1}' | grep -qx "$(readlink -f "$DISK")"; then
      warn "$DISK holds $m. Stopped; tell Claude."; exit 1
    fi
  done
  say "The whole disk ${BOLD}$(lsblk -dno MODEL,SIZE "$DISK" | xargs)${RESET} gets wiped: every partition on it."
  say "This PC must not boot from it: anything on it that is mounted or used as swap stops this stage."
  printf '\n'; lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS "$DISK" | sed 's/^/    /'; printf '\n'
  # Refuse if anything on it is in use (mounted or swap)
  local dev
  for dev in $(lsblk -nrpo NAME "$DISK"); do
    if findmnt -n -S "$dev" >/dev/null || swapon --noheadings --show=NAME | grep -qx "$dev"; then
      warn "$dev is in use (mounted or swap). Stopped; tell Claude."; exit 1
    fi
  done
  # A volume group that also lives on another disk would lose a PV: refuse instead of degrading it
  local vg pv devs; mapfile -t devs < <(lsblk -nrpo NAME "$DISK")
  for vg in $(sudo pvs --noheadings -o vg_name "${devs[@]}" 2>/dev/null | sort -u); do
    for pv in $(sudo pvs --noheadings -o pv_name --select "vg_name=$vg" 2>/dev/null); do
      printf '%s\n' "${devs[@]}" | grep -qx "$(readlink -f "$pv")" \
        || { warn "Volume group $vg also uses $pv (another disk). Stopped; tell Claude."; exit 1; }
    done
  done
  danger "Everything on $(readlink -f "$DISK") is destroyed. This can't be undone." wipe \
    || { warn "Stopped, nothing changed."; exit 0; }

  # Old LVM first, or the kernel keeps its volumes open
  for vg in $(sudo pvs --noheadings -o vg_name "${devs[@]}" 2>/dev/null | sort -u); do
    sudo vgchange -an "$vg" >/dev/null 2>&1 || true
  done
  sudo wipefs -a "$DISK"-part* >/dev/null 2>&1 || true
  # TRIM the whole SSD (instant, really empties it); wipefs if the drive refuses
  sudo blkdiscard -f "$DISK" 2>/dev/null || sudo wipefs -a "$DISK"
  sudo parted -s "$DISK" mklabel gpt mkpart photos xfs 1MiB 100%
  sudo udevadm settle
  sudo mkfs.xfs -f -L photos "$DISK-part1" >/dev/null
  sudo udevadm settle
  local uuid; uuid=$(sudo blkid -s UUID -o value "$DISK-part1")
  [[ -n "$uuid" ]] || { warn "No filesystem UUID; tell Claude."; exit 1; }
  say "New XFS filesystem \"photos\" (UUID $uuid)."
  write_env PHOTOS_UUID "$uuid"

  sudo cp -a /etc/fstab /etc/fstab.pre-immich
  # nofail: boot goes on without the disk; Immich then refuses to start (missing .immich markers)
  grep -q "[[:space:]]${PHOTOS}[[:space:]]" /etc/fstab || \
    printf 'UUID=%s %s xfs defaults,nofail 0 2   # Immich library (homelab RUNBOOK 7c)\n' "$uuid" "$PHOTOS" \
      | sudo tee -a /etc/fstab >/dev/null
  sudo mkdir -p "$PHOTOS"
  sudo systemctl daemon-reload
  findmnt -n "$PHOTOS" >/dev/null || sudo mount "$PHOTOS"
  sudo chown "$(id -u):$(id -g)" "$PHOTOS"
  retry_check "$PHOTOS mounted from fstab and writable" disk_ok || true
  pause
}

stage_start() {
  stage "Immich starts (Claude's part)" || return 0
  if immich_up && sso_on; then say "Immich is running and configured."; skip_offer && return; fi
  say "The disk is ready. Starting the containers and setting up SSO is Claude's part."
  step "Tell Claude: ${BOLD}disk is mounted${RESET}. Then run ${BOLD}./immich-wizard.sh${RESET} again"
  step "(skip stage 1 when it asks)."
  exit 0
}

stage_zones() {
  stage "NetBird: private DNS zones for photos" || return 0
  if zone_ok photos; then say "photos.$DOMAIN and photos.$ALIAS already resolve to $NB_IP here."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones → Add Zone, twice (like home/auth/paperless/jellyfin):"
  local d
  for d in "$DOMAIN" "$ALIAS"; do
    step "  Zone ${BOLD}photos.$d${RESET} · Distribution groups: Streams, Admin, Plex-Server · Search domain: off"
  done
  step "In each zone: Add Record → type A · name = the zone name itself (@) · value ${BOLD}$NB_IP${RESET}."
  pause "Press Enter when both zones have their record…"
  for _ in $(seq 1 12); do zone_ok photos && break; sleep 5; done
  retry_check "this peer resolves photos.$DOMAIN / photos.$ALIAS → $NB_IP" zone_ok photos || true
  retry_check "MX $ALIAS still Proton on a peer" mx_via_peer || true
  pause
}

stage_services() {
  stage "NetBird: 2 reverse proxy services, TLS passthrough" || return 0
  if public_ok; then say "photos.$DOMAIN already answers through the cluster."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy/services"
  step "Reverse Proxy → Services → Add Service: ${BOLD}photos.$DOMAIN${RESET}, then ${BOLD}photos.$ALIAS${RESET}."
  step "Each exactly like the other 8:"
  step "  Mode: ${BOLD}TLS${RESET} (passthrough). ${RED}Never HTTP${RESET}: that would decrypt at NetBird."
  step "  Listen port: ${BOLD}$(pub_port)${RESET} · Advanced Settings: ${BOLD}Preserve Client Source IP${RESET} on."
  step "  Target: Peer ${BOLD}$PEER${RESET} · port ${BOLD}8444${RESET} · ${BOLD}PROXY Protocol${RESET}: on."
  note "No Infomaniak change: the * CNAME already covers photos."
  pause "Press Enter when both are saved…"
  retry_check "photos.* → $(cluster) in public DNS" public_dns_ok photos || true
  for _ in $(seq 1 12); do public_ok && break; sleep 5; done
  retry_check "cluster passes photos TLS through (Let's Encrypt) to Immich" public_ok || true
  pause
}

stage_browser() {
  stage "First sign-in in the browser" || return 0
  open_url "https://photos.$DOMAIN:$(pub_port)/"
  step "Click ${BOLD}$BRAND account${RESET} → sign in as usual (passkey)."
  step "You land in Immich as yourself. Top right → your avatar → you see ${BOLD}Administration${RESET}"
  step "(you're in authentik Admins, so Immich makes you admin)."
  note "The local admin photos-admin@$DOMAIN is only for emergencies; its password is in .env."
  confirm "Signed in with $BRAND account, and you're an Immich admin?" || FAILED+=("browser SSO login")
}

stage_phone() {
  stage "Phone: Immich app + backup" || return 0
  step "Install ${BOLD}Immich${RESET} (App Store / Play Store)."
  step "Server URL: ${BOLD}https://photos.$DOMAIN:$(pub_port)${RESET} → ${BOLD}$BRAND account${RESET} → sign in."
  step "Backup (cloud icon, top right) → select albums (e.g. Recents) → turn on backup."
  step "Test once on mobile data with NetBird ${BOLD}off${RESET}: take a photo, it uploads."
  confirm "Phone signed in and a photo arrived in the web view?" || FAILED+=("phone app login/upload")
}

stage_sso_only() {
  stage "Immich: SSO only" || return 0
  if sso_only_on; then say "Already SSO only."; skip_offer && return; fi
  say "From now on the login page goes straight to Authentik, and passwords are off."
  note "Break-glass: docker compose exec immich-server immich-admin enable-password-login"
  if confirm "Your browser and phone logins worked. Switch to SSO only now"; then
    ./immich/setup.sh sso-only
    retry_check "Immich SSO only" sso_only_on || true
  else
    FAILED+=("SSO only not switched on")
  fi
  pause
}

stage_proton() {
  stage "Proton Drive: off-box copy of photos and backups" || return 0
  if pd_session && timer_on; then say "proton-drive is signed in and the timer runs."; skip_offer && return; fi
  if ! pd_installed || ! pdh_installed; then
    say "Installing Proton's Drive CLI (checksum from proton.me) and building the patched one…"
    ./proton-drive/update.sh || true
  fi
  check "proton-drive (official) installed" pd_installed || true

  say "The session is kept in ${BOLD}pass${RESET} with its own GPG key without a passphrase, so"
  say "the nightly timer works without a desktop login."
  warn "That session can reach your whole Proton Drive. Anyone who gets your user on this box can too."
  command -v pass >/dev/null 2>&1 || install_pkgs pass
  if [[ ! -f ~/.password-store/.gpg-id ]]; then
    local uid="homelab backup <photos-admin@$DOMAIN>" fpr
    gpg --list-keys "$uid" >/dev/null 2>&1 || \
      gpg --batch --pinentry-mode loopback --passphrase '' --quick-gen-key "$uid" default default never
    fpr=$(gpg --list-keys --with-colons "$uid" | awk -F: '/^fpr/ {print $10; exit}')
    pass init "$fpr"
  else
    note "You already have a pass store; proton-drive uses it with its existing key."
    note "That key must work without a passphrase prompt, or the nightly timer fails."
  fi

  if ! pd_session; then
    step "Next, sign in to Proton in the browser (password + 2FA). Use your account with the 2 TB."
    pause "Press Enter to start the sign-in…"
    in_terminal "Sign in to Proton Drive (asks for your Proton password)" \
      env PROTON_DRIVE_CREDENTIALS_STORE=pass "$PD" auth login || true
  fi
  retry_check "proton-drive signed in" pd_session || true

  say "First copy: photos + encrypted backups (it can take a while)…"
  /usr/bin/bash ./backup-photos.sh || FAILED+=("first backup-photos.sh run")
  retry_check "/my-files/homelab/photos exists in Proton Drive" pd_backed_up || true

  say "Nightly timer (04:30, after the 03:30 backup):"
  ansible_timers || FAILED+=("ansible-playbook --tags timers")
  retry_check "homelab-photos-backup.timer enabled" timer_on || true
  open_url "https://drive.proton.me/"
  step "In Proton Drive: My files → homelab → photos and backups are there."
  pause
}

stage_local() {
  stage "Second copy on the primary SSD + nightly dedupe" || return 0
  if copy_done && local_timer_on; then say "The copy exists and the timer runs."; skip_offer && return; fi
  say "Every night at 04:00 (photos-local.sh):"
  step "identical photos in your two accounts share their space on the photos disk (duperemove)"
  step "the originals are copied to ${BOLD}$COPY${RESET} on the Kingston SSD; deleted ones kept 30 days"
  say "$COPY becomes its own btrfs subvolume (easy to measure or remove later)."
  if ! copy_ready; then
    [[ -e "$COPY" ]] || sudo btrfs subvolume create "$COPY" >/dev/null
    sudo chown "$(id -u):$(id -g)" "$COPY"
  fi
  retry_check "$COPY on btrfs, writable" copy_ready || true
  ansible_timers || FAILED+=("ansible-playbook --tags timers")
  say "First run (the full copy takes a few minutes)…"
  sudo systemctl start homelab-photos-local.service || warn "first run failed: journalctl -u homelab-photos-local -n 30"
  retry_check "$COPY/current holds the library" copy_done || true
  retry_check "homelab-photos-local.timer enabled" local_timer_on || true
  pause
}

stage_proton_photos() {
  stage "Proton Photos: patched CLI, album Family, weekly updates" || return 0
  if pdh_session && family_album && update_timer_on; then say "All set."; skip_offer && return; fi
  say "Photos also go to Proton Photos (album ${BOLD}Family${RESET}) with our build of Proton's CLI:"
  step "Live Photos stay live, thumbnails come from Immich (proton-drive/README.md)."
  pdh_installed || ./proton-drive/update.sh || true
  retry_check "patched proton-drive built" pdh_installed || true
  if ! pdh_session; then
    say "It has its own session (shows as ${BOLD}external-drive-homelab_photos${RESET} in your Proton sessions)."
    pause "Press Enter to sign in (browser, password + 2FA)…"
    in_terminal "Sign in to Proton Drive with the patched CLI (asks for your Proton password)" \
      env PROTON_DRIVE_CREDENTIALS_STORE=pass "$PDH" auth login || true
  fi
  retry_check "patched proton-drive signed in" pdh_session || true
  if ! family_album; then
    open_url "https://drive.proton.me/photos"
    step "Photos → Albums → New album ${BOLD}Family${RESET}; share it with the family."
    pause
  fi
  retry_check "album Family exists" family_album || true
  say "Weekly update check (Sundays 02:30):"
  ansible_timers || FAILED+=("ansible-playbook --tags timers")
  retry_check "homelab-proton-drive-update.timer enabled" update_timer_on || true
  pause
}

run_wizard() {

TOTAL_STAGES=10

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab: Photos (Immich) at photos.$DOMAIN"
stage_disk
stage_start
stage_zones
stage_services
stage_browser
stage_phone
stage_sso_only
stage_proton
stage_local
stage_proton_photos

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
