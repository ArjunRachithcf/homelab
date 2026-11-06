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


# Homelab public access: the human-only parts of RUNBOOK.md "Public access".
#   ./public-wizard.sh           walk through every stage (re-runnable; finished stages can be skipped)
#   ./public-wizard.sh --check   only run the automatic checks


cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE=.env
[[ -f $ENV_FILE ]] || { echo "No .env in $PWD."; exit 1; }

DOMAIN=$(_existing DOMAIN)          # canonical
ALIAS=$(_existing DOMAIN_ALIAS)     # second domain, carries Proton Mail
NB_IP=$(_existing NETBIRD_IP)
BRAND=$(_existing BRAND || echo Home)          # how pages and mails name this homelab
SLUG=$(_existing BRAND_SLUG || echo home)
PEER=$(_existing NETBIRD_PEER || hostname -s)   # this PC in the NetBird dashboard
HOSTS=(home auth paperless jellyfin photos music)
NNAMES=$(( ${#HOSTS[@]} * 2 ))   # each name on both domains
FAILED=()

# ── Helpers for this wizard (shared: lib/wizard.sh, lib/wizard-checks.sh) ──

# link_flow FIELD SLUG: set recovery_flow/enrollment_flow on the login identification stage
# from here, for when the admin UI's flow search doesn't list the new flow.
link_flow() {
  confirm "The dropdown doesn't list ${2}? Set it from here instead" || return 0
  FIELD="$1" SLUG="$2" docker compose exec -T -e FIELD -e SLUG authentik-worker ak shell -c '
import os
from authentik.flows.models import Flow
from authentik.stages.identification.models import IdentificationStage
s = IdentificationStage.objects.get(name="default-authentication-identification")
setattr(s, os.environ["FIELD"], Flow.objects.get(slug=os.environ["SLUG"])); s.save()
print("OUT:✓", os.environ["FIELD"], "=", os.environ["SLUG"])
' 2>&1 | sed -n 's/^OUT:/  /p'
}

# Caddy on this box: :443 is the NetBird path, :8444 the public one (needs a PROXY header).
loc_443()   { curl -s -o /dev/null -m 10 -w '%{redirect_url}' --resolve "$1:443:$NB_IP" "https://$1$2"; }
loc_8444()  { curl -s -o /dev/null -m 10 -w '%{redirect_url}' --haproxy-protocol --resolve "$1:8444:$NB_IP" "https://$1:8444$2"; }
to_authentik() { [[ "$1" == *outpost.goauthentik.io/start* || "$1" == *"auth.$DOMAIN"* ]]; }

# home is served on the public port (Authentik's external host has it); peers use it too.
loc_pub()   { curl -s -o /dev/null -m 10 -w '%{redirect_url}' --resolve "$1:$(pub_port):$NB_IP" "https://$1:$(pub_port)$2"; }
home_gated()     { to_authentik "$(loc_pub "home.$DOMAIN" /)"; }
ldap_up()        { [[ -n "$(docker compose ps --status running -q authentik-ldap)" ]]; }
jellyfin_ldap()  { docker compose exec -T jellyfin test -f /config/plugins/configurations/LDAP-Auth.xml; }
ldap_ready()     { ldap_up && jellyfin_ldap; }
public_jellyfin_ok() { [[ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' --haproxy-protocol --resolve "jellyfin.$DOMAIN:8444:$NB_IP" "https://jellyfin.$DOMAIN:8444/System/Info/Public")" == 200 ]]; }
email_set()      { [[ -n "$(_existing AUTHENTIK_EMAIL__PASSWORD)" ]]; }

# Mail guard: Proton's MX on the alias must never change.
mx_now()   { dig +short MX "$ALIAS" @1.1.1.1 | sort | tr '\n' ' '; }
MX_BEFORE=""
mx_snapshot() { MX_BEFORE=$(mx_now); note "MX $ALIAS now: ${MX_BEFORE:-<none>}"; }
mx_same()  { [[ "$(mx_now)" == "$MX_BEFORE" && "$MX_BEFORE" == *protonmail* ]]; }

wildcard_ok() {
  local d c; c=$(cluster); [[ -n "$c" ]] || return 1
  for d in "$DOMAIN" "$ALIAS"; do
    # fresh label each time: a fixed one gets "doesn't exist" cached from before the wildcard
    dig +short CNAME "wz$RANDOM$RANDOM.$d" @1.1.1.1 | grep -qi "^${c%.}\.\?$" || return 1
  done
}
# The NetBird proxy hands TLS through untouched: the cert must be Caddy's Let's Encrypt one.
passthrough_ok() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  echo | openssl s_client -connect "$ip:$(pub_port)" -servername "home.$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | grep -q "Let's Encrypt"
}
public_home_gated() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  to_authentik "$(curl -s -o /dev/null -m 10 -w '%{redirect_url}' --resolve "home.$DOMAIN:$(pub_port):$ip" "https://home.$DOMAIN:$(pub_port)/")"
}
public_dns_cut_over() {
  local d h c; c=$(cluster)
  for d in "$DOMAIN" "$ALIAS"; do for h in auth paperless jellyfin; do
    dig +short "$h.$d" @1.1.1.1 | head -n1 | grep -qi "^${c%.}\.\?$" || return 1
  done; done
}
public_admin_blocked() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  [[ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' --resolve "auth.$DOMAIN:$(pub_port):$ip" "https://auth.$DOMAIN:$(pub_port)/if/admin/")" == 403 ]]
}
listens_on_nb_only() {
  local addrs; addrs=$(ss -tlnH | awk '{print $4}' | grep -E ":(80|443|8444|$(pub_port))\$" || true)
  [[ $(grep -c "^$NB_IP:" <<<"$addrs") -ge 3 ]] && ! grep -vq "^$NB_IP:" <<<"$addrs"
}

run_checks() {
  say "${BOLD}This box${RESET}"
  check "80/443/8444/$(pub_port) listen only on $NB_IP"            listens_on_nb_only || true
  check "8444 refuses TLS without a PROXY header"      bash -c "! curl -s -m 5 -o /dev/null --resolve auth.$DOMAIN:8444:$NB_IP https://auth.$DOMAIN:8444/" || true
  check "Authentik mail configured"                    email_set || true
  check "recovery flow exists"                         recovery_ok || true
  check "enrollment flow exists"                       enrollment_ok || true
  check "home.$DOMAIN sends you to Authentik"          home_gated || true
  check "LDAP outpost running, Jellyfin LDAP plugin set" ldap_ready || true
  check "Jellyfin answers on the public listener"      public_jellyfin_ok || true
  check "NetBird zones: ${HOSTS[*]} → $NB_IP"          zone_ok "${HOSTS[@]}" || true
  check "MX $ALIAS still Proton (as a peer sees it)"   mx_via_peer || true
  say "${BOLD}Public internet${RESET}"
  check "wildcard CNAMEs → $(cluster)"                 wildcard_ok || true
  check "cluster passes TLS through (Let's Encrypt)"   passthrough_ok || true
  check "public home.$DOMAIN sends you to Authentik"   public_home_gated || true
  check "auth/paperless/jellyfin public → cluster"     public_dns_cut_over || true
  check "public Authentik admin → 403"                 public_admin_blocked || true
}

# ── Stages ────────────────────────────────────────────────────────────────

stage_email() {
  stage "Authentik mail via Proton (noreply@$ALIAS)" || return 0
  if email_set && ! confirm "A Proton SMTP token is already in .env. Replace it"; then
    :
  else
    open_url "https://account.proton.me/u/0/mail/addresses"
    step "Settings → Identity and addresses: add the address (or alias) ${BOLD}noreply@$ALIAS${RESET}."
    open_url "https://account.proton.me/u/0/mail/imap-smtp"
    step "Settings → IMAP/SMTP → SMTP tokens → Generate token."
    step "  Token name: authentik · Email address: noreply@$ALIAS → Generate → copy it."
    note "It's shown once. It goes straight into .env (mode 600) and is never printed."
    ask_secret AUTHENTIK_EMAIL__PASSWORD "Paste the SMTP token:"
    write_env AUTHENTIK_EMAIL__USERNAME "noreply@$ALIAS"
    write_env AUTHENTIK_EMAIL__PASSWORD "$AUTHENTIK_EMAIL__PASSWORD"
    write_env AUTHENTIK_EMAIL__FROM "$BRAND <noreply@$ALIAS>"
    chmod 600 "$ENV_FILE"
  fi
  say "Restarting Authentik with the mail settings…"
  docker compose up -d authentik-server authentik-worker >/dev/null 2>&1
  for _ in $(seq 1 30); do [[ "$(code_443 "auth.$DOMAIN" /-/health/live/)" == 200 ]] && break; sleep 5; done
  ask TEST_TO "Send a test mail to (your own address):"
  if check "Authentik sent the test mail" docker compose exec -T authentik-worker ak test_email "$TEST_TO"; then
    confirm "Did it arrive (check spam too)?" || FAILED+=("test mail didn't arrive")
    note "Optional: in Proton, open the mail → View headers. Look for spf=pass, dkim=pass."
  fi
  warn "Don't continue to sign-ups until this mail arrives: they'd get stuck unverified."
  pause
}

stage_mfa() {
  stage "Authentik: force MFA for everyone" || return 0
  skip_offer && return
  open_url "https://auth.$DOMAIN/if/admin/#/flow/stages"
  step "Flows and Stages → Stages → ${BOLD}default-authentication-mfa-validation${RESET} → Edit."
  step "  Device classes: TOTP and WebAuthn (passkeys) ticked."
  step "  Not configured action: ${BOLD}Force the user to configure an authenticator${RESET}."
  step "  Configuration stages: default-authenticator-webauthn-setup and"
  step "                        default-authenticator-totp-setup (ctrl-click both) → Update."
  warn "Already have a passkey or TOTP on your admin account? Then you won't get locked out."
  step "Check: log out (avatar → Sign out), log in again: Authentik asks for your MFA."
  pause "Press Enter once a fresh login asks for MFA…"
}

stage_recovery() {
  stage "Authentik: \"Forgot password?\" via email" || return 0
  if recovery_ok; then say "The recovery flow already exists."; skip_offer && return; fi
  open_url "https://auth.$DOMAIN/if/admin/#/blueprints/instances"
  step "Customization → Blueprints → Create:"
  step "  Name: recovery · Path: ${BOLD}example/flows-recovery-email-verification.yaml${RESET} · Enabled → Create."
  step "  Then the ▶ (Apply) button on its row."
  open_url "https://auth.$DOMAIN/if/admin/#/flow/stages"
  step "Stages → ${BOLD}default-authentication-identification${RESET} → Edit → Flow settings:"
  step "  Recovery flow: ${BOLD}default-recovery-flow${RESET} → Update."
  note "It's a search box: type \"recovery\". Reload the page if the flow is brand new."
  pause "Press Enter when done…"
  link_flow recovery_flow default-recovery-flow
  retry_check "recovery flow answers" recovery_ok || true
  pause
}

stage_enrollment() {
  stage "Authentik: family sign-up (@$ALIAS / @$DOMAIN only)" || return 0
  if enrollment_ok; then say "The enrollment flow already exists."; skip_offer && return; fi
  email_set || warn "No mail configured yet (stage 1). Sign-ups need it to verify the address."
  open_url "https://auth.$DOMAIN/if/admin/#/blueprints/instances"
  step "Blueprints → Create: Name: enrollment · Path:"
  step "  ${BOLD}example/flows-enrollment-email-verification.yaml${RESET} · Enabled → Create → ▶ Apply."
  note "It creates the user inactive; the link in the verification mail activates them."
  open_url "https://auth.$DOMAIN/if/admin/#/policy/policies"
  step "Policies → Create → ${BOLD}Expression Policy${RESET}, name ${BOLD}$SLUG-email-domain${RESET}, expression:"
  printf '%s\n' \
    '      email = request.context.get("prompt_data", {}).get("email", "")' \
    "      if email.lower().rsplit(\"@\", 1)[-1] in (\"$ALIAS\", \"$DOMAIN\"):" \
    '          return True' \
    "      ak_message(\"Please use your @$ALIAS address\")" \
    '      return False'
  step "Stages → ${BOLD}default-enrollment-prompt-second${RESET} (name + email) → Edit →"
  step "  Validation policies: add $SLUG-email-domain → Update."
  note "default-enrollment-user-write: leave Group empty; authentik/family.yaml adds family + paperless-users."
  step "Flows → ${BOLD}default-enrollment-flow${RESET} → Stage Bindings → Bind existing stage:"
  step "  ${BOLD}default-authentication-mfa-validation${RESET}, order ${BOLD}90${RESET} (forced MFA setup before login)."
  step "Stages → default-authentication-identification → Edit → Flow settings:"
  step "  Enrollment flow: ${BOLD}default-enrollment-flow${RESET} → Update. (Login page now shows \"Sign up\".)"
  note "Not listed? Continue; the next prompt can set it from here."
  step "Policies → Create → ${BOLD}Reputation Policy${RESET}: name login-reputation, check IP + username,"
  step "  threshold -5. Then Flows → default-authentication-flow → Stage Bindings →"
  step "  default-authentication-identification row → Bind existing policy → login-reputation,"
  step "  ${BOLD}Negate result: on${RESET} (the policy is true for BAD reputation; without negate nobody gets the username field)."
  pause "Press Enter when done…"
  enrollment_ok && link_flow enrollment_flow default-enrollment-flow
  retry_check "enrollment flow answers" enrollment_ok || true
  pause
}

stage_family() {
  stage "Authentik: group family, sign-up alerts, brand" || return 0
  skip_offer && return
  open_url "https://auth.$DOMAIN/if/admin/#/identity/groups"
  step "Directory → Groups → Create: ${BOLD}family${RESET}. Open it → Users → add yourself."
  open_url "https://auth.$DOMAIN/if/admin/#/policy/policies"
  step "Policies → Create → ${BOLD}Event Matcher Policy${RESET}: name new-user-created,"
  step "  Action: Model Created · Model: User (authentik_core) → Create."
  open_url "https://auth.$DOMAIN/if/admin/#/events/rules"
  step "Events → Notification Rules → Create: name ${BOLD}New sign-up${RESET},"
  step "  Destination group: authentik Admins · Transports: default-local-transport and"
  step "  default-email-transport · Severity: notice → Create."
  step "  Expand the rule → Bind existing policy → new-user-created."
  open_url "https://auth.$DOMAIN/if/admin/#/core/brands"
  step "System → Brands → authentik-default → Edit:"
  step "  Title: ${BOLD}$BRAND${RESET}"
  step "  Logo: https://home.$DOMAIN:$(pub_port)/logo-white.png   Favicon: https://home.$DOMAIN:$(pub_port)/favicon.png"
  note "If Logo/Favicon are file pickers, upload ~/homelab/site/logo-white.png and favicon.png."
  pause "Press Enter when done…"
}

stage_forward_auth() {
  stage "Authentik: forward-auth for the landing page" || return 0
  if home_gated; then say "home.$DOMAIN already redirects to Authentik."; skip_offer && return; fi
  open_url "https://auth.$DOMAIN/if/admin/#/core/applications"
  step "Applications → ${BOLD}Create with provider${RESET}:"
  step "  Name ${BOLD}Home${RESET}, slug home → Provider type: Proxy Provider →"
  step "  ${BOLD}Forward auth (single application)${RESET}, External host ${BOLD}https://home.$DOMAIN:$(pub_port)${RESET}."
  step "  Authorization flow: ${BOLD}default-provider-authorization-implicit-consent${RESET};"
  step "  Invalidation flow: ${BOLD}default-provider-invalidation-flow${RESET}."
  step "  Bindings: none (any signed-in user) → Submit."
  open_url "https://auth.$DOMAIN/if/admin/#/outpost/outposts"
  step "Applications → Outposts → ${BOLD}authentik Embedded Outpost${RESET} → Edit:"
  step "  Applications: add Home (keep any already there)."
  step "  Advanced settings: ${BOLD}authentik_host: https://auth.$DOMAIN${RESET} → Update."
  note "Leave the outpost named \"ldap\" alone: Claude set it up for Jellyfin."
  pause "Press Enter when done…"
  say "Waiting for the outpost to pick it up…"
  for _ in $(seq 1 12); do home_gated && break; sleep 5; done
  retry_check "home.$DOMAIN → Authentik login" home_gated || true
  note "The browser check (tiles) comes after the NetBird zones stage: the name doesn't resolve yet."
  pause
}

stage_jellyfin_ldap() {
  stage "Jellyfin + Swiftfin: sign in with Authentik" || return 0
  skip_offer && return
  check "LDAP outpost running" ldap_up || true
  check "Jellyfin LDAP plugin configured" jellyfin_ldap || true
  say "Jellyfin now checks logins against Authentik (members of ${BOLD}family${RESET} only)."
  say "The password is ${BOLD}<Authentik password>;<6-digit code>${RESET}, e.g. hunter2;123456."
  note "Only TOTP codes work here (authenticator app), not passkeys. Everyone in family needs TOTP."
  open_url "https://auth.$DOMAIN/if/user/#/settings"
  step "Your account → MFA Devices: make sure you have a TOTP device (add one if passkey-only)."
  open_url "https://jellyfin.$DOMAIN:$(pub_port)/web/"
  step "Jellyfin: sign out, sign in as your ${BOLD}Authentik username${RESET} with password;code."
  note "Same name as an existing Jellyfin user? Dashboard → Users → you → Authentication Provider →"
  note "LDAP-Authentication first. Just renamed or added to family? Wait ~5 min (outpost cache)."
  note "You become a new Jellyfin user (admin, since you're in authentik Admins)."
  step "Swiftfin on your phone: add server https://jellyfin.$DOMAIN:$(pub_port), sign in the same way."
  note "Swiftfin keeps the session; the code is only needed when signing in again."
  step "Old local Jellyfin admin: Dashboard → Users → it → Password: set a long random one"
  step "  (password manager), and Profile → \"Maximum failed login attempts\": 3. Keep it as break-glass."
  confirm "Signed in to Jellyfin with password;code?" || FAILED+=("Jellyfin LDAP login")
}

stage_zones() {
  stage "NetBird: private DNS zones (peers keep the direct path)" || return 0
  if zone_ok "${HOSTS[@]}"; then say "All names already resolve to $NB_IP here."; skip_offer && return; fi
  mx_snapshot
  open_url "https://app.netbird.io/dns/zones"
  say "One zone per name, so nothing else under $DOMAIN/$ALIAS (MX!) is shadowed on peers."
  step "DNS → Zones → Add Zone, $NNAMES times. For each name below:"
  local d h
  for d in "$DOMAIN" "$ALIAS"; do for h in "${HOSTS[@]}"; do
    step "  Zone ${BOLD}$h.$d${RESET} · Distribution groups: Streams, Admin, Plex-Server · Search domain: off"
  done; done
  step "In each zone: Add Record → type A · name = the zone name itself · value ${BOLD}$NB_IP${RESET}."
  note "If the form wants a relative name, use @ (or leave it empty) for the zone apex."
  pause "Press Enter when all $NNAMES zones have their record…"
  for _ in $(seq 1 12); do zone_ok "${HOSTS[@]}" && break; sleep 5; done
  retry_check "this peer resolves all $NNAMES names → $NB_IP" zone_ok "${HOSTS[@]}" || true
  retry_check "MX $ALIAS still Proton on a peer" mx_via_peer || true
  step "Now in a browser over NetBird: ${BOLD}https://home.$DOMAIN:$(pub_port)${RESET} → log in → you see the tiles."
  note "Device still says 'not found'? Toggle NetBird off/on so it picks up the new zones."
  pause
}

stage_custom_domains() {
  stage "NetBird: reverse proxy custom domains" || return 0
  open_url "https://app.netbird.io/reverse-proxy/custom-domains"
  step "Reverse Proxy → Custom Domains → Add Domain: ${BOLD}$DOMAIN${RESET} · pick the EU proxy cluster → Save."
  step "Add Domain again: ${BOLD}$ALIAS${RESET} · same cluster → Save."
  step "Both show Pending Verification and the CNAME target, e.g. eu.proxy.netbird.io."
  ask NETBIRD_PROXY_CLUSTER "Paste the cluster target (no *., no trailing dot):"
  NETBIRD_PROXY_CLUSTER=${NETBIRD_PROXY_CLUSTER%.}
  write_env NETBIRD_PROXY_CLUSTER "$NETBIRD_PROXY_CLUSTER"
  note "Verify within 48 h, or NetBird drops the domain and you add it again."
  pause
}

stage_wildcard_dns() {
  stage "Infomaniak: wildcard CNAMEs (mail records untouched)" || return 0
  if wildcard_ok; then say "Both wildcards already point to $(cluster)."; skip_offer && return; fi
  mx_snapshot
  open_url "https://manager.infomaniak.com/v3/hosting-services/domains"
  local d
  for d in "$DOMAIN" "$ALIAS"; do
    step "Domains → ${BOLD}$d${RESET} → DNS zone → Add a record:"
    step "  Type CNAME · Source ${BOLD}*${RESET} · Target ${BOLD}$(cluster)${RESET} · TTL 1 hour."
  done
  warn "Add only. Don't edit or delete anything else, above all MX/SPF/DKIM/DMARC/TXT on $ALIAS."
  note "auth/paperless/jellyfin keep their A records for now; those still win over the wildcard."
  pause "Press Enter once both are saved…"
  say "Checking public DNS (up to 5 minutes)…"
  for _ in $(seq 1 30); do wildcard_ok && break; sleep 10; done
  retry_check "*.$DOMAIN and *.$ALIAS → $(cluster)" wildcard_ok || true
  retry_check "MX $ALIAS unchanged (mail guard)" mx_same || true
  open_url "https://app.netbird.io/reverse-proxy/custom-domains"
  step "Back in NetBird: each domain → Verify Domain → Start Verification → both ${BOLD}Active${RESET}."
  pause "Press Enter once both are Active…"
}

stage_services() {
  stage "NetBird: $NNAMES reverse proxy services, TLS passthrough" || return 0
  if passthrough_ok && public_home_gated; then say "Services already answer through the cluster."; skip_offer && return; fi
  open_url "https://app.netbird.io/reverse-proxy/services"
  step "Reverse Proxy → Services → Add Service, once per name:"
  local d h
  for d in "$DOMAIN" "$ALIAS"; do
    step "  $(for h in "${HOSTS[@]}"; do printf '%s ' "$h.$d"; done)"
  done
  step "Each one:"
  step "  Mode: ${BOLD}TLS${RESET} (passthrough). ${RED}Never HTTP${RESET}: that would decrypt at NetBird."
  step "  Listen port: ${BOLD}9443${RESET} on all $NNAMES (eu1 won't open 443 or 8443 for TLS services;"
  step "  9443 was tested 2026-10-17). Advanced Settings: ${BOLD}Preserve Client Source IP${RESET} on (= PROXY v2)."
  step "  Target: Peer ${BOLD}$PEER${RESET} · port ${BOLD}8444${RESET} · ${BOLD}PROXY Protocol${RESET}: on."
  step "  Access Control tab (optional): country allow-list $(_existing HOME_COUNTRY || echo "your country") (+ where you travel), CrowdSec enforce."
  ask NETBIRD_PROXY_PORT "Which listen port did the services get? [9443]"
  write_env NETBIRD_PROXY_PORT "${NETBIRD_PROXY_PORT:-9443}"
  if [[ "${NETBIRD_PROXY_PORT:-9443}" != 9443 ]]; then
    warn "Not 9443: stop here and tell Claude the port."
    note "Caddy, Authentik URLs and the landing page are set up for :9443."
    exit 0
  fi
  open_url "https://app.netbird.io/access-control"
  note "No policy is needed for the proxy: its peers (proxy-…netbird.cloud) reach $PEER on their own."
  step "Access Control → Policies → your ${BOLD}Streams${RESET} policy: add TCP ${BOLD}$(pub_port)${RESET}"
  step "  (now 80, 443, $(pub_port), 32400), so peers can use the same :$(pub_port) URLs. Admin stays 22."
  pause "Press Enter when done…"
  for _ in $(seq 1 12); do passthrough_ok && break; sleep 5; done
  retry_check "cluster serves Caddy's Let's Encrypt cert (passthrough, no decryption)" passthrough_ok || true
  retry_check "public home.$DOMAIN → Authentik login" public_home_gated || true
  retry_check "public Authentik admin → 403" public_admin_blocked || true
  pause
}

stage_cutover() {
  stage "Infomaniak: switch auth/paperless/jellyfin to public + apex redirect" || return 0
  if public_dns_cut_over; then say "Already cut over."; skip_offer && return; fi
  mx_snapshot
  warn "From now on peers rely on the NetBird zones (stage 9). Those checks passed, didn't they?"
  confirm "Delete the 6 A records now" || { FAILED+=("cutover postponed"); return; }
  open_url "https://manager.infomaniak.com/v3/hosting-services/domains"
  local d
  for d in "$DOMAIN" "$ALIAS"; do
    step "Domains → ${BOLD}$d${RESET} → DNS zone: delete ONLY the A records"
    step "  ${BOLD}auth${RESET}, ${BOLD}paperless${RESET}, ${BOLD}jellyfin${RESET} (target $NB_IP). The wildcard takes over."
    step "  Web redirection for ${BOLD}$d${RESET} → https://home.$DOMAIN:$(pub_port) (permanent)."
  done
  warn "Proton records on $ALIAS (MX, SPF, DKIM CNAMEs, DMARC, verification TXT): don't touch."
  note "If the web redirect asks to replace records, it may only replace apex A/AAAA, never MX/TXT."
  pause "Press Enter once done…"
  say "Checking public DNS (up to 10 minutes)…"
  for _ in $(seq 1 60); do public_dns_cut_over && break; sleep 10; done
  retry_check "auth/paperless/jellyfin public → cluster" public_dns_cut_over || true
  retry_check "MX $ALIAS unchanged (mail guard)" mx_same || true
  pause
}

stage_verify() {
  stage "End to end, from your phone" || return 0
  run_checks
  printf '\n'
  say "${BOLD}Phone on mobile data, NetBird OFF:${RESET}"
  local t
  for t in "$DOMAIN opens home.$DOMAIN:$(pub_port) → $BRAND-branded Authentik login → passkey/TOTP → tiles with green dots" \
           "The padlock's certificate is issued by Let's Encrypt (not Sectigo/ZeroSSL)" \
           "A test account (@$ALIAS) can sign up, gets the mail, must set up MFA, then sees \"request is in\"" \
           "You got a bell notification (and a mail) for that sign-up" \
           "After adding it to family (and a TOTP device), it sees tiles; Swiftfin signs in with password;code" \
           "That test account is refused by Paperless" \
           "https://auth.$DOMAIN:$(pub_port)/if/admin/ shows 403"; do
    confirm "$t?" || FAILED+=("phone: $t")
  done
  say "${BOLD}Phone with NetBird ON:${RESET}"
  for t in "auth.$DOMAIN/if/admin/ works (NetBird path)" \
           "Swiftfin and Plex play as before"; do
    confirm "$t?" || FAILED+=("phone: $t")
  done
  say "Real public client IPs Authentik saw recently (should include your phone's mobile IP):"
  docker compose logs --since 30m authentik-server 2>/dev/null | grep -o '"remote": "[^"]*"' \
    | sort -u | grep -vE '"(100\.|172\.|127\.)' | tail -n 5 | sed 's/^/    /' || true
  note "Then delete the test account: Directory → Users."
  pause
}

run_wizard() {

TOTAL_STAGES=13

if [[ ${1:-} == --check ]]; then
  run_checks
  (( ${#FAILED[@]} )) && { printf '\n'; warn "${#FAILED[@]} check(s) failed"; exit 1; }
  printf '\n  %sAll checks passed.%s\n\n' "$GREEN" "$RESET"; exit 0
fi

banner "Homelab: public access for $DOMAIN / $ALIAS"
stage_email
stage_mfa
stage_recovery
stage_enrollment
stage_family
stage_forward_auth
stage_jellyfin_ldap
stage_zones
stage_custom_domains
stage_wildcard_dns
stage_services
stage_cutover
stage_verify

for f in "${FAILED[@]}"; do SKIPPED+=("failed: $f"); done
finish
}

run_wizard "$@"; exit
