#!/usr/bin/env bash
# tests/firewall/guard.sh [GUARD.nft]: the firewall role's mesh guard in throwaway network namespaces,
# no root needed (a user namespace; CI runs it with sudo). A "box" with the NetBird address on wt0, a host
# listener on it and a container behind Docker-style DNAT; around it a LAN host that routes the address via
# the box, a NetBird peer, and the container itself. With the guard, the LAN host gets neither (before
# the DNAT too); the peer, the box itself and the container still do. IPv6 passes untouched.
# Default GUARD: tests/golden/full/firewall/homelab-mesh-guard.nft (the example instance, rendered by tests/golden.sh).
set -euo pipefail
cd "$(dirname "$0")/../.."
GUARD=$(realpath "${1:-tests/golden/full/firewall/homelab-mesh-guard.nft}")
for t in unshare nsenter ip nft curl python3; do
  command -v "$t" >/dev/null || { echo "· $t not installed: guard not tested"; exit 2; }
done
NB=$(sed -n 's/.*ip daddr \([0-9.]*\) .*/\1/p' "$GUARD")
[[ -n "$NB" ]] || { echo "✗ no 'ip daddr' in $GUARD" >&2; exit 1; }

if [[ ${GUARD_INSIDE:-} != 1 ]]; then
  # Our own network namespace (and user namespace unless root), where we may do anything
  [[ $EUID == 0 ]] && exec unshare -n env GUARD_INSIDE=1 "$0" "$GUARD"
  exec unshare -rn env GUARD_INSIDE=1 "$0" "$GUARD"
fi

pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT
# Three child namespaces, each held by a sleep (started here: a $(…) would wait for it to end)
unshare -n sleep 120 >/dev/null 2>&1 & LAN=$!; pids+=("$LAN")
unshare -n sleep 120 >/dev/null 2>&1 & PEER=$!; pids+=("$PEER")
unshare -n sleep 120 >/dev/null 2>&1 & CTR=$!; pids+=("$CTR")
sleep 0.2
inside() { local p=$1; shift; nsenter -t "$p" -n "$@"; }
serve() { local p=$1 addr=$2 port=$3   # an HTTP listener on addr:port inside namespace p ("" = here)
  if [[ -n $p ]]; then timeout 60 nsenter -t "$p" -n python3 -m http.server "$port" --bind "$addr" >/dev/null 2>&1 &
  else timeout 60 python3 -m http.server "$port" --bind "$addr" >/dev/null 2>&1 & fi
  pids+=($!)
}
code() { local p=$1 url=$2   # HTTP status from namespace p ("" = here), 000 = no answer
  if [[ -n $p ]]; then inside "$p" curl -s -g -o /dev/null -m 1 -w '%{http_code}' "$url" || true
  else curl -s -g -o /dev/null -m 1 -w '%{http_code}' "$url" || true; fi
}

# The box: lo, lan0 (LAN), wt0 (NetBird, the address), br-test (a compose network) with one container
ip link set lo up
ip link add lan0 type veth peer name eth0 netns "$LAN"
ip link add wt0 type veth peer name eth0 netns "$PEER"
ip link add br-test type bridge
ip link add veth-ctr type veth peer name eth0 netns "$CTR"
ip link set veth-ctr master br-test
ip addr add 172.17.22.122/24 dev lan0; ip -6 addr add fd00:22::1/64 dev lan0 nodad
ip addr add "$NB/10" dev wt0
ip addr add 172.30.0.1/24 dev br-test
for i in lan0 wt0 br-test veth-ctr; do ip link set "$i" up; done
sysctl -qw net.ipv4.ip_forward=1
inside "$LAN" sh -c "ip link set lo up; ip link set eth0 up; ip addr add 172.17.22.50/24 dev eth0
  ip -6 addr add fd00:22::50/64 dev eth0 nodad; ip route add $NB/32 via 172.17.22.122"   # routes the NetBird address via the box
inside "$PEER" sh -c "ip link set lo up; ip link set eth0 up; ip addr add 100.64.99.99/10 dev eth0"
inside "$CTR" sh -c "ip link set lo up; ip link set eth0 up; ip addr add 172.30.0.2/24 dev eth0; ip route add default via 172.30.0.1"

# A host listener on the NetBird address (sshd, Plex), and a published port: Docker's DNAT to the container
serve "" "$NB" 8022
serve "" "fd00:22::1" 8023
serve "$CTR" 172.30.0.2 8444
nft -f - <<NFT
table ip docker_like {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    ip daddr $NB tcp dport 8444 dnat to 172.30.0.2:8444
  }
  chain output {
    type nat hook output priority -100; policy accept;
    ip daddr $NB tcp dport 8444 dnat to 172.30.0.2:8444
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip daddr 172.30.0.2 tcp dport 8444 masquerade
  }
}
NFT
sleep 0.5

fails=0
want() { local what=$1 expect=$2 got=$3
  if [[ $got == "$expect" ]]; then echo "  ✓ $what: $got"; else echo "  ✗ $what: $got, expected $expect"; fails=$((fails + 1)); fi
}
echo "without the guard (the hole):"
want "LAN host → $NB:8022 (host listener)" 200 "$(code "$LAN" "http://$NB:8022/")"
want "LAN host → $NB:8444 (DNAT to a container)" 200 "$(code "$LAN" "http://$NB:8444/")"

nft -f "$GUARD"
echo "with the guard:"
want "LAN host → $NB:8022" 000 "$(code "$LAN" "http://$NB:8022/")"
want "LAN host → $NB:8444" 000 "$(code "$LAN" "http://$NB:8444/")"
want "NetBird peer → $NB:8022" 200 "$(code "$PEER" "http://$NB:8022/")"
want "NetBird peer → $NB:8444" 200 "$(code "$PEER" "http://$NB:8444/")"
want "the box itself → $NB:8022" 200 "$(code "" "http://$NB:8022/")"
want "the box itself → $NB:8444" 200 "$(code "" "http://$NB:8444/")"
want "a container → $NB:8022" 200 "$(code "$CTR" "http://$NB:8022/")"
want "LAN host → [fd00:22::1]:8023 (IPv6 untouched)" 200 "$(code "$LAN" "http://[fd00:22::1]:8023/")"
dropped=$(nft list table inet homelab_mesh_guard | sed -n 's/.*counter packets \([0-9]*\).*/\1/p')
want "packets the guard dropped > 0" yes "$([[ ${dropped:-0} -gt 0 ]] && echo yes || echo "no ($dropped)")"

if (( fails )); then echo "✗ mesh guard: $fails check(s) failed" >&2; exit 1; fi
echo "✓ mesh guard: the LAN can't reach $NB; NetBird, the box and its containers can"
