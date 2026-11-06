# ~/homelab/lib/firewall.sh: is the LAN closed the way the firewall role leaves it? Sourced by
# heartbeat.sh and lib/wizard-checks.sh. Only firewalld's runtime queries, which any user may make.
# shellcheck shell=bash

# The role has run on this machine (its unit file is world-readable; /etc/firewalld isn't)
lan_firewall_here() { [[ -e /etc/systemd/system/homelab-mesh-guard.service ]]; }

# What's wrong, one line each; nothing when the LAN is closed
lan_problems() {
  local zone
  zone=$(firewall-cmd --get-default-zone 2>/dev/null) || { echo "firewalld isn't running"; return; }
  [[ $zone == homelab-lan ]] || echo "the default zone is $zone, not homelab-lan"
  [[ "$(firewall-cmd --get-zone-of-interface=wt0 2>/dev/null)" == trusted ]] || echo "wt0 isn't in zone trusted"
  systemctl is-active -q homelab-mesh-guard || echo "homelab-mesh-guard isn't active"
}

lan_closed() { [[ -z "$(lan_problems)" ]]; }
