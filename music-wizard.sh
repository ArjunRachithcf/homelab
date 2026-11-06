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


# Homelab Music (Navidrome, music.<domain>): the human-only parts of RUNBOOK.md step 7d.
#   ./music-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./music-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
MUSIC="$(_existing MEDIA_DIR)/Music"
ADMIN=$(_existing ADMIN_USER || echo "$USER")   # Authentik username; Navidrome makes its first user admin
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
PY_IMAGE=docker.io/library/python:3.13.9-alpine3.22
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

nd_up()        { [[ -n "$(docker compose ps --status running -q navidrome)" ]]; }
no_host_port() { ! ss -tlnH | awk '{print $4}' | grep -qE ':4533$'; }
# Web UI: Authentik first (302 to auth.*). Apps over NetBird: Navidrome itself answers.
gated_9443()   { curl -s -m 10 -o /dev/null -w '%{redirect_url}' --resolve "music.$DOMAIN:$(pub_port):$NB_IP" \
                   "https://music.$DOMAIN:$(pub_port)/app/" | grep -q "^https://auth\.$DOMAIN"; }
apps_9443()    { curl -s -m 10 --resolve "music.$DOMAIN:$(pub_port):$NB_IP" \
                   "https://music.$DOMAIN:$(pub_port)/rest/ping?f=json&v=1.16.1&c=check" | grep -q subsonic-response; }
# Public listener: no app logins at all
# Since 7e the apps are public: a wrong password on the public listener reaches Navidrome and fails there
apps_8444_wrong_pw() { curl -s -m 10 --haproxy-protocol --resolve "music.$DOMAIN:8444:$NB_IP" \
                   "https://music.$DOMAIN:8444/rest/ping?c=check&f=json&v=1.16.1&u=nobody-check&p=wrong" \
                   | grep -q '"status":"failed"'; }
# SELECT on Navidrome's DB (SQLite/WAL, so read-write, as its own user; no network)
nd_sql() { docker run --rm --network none --user "$(id -u):$(id -g)" -v homelab_navidrome-data:/d "$PY_IMAGE" \
             python3 -c 'import sqlite3,sys; [print(*r) for r in sqlite3.connect("/d/navidrome.db").execute(sys.argv[1])]' "$1"; }
admin_ok()     { local a; a=$(nd_sql "select lower(user_name) from user where is_admin=1"); [[ "$a" == "$ADMIN" ]]; }
music_there()  { find "$MUSIC" -type f \( -iname '*.flac' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.ogg' -o -iname '*.opus' \) -print -quit | grep -q .; }
scanned()      { [[ "$(nd_sql "select count(*) from media_file")" -gt 0 ]]; }
# The cluster must hand TLS through (Caddy's Let's Encrypt cert), and Authentik must come first.
public_ok() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  echo | openssl s_client -connect "$ip:$(pub_port)" -servername "music.$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | grep -q "Let's Encrypt" || return 1
  curl -s -m 10 -o /dev/null -w '%{redirect_url}' --resolve "music.$DOMAIN:$(pub_port):$ip" \
    "https://music.$DOMAIN:$(pub_port)/app/" | grep -q "^https://auth\.$DOMAIN"
}

run_checks() {
  say "${BOLD}This box${RESET}"
  check "Navidrome running"                              nd_up || true
  check "Navidrome on no host port (4533)"               no_host_port || true
  check "music.$DOMAIN web UI asks Authentik first"      gated_9443 || true
  check "music.$DOMAIN answers Subsonic apps (NetBird)"  apps_9443 || true
  check "Subsonic apps public, wrong password refused"   apps_8444_wrong_pw || true
  check "Navidrome's only admin is $ADMIN"               admin_ok || true
  check "music files in $MUSIC"                          music_there || true
  check "Navidrome has scanned songs"                    scanned || true
  check "NetBird zones music.$DOMAIN / .$ALIAS → $NB_IP" zone_ok music || true
  check "MX $ALIAS still Proton (as a peer sees it)"     mx_via_peer || true
  say "${BOLD}Public internet${RESET}"
  check "music.* → $(cluster) (wildcard)"                public_dns_ok music || true
  check "cluster passes music TLS through, Authentik first" public_ok || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_zones() {
  stage "NetBird: private DNS zones for music" || return 0
  if zone_ok music; then say "music.$DOMAIN and music.$ALIAS already resolve to $NB_IP here."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones → Add Zone, twice (like home/auth/paperless/jellyfin/photos):"
  local d
  for d in "$DOMAIN" "$ALIAS"; do
    step "  Zone ${BOLD}music.$d${RESET} · Distribution groups: Streams, Admin, Plex-Server · Search domain: off"
  done
  step "In each zone: Add Record → type A · name = the zone name itself (@) · value ${BOLD}$NB_IP${RESET}."
  pause "Press Enter when both zones have their record…"
  for _ in $(seq 1 12); do zone_ok music && break; sleep 5; done
  retry_check "this peer resolves music.$DOMAIN / music.$ALIAS → $NB_IP" zone_ok music || true
  retry_check "MX $ALIAS still Proton on a peer" mx_via_peer || true
  pause
}

# Before the public services: whoever opens Navidrome first becomes its admin.
stage_first_login() {
  stage "First sign-in: you, before anyone else" || return 0
  if admin_ok; then say "You're already Navidrome's admin."; skip_offer && return; fi
  if [[ -n "$(nd_sql "select 1 from user limit 1")" ]]; then
    warn "Navidrome already has a user, and it isn't you as admin. Tell Claude before going on."
    FAILED+=("Navidrome admin"); return
  fi
  warn "Navidrome makes its very first user the admin. Do this before stage 3 (going public)."
  step "On a NetBird device (laptop), signed in to home already:"
  open_url "https://music.$DOMAIN:$(pub_port)/"
  step "You go through the $BRAND sign-in (or straight in) and land in Navidrome as ${BOLD}$ADMIN${RESET}."
  note "Library is empty until stage 5; that's fine."
  pause "Press Enter once Navidrome shows you signed in…"
  retry_check "Navidrome's only admin is $ADMIN" admin_ok || true
  pause
}

stage_services() {
  stage "NetBird: 2 reverse proxy services, TLS passthrough" || return 0
  if public_ok; then say "music.$DOMAIN already answers through the cluster."; skip_offer && return; fi
  admin_ok || { warn "Stage 2 isn't done: going public now could make someone else Navidrome's admin."
                confirm "Go public anyway" || { FAILED+=("public services (waiting for admin)"); return; }; }
  open_url "https://app.netbird.io/reverse-proxy/services"
  step "Reverse Proxy → Services → Add Service: ${BOLD}music.$DOMAIN${RESET}, then ${BOLD}music.$ALIAS${RESET}."
  step "Each exactly like the other 10:"
  step "  Mode: ${BOLD}TLS${RESET} (passthrough). ${RED}Never HTTP${RESET}: that would decrypt at NetBird."
  step "  Listen port: ${BOLD}$(pub_port)${RESET} · Advanced Settings: ${BOLD}Preserve Client Source IP${RESET} on."
  step "  Target: Peer ${BOLD}$PEER${RESET} · port ${BOLD}8444${RESET} · ${BOLD}PROXY Protocol${RESET}: on."
  note "No Infomaniak change: the * CNAME already covers music."
  pause "Press Enter when both are saved…"
  retry_check "music.* → $(cluster) in public DNS" public_dns_ok music || true
  for _ in $(seq 1 12); do public_ok && break; sleep 5; done
  retry_check "cluster passes music TLS through, Authentik first" public_ok || true
  pause
}

stage_apps() {
  stage "Music apps on your phone (over NetBird)" || return 0
  say "Apps (Symfonium, Substreamer, Feishin, play:Sub…) can't do the $BRAND sign-in, so they use"
  say "a separate Navidrome password (long and random: since 7e the apps work without NetBird too)."
  say "Navidrome made a random password at your first sign-in, and its own page asks for the current"
  say "one, so it's set here on the command line (a long one, in Proton Pass; typed, not shown):"
  if _proto; then
    in_terminal "Set your Navidrome app password (typed, not shown)" \
      docker compose exec navidrome /app/navidrome user edit -u "$ADMIN" --set-password
  else
    docker compose exec navidrome /app/navidrome user edit -u "$ADMIN" --set-password 2>&1 | grep -v 'level=' \
      || FAILED+=("navidrome user edit --set-password")
  fi
  note "Family: you set theirs as admin (Navidrome → Users → them: no current password needed), or the"
  note "same command with -u <their username>."
  step "In the app: server ${BOLD}https://music.$DOMAIN:$(pub_port)${RESET} · user ${BOLD}$ADMIN${RESET} · that password."
  confirm "App connected (NetBird on)?" || FAILED+=("Subsonic app login")
}

stage_music() {
  stage "Your music → $MUSIC" || return 0
  if music_there && scanned; then say "Music is there and scanned."; skip_offer && return; fi
  say "Navidrome reads $MUSIC read-only and picks up new files by itself (watcher + daily scan)."
  step "Copy it from the laptop, e.g.: ${BOLD}rsync -a --info=progress2 ~/Music/ homelab:$MUSIC/${RESET}"
  note "Artist/Album/NN - Title.flac works best; tags matter more than folder names."
  pause "Press Enter when the copy is done…"
  for _ in $(seq 1 24); do scanned && break; sleep 5; done
  retry_check "music files in $MUSIC" music_there || true
  retry_check "Navidrome has scanned songs" scanned || true
}

run_wizard() {

TOTAL_STAGES=5

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab: Music (Navidrome) at music.$DOMAIN"
stage_zones
stage_first_login
stage_services
stage_apps
stage_music

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
