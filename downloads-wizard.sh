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


# Downloads on request (components downloads, movies-tv, albums, subtitles, book-requests, byparr, requests; all
# opt-in): the VPN tunnel (gluetun) with qBittorrent, Prowlarr and Byparr in it, Radarr, Sonarr, Recyclarr, Lidarr,
# Bazarr, Bindery, Seerr. Authentik is the one sign-in a person uses; each app's own credentials are generated (.env).
# Their settings over the APIs: downloads/run.sh (downloads/setup.py). Stages are defined in the order they run.
#   ./downloads-wizard.sh           walk through every stage of the chosen ones (re-runnable; finished stages can be skipped)
#   ./downloads-wizard.sh --check   only run the automatic checks (nothing when downloads isn't chosen)


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }
source lib/components.sh   # component_listed: opt-in components count only when COMPONENTS names them

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS || true)
NB_IP=$(_existing NETBIRD_IP)
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
MEDIA=$(_existing MEDIA_DIR || echo /srv/media)
PROVIDER=$(_existing VPN_SERVICE_PROVIDER || echo protonvpn)
PF=1; [[ $(_existing DOWNLOADS_PORT_FORWARDING || echo true) == true ]] || PF=0
DL_NET=$(_existing DOWNLOADS_NET || echo 198.51.100)
on() { component_listed "$1"; }
MOVIES_TV=0; on movies-tv && MOVIES_TV=1
# What this run looks after: never a service of a component that isn't chosen (naming one would start it anyway).
# NAMES: behind forward-auth, the Admin group's zones; FAMILY_NAMES: the family's zones. Seerr and Bindery start in
# their own stages (their first sign-in makes their admin), never in start.
NAMES=(qbit prowlarr); APPS=(qbittorrent prowlarr); FAMILY_NAMES=()
on byparr && APPS+=(byparr)
if ((MOVIES_TV)); then NAMES+=(radarr sonarr); APPS+=(radarr sonarr recyclarr); fi
if on albums; then NAMES+=(lidarr); APPS+=(lidarr); fi
if on subtitles; then NAMES+=(subs); APPS+=(bazarr); fi
on book-requests && FAMILY_NAMES+=(bindery)
on requests && FAMILY_NAMES+=(requests)
FAILED=()
STATUS=""

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

up()      { [[ -n "$(docker compose ps --status running -q "$1" 2>/dev/null)" ]]; }
healthy() { [[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
                  "$(docker compose ps -q "$1" 2>/dev/null)" 2>/dev/null)" == healthy ]]; }
vpn_ok()  { healthy gluetun; }
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
# sorted JSON list of the arguments, as st prints one
json_list() { python3 -c 'import json, sys; print(json.dumps(sorted(sys.argv[1:])))' "$@"; }
home_ip()     { curl -s -m 10 https://1.1.1.1/cdn-cgi/trace | sed -n 's/^ip=//p'; }
exit_ip_ok()  { status_load; local v h; v=$(st vpn.public_ip); h=$(home_ip); [[ -n $v && -n $h && $v != "$h" ]]; }
port_ok()     { status_load; local p; p=$(st vpn.port); [[ $p =~ ^[0-9]+$ && $p -gt 0 && $p == "$(st qbittorrent.listen_port)" ]]; }
# Nothing of theirs on this PC's own addresses (they have none: Caddy is the only way in)
no_host_ports() { ! ss -tlnH | awk '{print $4}' | grep -qE ':(8000|8080|9696|7878|8989|8686|6767|8787|5055|8191)$'; }
gated()       { [[ "$(code "$1" /)" == 302 ]]; }                 # forward-auth: unauthenticated → Authentik
# Caddy itself reaches the app (the 302 above is Authentik's answer, not the app's)
caddy_reaches() {
  local to
  case $1 in
    qbit) to=gluetun:8080/ ;; prowlarr) to=gluetun:9696/ping ;; radarr) to=radarr:7878/ping ;; sonarr) to=sonarr:8989/ping ;;
    lidarr) to=lidarr:8686/ping ;; subs) to=bazarr:6767/api/system/ping ;; bindery) to=bindery:8787/api/v1/health ;;
    requests) to=seerr:5055/api/v1/status ;;
  esac
  docker compose exec -T caddy wget -q -T 10 -O /dev/null "http://$to"
}
reachable()   { gated "$1" && caddy_reaches "$1"; }
# Seerr: no forward-auth (its sign-in is Jellyfin's), its own accounts and Quick Connect shut in Caddy
requests_ok() { [[ "$(code requests /api/v1/status)" == 200 && "$(code requests /api/v1/auth/local)" == 403 ]] \
                  && caddy_reaches requests; }
categories()  { local c=(); ((MOVIES_TV)) && c+=(movies tv); on albums && c+=(music)
                on book-requests && c+=(books audiobooks); json_list "${c[@]}"; }
qbt_ok()      { status_load; [[ $(st qbittorrent.categories) == "$(categories)" && $(st qbittorrent.interface) == tun0 \
                  && $(st qbittorrent.upnp) == False && $(st qbittorrent.whitelist) == "$DL_NET.11/32" ]]; }
# Every app refuses a neighbour without its own credentials (downloads/setup.py status, which is one)
locked_ok()   { status_load; [[ $(st locked) == '{'*'}' && $(st locked) != '{}' && $(st locked) != *false* ]]; }
arrs_ok()     { status_load; local a; for a in radarr sonarr; do
                  [[ $(st "$a.root") == True && $(st "$a.client") == True ]] || return 1; done; }
prowlarr_apps() { local a=(); ((MOVIES_TV)) && a+=(Radarr Sonarr); on albums && a+=(Lidarr); json_list "${a[@]}"; }
prowlarr_apps_ok() { status_load; [[ $(st prowlarr.apps) == "$(prowlarr_apps)" ]]; }
byparr_ok()        { status_load; [[ $(st prowlarr.proxies) == '["FlareSolverr"]' ]]; }
indexers_ok()      { status_load; [[ $(st prowlarr.indexers) =~ ^[1-9] ]]; }
indexers_synced()  { status_load; local a; for a in radarr sonarr lidarr; do
                       [[ -z $(st "$a") || $(st "$a.indexers") =~ ^[1-9] ]] || return 1; done; }
no_new_seasons()   { status_load; [[ $(st sonarr.monitor_new_seasons) == 0 ]]; }
no_collections()   { status_load; [[ $(st radarr.monitored_collections) == 0 ]]; }
profiles_ok()      { status_load; [[ $(st radarr.profiles) == *'"HD Bluray + WEB"'* && $(st sonarr.profiles) == *'"WEB-1080p"'* ]]; }
lidarr_ok()        { status_load; [[ $(st lidarr.root) == True && $(st lidarr.client) == True && $(st lidarr.tags_written) == False \
                       && $(st lidarr.monitor_new_albums) == 0 ]]; }
languages()        { local l; IFS=, read -ra l <<<"$(_existing BAZARR_LANGUAGES | tr -d ' ' || echo en)"; json_list "${l[@]:-en}"; }
bazarr_ok()        { status_load; [[ $(st bazarr.sonarr) == True && $(st bazarr.radarr) == True && $(st bazarr.signed_in) == True \
                       && $(st bazarr.languages) == "$(languages)" ]]; }
bindery_ok()       { status_load; [[ $(st bindery.ready) == True && $(st bindery.setup_open) == False \
                       && $(st bindery.client) == True && $(st bindery.prowlarr) == True ]]; }
bindery_sso_ok()   { status_load; [[ $(st bindery.sso) == ok ]]; }
seerr_ready()      { status_load; [[ $(st seerr.ready) == True ]]; }
seerr_ok()         { status_load; [[ $(st seerr.ours) == True && $(st seerr.initialized) == True && $(st seerr.local_login) == False \
                       && $(st seerr.radarr) == True && $(st seerr.sonarr) == True ]]; }
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
project() { docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])'; }
members() { ./authentik/setup-ssh.sh members "$1" | paste -sd, -; }
# own_stage SERVICE COMMAND…: an app whose first sign-in makes its admin, so nobody may come first: created, its volume
# handed over, started and set up back to back (COMMAND), stopped again if that fails
own_stage() {
  local svc=$1; shift
  docker compose create "$svc" >/dev/null || { FAILED+=("docker compose create $svc"); return 1; }
  bash downloads/run.sh init >/dev/null || { FAILED+=("downloads/run.sh init"); return 1; }
  docker compose up -d --no-deps "$svc" >/dev/null || { FAILED+=("start $svc"); return 1; }
  local _; for _ in $(seq 1 40); do healthy "$svc" && break; sleep 3; done
  healthy "$svc" || { docker compose stop "$svc" >/dev/null; FAILED+=("$svc didn't start (docker compose logs $svc)"); return 1; }
  if ! "$@"; then
    docker compose stop "$svc" >/dev/null
    FAILED+=("$svc: its first setup (stopped again)"); return 1
  fi
}
# Seerr's first sign-in, the password typed in its stage in this one command's environment only (run.sh hands it on
# by stdin)
seerr_first() { JELLYFIN_ADMIN_PASSWORD=$1 bash downloads/run.sh seerr-init; }
# Caddy lets anyone reach Seerr only once this exists (caddy/components/requests/sites.caddy): its admin is ours
SEERR_OURS=site/status/requests-ready

run_checks() {
  if ! on downloads; then say "Downloads isn't chosen (COMPONENTS): nothing to check."; return 0; fi
  say "${BOLD}The VPN${RESET}"
  check "gluetun's tunnel works (healthy)"                 vpn_ok || true
  check "the exit address isn't this PC's"                 exit_ip_ok || true
  if ((PF)); then check "the VPN's forwarded port is qBittorrent's" port_ok || true; fi
  check "WireGuard loads at boot"                          wg_boot || true
  check "the downloads guard's timer"                      guard_on || true
  say "${BOLD}The apps${RESET}"
  local s n
  for s in "${APPS[@]}"; do check "$s running" up "$s" || true; done
  on book-requests && { check "bindery running" up bindery || true; }
  on requests && { check "seerr running" up seerr || true; }
  check "none of them on this PC's own addresses"          no_host_ports || true
  for n in "${NAMES[@]}"; do check "$n.$DOMAIN: Authentik first, Caddy reaches the app" reachable "$n" || true; done
  on book-requests && { check "bindery.$DOMAIN: Authentik first, Caddy reaches the app" reachable bindery || true; }
  on requests && { check "requests.$DOMAIN: Seerr, its own accounts shut" requests_ok || true; }
  check "NetBird zones for ${NAMES[*]} ${FAMILY_NAMES[*]} → $NB_IP" zone_ok "${NAMES[@]}" "${FAMILY_NAMES[@]}" || true
  check "each app refuses a neighbour without its own credentials" locked_ok || true
  check "qBittorrent: its categories, through the VPN only, Caddy's address alone signed in" qbt_ok || true
  on byparr && { check "Prowlarr: Byparr for the sites tagged cloudflare" byparr_ok || true; }
  if ((MOVIES_TV)); then
    check "Radarr and Sonarr: root folders, qBittorrent"   arrs_ok || true
    check "Prowlarr: its apps connected"                   prowlarr_apps_ok || true
    check "no series watches for new seasons by itself"   no_new_seasons || true
    check "no Radarr collection adds films by itself"     no_collections || true
    check "torrents/ and the libraries on one volume (hardlinks)" same_volume || true
  fi
  on albums && { check "Lidarr: Music/, qBittorrent, no tags written, no artist watches new albums" lidarr_ok || true; }
  on subtitles && { check "Bazarr: Sonarr, Radarr, the languages" bazarr_ok || true; }
  on book-requests && { check "Bindery: Authentik's sign-in only, qBittorrent, Prowlarr" bindery_ok || true; }
  on requests && { check "Seerr: set up, Jellyfin sign-in only, Radarr and Sonarr" seerr_ok || true; }
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_secrets() {
  stage "The VPN's key" || return 0
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
  note "No password to choose: you sign in to every app with $BRAND's sign-in. Their own (generated, in .env) are"
  note "break-glass and the apps' way to each other."
  pause
}

stage_host() {
  stage "This PC: kernel WireGuard at every boot, the downloads guard (Ansible)" || return 0
  if wg_boot && guard_on; then say "Both are there."; skip_offer && return; fi
  say "Ansible writes /etc/modules-load.d/homelab-wireguard.conf (the tunnel is kernel WireGuard, which a container"
  say "can't load) and the timer of the downloads guard: every 10 minutes it stops the torrents when the disk runs"
  say "low and brings the apps in the VPN back after it restarted. BECOME password: your sudo password."
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
  # Caddy joins the arr and requests networks and reads the new sites (./homelab configure wrote them)
  docker compose up -d --force-recreate --no-deps caddy >/dev/null || FAILED+=("recreate Caddy")
  # #8's network (downloads, its range not pinned): its apps and Caddy have just left it
  docker network rm "$(project)_downloads" >/dev/null 2>&1 || true
  retry_check "the exit address isn't this PC's" exit_ip_ok || true
  if ((PF)); then
    for _ in $(seq 1 12); do port_ok && break; sleep 5; done   # the hook runs once qBittorrent answers
    retry_check "the VPN's forwarded port is qBittorrent's" port_ok || true
  fi
  check "none of them on this PC's own addresses" no_host_ports || true
  pause
}

stage_authentik() {
  stage "Authentik: the apps' sign-in" || return 0
  local c
  for c in downloads movies-tv albums subtitles book-requests; do
    on "$c" || continue
    ./authentik/setup-ssh.sh component "$c" || FAILED+=("authentik/$c.yaml")
  done
  local n
  for n in "${NAMES[@]}"; do retry_check "$n.$DOMAIN: Authentik first, Caddy reaches the app" reachable "$n" || true; done
  pause
}

stage_zones() {
  stage "NetBird: private DNS zones for the apps" || return 0
  if zone_ok "${NAMES[@]}" "${FAMILY_NAMES[@]}"; then say "They already resolve to $NB_IP here."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones → Add Zone, for each of these (like n8n's and term's):"
  local n d
  for n in "${NAMES[@]}"; do for d in "$DOMAIN" ${ALIAS:+"$ALIAS"}; do
    step "  Zone ${BOLD}$n.$d${RESET} · Distribution groups: ${BOLD}Admin${RESET} only · Search domain: off"
  done; done
  for n in "${FAMILY_NAMES[@]}"; do for d in "$DOMAIN" ${ALIAS:+"$ALIAS"}; do
    step "  Zone ${BOLD}$n.$d${RESET} · Distribution groups: the family's, as ${BOLD}jellyfin.$d${RESET} has them · Search domain: off"
  done; done
  step "In each zone: Add Record → type A · name = the zone name itself (@) · value ${BOLD}$NB_IP${RESET}."
  pause "Press Enter when every zone has its record…"
  retry_check "this peer resolves them to $NB_IP" zone_ok "${NAMES[@]}" "${FAMILY_NAMES[@]}" || true
  pause
}

stage_connect() {
  stage "Connect them: qBittorrent's folders and limits, the apps to it, Prowlarr to them" || return 0
  bash downloads/run.sh apply || FAILED+=("downloads/run.sh apply (its ✗ lines say which app)")
  retry_check "qBittorrent: its categories, through the VPN only, Caddy's address alone signed in" qbt_ok || true
  if ((MOVIES_TV)); then
    retry_check "Radarr and Sonarr: root folders, qBittorrent" arrs_ok || true
    retry_check "Prowlarr: its apps connected" prowlarr_apps_ok || true
  fi
  on byparr && { retry_check "Prowlarr: Byparr for the sites tagged cloudflare" byparr_ok || true; }
  check "each app refuses a neighbour without its own credentials" locked_ok || true
  say "One sign-in: $BRAND's. After it every app opens without a login of its own."
  pause
}

stage_indexers() {
  stage "Prowlarr: the search sites you're entitled to use" || return 0
  open_url "https://prowlarr.$DOMAIN:$(pub_port)/"
  step "Indexers → ${BOLD}Add Indexer${RESET}: the sites you use (a private tracker: its key or login there). Each one's"
  step "  ${BOLD}Redirect${RESET} stays off: the apps then fetch through Prowlarr, behind the VPN. Test, Save."
  if on byparr; then
    step "A site behind Cloudflare's check (its test says so): give it the tag ${BOLD}cloudflare${RESET}, then Test again: Byparr"
    step "  solves the check for it, inside the VPN. Other sites never go through Byparr."
  fi
  if ((MOVIES_TV)) || on albums; then step "Prowlarr hands them to the apps by itself (Settings → Apps)."; fi
  pause "Press Enter when one is added at least…"
  retry_check "Prowlarr has a search site" indexers_ok || true
  if ((MOVIES_TV)) || on albums; then retry_check "the apps have it too" indexers_synced || true; fi
  pause
}

stage_library() {
  ((MOVIES_TV)) || return 0
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
  ((MOVIES_TV)) || return 0
  stage "Qualities and sizes from TRaSH Guides (Recyclarr)" || return 0
  say "Recyclarr gives Radarr the profile ${BOLD}HD Bluray + WEB${RESET} and Sonarr ${BOLD}WEB-1080p${RESET} (1080p, sensible sizes), and"
  say "syncs them again every week."
  docker compose exec -T recyclarr recyclarr sync || FAILED+=("recyclarr sync")
  retry_check "the two profiles are there" profiles_ok || true
  pause
}

stage_try() {
  ((MOVIES_TV)) || return 0
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

stage_subtitles() {
  on subtitles || return 0
  stage "Subtitles: Bazarr for what Radarr and Sonarr look after" || return 0
  say "Bazarr fetches subtitles in ${BOLD}$(_existing BAZARR_LANGUAGES || echo en)${RESET} (BAZARR_LANGUAGES) for the films and series Radarr and"
  say "Sonarr monitor, from providers without an account, and saves them beside the video (.srt)."
  bash downloads/run.sh apply || FAILED+=("downloads/run.sh apply")
  retry_check "Bazarr: Sonarr, Radarr, the languages" bazarr_ok || true
  open_url "https://subs.$DOMAIN:$(pub_port)/"
  note "More subtitles: a free OpenSubtitles.com account in Bazarr → Settings → Providers (optional)."
  pause
}

stage_albums() {
  on albums || return 0
  stage "Albums: Lidarr with your music, watched by nobody" || return 0
  say "Lidarr adds the artists already in Music/ by itself, monitored by nobody and the files left as they are: it"
  say "writes no tags and renames nothing (a changed file would no longer be the torrent's)."
  bash downloads/run.sh apply || FAILED+=("downloads/run.sh apply")
  retry_check "Lidarr: Music/, qBittorrent, no tags written, no artist watches new albums" lidarr_ok || true
  open_url "https://lidarr.$DOMAIN:$(pub_port)/add/search"
  step "An album: Lidarr → ${BOLD}Add New${RESET} → the artist, Monitor ${BOLD}None${RESET}, Add; then that album → ${BOLD}Search${RESET}."
  step "It arrives in Music/ (Navidrome's), $(_existing LIDARR_QUALITY_PROFILE || echo Standard) (LIDARR_QUALITY_PROFILE)."
  pause
}

stage_books() {
  on book-requests || return 0
  stage "Book requests: Bindery, its sign-in through Authentik" || return 0
  say "Bindery's first visitor would become its admin, so it starts only now, and its sign-in through $BRAND is"
  say "set at once (which also shuts its own setup page)."
  if ! { status_load; [[ $(st bindery.ready) == True ]]; }; then
    own_stage bindery bash downloads/run.sh bindery-init || return 0
  fi
  bash downloads/run.sh apply || FAILED+=("downloads/run.sh apply")
  retry_check "Bindery: Authentik's sign-in only, qBittorrent, Prowlarr" bindery_ok || true
  retry_check "bindery.$DOMAIN: Authentik first, Caddy reaches the app" reachable bindery || true
  open_url "https://bindery.$DOMAIN:$(pub_port)/"
  step "Sign in with ${BOLD}$BRAND${RESET}: you're Bindery's admin (authentik Admins). The family may ask for books there"
  step "  (kids can't); you approve them under ${BOLD}Requests${RESET}. An author someone asks for adds none of their books."
  retry_check "Bindery reached $BRAND for its sign-in" bindery_sso_ok || true
  pause
}

stage_requests() {
  on requests || return 0
  stage "Requests: Seerr, signed in through Jellyfin" || return 0
  say "Seerr's first sign-in becomes its admin, so it starts only now and that sign-in is yours at once: Jellyfin's"
  say "own admin account (the local one from Jellyfin's setup, not an Authentik one), its password used once, never"
  say "stored. The family then signs in with their Jellyfin account, which is $BRAND's (password;code)."
  if ! seerr_ready; then
    rm -f "$SEERR_OURS"   # set up again: nobody reaches it until it's ours
    ask JELLYFIN_ADMIN_USER "Jellyfin's own admin account:" || { FAILED+=("requests: Jellyfin's admin"); return 0; }
    write_env JELLYFIN_ADMIN_USER "$JELLYFIN_ADMIN_USER"
    warn "Careful: a few wrong passwords lock that account in Jellyfin."
    ask_secret JELLYFIN_ADMIN_PASSWORD "Its password (used once, not stored):" || { FAILED+=("requests: the password"); return 0; }
    own_stage seerr seerr_first "$JELLYFIN_ADMIN_PASSWORD" || { unset JELLYFIN_ADMIN_PASSWORD; return 0; }
    unset JELLYFIN_ADMIN_PASSWORD
  fi
  # Its admin and its Jellyfin ours: only then does Caddy let anyone reach it
  if ! bash downloads/run.sh seerr-init; then
    rm -f "$SEERR_OURS"; FAILED+=("Seerr isn't ours (see above)"); return 0
  fi
  mkdir -p "$(dirname "$SEERR_OURS")" && : > "$SEERR_OURS"
  if on ntfy; then
    ./ntfy/setup.sh token seerr "$(_existing SEERR_NTFY_TOPIC || echo requests)" SEERR_NTFY_TOKEN || FAILED+=("ntfy token for Seerr")
  fi
  bash downloads/run.sh apply || FAILED+=("downloads/run.sh apply")
  # Its trust in Caddy's forwarded client (the session cookie Secure) counts from its next start
  docker compose restart seerr >/dev/null || FAILED+=("restart seerr")
  for _ in $(seq 1 40); do healthy seerr && break; sleep 3; done
  say "Jellyfin's users into Seerr: $BRAND's admins its admins, the family's requests approved automatically, a kid's"
  say "waiting for you$(on ntfy && echo " (ntfy, topic $(_existing SEERR_NTFY_TOPIC || echo requests))")."
  # Each group read on its own: a lookup that fails must stop this, or kids (in family too) would count as adults
  local admins family kids
  if admins=$(members "authentik Admins") && family=$(members family) && kids=$(members kids); then
    SEERR_ADMINS=$admins SEERR_FAMILY=$family SEERR_KIDS=$kids bash downloads/run.sh seerr-users || FAILED+=("Seerr: the users")
  else
    FAILED+=("Seerr's users: Authentik's groups couldn't be read (./authentik/setup-ssh.sh members family)")
  fi
  retry_check "Seerr: set up, Jellyfin sign-in only, Radarr and Sonarr" seerr_ok || true
  retry_check "requests.$DOMAIN: Seerr, its own accounts shut" requests_ok || true
  open_url "https://requests.$DOMAIN:$(pub_port)/"
  step "Sign in with your Jellyfin account (your $BRAND one, password;code). Someone who signs in for the first time"
  step "  asks with approval until this step runs again; Users → their permissions otherwise."
  on ntfy && note "Your phone: ./ntfy/setup.sh user $(whoami) $(_existing SEERR_NTFY_TOPIC || echo requests) (reads that topic)."
  pause
}

stage_requests_proxy() {
  on requests || return 0
  stage "NetBird reverse proxy: requests, public" || return 0
  if pub_ok requests >/dev/null 2>&1; then say "It's public."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy"
  step "Reverse Proxy → Services → Add, exactly like jellyfin's, for each of:"
  step "    requests.$DOMAIN   requests.$ALIAS"
  step "  Mode ${BOLD}TLS${RESET} (passthrough, never HTTP) · listen ${BOLD}$(pub_port)${RESET} · target peer $PEER, port ${BOLD}8444${RESET} ·"
  step "  Preserve Client Source IP ${BOLD}on${RESET} (PROXY v2)."
  note "No DNS change: the wildcard CNAME covers the new name."
  pause "Press Enter when done…"
  retry_check "requests.$DOMAIN public" pub_ok requests || true
  pause
}

stage_backup() {
  stage "Backup: the apps' settings in the hourly backup" || return 0
  say "A backup now (sudo), then a look at its Downloads section."
  sudo systemctl start homelab-backup.service || FAILED+=("homelab-backup")
  retry_check "the last backup has the downloads apps" backup_has_downloads || true
  pause
}

run_wizard() {

n_on() { local n=0 c; for c in "$@"; do on "$c" && n=$((n + 1)); done; echo "$n"; }
TOTAL_STAGES=$(( 8 + 3 * MOVIES_TV + $(n_on subtitles albums book-requests) + 2 * $(n_on requests) ))

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Downloads on request: the VPN, qBittorrent, Prowlarr and what you chose to ask for"
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
stage_subtitles
stage_albums
stage_books
stage_requests
stage_requests_proxy
stage_backup

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
