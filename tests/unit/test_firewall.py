"""The firewall role (ansible/roles/firewall): what the LAN may reach, rendered in tests/golden/full/firewall/, and the
mesh guard's behaviour in network namespaces (tests/firewall/guard.sh)."""
import json
import re
import shutil
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/golden/full"
ROLE = ROOT / "ansible/roles/firewall"
sys.path.insert(0, str(ROOT / "lib/setup"))
import engine  # noqa: E402


def example_value(key: str) -> str:
    return re.search(rf"^{key}=(.*)$", (ROOT / "tests/fixtures/full.env").read_text(), re.M).group(1)


class Firewall(unittest.TestCase):
    def test_every_published_port_is_on_the_netbird_ip(self):
        # The guard covers exactly the NetBird address: a port published anywhere else would bypass it
        compose = json.loads((GOLDEN / "compose.json").read_text())
        nb = example_value("NETBIRD_IP")
        for name, svc in compose["services"].items():
            for p in svc.get("ports", []):
                self.assertEqual(p.get("host_ip"), nb, f"{name} publishes {p.get('published')} on {p.get('host_ip')!r}")

    def test_bridges_are_docker0_or_br(self):
        # The guard lets in docker0 and br-*: a network with its own bridge name would be cut off
        compose = json.loads((GOLDEN / "compose.json").read_text())
        for name, net in compose.get("networks", {}).items():
            self.assertNotIn("com.docker.network.bridge.name", net.get("driver_opts") or {}, name)

    def test_lan_zone(self):
        zone = ET.parse(GOLDEN / "firewall/homelab-lan.xml").getroot()
        self.assertIsNone(zone.get("target"))  # default: refuse what isn't listed, ICMP kept
        services = [s.get("name") for s in zone.iter("service")]
        self.assertEqual(services[:3], ["dhcpv6-client", "mdns", "homelab-netbird"])
        allow = example_value("LAN_ALLOW").split()  # firewalld services and PORT/PROTO
        self.assertEqual(services[3:], [w for w in allow if "/" not in w])
        self.assertEqual([f"{p.get('port')}/{p.get('protocol')}" for p in zone.iter("port")], [w for w in allow if "/" in w])
        for refused in ("ssh", "plex", "http", "https"):
            self.assertNotIn(refused, services)
        for p in zone.iter("port"):
            self.assertNotIn("-", p.get("port"), "a port range on the LAN")
        # Matter (home-assistant is chosen): the devices' port 5540, IPv6 only
        rules = list(zone.iter("rule"))
        self.assertEqual([(r.get("family"), r.find("source-port").get("port")) for r in rules], [("ipv6", "5540")])

    def test_policy_refuses_before_docker_accepts(self):
        policy = ET.parse(GOLDEN / "firewall/homelab-no-route.xml").getroot()
        self.assertEqual(policy.get("target"), "REJECT")
        self.assertLess(int(policy.get("priority")), -1)  # docker-forwarding is -1
        self.assertEqual(policy.find("ingress-zone").get("name"), "homelab-lan")
        self.assertEqual(policy.find("egress-zone").get("name"), "ANY")

    def test_names_fit_older_firewalld(self):
        # Ubuntu 24.04's firewalld 2.1 limits zone and policy names to 17 characters
        for f in ("zones/homelab-lan", "policies/homelab-no-route", "services/homelab-netbird"):
            self.assertLessEqual(len(Path(f).name), 17, f)

    def test_wt0_trusted_before_the_default_zone_moves(self):
        # Otherwise the reload could drop wt0 into homelab-lan, which has no ssh: locked out over NetBird
        tasks = (ROLE / "tasks/main.yml").read_text()
        self.assertLess(tasks.index("zone: trusted"), tasks.index("--set-default-zone"))
        self.assertLess(tasks.index("zone: trusted"), tasks.index("notify: Reload firewalld"))

    def test_before_docker_in_the_playbook(self):
        roles = re.findall(r"role: (\w+)", (ROOT / "ansible/site.yml").read_text())
        self.assertLess(roles.index("firewall"), roles.index("docker"))

    def test_guard_unit_is_not_a_job(self):
        # A oneshot that stays active: live-safe.sh must count only running units, or the box is busy for good
        self.assertIn("RemainAfterExit=yes", (GOLDEN / "firewall/homelab-mesh-guard.service").read_text())
        self.assertIn("--state=activating,deactivating", (ROOT / "ansible/live-safe.sh").read_text())

    def test_lan_allow_values(self):
        opt = {"type": "firewall"}
        self.assertIsNone(engine.check_value(opt, ""))
        self.assertIsNone(engine.check_value(opt, "samba-client transmission-client 51413/tcp 1714-1764/udp"))
        self.assertIn("ssh;reboot", engine.check_value(opt, "mdns ssh;reboot") or "")
        self.assertIsNotNone(engine.check_value(opt, "51413"))

    @unittest.skipUnless(shutil.which("unshare") and shutil.which("nft"), "unshare or nft not installed")
    def test_guard_in_network_namespaces(self):
        if subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode != 0:
            self.skipTest("no unprivileged user namespaces here (CI runs tests/firewall/guard.sh with sudo)")
        r = subprocess.run(["bash", str(ROOT / "tests/firewall/guard.sh")], capture_output=True, text=True, timeout=120)
        if r.returncode == 2:
            self.skipTest(r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
