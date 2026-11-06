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


# Homelab step 7e (2026-10-26): Navidrome apps public, home page status + kids, Kavita (read.*),
# OpenCloud (files.*) with the disk restructure, Sharry (share.*). RUNBOOK.md step 7e.
#   ./files-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./files-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }
source lib/components.sh   # component_on (COMPONENTS in .env; none set = all)

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
NVME1=$(_existing NVME1_DIR || echo /srv/nvme1)
FILES_COPY=$(_existing FILES_COPY_DIR || echo /srv/files-copy)
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
FILES=$(_existing FILES_DIR || echo /srv/files)
PHOTOS=$(_existing IMMICH_UPLOAD_DIR || echo /srv/photos)
BOOKS="$(_existing MEDIA_DIR || echo /srv/media)/Books"
PUBLIC_NAMES=(read files share)               # NetBird reverse proxy services (× both domains)
NEW_SERVICES=(kavita opencloud sharry-db sharry)
FAILED=()
STATE=$HOME/.local/state/homelab; mkdir -p "$STATE"

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

secrets_ok() { local k; for k in KAVITA_OIDC_CLIENT_SECRET SHARRY_DB_PASSWORD SHARRY_SERVER_SECRET SHARRY_OIDC_CLIENT_SECRET; do
                 [[ -n "$(_existing "$k" || true)" ]] || return 1; done; }
hb_key_set() { [[ -n "$(_existing HEARTBEAT_API_KEY_RO || true)" ]]; }
# New layout: the disk at /srv/nvme1, photos/ and files/ bind-mounted
layout_ok()  { mountpoint -q "$NVME1" && mountpoint -q "$PHOTOS" && mountpoint -q "$FILES" \
                 && [[ "$(stat -c %d "$NVME1")" == "$(stat -c %d "$PHOTOS")" ]] && [[ -f "$PHOTOS/library/.immich" ]]; }
copies_ok()  { [[ -d "$FILES_COPY" && -d "$NVME1/files-snap" ]]; }
books_ok()   { local d; for d in Books Comics Manga Kids; do [[ -d "$BOOKS/$d" ]] || return 1; done; }
timer_on()   { systemctl is-active "homelab-$1.timer"; }
all_running() { local s; for s in "${NEW_SERVICES[@]}"; do running "$s" || return 1; done; }
# No new listener on the host: only Caddy's, sshd's and the others from CLAUDE.md "Ports"
no_new_ports() { ! ss -Htln | awk '{print $4}' | grep -qE ':(5000|9200|9090|5432)$'; }
container_t()  { local s pid; for s in kavita opencloud sharry; do
                   pid=$(docker inspect -f '{{.State.Pid}}' "$(docker compose ps -q "$s")" 2>/dev/null) || return 1
                   [[ "$(ps -o label= -p "$pid")" == *:container_t:* ]] || return 1; done; }
immich_assets() { docker compose exec -T immich-db psql -U postgres -d immich -Atc \
                    "SELECT count(*) FROM asset WHERE \"deletedAt\" IS NULL" 2>/dev/null | tr -dc 0-9; }
immich_ok()   { [[ "$(code photos /api/server/ping)" == 200 ]]; }
# OpenCloud's issuer is Authentik's root (global issuer mode); Caddy rewrites the discovery there
oc_issuer_ok() { curl -s -m 10 --resolve "auth.$DOMAIN:$(pub_port):$NB_IP" "https://auth.$DOMAIN:$(pub_port)/.well-known/openid-configuration" \
                   | jq -e --arg i "https://auth.$DOMAIN:$(pub_port)/" '.issuer == $i'; }
kavita_oidc() { docker compose exec -T kavita cat /kavita/config/appsettings.json 2>/dev/null \
                  | jq -e '.OpenIdConnectSettings.Authority != "" and .OpenIdConnectSettings.Secret != ""'; }
public_links_off() { [[ "$(code files /s/test-link)" == 403 && "$(code files /dav/public-files/x)" == 403 ]]; }
validity_locked()  { [[ "$(code share /api/v2/sec/share/abc/validity -X POST -H 'Content-Type: application/json' -d '{"value":1}')" == 403 ]]; }
music_public() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  curl -s -m 10 --resolve "music.$DOMAIN:$(pub_port):$ip" \
    "https://music.$DOMAIN:$(pub_port)/rest/ping?c=check&f=json&v=1.16.1&u=nobody-check&p=wrong" | grep -q '"status":"failed"'
}
hb_file_ok() { jq -e '.outside != null and (now - .time) < 900' site/status/heartbeat.json; }
# Detail sheets: the watcher, the hourly disk report, the ops container, its reports
term_dns()   { [[ "$(dig +short "term.$DOMAIN" | head -n1)" == "$NB_IP" ]]; }
term_ok()    { running termix && [[ "$(curl -sk -o /dev/null -w '%{http_code}' --resolve "term.$DOMAIN:$(pub_port):$NB_IP" "https://term.$DOMAIN:$(pub_port)/")" == 200 ]]; }
ntfy_dns()   { [[ "$(dig +short "ntfy.$DOMAIN" | head -n1)" == "$NB_IP" ]]; }
ntfy_ok()    { running ntfy && [[ "$(code ntfy /v1/health)" == 200 ]] && [[ "$(code ntfy /bills/json?poll=1)" == 403 ]]; }
ntfy_token() { [[ -n "$(_existing NTFY_N8N_TOKEN || true)" ]]; }
n8n_dns()    { [[ "$(dig +short "n8n.$DOMAIN" | head -n1)" == "$NB_IP" ]]; }
n8n_ok()     { running n8n && [[ "$(code n8n /)" == 302 ]]; }   # forward-auth: unauthenticated → Authentik
ops_ok()     { systemctl is-active homelab-ops.path && timer_on storage-report && running ops \
                 && [[ -s ops/data/outside.json && -s ops/data/storage.json ]]; }
backup_has_7e() { backup_section_ok 'Apps (RUNBOOK.md 7e)' 'not created yet|skipped'; }

ansible_tags() {
  command -v ansible-playbook >/dev/null \
    || install_pkgs ansible-core ansible-collection-ansible-posix ansible-collection-community-general
  ansible-playbook -K ansible/site.yml --tags "$1" "${@:2}"
}

# nd_password USER : a new 32-character Navidrome password for USER, shown once. Navidrome only
# takes it at a terminal prompt (twice), so `script` gives it one.
nd_password() {
  local u=$1 pw; pw=$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 32)
  printf '%s\n%s\n' "$pw" "$pw" | script -qec "docker compose exec navidrome /app/navidrome user edit -u '$u' --set-password" /dev/null >/dev/null 2>&1 || return 1
  # Prove it: a Subsonic ping with it (over NetBird, straight to Caddy)
  curl -s -m 10 --resolve "music.$DOMAIN:$(pub_port):$NB_IP" \
    "https://music.$DOMAIN:$(pub_port)/rest/ping?c=check&f=json&v=1.16.1&u=$u&p=$pw" | grep -q '"status":"ok"' || return 1
  printf '  %s%s%s  %s%s%s\n' "$BOLD" "$u" "$RESET" "$GREEN" "$pw" "$RESET"
}

run_checks() {
  say "${BOLD}This box${RESET}"
  check "secrets for Kavita and Sharry in .env"                secrets_ok || true
  check "healthchecks.io read-only key in .env"                hb_key_set || true
  check "disk at $NVME1, $PHOTOS and $FILES bind-mounted"     layout_ok || true
  check "snapshot folder and $FILES_COPY exist"               copies_ok || true
  check "Kavita's folders $BOOKS/{Books,Comics,Manga,Kids}"   books_ok || true
  check "homelab-files-local.timer active"                     timer_on files-local || true
  check "Kavita, OpenCloud, Sharry running"                    all_running || true
  check "no new host listener (5000, 9200, 9090, 5432)"        no_new_ports || true
  check "the new containers run as container_t"                container_t || true
  check "Kavita's OIDC settings in appsettings.json"           kavita_oidc || true
  say "${BOLD}Authentik + Caddy (NetBird path)${RESET}"
  check "Authentik OIDC app kavita"                            oidc_app kavita || true
  check "Authentik OIDC app sharry"                            oidc_app sharry || true
  check "OpenCloud's issuer at auth.$DOMAIN/ (global)"         oc_issuer_ok || true
  check "read.$DOMAIN answers"                                 answers read /api/health || true
  check "files.$DOMAIN answers"                                answers files /status.php || true
  check "share.$DOMAIN answers"                                answers share /api/v2/open/info/version || true
  check "Photos still answers"                                 immich_ok || true
  check "OpenCloud public links refused (403)"                 public_links_off || true
  check "Sharry: changing a share's expiry refused (403)"      validity_locked || true
  say "${BOLD}Public internet${RESET}"
  local n; for n in "${PUBLIC_NAMES[@]}"; do check "$n.$DOMAIN public (TLS passthrough, Let's Encrypt)" pub_ok "$n" || true; done
  check "music apps public, a wrong password refused"          music_public || true
  say "${BOLD}Home page + backup${RESET}"
  check "heartbeat.json fresh, with healthchecks.io's view"    hb_file_ok || true
  check "detail sheets: ops, its path watcher, disk report"    ops_ok || true
  # Their own components: no check for one that isn't chosen
  if component_on terminal; then check "Terminal (Termix) answers on the NetBird listener" term_ok || true; fi
  if component_on ntfy; then
    check "ntfy answers, an anonymous read refused (403)"       ntfy_ok || true
    if component_on n8n; then check "ntfy token for n8n in .env" ntfy_token || true; fi
  fi
  if component_on n8n; then
    check "n8n behind Authentik (302 without a sign-in)"       n8n_ok || true
  fi
  check "last backup has Kavita, OpenCloud, Sharry"            backup_has_7e || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_secrets() {
  stage "Secrets and the healthchecks.io read-only key" || return 0
  if secrets_ok && hb_key_set; then say "All in .env."; skip_offer && return; fi
  say "Secrets go straight into .env (never shown):"
  gen KAVITA_OIDC_CLIENT_SECRET "openssl rand -base64 48 | tr -d '\n/+='"
  gen SHARRY_OIDC_CLIENT_SECRET "openssl rand -base64 48 | tr -d '\n/+='"
  gen SHARRY_DB_PASSWORD        "openssl rand -base64 36 | tr -dc A-Za-z0-9"
  gen SHARRY_SERVER_SECRET      "echo \"b64:\$(openssl rand -base64 32 | tr -d '\n')\""
  say "The home page shows healthchecks.io's own view of the heartbeat. It needs a ${BOLD}read-only${RESET} key"
  say "(it can't ping, pause or change anything):"
  open_url "https://healthchecks.io/projects/"
  step "Your project → Settings → API Access → ${BOLD}Create API key (read-only)${RESET}."
  step "Name the check ${BOLD}homelab${RESET} if the project has more than one."
  if ask_secret HEARTBEAT_API_KEY_RO "Paste the read-only key:"; then
    [[ -n "$HEARTBEAT_API_KEY_RO" ]] && write_env HEARTBEAT_API_KEY_RO "$HEARTBEAT_API_KEY_RO"
  fi
  retry_check "secrets in .env" secrets_ok || true
  pause
}

stage_disk() {
  stage "Second NVMe: $NVME1 with photos/ and files/ (Immich down ~5 min)" || return 0
  if layout_ok && copies_ok && books_ok && timer_on files-local >/dev/null; then say "New layout in place."; skip_offer && return; fi
  say "The whole disk is Immich's library at $PHOTOS today; OpenCloud's files must not live inside it"
  say "(Immich's :Z label would reach them). Ansible moves Immich's folders into $NVME1/photos (a rename"
  say "on the same disk, instant), remounts the disk at $NVME1 and bind-mounts photos/ back at $PHOTOS"
  say "and files/ at $FILES. Also: Kavita's book folders, $FILES_COPY, the files-local timer."
  local before; before=$(immich_assets)
  say "Immich has ${BOLD}${before:-?}${RESET} assets now."
  say "First the dry run (fstab diff, no changes):"
  pause "Press Enter for the dry run…"
  ansible_tags storage,timers --check --diff || true
  confirm "Looks right? Stop Immich and do it" || { FAILED+=("disk restructure (not confirmed)"); return; }
  note "Outside 02:00–05:30; it asks for your sudo password."
  # Ansible refuses while a homelab job runs (the hourly backup, the change watcher's pushes): wait
  # for a quiet moment first, so Immich isn't stopped for nothing
  say "Waiting until no backup job runs (up to 30 min)…"
  bash ansible/live-safe.sh "$HOME" 1800 || { FAILED+=("box busy, disk not moved"); return; }
  docker compose stop immich-server immich-machine-learning || { FAILED+=("stop Immich"); return; }
  if ! ansible_tags storage,timers; then
    warn "Ansible failed: Immich starts again; look at the error above, then re-run this stage."
    FAILED+=("ansible-playbook --tags storage,timers")
  fi
  # :Z on the moved library is relabelled on the next create
  docker compose up -d --force-recreate immich-server immich-machine-learning || FAILED+=("start Immich")
  say "Waiting for Immich…"
  local _; for _ in $(seq 1 36); do immich_ok && break; sleep 5; done
  retry_check "disk at $NVME1, binds in place" layout_ok || true
  retry_check "Photos answers again"           immich_ok || true
  local after; after=$(immich_assets)
  if [[ -n "$before" && "$after" == "$before" ]]; then check "Immich still has $before assets" true
  else check "Immich asset count unchanged ($before → ${after:-?})" false || true; fi
  step "Open ${BOLD}https://photos.$DOMAIN:$(pub_port)${RESET} and scroll: thumbnails load, a photo opens."
  confirm "Photos look normal?" || FAILED+=("Immich after the move (look at it)")
  retry_check "Kavita's book folders" books_ok || true
  retry_check "files-local timer"      timer_on files-local || true
  pause
}

stage_authentik() {
  stage "Authentik: group kids, apps Reading, Files (4 clients), Share" || return 0
  if oidc_app kavita && oidc_app sharry && oc_issuer_ok; then say "Apps exist."; skip_offer && return; fi
  say "Applies family.yaml (group kids, mapping email (verified)), then reading.yaml, files.yaml, share.yaml."
  ./authentik/setup-ssh.sh family || FAILED+=("authentik/setup-ssh.sh family")
  ./authentik/setup-ssh.sh files || FAILED+=("authentik/setup-ssh.sh files")
  docker compose up -d --force-recreate --no-deps caddy || FAILED+=("recreate caddy")
  retry_check "OIDC app kavita"            oidc_app kavita || true
  retry_check "OIDC app sharry"            oidc_app sharry || true
  retry_check "OpenCloud's issuer at auth.$DOMAIN/" oc_issuer_ok || true
  pause
}

stage_start() {
  stage "Start Kavita, OpenCloud, Sharry" || return 0
  if all_running && kavita_oidc; then say "All running."; skip_offer && return; fi
  say "Pulls, starts, recreates Navidrome (login limits) and writes Kavita's OIDC client into its"
  say "appsettings.json (then a restart)."
  docker compose pull "${NEW_SERVICES[@]}" || FAILED+=("docker compose pull")
  # A new named volume belongs to root; Sharry runs as uid 10001
  local sharry_tag; sharry_tag=$(_existing SHARRY_TAG || true)
  docker volume create homelab_sharry-files >/dev/null
  docker run --rm --network none --user 0 --entrypoint chown -v homelab_sharry-files:/data \
    "docker.io/eikek0/sharry:${sharry_tag:-v1.17.0}" 10001:10001 /data || FAILED+=("sharry-files owner")
  docker compose up -d "${NEW_SERVICES[@]}" || FAILED+=("docker compose up")
  docker compose up -d navidrome || FAILED+=("recreate navidrome")
  # Kavita's first start creates and migrates its DB; a restart in the middle leaves EF's migration
  # lock behind and every later start hangs on it (seen 2026-10-26). So: healthy first, then stop,
  # write appsettings.json, start.
  local _; for _ in $(seq 1 60); do answers read /api/health && break; sleep 5; done
  docker compose stop kavita >/dev/null || true
  # Python in a throwaway container, on the volume (Kavita's image has none). The secret goes in via env.
  KAVITA_OIDC_CLIENT_SECRET=$(_existing KAVITA_OIDC_CLIENT_SECRET) docker run --rm --network none \
    -e KAVITA_OIDC_CLIENT_SECRET -e AUTH="https://auth.$DOMAIN:$(pub_port)/application/o/kavita/" \
    -v homelab_kavita-config:/c docker.io/library/python:3.13.9-alpine3.22 python3 -c '
import json, os
p = "/c/appsettings.json"; d = json.load(open(p))
d.setdefault("OpenIdConnectSettings", {}).update(
    Authority=os.environ["AUTH"], ClientId="kavita", Secret=os.environ["KAVITA_OIDC_CLIENT_SECRET"])
json.dump(d, open(p, "w"), indent=2)' || FAILED+=("Kavita appsettings.json")
  docker compose start kavita >/dev/null || true
  for _ in $(seq 1 24); do answers read /api/health && answers files /status.php && break; sleep 5; done
  retry_check "Kavita, OpenCloud, Sharry running"   all_running || true
  retry_check "Kavita's OIDC settings"              kavita_oidc || true
  retry_check "read.$DOMAIN answers"                answers read /api/health || true
  retry_check "files.$DOMAIN answers"               answers files /status.php || true
  retry_check "share.$DOMAIN answers"               answers share /api/v2/open/info/version || true
  check "no new host listener"                      no_new_ports || true
  check "containers are container_t"                container_t || true
  pause
}

stage_zones() {
  stage "NetBird DNS zones: read, files, share" || return 0
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: like books, one zone per name, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  groups Streams, Admin, Plex-Server. 6 zones:"
  local n; for n in "${PUBLIC_NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  note "Nothing public yet: Kavita's first account must be yours (next stage)."
  pause "Press Enter when done…"
}

stage_kavita() {
  stage "Kavita: your break-glass admin, libraries, OpenID Connect" || return 0
  open_url "https://read.$DOMAIN:$(pub_port)/"
  step "Kavita asks for the first account: that's the local ${BOLD}admin${RESET} (break-glass). Long password"
  step "  → Proton Pass entry \"Kavita break-glass\"."
  step "Settings → Libraries → Add, four times, type and folder:"
  step "  ${BOLD}Books${RESET} (Book) /books/Books · ${BOLD}Comics${RESET} (Comic) /books/Comics ·"
  step "  ${BOLD}Manga${RESET} (Manga) /books/Manga · ${BOLD}Kids${RESET} (Book) /books/Kids"
  step "Settings → OpenID Connect: Authority, Client ID and Secret are already filled in (from .env)."
  step "  Provider name ${BOLD}$BRAND account${RESET} · ${BOLD}Provision accounts${RESET} on · ${BOLD}Sync user settings${RESET} on"
  step "  Roles claim: leave the default (Kavita maps \"roles\" onto it) · Roles prefix empty"
  step "  ${BOLD}Auto login${RESET} on · ${BOLD}Disable password authentication${RESET} on → Save."
  note "The admin still signs in with its password at /login?skipAutoLogin=true."
  step "Private window → read.$DOMAIN → it goes straight to the $BRAND sign-in → you land in Kavita"
  step "  as yourself, admin, with all four libraries."
  confirm "Signed in with your $BRAND account?" || FAILED+=("Kavita OIDC sign-in")
  pause
}

stage_proxy() {
  stage "NetBird reverse proxy: 6 public services" || return 0
  local all=1 n; for n in "${PUBLIC_NAMES[@]}"; do pub_ok "$n" >/dev/null 2>&1 || all=0; done
  if (( all )); then say "All three names are public."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy"
  step "Reverse Proxy → Services → Add, exactly like books, for each of:"
  for n in "${PUBLIC_NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  step "  Mode ${BOLD}TLS${RESET} (passthrough, never HTTP) · listen ${BOLD}$(pub_port)${RESET} · target peer $PEER, port ${BOLD}8444${RESET} ·"
  step "  Preserve Client Source IP ${BOLD}on${RESET} (PROXY v2)."
  note "No Infomaniak change: the wildcard CNAME covers the new names."
  pause "Press Enter when done…"
  for n in "${PUBLIC_NAMES[@]}"; do retry_check "$n.$DOMAIN public" pub_ok "$n" || true; done
  pause
}

stage_opencloud() {
  stage "OpenCloud: first sign-in, then the desktop app at :$(pub_port)" || return 0
  open_url "https://files.$DOMAIN:$(pub_port)/"
  step "Sign in with your $BRAND account. OpenCloud creates your account as ${BOLD}Admin${RESET}"
  step "  (authentik Admins → opencloudAdmin)."
  step "Try to share a file by link: there's no working link (Caddy refuses them; RUNBOOK.md 7e)."
  confirm "Signed in, files page shows?" || FAILED+=("OpenCloud web sign-in")
  say "${BOLD}The desktop app${RESET} (opencloud.eu → Download, Mac or Linux): server"
  step "${BOLD}https://files.$DOMAIN:$(pub_port)${RESET} → browser opens the $BRAND sign-in → back in the app."
  step "Let it sync a test folder; put a file in, see it on the web page."
  warn "That a client accepts a port in the URL isn't documented. If it fails, stop here and tell Claude"
  warn "what it said: the fix may need a change on the box, not in the app."
  confirm "Desktop app signed in and syncing?" || FAILED+=("OpenCloud desktop app at :$(pub_port)")
  note "Phones: the OpenCloud app (App Store / Play), same server URL."
  pause
}

stage_sharry() {
  stage "Sharry: a share from outside, expiry locked" || return 0
  open_url "https://share.$DOMAIN:$(pub_port)/"
  step "Sign in ($BRAND account) → New share → a small file, ${BOLD}validity 7 days${RESET}, a password → Publish."
  step "Copy the share link. On your phone with ${BOLD}NetBird off${RESET} (mobile data): open it, enter the"
  step "  password, the file downloads."
  confirm "Downloaded from outside?" || FAILED+=("Sharry download from outside")
  check "changing a share's expiry is refused (403)" validity_locked || true
  note "Shares can only ever be ≤ 30 days; they're deleted a day after they expire."
  pause
}

stage_navidrome() {
  stage "Music apps without NetBird: long random passwords" || return 0
  say "Subsonic apps now work from the internet too. A Navidrome password has no MFA, so every user"
  say "gets a 32-character random one (failed logins are limited per IP + user). Shown once each:"
  say "store it in Proton Pass (shared with that person) and type it into their app."
  local users u; users=$(docker compose exec -T navidrome /app/navidrome user list -f json 2>/dev/null \
                           | jq -r '.[].username // empty' 2>/dev/null || true)
  [[ -n "$users" ]] || { warn "couldn't list Navidrome's users"; FAILED+=("navidrome user list"); pause; return; }
  if _proto; then
    # The passwords must not go through the app (or an assistant): they're shown in a terminal only
    in_terminal "Make the app passwords in a terminal: each is shown there once, never here" \
      env HOMELAB_STAGES=navidrome ./files-wizard.sh
  else
    for u in $users; do
      confirm "New app password for $u" || continue
      nd_password "$u" || { warn "setting it for $u failed"; FAILED+=("Navidrome password $u"); }
    done
  fi
  step "In the app: server ${BOLD}https://music.$DOMAIN:$(pub_port)${RESET}, the user and that password; NetBird off."
  confirm "App plays with NetBird off?" || FAILED+=("Subsonic app without NetBird")
  retry_check "music apps public, wrong password refused" music_public || true
  pause
}

stage_terminal() {
  stage "Terminal (Termix): SSH from the browser, NetBird only, admins only" || return 0
  say "term.$DOMAIN is a web SSH client (like Termius). Signing in to it is not signing in to the PC:"
  say "sshd still wants a key (the one Termix holds) plus your password;TOTP every session."
  running termix || { docker compose up -d termix >/dev/null || FAILED+=("start termix"); }
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: ${BOLD}term.$DOMAIN${RESET} and ${BOLD}term.$ALIAS${RESET}, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  distribution group ${BOLD}Admin only${RESET} (not Streams: like ssh, only Admin devices)."
  pause "Press Enter when done…"
  retry_check "term.$DOMAIN resolves to $NB_IP" term_dns || true
  open_url "https://term.$DOMAIN:$(pub_port)/"
  step "It goes straight to the $BRAND sign-in (passkey). Your first sign-in makes you Termix's admin."
  step "Termix → ${BOLD}Credentials${RESET} → New → SSH key → ${BOLD}Generate${RESET} (Ed25519), name it termix → Save."
  local keys=""
  ask_lines keys "Copy its ${BOLD}public key${RESET} (one line, ssh-ed25519 …) and paste it here, then an empty line:"
  if [[ -n "$keys" ]]; then
    printf '%s' "$keys" | ./authentik/setup-ssh.sh add-key || { warn "add-key failed (see above)"; FAILED+=("termix key → Authentik"); }
  fi
  step "Termix → ${BOLD}Hosts${RESET} → New: name ${BOLD}homelab${RESET}, address ${BOLD}$NB_IP${RESET}, port 22,"
  step "  user ${BOLD}$(whoami)${RESET}, authentication: the ${BOLD}termix${RESET} credential → Save → Connect."
  step "It asks for ${BOLD}password;TOTP${RESET} (as in a terminal), then you have a shell."
  note "Admin Settings → Plugins: switch off Remote desktop and Docker (not used here)."
  confirm "Shell opened in the browser?" || FAILED+=("Termix shell")
  pause
}

stage_ntfy() {
  stage "Notifications (ntfy): push to your phone, NetBird only" || return 0
  say "ntfy.$DOMAIN sends push messages to the ntfy app (from n8n's workflows, for a start). Nobody may"
  say "read or write anything there without a user: you get a read user, n8n a write-only token."
  running ntfy || { docker compose up -d ntfy >/dev/null || FAILED+=("start ntfy"); }
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: ${BOLD}ntfy.$DOMAIN${RESET} and ${BOLD}ntfy.$ALIAS${RESET}, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  distribution group ${BOLD}Admin only${RESET} (your phone is in it)."
  pause "Press Enter when done…"
  retry_check "ntfy.$DOMAIN resolves to $NB_IP" ntfy_dns || true
  ./ntfy/setup.sh n8n-token || FAILED+=("ntfy token for n8n")
  # The password must not go through the app (or an assistant): ntfy asks for it in a terminal
  in_terminal "Make your ntfy user in a terminal: ntfy asks for its password there" \
    ./ntfy/setup.sh user "$(whoami)" || FAILED+=("ntfy user $(whoami)")
  step "Phone: install ${BOLD}ntfy${RESET} (App Store / Play Store). Settings → Users → add: server"
  step "  ${BOLD}https://ntfy.$DOMAIN:$(pub_port)${RESET}, user ${BOLD}$(whoami)${RESET} and that password."
  step "Then ${BOLD}+${RESET} → topic ${BOLD}bills${RESET}, ${BOLD}Use another server${RESET}: the same address. NetBird on, as for the other apps."
  note "On iOS the push itself comes through ntfy.sh (only a message ID); the phone then fetches it from here."
  retry_check "ntfy answers, an anonymous read refused" ntfy_ok || true
  pause
}

stage_automations() {
  stage "Automations (n8n)" || return 0
  say "n8n.$DOMAIN runs your workflows; they can send to ntfy (the token from the last stage)."
  say "Admins only: Authentik first, then n8n's own login."
  running n8n || { docker compose up -d n8n >/dev/null || FAILED+=("start n8n"); }
  ./authentik/setup-ssh.sh n8n || FAILED+=("authentik/n8n.yaml")
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: ${BOLD}n8n.$DOMAIN${RESET} and ${BOLD}n8n.$ALIAS${RESET}, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  distribution group ${BOLD}Admin only${RESET}."
  pause "Press Enter when done…"
  retry_check "n8n.$DOMAIN resolves to $NB_IP" n8n_dns || true
  retry_check "n8n behind Authentik" n8n_ok || true
  open_url "https://n8n.$DOMAIN:$(pub_port)/"
  step "After the $BRAND sign-in, n8n asks for its ${BOLD}owner account${RESET} (once): your e-mail and a long password"
  step "  (in your password manager). Skip its questionnaire and the license offer."
  confirm "n8n's owner account made?" || FAILED+=("n8n owner account")
  pause
}

stage_kids() {
  stage "Kids" || return 0
  say "A child signs up like everyone (@$ALIAS address), then joins ${BOLD}kids${RESET}: home and the family app show"
  say "only their apps, Kavita only the Kids library (Everyone 10+), no Paperless, no Share."
  while ask KID_USER "A child's Authentik username (empty = done):" && [[ -n "${KID_USER:-}" ]]; do
    ./authentik/setup-ssh.sh kid "$KID_USER" || FAILED+=("kid $KID_USER")
    KID_USER=""
  done
  say "${BOLD}kids${RESET} only changes what they see. Apps that let them in limit them themselves: Kavita (Kids library)"
  say "already does; Jellyfin and Audiobookshelf need it per child. Their accounts exist only after a first"
  say "sign-in, so: the child signs in once to each (from home), then:"
  open_url "https://jellyfin.$DOMAIN:$(pub_port)/web/#/dashboard/users"
  step "Jellyfin → Dashboard → Users → the child → ${BOLD}Parental control${RESET}: Maximum allowed parental rating"
  step "(e.g. 12), and block unrated items. ${BOLD}Access${RESET}: only the libraries meant for them."
  confirm "Jellyfin limited for each child?" || FAILED+=("Jellyfin parental rating for kids")
  open_url "https://books.$DOMAIN:$(pub_port)/config/users"
  step "Audiobookshelf → Settings → Users → the child: untick ${BOLD}Can access explicit content${RESET}, set libraries."
  confirm "Audiobookshelf limited for each child?" || FAILED+=("Audiobookshelf explicit content for kids")
  note "Photos, Files and Music: each child has their own account and only their own items (Music has no age filter)."
  note "Test with their account: home shows the big tiles; Kavita shows Kids only; Jellyfin hides older titles."
  pause
}

stage_home() {
  stage "Home page: status for admins" || return 0
  say "heartbeat.sh now also writes site/status/heartbeat.json (every 5 min)."
  ./heartbeat.sh --dry-run >/dev/null || true
  retry_check "heartbeat.json with healthchecks.io's view" hb_file_ok || true
  say "Tapping Outside check, Last backup or Disk space opens details (graphs, Back up now, cleanups)."
  say "Ansible installs the job watcher (homelab-ops.path) and the hourly disk report (sudo):"
  if ! ops_ok >/dev/null; then
    bash ansible/live-safe.sh "$HOME" 1800 || warn "busy; Ansible may refuse, try again later"
    ansible_tags timers || FAILED+=("ansible --tags timers (ops)")
    docker compose up -d ops >/dev/null || FAILED+=("start ops")
    [[ -s ops/data/storage.json ]] || python3 storage-report.py || true
  fi
  retry_check "detail sheets ready" ops_ok || true
  open_url "https://home.$DOMAIN:$(pub_port)/"
  step "You see four tiles (apps, outside check, last backup, disk), your apps in groups."
  step "A family member's account sees one line instead of the tiles; a kid's account the big tiles."
  step "On the iPhone: Safari → Share → ${BOLD}Add to Home Screen${RESET} (the $BRAND icon, opens full screen)."
  step "Tap ${BOLD}Last backup${RESET} → ${BOLD}Back up now${RESET}: it shows Local backup…, Proton copy…, Done."
  confirm "Home page looks right?" || FAILED+=("home page")
  say "A first OpenCloud snapshot (otherwise only the nightly timer makes one), then a backup with it:"
  ./files-local.sh || FAILED+=("files-local.sh")
  sudo systemctl start homelab-backup.service || FAILED+=("homelab-backup")
  retry_check "last backup has Kavita, OpenCloud, Sharry" backup_has_7e || true
  pause
}

run_wizard() {

TOTAL_STAGES=15

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab 7e, 7g, 7h: Reading, Files, Share, public music apps, notifications, automations, kids, home status"
stage_secrets
stage_disk
stage_authentik
stage_start
stage_zones
stage_kavita
stage_proxy
stage_opencloud
stage_sharry
stage_navidrome
stage_terminal
stage_ntfy
stage_automations
stage_kids
stage_home

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
