# ~/homelab/lib/wizard-checks.sh: checks and helpers the *-wizard.sh share (sourced by lib/wizard.sh).
# shellcheck shell=bash
# They read the wizard's DOMAIN, ALIAS (DOMAIN_ALIAS) and NB_IP (NETBIRD_IP), which each wizard sets
# from .env after sourcing the library, and ENV_FILE. A wizard's own check stays in the wizard.
# shellcheck source=lib/firewall.sh
source "$(dirname "${BASH_SOURCE[0]}")/firewall.sh"   # lan_closed (the firewall role)

# ── The NetBird reverse proxy (public path) ──
pub_port()   { local p; p=$(_existing NETBIRD_PROXY_PORT || true); printf '%s' "${p:-9443}"; }
cluster()    { _existing NETBIRD_PROXY_CLUSTER; }
cluster_ip() { dig +short A "$(cluster)" @1.1.1.1 | grep -E '^[0-9.]+$' | head -n1; }
# pub_code NAME PATH: HTTP status of https://NAME.<domain>:<port>PATH through the proxy cluster
pub_code() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || { echo 000; return; }
  curl -s -o /dev/null -m 10 -w '%{http_code}' --resolve "$1.$DOMAIN:$(pub_port):$ip" "https://$1.$DOMAIN:$(pub_port)$2"
}
# pub_ok NAME: public through the proxy (TLS passthrough: Caddy's Let's Encrypt certificate) and answering
# (200, or a redirect, e.g. to the app's sign-in)
pub_ok() {
  local ip; ip=$(cluster_ip); [[ -n "$ip" ]] || return 1
  echo | openssl s_client -connect "$ip:$(pub_port)" -servername "$1.$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | grep -q "Let's Encrypt" || return 1
  [[ "$(pub_code "$1" /)" =~ ^(200|30[2378])$ ]]
}
# public_dns_ok NAME: NAME.<domain> and NAME.<alias> are the wildcard CNAME to the proxy cluster
public_dns_ok() {
  local d c; c=$(cluster); [[ -n "$c" ]] || return 1
  for d in "$DOMAIN" "$ALIAS"; do dig +short "$1.$d" @1.1.1.1 | head -n1 | grep -qi "^${c%.}\.\?$" || return 1; done
}

# ── NetBird peers (private DNS zones, Caddy on the NetBird IP) ──
# resolves_to NAME IP: IP is among NAME's IPv4 answers through this machine's resolver (on a peer: NetBird's
# DNS), exactly; libc's lookup, so also without systemd-resolved
resolves_to() {
  python3 -c 'import socket, sys
try:
    sys.exit(sys.argv[2] not in {a[4][0] for a in socket.getaddrinfo(sys.argv[1], None, socket.AF_INET)})
except OSError:
    sys.exit(1)' "$1" "$2"
}
# zone_ok NAME…: each NAME.<domain> and NAME.<alias> resolves to this PC's NetBird IP (its DNS zone)
zone_ok() {
  local d h
  for d in "$DOMAIN" ${ALIAS:+"$ALIAS"}; do for h in "$@"; do
    resolves_to "$h.$d" "$NB_IP" || return 1
  done; done
}
# code NAME PATH [curl args…]: HTTP status of https://NAME.<domain>:<port>PATH, straight to Caddy
code() { local n=$1 p=$2; shift 2
  curl -s -o /dev/null -m 15 -w '%{http_code}' "$@" --resolve "$n.$DOMAIN:$(pub_port):$NB_IP" "https://$n.$DOMAIN:$(pub_port)$p"; }
# body NAME PATH [curl args…]: the same, the response body
body() { local n=$1 p=$2; shift 2
  curl -s -m 15 "$@" --resolve "$n.$DOMAIN:$(pub_port):$NB_IP" "https://$n.$DOMAIN:$(pub_port)$p"; }
answers()  { [[ "$(code "$1" "$2")" =~ ^(200|204)$ ]]; }
oidc_app() { [[ "$(code auth "/application/o/$1/.well-known/openid-configuration")" == 200 ]]; }
# code_443 HOST PATH: the same on 443 (the peers' listener without a port in the URL)
code_443() { curl -s -o /dev/null -m 10 -w '%{http_code}' --resolve "$1:443:$NB_IP" "https://$1$2"; }
enrollment_ok() { [[ "$(code_443 "auth.$DOMAIN" /api/v3/flows/executor/default-enrollment-flow/)" == 200 ]]; }
recovery_ok()   { [[ "$(code_443 "auth.$DOMAIN" /api/v3/flows/executor/default-recovery-flow/)" == 200 ]]; }
# Proton Mail's MX on the alias domain, as a NetBird peer resolves it (the zones must not hide it)
mx_via_peer() { resolvectl query --legend=no -t MX "$ALIAS" 2>/dev/null | grep -qi protonmail; }

# ── This box ──
running() { [[ -n "$(docker compose ps --status running -q "$1")" ]]; }
# backup_section_ok TITLE BAD: the newest homelab-backup run has the section "TITLE…" (backup.sh's unindented
# lines) and none of its lines matches BAD (ERE). Only that run: a count of journal lines reaches back into
# earlier runs, whose "not created yet" outlive the fix. The log is read once into a variable: grep -q on a
# journalctl pipe kills it with SIGPIPE (141 under pipefail)
backup_section_ok() {
  local id log
  id=$(systemctl show -p InvocationID --value homelab-backup.service 2>/dev/null) && [[ -n $id ]] || return 1
  log=$(journalctl _SYSTEMD_INVOCATION_ID="$id" --no-pager -o cat 2>/dev/null) || return 1
  grep -qxF "$1…" <<<"$log" || return 1
  ! awk -v t="$1…" '$0 == t { on = 1; next } /^[^ ]/ { on = 0 } on' <<<"$log" | grep -qE "$2"
}
# gen KEY "openssl …": write a generated secret into .env unless it's there (never shown)
gen() {
  local key=$1 cmd=$2
  [[ -n "$(_existing "$key" || true)" ]] && { note "$key already in .env"; return 0; }
  write_env "$key" "$(bash -c "$cmd")" >/dev/null
  printf '  %s✓ generated%s %s → %s\n' "$GREEN" "$RESET" "$key" "$ENV_FILE"
}
# The homelab-* units (ansible/roles/timers), installing Ansible first if needed
ansible_timers() {
  command -v ansible-playbook >/dev/null \
    || install_pkgs ansible-core ansible-collection-ansible-posix ansible-collection-community-general
  ansible-playbook -K ansible/site.yml --tags timers
}
