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


# Downloads on request (components downloads and movies-tv, opt-in): the VPN tunnel (gluetun) with qBittorrent and
# Prowlarr in it, Radarr, Sonarr, Recyclarr. Their settings over the APIs: downloads/run.sh (downloads/setup.py).
#   ./downloads-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./downloads-wizard.sh --check   only run the automatic checks (nothing when downloads isn't chosen)


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }
source lib/components.sh   # component_listed: opt-in components count only when COMPONENTS names them

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS || true)
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
MEDIA=$(_existing MEDIA_DIR || echo /srv/media)
ADMIN=$(_existing ADMIN_USER || echo "$USER")   # the apps' own logins take the same username
PROVIDER=$(_existing VPN_SERVICE_PROVIDER || echo protonvpn)
PF=1; [[ $(_existing DOWNLOADS_PORT_FORWARDING || echo true) == true ]] || PF=0
MOVIES_TV=0; component_listed movies-tv && MOVIES_TV=1
# What this run looks after: never a service of a component that isn't chosen (naming one would start it anyway)
NAMES=(qbit prowlarr); APPS=(qbittorrent prowlarr)
if ((MOVIES_TV)); then NAMES+=(radarr sonarr); APPS+=(radarr sonarr recyclarr); fi
FAILED=()
STATUS=""

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

up()      { [[ -n "$(docker compose ps --status running -q "$1" 2>/dev/null)" ]]; }
vpn_ok()  { [[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
                  "$(docker compose ps -q gluetun 2>/dev/null)" 2>/dev/null)" == healthy ]]; }
# downloads/setup.py status (the apps as the downloads network sees them), read once per round of checks
status_load() { STATUS=$(bash downloads/run.sh status 2>/dev/null || true); }
# st PATH: one value of it (a.b.c), "" when missing; lists and objects as JSON
st() {
  python3 -c 'import json, sys
try:
    d = json.loads(sys.argv[2])
    for k in sys.argv[1].split("."):
        d = d.get(k) if isinstance(d, dict) else None
except ValueError:
    d = None
print("" if d is None else json.dumps(d) if isinstance(d, (list, dict)) else d)' "$1" "$STATUS"
}
home_ip()     { curl -s -m 10 https://1.1.1.1/cdn-cgi/trace | sed -n 's/^ip=//p'; }
exit_ip_ok()  { status_load; local v h; v=$(st vpn.public_ip); h=$(home_ip); [[ -n $v && -n $h && $v != "$h" ]]; }
port_ok()     { status_load; local p; p=$(st vpn.port); [[ $p =~ ^[0-9]+$ && $p -gt 0 && $p == "$(st qbittorrent.listen_port)" ]]; }
# Nothing of theirs on this PC's own addresses (they have none: Caddy is the only way in)
no_host_ports() { ! ss -tlnH | awk '{print $4}' | grep -qE ':(8000|8080|9696|7878|8989)$'; }
gated()       { [[ "$(code "$1" /)" == 302 ]]; }                 # forward-auth: unauthenticated → Authentik
# Caddy itself reaches the app (the 302 above is Authentik's answer, not the app's)
caddy_reaches() {
  local to
  case $1 in qbit) to=gluetun:8080/ ;; prowlarr) to=gluetun:9696/ping ;; radarr) to=radarr:7878/ping ;; sonarr) to=sonarr:8989/ping ;; esac
  docker compose exec -T caddy wget -q -T 10 -O /dev/null "http://$to"
}
reachable()   { gated "$1" && caddy_reaches "$1"; }
qbt_ok()      { status_load; [[ $(st qbittorrent.categories) == '["movies", "tv"]' && $(st qbittorrent.interface) == tun0 \
                  && $(st qbittorrent.upnp) == False ]]; }
arrs_ok()     { status_load; local a; for a in radarr sonarr; do
                  [[ $(st "$a.root") == True && $(st "$a.client") == True ]] || return 1; done; }
prowlarr_apps_ok() { status_load; [[ $(st prowlarr.apps) == '["Radarr", "Sonarr"]' ]]; }
indexers_ok()      { status_load; [[ $(st prowlarr.indexers) =~ ^[1-9] ]]; }
indexers_synced()  { status_load; [[ $(st radarr.indexers) =~ ^[1-9] && $(st sonarr.indexers) =~ ^[1-9] ]]; }
no_new_seasons()   { status_load; [[ $(st sonarr.monitor_new_seasons) == 0 ]]; }
no_collections()   { status_load; [[ $(st radarr.monitored_collections) == 0 ]]; }
profiles_ok()      { status_load; [[ $(st radarr.profiles) == *'"HD Bluray + WEB"'* && $(st sonarr.profiles) == *'"WEB-1080p"'* ]]; }
# torrents/ and the libraries on one filesystem and btrfs subvolume (each has its own st_dev): else no hardlinks
same_volume() { local d t; t=$(stat -c %d "$MEDIA/torrents") || return 1
                for d in "$MEDIA/Movies" "$MEDIA/TV Shows"; do [[ $(stat -c %d "$d") == "$t" ]] || return 1; done; }
hardlinked()  { find "$MEDIA/Movies" -type f -links +1 -mtime -2 -print -quit | grep -q .; }
wg_boot()     { [[ -f /etc/modules-load.d/homelab-wireguard.conf ]] && grep -qw '^wireguard' /proc/modules; }
guard_on()    { systemctl is-active -q homelab-downloads-guard.timer; }
backup_has_downloads() { backup_section_ok 'Downloads' 'not created yet|skipped'; }
ansible_tags() {
  command -v ansible-playbook >/dev/null \
    || install_pkgs ansible-core ansible-collection-ansible-posix ansible-collection-community-general
  ansible-playbook -K ansible/site.yml --tags "$1"
}
vpn_hint() {
  warn "The tunnel doesn't work yet. Most often: the key (copied whole? from a configuration with NAT-PMP, Moderate"
  warn "NAT off, a P2P server; Proton's expire after about a year), or VPN_SERVER_COUNTRIES without such servers."
  warn "Its last lines: docker compose logs --tail 30 gluetun"
}

run_checks() {
  if ! component_listed downloads; then say "Downloads isn't chosen (COMPONENTS): nothing to check."; return 0; fi
  say "${BOLD}The VPN${RESET}"
  check "gluetun's tunnel works (healthy)"                 vpn_ok || true
  check "the exit address isn't this PC's"                 exit_ip_ok || true
  if ((PF)); then check "the VPN's forwarded port is qBittorrent's" port_ok || true; fi
  check "WireGuard loads at boot"                          wg_boot || true
  check "the downloads guard's timer"                      guard_on || true
  say "${BOLD}The apps${RESET}"
  local s n
  for s in "${APPS[@]}"; do check "$s running" up "$s" || true; done
  check "none of them on this PC's own addresses"          no_host_ports || true
  for n in "${NAMES[@]}"; do check "$n.$DOMAIN: Authentik first, Caddy reaches the app" reachable "$n" || true; done
  check "NetBird zones for ${NAMES[*]} → $NB_IP"           zone_ok "${NAMES[@]}" || true
  check "qBittorrent: categories movies/tv, through the VPN only" qbt_ok || true
  if ((MOVIES_TV)); then
    check "Radarr and Sonarr: root folders, qBittorrent"   arrs_ok || true
    check "Prowlarr: Radarr and Sonarr connected"          prowlarr_apps_ok || true
    check "no series watches for new seasons by itself"   no_new_seasons || true
    check "no Radarr collection adds films by itself"     no_collections || true
    check "torrents/ and the libraries on one volume (hardlinks)" same_volume || true
  fi
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_secrets() {
  stage "The VPN's key, and one password for the apps' own logins" || return 0
  say "Downloads go only through your VPN ($PROVIDER, in a container): torrent peers and search sites see the VPN's"
  say "address, never this PC's, and with the tunnel down nothing of them goes out."
  if [[ $PROVIDER == protonvpn ]]; then
    open_url "https://account.proton.me/u/0/vpn/WireGuard"
    step "Proton VPN → Downloads → ${BOLD}WireGuard configuration${RESET}: a name (e.g. $(hostname -s)-downloads), platform"
    step "  GNU/Linux, ${BOLD}NAT-PMP (port forwarding) on${RESET}, ${BOLD}Moderate NAT off${RESET}, a ${BOLD}P2P${RESET} server (⇄), Create."
    step "Copy the value after ${BOLD}PrivateKey =${RESET}: Proton shows it only now. Such a key lasts about a year."
  else
    step "From $PROVIDER: a WireGuard configuration (with port forwarding, if it has it), and its PrivateKey value."
  fi
  while true; do
    ask_secret VPN_WIREGUARD_PRIVATE_KEY "The PrivateKey (44 characters, ends with =):" || { FAILED+=("the VPN key"); return 0; }
    [[ $VPN_WIREGUARD_PRIVATE_KEY =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] && break
    warn "That isn't a WireGuard key: 44 characters of base64, the last one =. Copy only what follows PrivateKey = ."
  done
  write_env VPN_WIREGUARD_PRIVATE_KEY "$VPN_WIREGUARD_PRIVATE_KEY"
  say "Which countries its servers may be in (gluetun's English names, commas between), e.g. ${BOLD}Switzerland${RESET};"
  say "empty: any. With port forwarding gluetun takes only the provider's P2P servers among them."
  while ask VPN_SERVER_COUNTRIES "VPN server countries:"; do
    [[ $VPN_SERVER_COUNTRIES =~ ^[A-Za-z][A-Za-z\ ,.-]*$ || -z $VPN_SERVER_COUNTRIES ]] && break
    warn "Letters, spaces and commas only."
  done
  write_env VPN_SERVER_COUNTRIES "${VPN_SERVER_COUNTRIES:-}"
  say "qBittorrent, Prowlarr, Radarr and Sonarr each keep their own login after $BRAND's: username ${BOLD}$ADMIN${RESET}"
  say "and one password for all four, yours (keep it in your password manager); 12 characters at least."
  local old; old=$(_existing DOWNLOADS_ADMIN_PASSWORD || true)
  while true; do
    ask_secret DOWNLOADS_ADMIN_PASSWORD "Their password:" || { FAILED+=("the apps' password"); return 0; }
    if ((${#DOWNLOADS_ADMIN_PASSWORD} < 12)) || [[ $DOWNLOADS_ADMIN_PASSWORD == *$'\n'* ]]; then
      warn "At least 12 characters, on one line."; continue
    fi
    [[ -n $old && $DOWNLOADS_ADMIN_PASSWORD == "$old" ]] && break   # Enter kept the current one
    ask_secret DOWNLOADS_ADMIN_PASSWORD_AGAIN "The same once more:" || { FAILED+=("the apps' password"); return 0; }
    [[ $DOWNLOADS_ADMIN_PASSWORD_AGAIN == "$DOWNLOADS_ADMIN_PASSWORD" ]] && break
    warn "They differ: again."
  done
  write_env DOWNLOADS_ADMIN_PASSWORD "$DOWNLOADS_ADMIN_PASSWORD"
  [[ -z $old || $old == "$DOWNLOADS_ADMIN_PASSWORD" ]] || note "A new password: the next two steps (start, connect) hand it to the apps."
  pause
}

stage_host() {
  stage "This PC: kernel WireGuard at every boot, the downloads guard (Ansible)" || return 0
  if wg_boot && guard_on; then say "Both are there."; skip_offer && return; fi
  say "Ansible writes /etc/modules-load.d/homelab-wireguard.conf (the tunnel is kernel WireGuard, which a container"
  say "can't load) and the timer of the downloads guard: every 10 minutes it stops the torrents when the disk runs"
  say "low and brings qBittorrent and Prowlarr back after the VPN restarted. BECOME password: your sudo password."
  bash ansible/live-safe.sh "$HOME" 1800 || warn "busy; Ansible may refuse, try again later"
  ansible_tags docker,timers || FAILED+=("ansible --tags docker,timers")
  retry_check "WireGuard loads at boot" wg_boot || true
  retry_check "the downloads guard's timer" guard_on || true
  pause
}

stage_start() {
  stage "Start the VPN, then the apps in and beside it" || return 0
  [[ -n "$(_existing VPN_WIREGUARD_PRIVATE_KEY || true)" ]] \
    || { warn "No VPN key in .env yet: the first step comes first."; FAILED+=("start: no VPN key"); return 0; }
  say "Their settings volumes go to you first (they run as you, not root), then the VPN, then the apps."
  docker compose stop "${APPS[@]}" >/dev/null 2>&1 || true
  docker compose create gluetun "${APPS[@]}" >/dev/null || { FAILED+=("docker compose create"); return 0; }
  bash downloads/run.sh init || { FAILED+=("downloads/run.sh init"); return 0; }
  docker compose up -d gluetun >/dev/null || { FAILED+=("start gluetun"); return 0; }
  say "The tunnel takes up to a minute…"
  for _ in $(seq 1 18); do vpn_ok && break; sleep 5; done
  if ! retry_check "the VPN's tunnel works (gluetun healthy)" vpn_ok; then vpn_hint; return 0; fi
  docker compose up -d "${APPS[@]}" >/dev/null || FAILED+=("start ${APPS[*]}")
  # Caddy joins the downloads network and reads the new sites (./homelab configure wrote them)
  docker compose up -d --force-recreate --no-deps caddy >/dev/null || FAILED+=("recreate Caddy")
  retry_check "the exit address isn't this PC's" exit_ip_ok || true
  if ((PF)); then
    for _ in $(seq 1 12); do port_ok && break; sleep 5; done   # the hook runs once qBittorrent answers
    retry_check "the VPN's forwarded port is qBittorrent's" port_ok || true
  fi
  check "none of them on this PC's own addresses" no_host_ports || true
  pause
}

stage_authentik() {
  stage "Authentik: the apps for admins" || return 0
  ./authentik/setup-ssh.sh component downloads || FAILED+=("authentik/downloads.yaml")
  if ((MOVIES_TV)); then ./authentik/setup-ssh.sh component movies-tv || FAILED+=("authentik/movies-tv.yaml"); fi
  local n
  for n in "${NAMES[@]}"; do retry_check "$n.$DOMAIN: Authentik first, Caddy reaches the app" reachable "$n" || true; done
  pause
}

stage_zones() {
  stage "NetBird: private DNS zones for qbit, prowlarr, radarr, sonarr (Admin only)" || return 0
  if zone_ok "${NAMES[@]}"; then say "They already resolve to $NB_IP here."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones → Add Zone, for each of these (like n8n's and term's):"
  local n d
  for n in "${NAMES[@]}"; do for d in "$DOMAIN" ${ALIAS:+"$ALIAS"}; do
    step "  Zone ${BOLD}$n.$d${RESET} · Distribution groups: ${BOLD}Admin${RESET} only · Search domain: off"
  done; done
  step "In each zone: Add Record → type A · name = the zone name itself (@) · value ${BOLD}$NB_IP${RESET}."
  pause "Press Enter when every zone has its record…"
  retry_check "this peer resolves them to $NB_IP" zone_ok "${NAMES[@]}" || true
  pause
}

stage_connect() {
  stage "Connect them: qBittorrent's folders and limits, Radarr and Sonarr to it, Prowlarr to both" || return 0
  bash downloads/run.sh apply || { FAILED+=("downloads/run.sh apply"); return 0; }
  retry_check "qBittorrent: categories movies/tv, through the VPN only" qbt_ok || true
  if ((MOVIES_TV)); then
    retry_check "Radarr and Sonarr: root folders, qBittorrent" arrs_ok || true
    retry_check "Prowlarr: Radarr and Sonarr connected" prowlarr_apps_ok || true
  fi
  say "Their own logins, after $BRAND's: ${BOLD}$ADMIN${RESET} and the password from the first step."
  pause
}

stage_indexers() {
  stage "Prowlarr: the search sites you're entitled to use" || return 0
  open_url "https://prowlarr.$DOMAIN:$(pub_port)/"
  step "After the $BRAND sign-in, Prowlarr's own login ($ADMIN, the password from the first step)."
  step "Indexers → ${BOLD}Add Indexer${RESET}: the sites you use (a private tracker: its key or login there). Each one's"
  step "  ${BOLD}Redirect${RESET} stays off: Radarr and Sonarr then fetch through Prowlarr, behind the VPN. Test, Save."
  if ((MOVIES_TV)); then step "Prowlarr hands them to Radarr and Sonarr by itself (Settings → Apps)."; fi
  pause "Press Enter when one is added at least…"
  retry_check "Prowlarr has a search site" indexers_ok || true
  if ((MOVIES_TV)); then retry_check "Radarr and Sonarr have it too" indexers_synced || true; fi
  pause
}

stage_library() {
  stage "Your films and series: into Radarr and Sonarr, watched by nobody" || return 0
  say "Then Radarr and Sonarr know what's there (nothing gets downloaded twice). Monitor ${BOLD}None${RESET}: none of it is"
  say "searched for or upgraded unless someone asks."
  open_url "https://radarr.$DOMAIN:$(pub_port)/add/import"
  step "Radarr → Movies → ${BOLD}Library Import${RESET} → /data/Movies → check the matches → Monitor ${BOLD}None${RESET} → Import."
  open_url "https://sonarr.$DOMAIN:$(pub_port)/add/import"
  step "Sonarr → Series → ${BOLD}Library Import${RESET} → /data/TV Shows → check the matches → Monitor ${BOLD}None${RESET} → Import."
  pause "Press Enter when both are done (an empty library: Enter as well)…"
  # Sonarr's import keeps "monitor new seasons" on for every series: a new season would download by itself
  bash downloads/run.sh unmonitor-new-seasons || FAILED+=("Sonarr: new seasons")
  check "no series watches for new seasons by itself" no_new_seasons || true
  check "no Radarr collection adds films by itself" no_collections || true
  check "torrents/ and the libraries on one volume (hardlinks)" same_volume || true
  pause
}

stage_quality() {
  stage "Qualities and sizes from TRaSH Guides (Recyclarr)" || return 0
  say "Recyclarr gives Radarr the profile ${BOLD}HD Bluray + WEB${RESET} and Sonarr ${BOLD}WEB-1080p${RESET} (1080p, sensible sizes), and"
  say "syncs them again every week."
  docker compose exec -T recyclarr recyclarr sync || FAILED+=("recyclarr sync")
  retry_check "the two profiles are there" profiles_ok || true
  pause
}

stage_try() {
  stage "Try it: ask for one film you're entitled to" || return 0
  open_url "https://radarr.$DOMAIN:$(pub_port)/add/new"
  step "Radarr → Movies → ${BOLD}Add New${RESET}: a film, profile ${BOLD}HD Bluray + WEB${RESET}, Monitor ${BOLD}Movie Only${RESET} (never"
  step "  ${BOLD}Movie and Collection${RESET}: that adds every film of its series), ${BOLD}Start search for missing movie${RESET} on, Add."
  step "Radarr's Activity → Queue shows it, then qBittorrent (https://qbit.$DOMAIN:$(pub_port)/, category movies)."
  pause "Press Enter when Radarr shows it as downloaded…"
  retry_check "a film in Movies/ is a hardlink of its download (no second copy)" hardlinked || true
  step "Jellyfin and Plex find it with their next library scan."
  confirm "Does it play?" || FAILED+=("the first film")
}

stage_backup() {
  stage "Backup: the apps' settings in the hourly backup" || return 0
  say "A backup now (sudo), then a look at its Downloads section."
  sudo systemctl start homelab-backup.service || FAILED+=("homelab-backup")
  retry_check "the last backup has the downloads apps" backup_has_downloads || true
  pause
}

run_wizard() {

TOTAL_STAGES=11

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Downloads on request: the VPN, qBittorrent, Prowlarr, Radarr, Sonarr"
stage_secrets
stage_host
stage_start
stage_authentik
stage_zones
stage_connect
stage_indexers
stage_library
stage_quality
stage_try
stage_backup

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
