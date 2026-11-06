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


# Homelab step 7f (2026-10-27): the family apps, one component each (the ones chosen in COMPONENTS):
# chores (Donetick, chores.*), things (Homebox, stuff.*), family-tree (Gramps Web, tree.*), notes (Memos,
# notes.*, our patched build), wiki (Outline, wiki.*), news (FreshRSS, news.*), games (RomM, games.*),
# places (Dawarich, places.*). RUNBOOK.md step 7f.
#   ./family-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./family-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }
source lib/components.sh   # component_on (COMPONENTS in .env; none set = all)

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
# FreshRSS's admin (compose: FRESHRSS_ADMIN_USER, else ADMIN_LOGIN, else ADMIN_USER)
FR_ADMIN="admin"
for k in ADMIN_USER ADMIN_LOGIN FRESHRSS_ADMIN_USER; do v=$(_existing "$k" || true); [[ -n $v ]] && FR_ADMIN=$v; done
GAMES="$(_existing MEDIA_DIR || echo /srv/media)/Games"
# component → its name (NetBird zone, public service, Caddy site), Authentik slug, health path through Caddy
declare -A NAME=([chores]=chores [things]=stuff [family-tree]=tree [notes]=notes [wiki]=wiki [news]=news
                 [games]=games [places]=places)
declare -A SLUG=([chores]=donetick [things]=homebox [family-tree]=gramps [notes]=memos [wiki]=outline
                 [news]=freshrss [games]=romm [places]=dawarich)
declare -A HEALTH=([chores]=/api/v1/health [things]=/api/v1/status [family-tree]=/ready [notes]=/healthz
                   [wiki]=/_health [news]=/api/ [games]=/api/heartbeat [places]=/api/v1/health)
# its app containers, their databases and caches
declare -A APP=([chores]=donetick [things]=homebox [family-tree]="gramps gramps-celery" [notes]=memos
                [wiki]=outline [news]=freshrss [games]=romm [places]=dawarich)
declare -A HELPERS=([family-tree]=gramps-redis [wiki]="outline-db outline-redis" [games]=romm-db
                    [places]="dawarich-db dawarich-redis")
# Run as HOST_UID: their new named volumes come up root's and must be handed over once
declare -A UID_VOLS=([chores]=donetick-data [things]=homebox-data
                     [games]="romm-resources romm-assets romm-config romm-redis"
                     [places]="dawarich-public dawarich-storage dawarich-watched")
declare -A SECRETS_OF=(
  [chores]="DONETICK_OIDC_CLIENT_SECRET DONETICK_JWT_SECRET"
  [things]="HOMEBOX_OIDC_CLIENT_SECRET HOMEBOX_API_KEY_PEPPER"
  [family-tree]="GRAMPS_OIDC_CLIENT_SECRET GRAMPS_SECRET_KEY GRAMPS_TREE"
  [notes]="MEMOS_OIDC_CLIENT_SECRET"
  [wiki]="OUTLINE_OIDC_CLIENT_SECRET OUTLINE_SECRET_KEY OUTLINE_UTILS_SECRET OUTLINE_DB_PASSWORD"
  [news]="FRESHRSS_OIDC_CLIENT_SECRET FRESHRSS_OIDC_CRYPTO_KEY"
  [games]="ROMM_OIDC_CLIENT_SECRET ROMM_AUTH_SECRET_KEY ROMM_DB_PASSWORD ROMM_DB_ROOT_PASSWORD"
  [places]="DAWARICH_OIDC_CLIENT_SECRET DAWARICH_SECRET_KEY_BASE DAWARICH_DB_PASSWORD")
# The chosen ones, in this order
ON=() NAMES=() SLUGS=() APPS=() NEW_SERVICES=() UID_VOLUMES=() SECRETS=()
# shellcheck disable=SC2206  # the tables hold space-separated word lists
for c in chores things family-tree notes wiki news games places; do
  component_on "$c" || continue
  ON+=("$c"); NAMES+=("${NAME[$c]}"); SLUGS+=("${SLUG[$c]}")
  APPS+=(${APP[$c]}); NEW_SERVICES+=(${HELPERS[$c]:-} ${APP[$c]}); UID_VOLUMES+=(${UID_VOLS[$c]:-}); SECRETS+=(${SECRETS_OF[$c]})
done
on() { [[ " ${ON[*]} " == *" $1 "* ]]; }
APP_LIST=$(printf '%s, ' "${NAMES[@]}"); APP_LIST=${APP_LIST%, }
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

secrets_ok() { local k; for k in "${SECRETS[@]}"; do [[ -n "$(_existing "$k" || true)" ]] || return 1; done; }
memos_image() { docker image inspect "homelab-memos:$(_existing MEMOS_TAG || echo 0.31.0)-sso"; }
all_running() { local s; for s in "${NEW_SERVICES[@]}"; do running "$s" || return 1; done; }
all_oidc()    { local s; for s in "${SLUGS[@]}"; do oidc_app "$s" || return 1; done; }
# Each app's own health path through Caddy (the same as the home page's status lines)
all_answer() { local c; for c in "${ON[@]}"; do answers "${NAME[$c]}" "${HEALTH[$c]}" || return 1; done; }
# No new host listener: none of the new containers publishes a port (they stay on the compose network)
no_new_ports() { local s; for s in "${NEW_SERVICES[@]}"; do
                   [[ "$(docker inspect -f '{{len .NetworkSettings.Ports}}{{range $p, $b := .NetworkSettings.Ports}}{{if $b}}X{{end}}{{end}}' \
                        "$(docker compose ps -q "$s")" 2>/dev/null)" != *X* ]] || return 1; done; }
container_t()  { local s pid; for s in "${APPS[@]}"; do
                   pid=$(docker inspect -f '{{.State.Pid}}' "$(docker compose ps -q "$s")" 2>/dev/null) || return 1
                   [[ "$(ps -o label= -p "$pid")" == *:container_t:* ]] || return 1; done; }
# The doors Caddy or the patch keep shut
gramps_owner_shut() { [[ "$(code tree /api/token/create_owner/)" == 403 ]]; }
romm_local_shut()   { [[ "$(code games /api/users -X POST -H 'Content-Type: application/json' -d '{}')" == 403 \
                         && "$(code games /api/login -X POST)" == 403 ]]; }
# validateOnly: Memos runs every refusal first and then returns without writing, so this check never
# creates anyone (an admin, while Memos has no users) even where the patch isn't in effect
memos_pw_shut() {
  body notes /memos.api.v1.UserService/CreateUser -X POST -H 'Content-Type: application/json' \
    -d '{"user":{"username":"wizard-check","password":"wizard-check-password-1","email":"x@example.invalid"},"validateOnly":true}' \
    | grep -q 'password signup is not allowed'
}
# Remote-User from outside must not sign anyone in: still sent to Authentik
freshrss_header_shut() { [[ "$(code news /i/ -H "Remote-User: $FR_ADMIN" -H "X-WebAuth-User: $FR_ADMIN")" == 302 ]]; }
donetick_pw_shut() { [[ "$(code chores /api/v1/auth/login -X POST -H 'Content-Type: application/json' -d '{"username":"x","password":"y"}')" == 403 ]]; }
dawarich_no_pw()   { ! body places /users/sign_in | grep -q 'type="password"'; }
rails() { docker compose exec -T dawarich bin/rails runner "$1" 2>/dev/null | tail -n1; }
dawarich_demo_gone() { [[ "$(rails 'puts User.where(email: "demo@dawarich.app").count')" == 0 ]]; }
dawarich_admin()     { [[ "$(rails 'puts User.where(admin: true).count')" -ge 1 ]]; }
outline_admin()  { docker compose exec -T outline-db psql -U outline -d outline -Atc \
                     "SELECT count(*) FROM users WHERE role = 'admin' AND \"deletedAt\" IS NULL" 2>/dev/null | grep -qx '[1-9][0-9]*'; }
outline_no_passkeys() { docker compose exec -T outline-db psql -U outline -d outline -Atc \
                          "SELECT bool_or(\"passkeysEnabled\") FROM teams" 2>/dev/null | grep -qx f; }
backup_has_7f() { backup_section_ok 'Apps (RUNBOOK.md 7f)' 'not created yet|not running|skipped'; }

run_checks() {
  say "${BOLD}This box${RESET} ($APP_LIST)"
  check "their secrets in .env"                                secrets_ok || true
  on notes && { check "our Memos build (memos/build.sh)"       memos_image || true; }
  on games && { check "RomM's folder $GAMES"                   test -d "$GAMES/roms" || true; }
  check "the apps and their databases running"                 all_running || true
  check "no new host listener"                                 no_new_ports || true
  check "the apps run as container_t"                          container_t || true
  say "${BOLD}Authentik + Caddy (NetBird path)${RESET}"
  check "an Authentik OIDC app for each"                       all_oidc || true
  check "each answers on the NetBird listener"                 all_answer || true
  on family-tree && { check "Gramps: owner token without login refused (403)" gramps_owner_shut || true; }
  on games  && { check "RomM: local accounts and password login refused" romm_local_shut || true; }
  on notes  && { check "Memos: password sign-up refused (patched build)"  memos_pw_shut || true; }
  on chores && { check "Donetick: password login refused"                 donetick_pw_shut || true; }
  on places && { check "Dawarich: no password field"                      dawarich_no_pw || true; }
  on news   && { check "FreshRSS: a forged Remote-User signs no one in"   freshrss_header_shut || true; }
  on places && { check "Dawarich: you're admin, the demo admin is gone"   eval 'dawarich_admin && dawarich_demo_gone' || true; }
  on wiki   && { check "Outline: an admin exists, passkeys off"           eval 'outline_admin && outline_no_passkeys' || true; }
  say "${BOLD}Public internet${RESET}"
  local n; for n in "${NAMES[@]}"; do check "$n.$DOMAIN public (TLS passthrough, Let's Encrypt)" pub_ok "$n" || true; done
  say "${BOLD}Backup${RESET}"
  check "last backup has the family apps"                      backup_has_7f || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_secrets() {
  stage "Secrets$(on games && echo " (and, optional, IGDB for game covers)")" || return 0
  if secrets_ok; then say "All in .env."; skip_offer && return; fi
  say "Secrets go straight into .env (never shown):"
  local s A="openssl rand -base64 48 | tr -d '\n/+='"
  for s in "${SLUGS[@]}"; do gen "${s^^}_OIDC_CLIENT_SECRET" "$A"; done
  on chores && gen DONETICK_JWT_SECRET      "openssl rand -base64 48 | tr -d '\n'"
  on things && gen HOMEBOX_API_KEY_PEPPER   "openssl rand -base64 48 | tr -d '\n'"
  on family-tree && gen GRAMPS_SECRET_KEY   "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
  if on wiki; then
    gen OUTLINE_SECRET_KEY       "openssl rand -hex 32"
    gen OUTLINE_UTILS_SECRET     "openssl rand -hex 32"
    gen OUTLINE_DB_PASSWORD      "openssl rand -base64 36 | tr -dc A-Za-z0-9"
  fi
  on news && gen FRESHRSS_OIDC_CRYPTO_KEY "openssl rand -hex 32"
  if on games; then
    gen ROMM_AUTH_SECRET_KEY     "openssl rand -hex 32"
    gen ROMM_DB_PASSWORD         "openssl rand -base64 36 | tr -dc A-Za-z0-9"
    gen ROMM_DB_ROOT_PASSWORD    "openssl rand -base64 36 | tr -dc A-Za-z0-9"
  fi
  if on places; then
    gen DAWARICH_SECRET_KEY_BASE "openssl rand -hex 64"
    gen DAWARICH_DB_PASSWORD     "openssl rand -base64 36 | tr -dc A-Za-z0-9"
  fi
  # Gramps opens its tree by name and makes a new, empty one when the name changes: pinned here
  if on family-tree && [[ -z "$(_existing GRAMPS_TREE || true)" ]]; then write_env GRAMPS_TREE "$BRAND"; fi
  on games || { retry_check "secrets in .env" secrets_ok || true; pause; return 0; }
  say "RomM finds covers and titles by itself (Hasheous). IGDB adds more; it needs a free Twitch"
  say "developer app. Skip with Enter, add later (then: docker compose up -d romm)."
  if confirm "Set up IGDB now?"; then
    open_url "https://dev.twitch.tv/console/apps/create"
    step "Name ${BOLD}$BRAND RomM${RESET} · OAuth Redirect URL ${BOLD}http://localhost${RESET} · Category ${BOLD}Application Integration${RESET}"
    step "  · Client Type ${BOLD}Confidential${RESET} → Create → Manage → copy Client ID, New Secret."
    ask ROMM_IGDB_CLIENT_ID "Client ID:" && [[ -n "$ROMM_IGDB_CLIENT_ID" ]] && write_env ROMM_IGDB_CLIENT_ID "$ROMM_IGDB_CLIENT_ID"
    ask_secret ROMM_IGDB_CLIENT_SECRET "Client secret:" && [[ -n "$ROMM_IGDB_CLIENT_SECRET" ]] && write_env ROMM_IGDB_CLIENT_SECRET "$ROMM_IGDB_CLIENT_SECRET"
  fi
  retry_check "secrets in .env" secrets_ok || true
  pause
}

stage_memos() {
  on notes || return 0
  stage "Our Memos build (about 8 minutes)" || return 0
  if memos_image >/dev/null 2>&1; then say "Built."; skip_offer && return; fi
  say "Memos lets its admins sign in with a password and never makes an Authentik user admin."
  say "memos/sso-admin.patch fixes both; memos/build.sh builds upstream's tagged source with it,"
  say "runs upstream's tests and ours, then the image (memos/README.md)."
  ./memos/build.sh || FAILED+=("memos/build.sh")
  retry_check "homelab-memos image" memos_image || true
  pause
}

stage_authentik() {
  stage "Authentik: the apps, Caddy's new sites" || return 0
  if all_oidc; then say "Apps exist."; skip_offer && return; fi
  say "Applies authentik/<component>.yaml for each: one OIDC client per app, family only; Chores and Games also kids."
  ./authentik/setup-ssh.sh family-apps || FAILED+=("authentik/setup-ssh.sh family-apps")
  docker compose up -d --force-recreate --no-deps caddy || FAILED+=("recreate caddy")
  # Caddy takes a few seconds to listen again after the recreate
  local i; for i in {1..30}; do all_oidc && break; sleep 2; done
  retry_check "an Authentik OIDC app for each" all_oidc || true
  pause
}

stage_start() {
  stage "Start $APP_LIST" || return 0
  if all_running && all_answer; then say "All running."; skip_offer && return; fi
  on games && mkdir -p "$GAMES/roms" "$GAMES/bios"
  local s pulls=(); for s in "${NEW_SERVICES[@]}"; do [[ $s == memos ]] || pulls+=("$s"); done   # memos: built here
  docker compose pull "${pulls[@]}" >/dev/null || FAILED+=("docker compose pull")
  # Containers first: that fills the new volumes from the image (as root), then they're handed over
  docker compose create "${NEW_SERVICES[@]}" >/dev/null || FAILED+=("docker compose create")
  local v args=() i=0
  for v in "${UID_VOLUMES[@]}"; do args+=(-v "homelab_$v:/v/$i"); i=$((i + 1)); done
  if (( ${#args[@]} )); then
    docker run --rm --network none --user 0 --entrypoint chown "${args[@]}" docker.io/library/python:3.13.9-alpine3.22 \
      -R "$(id -u):$(id -g)" /v || FAILED+=("volume owners")
  fi
  docker compose up -d "${NEW_SERVICES[@]}" >/dev/null || FAILED+=("docker compose up")
  say "Waiting for them (first starts migrate their databases$(on games && echo "; RomM takes ~2 min"))…"
  local _; for _ in $(seq 1 60); do all_answer && break; sleep 5; done
  retry_check "the apps and their databases running"  all_running || true
  retry_check "each answers on the NetBird listener"  all_answer || true
  pause
}

stage_zones() {
  stage "NetBird DNS zones: ${#NAMES[@]} name(s)" || return 0
  local all=1 n; for n in "${NAMES[@]}"; do zone_ok "$n" || all=0; done
  if (( all )); then say "All resolve to $NB_IP."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: like books, one zone per name, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  groups Streams, Admin, Plex-Server. $(( 2 * ${#NAMES[@]} )) zones:"
  for n in "${NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  note "Nothing public yet: your account comes first in each app (next stage)."
  pause "Press Enter when done…"
  for n in "${NAMES[@]}"; do retry_check "$n.$DOMAIN → $NB_IP" zone_ok "$n" || true; done
}

stage_first() {
  stage "First sign-ins: you, before anyone else" || return 0
  say "Over NetBird, each app once, in a normal browser window (you're already signed in to $BRAND)."
  say "Admin comes from authentik Admins in Chores, Family tree, Notes and Games; the others below."
  if on wiki; then
    step "${BOLD}Wiki${RESET} https://wiki.$DOMAIN:$(pub_port)/ → you're its admin (first sign-in). Then Settings:"
    step "  Details → name ${BOLD}$BRAND${RESET} · Security → ${BOLD}Passkeys${RESET} off (they'd skip Authentik) ·"
    step "  Allowed domains ${BOLD}$DOMAIN${RESET}, ${BOLD}$ALIAS${RESET} · Members can create API keys: off."
    open_url "https://wiki.$DOMAIN:$(pub_port)/"
    pause "Press Enter when done…"
    retry_check "Outline: an admin exists, passkeys off" eval 'outline_admin && outline_no_passkeys' || true
  fi
  if on places; then
    step "${BOLD}Places${RESET} https://places.$DOMAIN:$(pub_port)/ → Sign in with $BRAND."
    open_url "https://places.$DOMAIN:$(pub_port)/"
    pause "Press Enter once you see the map…"
    say "Making you Dawarich's admin and deleting the demo admin it seeds (demo@dawarich.app):"
    local me; me=$(rails 'u = User.where.not(email: "demo@dawarich.app").order(:created_at).first; puts u&.email')
    if [[ -n "$me" ]] && confirm "Make ${BOLD}$me${RESET} admin and delete the demo user?"; then
      rails "User.find_by!(email: '$me').update!(admin: true); User.where(email: 'demo@dawarich.app').destroy_all; puts :ok" >/dev/null \
        || FAILED+=("Dawarich admin")
    fi
    retry_check "Dawarich: you're admin, the demo admin is gone" eval 'dawarich_admin && dawarich_demo_gone' || true
  fi
  local n rest=(); for n in "${NAMES[@]}"; do [[ $n == wiki || $n == places ]] || rest+=("$n"); done
  (( ${#rest[@]} )) || { pause; return 0; }
  for n in "${rest[@]}"; do step "  https://$n.$DOMAIN:$(pub_port)/"; done
  step "Open each once: it signs you straight in."
  on news && step "${BOLD}News${RESET}: you're its admin (FRESHRSS_ADMIN_USER)."
  if on things; then
    step "${BOLD}Things${RESET}: you get a collection \"Home\"; family members get their own until you invite them"
    step "  (Collection → Invite → send them the link)."
  fi
  confirm "Each signed you in?" || FAILED+=("first sign-ins")
  pause
}

stage_proxy() {
  stage "NetBird reverse proxy: $(( 2 * ${#NAMES[@]} )) public services" || return 0
  local all=1 n; for n in "${NAMES[@]}"; do pub_ok "$n" >/dev/null 2>&1 || all=0; done
  if (( all )); then say "All are public."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy"
  step "Reverse Proxy → Services → Add, exactly like books, for each of:"
  for n in "${NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  step "  Mode ${BOLD}TLS${RESET} (passthrough, never HTTP) · listen ${BOLD}$(pub_port)${RESET} · target peer $PEER, port ${BOLD}8444${RESET} ·"
  step "  Preserve Client Source IP ${BOLD}on${RESET} (PROXY v2)."
  note "No Infomaniak change: the wildcard CNAME covers the new names."
  pause "Press Enter when done…"
  for n in "${NAMES[@]}"; do retry_check "$n.$DOMAIN public" pub_ok "$n" || true; done
  pause
}

stage_phones() {
  stage "Phones and the home page" || return 0
  open_url "https://home.$DOMAIN:$(pub_port)/"
  step "Home page: a new panel ${BOLD}Family life${RESET} with $APP_LIST; a kid's account shows Chores and Games."
  on chores && step "${BOLD}Chores${RESET}: the Donetick app (App Store / Play) → server https://chores.$DOMAIN:$(pub_port) → $BRAND."
  on places && step "${BOLD}Places${RESET}: Dawarich app, or OwnTracks / Overland → your API key from Places → Settings."
  if on news; then
    step "${BOLD}News${RESET}: any Google Reader app (Reeder, NetNewsWire, FeedMe) → server https://news.$DOMAIN:$(pub_port)/api/greader.php,"
    step "  your username and the API password you set in News → Settings → Profile."
  fi
  if on games; then
    step "${BOLD}Games${RESET}: games go into $GAMES/roms/<platform>/ (e.g. snes, gba), BIOS files into bios/;"
    step "  then Games → Scan. Kids play in the browser."
  fi
  confirm "Home page and an app on the phone work?" || FAILED+=("home page / phone apps")
  pause
}

stage_backup() {
  stage "A backup with the family apps" || return 0
  sudo systemctl start homelab-backup.service || FAILED+=("homelab-backup")
  retry_check "last backup has the family apps" backup_has_7f || true
  pause
}

run_wizard() {

TOTAL_STAGES=$(( 8 + $(on notes && echo 1 || echo 0) ))
(( ${#ON[@]} )) || { echo "None of the family apps is chosen (COMPONENTS in .env): nothing to do."; exit 0; }

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab 7f: the family apps ($APP_LIST)"
stage_secrets
stage_memos
stage_authentik
stage_start
stage_zones
stage_first
stage_proxy
stage_phones
stage_backup

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
