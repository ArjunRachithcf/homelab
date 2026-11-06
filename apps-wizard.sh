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


# Homelab apps of 2026-10-25 (RUNBOOK.md step 7d): Home Assistant + Matter, AdGuard Home,
# Audiobookshelf, Actual, Karakeep, BentoPDF, and the outside heartbeat. Navidrome: ./music-wizard.sh.
#   ./apps-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./apps-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
ADMIN_USER=$(_existing ADMIN_USER || echo "$USER")
NOTIFY=$(_existing NOTIFY_TO || true)
GW=172.21.0.1                       # compose bridge gateway: Home Assistant listens here only
PUBLIC_NAMES=(books budget links pdf)         # NetBird reverse proxy services (× both domains)
PEER_NAMES=(ha dns)                           # NetBird only
NEW_SERVICES=(matter-server homeassistant adguard audiobookshelf actual karakeep-meili karakeep-chrome karakeep bentopdf)
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

gated()   { [[ "$(code "$1" /)" == 302 ]]; }            # forward-auth: unauthenticated → Authentik
not_public() { local c; c=$(pub_code "$1" /); [[ "$c" == 000 || "$c" =~ ^4 ]]; }

folders_ok()  { [[ -d "$(_existing AUDIOBOOKS_DIR || echo /srv/media/audiobooks)" ]]; }
secrets_ok()  { local k; for k in ABS_OIDC_CLIENT_SECRET ACTUAL_OIDC_CLIENT_SECRET KARAKEEP_OIDC_CLIENT_SECRET KARAKEEP_NEXTAUTH_SECRET KARAKEEP_MEILI_KEY; do
                  [[ -n "$(_existing "$k" || true)" ]] || return 1; done; }
conntrack_ok() { [[ "$(sysctl -n net.netfilter.nf_conntrack_udp_timeout_stream 2>/dev/null)" == 3600 ]]; }
nb_resolver_moved() { ss -Huln 'sport = :53' | awk '{print $4}' | grep -qx '127.0.0.153:53'; }
adguard_bound() { ss -Huln 'sport = :53' | awk '{print $4}' | grep -qx "$NB_IP:53"; }
# nothing on a wildcard address for 53 (the LAN never gets DNS from this box)
adguard_only_nb() { adguard_bound && ! ss -Hulnt 'sport = :53' | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):53$'; }
all_running() { local s; for s in "${NEW_SERVICES[@]}"; do running "$s" || return 1; done; }
ha_bind_ok()  { ./homeassistant/setup.sh status; }
matter_local() { [[ "$(ss -tlnH 'sport = :5580' | awk '{print $4}')" == "127.0.0.1:5580" ]]; }
adguard_answers() { dig +short +time=3 +tries=1 example.com "@$NB_IP" | grep -qE '^[0-9.]+$'; }
# Caddy proxies dns.* to adguard:3000; AdGuard's setup screen defaults its UI to port 80
adguard_ui_ok()   { docker compose exec -T caddy wget -q -O /dev/null http://adguard:3000/ 2>/dev/null; }
adguard_blocks()  { dig +short +time=3 +tries=1 doubleclick.net "@$NB_IP" | grep -qx '0.0.0.0'; }
zones_ok() {
  local n d; for n in "${PUBLIC_NAMES[@]}" "${PEER_NAMES[@]}"; do for d in "$DOMAIN" "$ALIAS"; do
    resolvectl query --legend=no -t A "$n.$d" 2>/dev/null | grep -q "$NB_IP" || return 1
  done; done
}
heartbeat_on() { [[ -n "$(_existing HEARTBEAT_URL || true)" ]] && systemctl is-active homelab-heartbeat.timer; }
heartbeat_ok() { ./heartbeat.sh --dry-run | grep -qx OK; }
abs_sso_only() { curl -s -m 10 --resolve "books.$DOMAIN:$(pub_port):$NB_IP" "https://books.$DOMAIN:$(pub_port)/status" \
                   | jq -e '.authMethods == ["openid"]'; }
backup_has_apps() { backup_section_ok 'Apps (RUNBOOK.md 7d)' 'not created yet'; }

ansible_tags() {
  command -v ansible-playbook >/dev/null \
    || install_pkgs ansible-core ansible-collection-ansible-posix ansible-collection-community-general
  ansible-playbook -K ansible/site.yml --tags "$1"
}

run_checks() {
  say "${BOLD}This box${RESET}"
  check "secrets for the apps in .env"                         secrets_ok || true
  check "audiobook folder exists"                              folders_ok || true
  check "conntrack keeps UDP 1 h (sleepy Matter devices)"      conntrack_ok || true
  check "NetBird's resolver moved to 127.0.0.153:53"           nb_resolver_moved || true
  check "new containers running"                               all_running || true
  check "Home Assistant listens on $GW:8123 only"              ha_bind_ok || true
  check "Matter server on 127.0.0.1:5580 only"                 matter_local || true
  check "AdGuard on $NB_IP:53, not on all interfaces"          adguard_only_nb || true
  check "AdGuard answers on $NB_IP"                            adguard_answers || true
  check "AdGuard blocks doubleclick.net"                       adguard_blocks || true
  check "AdGuard UI on port 3000 (Caddy, dns.$DOMAIN)"        adguard_ui_ok || true
  say "${BOLD}Authentik + Caddy (NetBird path)${RESET}"
  local a; for a in audiobookshelf karakeep actual; do check "Authentik OIDC app $a" oidc_app "$a" || true; done
  check "ha.$DOMAIN answers"                                   answers ha /manifest.json || true
  check "books.$DOMAIN answers"                                answers books /healthcheck || true
  check "budget.$DOMAIN answers"                               answers budget /health || true
  check "links.$DOMAIN answers"                                answers links /api/health || true
  check "pdf.$DOMAIN → Authentik (forward-auth)"               gated pdf || true
  check "dns.$DOMAIN → Authentik (forward-auth)"               gated dns || true
  check "Audiobookshelf: OIDC only"                            abs_sso_only || true
  check "NetBird zones (7 names × 2 domains) → $NB_IP"         zones_ok || true
  say "${BOLD}Public internet${RESET}"
  local n; for n in "${PUBLIC_NAMES[@]}"; do check "$n.$DOMAIN public (TLS passthrough, Let's Encrypt)" pub_ok "$n" || true; done
  check "ha.$DOMAIN not public"                                not_public ha || true
  check "dns.$DOMAIN not public"                               not_public dns || true
  say "${BOLD}Heartbeat + backup${RESET}"
  check "heartbeat: HEARTBEAT_URL set, timer active"           heartbeat_on || true
  check "heartbeat checks pass right now"                      heartbeat_ok || true
  check "last backup included the app volumes"                 backup_has_apps || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_host() {
  stage "This PC: secrets, folders, Matter sysctl, NetBird DNS" || return 0
  if secrets_ok && folders_ok && conntrack_ok && nb_resolver_moved; then say "All in place."; skip_offer && return; fi
  say "Secrets go straight into .env (never shown):"
  gen ABS_OIDC_CLIENT_SECRET      "openssl rand -base64 48 | tr -d '\n/+='"
  gen ACTUAL_OIDC_CLIENT_SECRET   "openssl rand -base64 48 | tr -d '\n/+='"
  gen KARAKEEP_OIDC_CLIENT_SECRET "openssl rand -base64 48 | tr -d '\n/+='"
  gen KARAKEEP_NEXTAUTH_SECRET    "openssl rand -base64 36 | tr -d '\n'"
  gen KARAKEEP_MEILI_KEY          "openssl rand -base64 36 | tr -dc 'A-Za-z0-9'"
  say "Ansible: audiobook folder, conntrack for Matter, NetBird's resolver → 127.0.0.153."
  warn "NetBird reconnects for a few seconds (detached, so an SSH session through it comes back)."
  note "Outside 02:00–05:30; it asks for your sudo password."
  pause "Press Enter to run it…"
  ansible_tags storage,smarthome,netbird || FAILED+=("ansible-playbook --tags storage,smarthome,netbird")
  retry_check "audiobook folder exists"                 folders_ok || true
  retry_check "conntrack keeps UDP 1 h"                  conntrack_ok || true
  retry_check "NetBird's resolver on 127.0.0.153:53"     nb_resolver_moved || true
  note "Copy audiobooks (one folder per book) into $(_existing AUDIOBOOKS_DIR || echo /srv/media/audiobooks)"
  note "whenever you like; Audiobookshelf rescans."
  pause
}

stage_authentik() {
  stage "Authentik: apps Audiobooks, Bookmarks, Budget, PDF tools, Ad blocking" || return 0
  if oidc_app actual && oidc_app karakeep && oidc_app audiobookshelf; then say "Apps exist."; skip_offer && return; fi
  say "Applies each app's blueprint (audiobooks, bookmarks, budget, pdf, adblock.yaml), adds the forward-auth"
  say "providers to the embedded outpost (Home stays) and you to the new group ${BOLD}budget${RESET}."
  ./authentik/setup-ssh.sh apps || FAILED+=("authentik/setup-ssh.sh apps")
  local a; for a in audiobookshelf karakeep actual; do retry_check "OIDC app $a" oidc_app "$a" || true; done
  note "Budget is only for the group budget (you). Another adult too? Directory → Groups → budget → add them."
  pause
}

stage_start() {
  stage "Start the containers" || return 0
  if all_running && ha_bind_ok; then say "All running, HA on $GW only."; skip_offer && return; fi
  say "Pulls the images, seeds Home Assistant's config (never on the LAN), recreates Caddy"
  say "(new sites), then starts the apps."
  docker compose pull "${NEW_SERVICES[@]}" || FAILED+=("docker compose pull")
  ./homeassistant/setup.sh init || FAILED+=("homeassistant/setup.sh init")
  docker compose up -d --force-recreate --no-deps caddy || FAILED+=("recreate caddy")
  docker compose up -d "${NEW_SERVICES[@]}" || FAILED+=("docker compose up (apps)")
  say "Waiting for Home Assistant's first start (a minute or two)…"
  local _; for _ in $(seq 1 24); do docker compose exec -T homeassistant test -e /config/.storage/http 2>/dev/null && break; sleep 5; done
  # HA takes the imported listen address only as a 5-minute trial; confirm it before it reverts
  ./homeassistant/setup.sh promote || FAILED+=("homeassistant/setup.sh promote")
  for _ in $(seq 1 24); do ha_bind_ok >/dev/null 2>&1 && break; sleep 5; done
  retry_check "new containers running"                    all_running || true
  retry_check "Home Assistant on $GW:8123 only"           ha_bind_ok || true
  retry_check "Matter server on 127.0.0.1:5580 only"      matter_local || true
  retry_check "AdGuard on $NB_IP:53"                      adguard_bound || true
  note "If AdGuard can't bind: ss -ulnp 'sport = :53' shows who has $NB_IP:53 (should be docker-proxy)."
  pause
}

stage_netbird() {
  stage "NetBird dashboard: DNS zones, policy, AdGuard as nameserver" || return 0
  if zones_ok; then say "Zones resolve."; skip_offer && return; fi
  open_url "https://app.netbird.io/dns/zones"
  step "DNS → Zones: like photos, one zone per name, A record at the apex → ${BOLD}$NB_IP${RESET},"
  step "  groups Streams, Admin, Plex-Server. 14 zones:"
  local n; for n in "${PUBLIC_NAMES[@]}" "${PEER_NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  open_url "https://app.netbird.io/access-control"
  step "Access Control → Policies → streams-to-homelab → add a rule: protocol ${BOLD}UDP${RESET}, port ${BOLD}53${RESET};"
  step "  and add ${BOLD}53${RESET} to the TCP rule's ports (80, 443, $(pub_port), 32400, 53)."
  open_url "https://app.netbird.io/dns/nameservers"
  step "DNS → Nameservers → Add → Custom DNS: name ${BOLD}AdGuard${RESET}, nameserver ${BOLD}$NB_IP${RESET} port 53 (UDP),"
  step "  then 9.9.9.9 as a second one (names still resolve while the PC is off; ads come back then)."
  step "  Distribution groups: ${BOLD}Streams${RESET} and ${BOLD}Admin${RESET} (not Plex-Server: this PC uses its own DNS)."
  step "  Match domains: none (all domains). Enabled → Save."
  note "Check in AdGuard's query log after a day that phones show up there; NetBird only falls back to 9.9.9.9"
  note "when AdGuard doesn't answer."
  pause "Press Enter when done…"
  retry_check "zones (7 names × 2 domains) → $NB_IP" zones_ok || true
  pause
}

stage_proxy() {
  stage "NetBird reverse proxy: 10 public services" || return 0
  local all=1 n; for n in "${PUBLIC_NAMES[@]}"; do pub_ok "$n" >/dev/null 2>&1 || all=0; done
  if (( all )); then say "All five names are public."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy"
  step "Reverse Proxy → Services → Add, exactly like photos, for each of:"
  for n in "${PUBLIC_NAMES[@]}"; do step "    $n.$DOMAIN   $n.$ALIAS"; done
  step "  Mode ${BOLD}TLS${RESET} (passthrough, never HTTP) · listen ${BOLD}$(pub_port)${RESET} · target peer $PEER, port ${BOLD}8444${RESET} ·"
  step "  Preserve Client Source IP ${BOLD}on${RESET} (PROXY v2)."
  warn "Not ha.* or dns.*: those stay NetBird only."
  note "No Infomaniak change: the wildcard CNAME covers the new names."
  pause "Press Enter when done…"
  for n in "${PUBLIC_NAMES[@]}"; do retry_check "$n.$DOMAIN public" pub_ok "$n" || true; done
  check "ha.$DOMAIN not public"  not_public ha || true
  check "dns.$DOMAIN not public" not_public dns || true
  pause
}

stage_adguard() {
  stage "AdGuard Home: first run" || return 0
  if adguard_answers && adguard_blocks; then say "AdGuard answers and blocks."; skip_offer && return; fi
  open_url "https://dns.$DOMAIN:$(pub_port)/"
  step "Sign in with your passkey (admins only), then AdGuard's setup:"
  step "  Admin web interface: ${BOLD}All interfaces, port 3000${RESET}. It suggests 80: change it, Caddy expects 3000."
  step "  DNS server: ${BOLD}All interfaces, port 53${RESET} (inside the container; outside only $NB_IP)."
  step "  Username + long random password → save in Proton Pass (\"homelab AdGuard\")."
  step "Then Settings → DNS settings:"
  step "  Upstream: ${BOLD}https://dns.quad9.net/dns-query${RESET} (Quad9, Swiss) · Bootstrap: 9.9.9.9, 149.112.112.112"
  step "  Enable DNSSEC → Apply. Filters → DNS blocklists: AdGuard DNS filter is on; add more if you like."
  pause "Press Enter when done…"
  retry_check "AdGuard answers on $NB_IP"     adguard_answers || true
  retry_check "AdGuard blocks doubleclick.net" adguard_blocks || true
  retry_check "AdGuard UI on port 3000 (Caddy, dns.$DOMAIN)" adguard_ui_ok || true
  pause
}

stage_homeassistant() {
  stage "Home Assistant + Matter" || return 0
  skip_offer && return
  open_url "https://ha.$DOMAIN/"
  step "Onboarding: owner account ${BOLD}$ADMIN_USER${RESET} (password in Proton Pass), home location, units."
  step "Profile (bottom left) → Security → ${BOLD}Multi-factor authentication${RESET} → Authenticator app → on."
  step "Settings → System → Network:"
  step "  HTTP server: listen address ${BOLD}$GW${RESET}, Trust X-Forwarded-For ${BOLD}on${RESET}, trusted proxy ${BOLD}172.21.1.11${RESET}"
  step "  (imported from the seed; confirm if asked). Home Assistant URL: ${BOLD}https://ha.$DOMAIN${RESET} (both)."
  pause "Press Enter when that matches…"
  ./homeassistant/setup.sh drop-http || FAILED+=("homeassistant/setup.sh drop-http")
  sleep 20; retry_check "HA still on $GW:8123 only" ha_bind_ok || true
  step "Settings → Devices & services → Add integration → ${BOLD}Matter${RESET} → URL ${BOLD}ws://127.0.0.1:5580/ws${RESET}."
  step "Phone: Home Assistant app → server ${BOLD}https://ha.$DOMAIN${RESET} (NetBird on) → sign in."
  step "New Matter device: app → Settings → Devices → Add device → scan its code (phone on home Wi-Fi, Bluetooth on)."
  step "Already in Apple Home / Google Home: there, device → \"Turn on pairing mode\", then add that code in HA."
  note "Thread devices need a border router (HomePod/Apple TV, Nest Hub, or a Thread USB stick)."
  note "  iPhone: HA app → Settings → Companion app → Troubleshooting → Sync Thread credentials."
  note "A Zigbee/Thread USB stick: tell Claude, it needs a devices: line in docker-compose.yml."
  pause
}

# Audiobookshelf's OIDC settings through its API, so the client secret is never shown or pasted
abs_oidc() {
  local key; ask_secret ABS_KEY "ABS API key (Settings → API Keys → Add, admin):" || return 1
  key=$ABS_KEY
  local base; base="https://auth.$DOMAIN:$(pub_port)/application/o"
  SECRET=$(_existing ABS_OIDC_CLIENT_SECRET) BASE=$base METHODS="$1" BRAND="$BRAND" python3 - <<'EOF' |
import json, os
b = os.environ["BASE"]
print(json.dumps({
  "authActiveAuthMethods": os.environ["METHODS"].split(","),
  "authOpenIDIssuerURL": f"{b}/audiobookshelf/",
  "authOpenIDAuthorizationURL": f"{b}/authorize/",
  "authOpenIDTokenURL": f"{b}/token/",
  "authOpenIDUserInfoURL": f"{b}/userinfo/",
  "authOpenIDJwksURL": f"{b}/audiobookshelf/jwks/",
  "authOpenIDLogoutURL": f"{b}/audiobookshelf/end-session/",
  "authOpenIDClientID": "audiobookshelf",
  "authOpenIDClientSecret": os.environ["SECRET"],
  "authOpenIDTokenSigningAlgorithm": "RS256",
  "authOpenIDButtonText": f"Sign in with your {os.environ['BRAND']} account",
  "authOpenIDAutoLaunch": True,
  "authOpenIDAutoRegister": True,
  "authOpenIDMatchExistingBy": "username",
  "authOpenIDMobileRedirectURIs": ["audiobookshelf://oauth", "shelfplayer://callback"],  # ABS app, ShelfPlayer (iOS)
  "authOpenIDGroupClaim": "absgroups",
  # Left out, ABS stores it as undefined and sends redirect_uri …/undefined/auth/openid/callback
  "authOpenIDSubfolderForRedirectURLs": "",
}))
EOF
  curl -sS -m 15 -o /dev/null -w '%{http_code}\n' -X PATCH -H 'Content-Type: application/json' \
    -K <(printf 'header = "Authorization: Bearer %s"\n' "$key") --data-binary @- \
    --resolve "books.$DOMAIN:$(pub_port):$NB_IP" "https://books.$DOMAIN:$(pub_port)/api/auth-settings" | grep -qx 200
}

stage_media() {
  stage "Audiobooks, Bookmarks, Budget, PDF: first sign-ins" || return 0
  if abs_sso_only; then say "Audiobookshelf is OIDC-only already."; skip_offer && return; fi
  say "${BOLD}Audiobooks${RESET} (Audiobookshelf): first a local root account (break-glass), then SSO only."
  open_url "https://books.$DOMAIN:$(pub_port)/"
  step "Create the root user (long random password → Proton Pass), add a library: folder ${BOLD}/audiobooks${RESET}"
  step "  (and /podcasts for podcasts). Settings → API Keys → Add (admin) → copy it."
  if abs_oidc "local,openid"; then printf '  %s✓%s OIDC set (button + auto-launch, roles from Authentik)\n' "$GREEN" "$RESET"
  else warn "couldn't set OIDC through the API (key? ABS up?)"; FAILED+=("ABS OIDC settings"); fi
  step "Sign out, sign in with ${BOLD}$BRAND account${RESET}: you arrive as admin (authentik Admins)."
  if confirm "Did the SSO sign-in work? Then turn off password logins"; then
    abs_oidc "openid" && retry_check "Audiobookshelf: OIDC only" abs_sso_only || true
    note "Delete the API key in ABS afterwards (Settings → API Keys)."
  fi
  step "Phone: ABS app (Android) or ShelfPlayer (iPhone), server ${BOLD}https://books.$DOMAIN:$(pub_port)${RESET} → Sign in with $BRAND account."
  pause
  say "${BOLD}Bookmarks${RESET} (Karakeep) and ${BOLD}Budget${RESET} (Actual): the first sign-in is the admin/owner, so you go first."
  open_url "https://links.$DOMAIN:$(pub_port)/"
  open_url "https://budget.$DOMAIN:$(pub_port)/"
  step "Karakeep: browser extension / app → server https://links.$DOMAIN:$(pub_port), sign in with an API key"
  step "  (Karakeep → Settings → API keys), since apps can't do the browser login."
  step "Actual: create your budget; import bank CSV/CAMT exports (Swiss banks have no bank sync in Actual)."
  open_url "https://pdf.$DOMAIN:$(pub_port)/"
  pause
}

stage_heartbeat() {
  stage "Heartbeat (healthchecks.io): a mail when the PC goes quiet" || return 0
  if heartbeat_on; then say "Heartbeat is on."; skip_offer && return; fi
  open_url "https://healthchecks.io/"
  step "Sign up (free) with ${NOTIFY:-your address} → project ${BOLD}homelab${RESET} → Add check:"
  step "  name ${BOLD}$PEER${RESET}, Schedule: Simple, Period ${BOLD}5 minutes${RESET}, Grace ${BOLD}15 minutes${RESET}."
  step "  Integrations: Email ${NOTIFY:-your address} (on). Copy the ping URL (https://hc-ping.com/…)."
  local url; while :; do
    ask_secret url "Ping URL (anyone with it can mark the check up or down):" || break
    [[ "$url" =~ ^https://hc-ping\.com/[A-Za-z0-9/_-]+$ ]] && { write_env HEARTBEAT_URL "$url"; break; }
    warn "that doesn't look like https://hc-ping.com/…"
  done
  say "Right now this would report:"; ./heartbeat.sh --dry-run | sed 's/^/    /'
  ansible_tags timers || FAILED+=("ansible-playbook --tags timers")
  retry_check "heartbeat timer active" heartbeat_on || true
  step "healthchecks.io shows the check ${BOLD}up${RESET} within 5 minutes."
  pause
}

stage_backup() {
  stage "First backup with the apps" || return 0
  say "Runs the backup now; the app volumes (HA, Matter fabric, AdGuard, Navidrome, ABS, Actual,"
  say "Karakeep) are in every archive from here on."
  sudo systemctl start homelab-backup.service || FAILED+=("homelab-backup")
  retry_check "last backup included the app volumes" backup_has_apps || true
  pause
}

run_wizard() {

TOTAL_STAGES=10

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab: Home Assistant, AdGuard, audiobooks, bookmarks, budget, PDF, heartbeat"
stage_host
stage_authentik
stage_start
stage_netbird
stage_proxy
stage_adguard
stage_homeassistant
stage_media
stage_heartbeat
stage_backup

printf '\n'; run_checks
for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
