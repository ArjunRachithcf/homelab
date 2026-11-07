"""The firewall role (ansible/roles/firewall): what the LAN may reach, rendered in tests/golden/full/firewall/, the
mesh guard's behaviour in network namespaces (tests/firewall/guard.sh), and the step that applies it
(hardening:firewall), run in a copy with its commands stubbed (DOCKER_HOST to nowhere)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


# The step's commands, each logging what it was asked to $STUB_LOG. The role "applies" (a real ansible-playbook run,
# not --check) by leaving $STATE/applied, which the firewalld and systemd stand-ins then report.
STEP_STUBS = {
    "netbird": '#!/bin/sh\nif [ -n "$NB_DOWN" ]; then echo "Management: Disconnected"; else echo "Management: Connected"; fi\n',
    "firewall-cmd": '#!/bin/sh\necho "firewall-cmd $*" >> "$STUB_LOG"\ncase "$*" in\n'
                    '  --get-services) echo "mdns samba-client ssh transmission-client" ;;\n'
                    '  --get-default-zone) if [ -e "$STATE/applied" ]; then echo homelab-lan; else echo FedoraWorkstation; fi ;;\n'
                    '  --get-zone-of-interface=wt0) echo trusted ;;\nesac\n',
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n'
                 'case "$*" in *homelab-mesh-guard*) [ -e "$STATE/applied" ] ;;\n'
                 '  *homelab-firewall-undo*) [ -n "$UNDO_ACTIVE" ] ;; *) exit 3 ;; esac\n',
    "ansible-playbook": '#!/bin/sh\necho "ansible-playbook $*" >> "$STUB_LOG"\n'
                        'case "$*" in *--check*) ;; *) : > "$STATE/applied" ;; esac\n'
                        'echo "PLAY RECAP"\necho "localhost : ok=30 changed=${STUB_CHANGED:-4} unreachable=0 failed=0"\n',
    # never runs what it's given: only test -e (the copy of /etc/firewalld) says "not there"
    "sudo": '#!/bin/sh\necho "sudo $*" >> "$STUB_LOG"\ncase "$*" in "test -e "*) exit 1 ;; esac\nexit 0\n',
    "ss": "#!/bin/sh\necho 'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*'\necho 'LISTEN 0 4096 127.0.0.1:631 0.0.0.0:*'\n",
    "ip": '#!/bin/sh\ncase "$*" in\n  "route show default") echo "default via 192.0.2.1 dev eth9 proto dhcp" ;;\n'
          '  *"addr show dev eth9"*) echo "2: eth9    inet 192.0.2.10/24 brd 192.0.2.255 scope global eth9" ;;\nesac\n',
    "curl": "#!/bin/sh\nprintf 302\n",
    "uname": "#!/bin/sh\necho Linux\n",
}


def firewall_step(answers: list[str], *, ssh: bool = False, applied: bool = False, changed: int = 4,
                  nb_down: bool = False, undo_active: bool = False) -> tuple[list[dict], list[str], str]:
    """hardening:firewall under the protocol: its messages, the commands it ran, and .env afterwards."""
    with tempfile.TemporaryDirectory() as d:
        box = Path(d) / "box"
        (box / "ansible").mkdir(parents=True)
        shutil.copy(ROOT / "hardening-wizard.sh", box)
        shutil.copytree(ROOT / "lib", box / "lib")
        (box / "ansible/live-safe.sh").write_text("exit 0\n")
        (box / ".env").write_text("DOMAIN=example.org\nDOMAIN_ALIAS=example.net\nNETBIRD_IP=100.64.0.5\n"
                                  "COMPONENTS=core,mesh\nLAN_ALLOW='samba-client'\n")
        bin_, state = Path(d) / "bin", Path(d) / "state"
        bin_.mkdir()
        state.mkdir()
        if applied:
            (state / "applied").touch()
        for name, body in STEP_STUBS.items():
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        log = Path(d) / "calls.log"
        env = {k: v for k, v in os.environ.items() if k not in ("SSH_CONNECTION", "SSH_CLIENT", "NB_DOWN", "UNDO_ACTIVE")}
        env.update(PATH=f"{bin_}:{os.environ['PATH']}", HOME=d, USER="admin", HOMELAB_UI="protocol",
                   HOMELAB_STAGES="firewall", HOMELAB_STAGES_TOTAL="1", TERM="dumb", STUB_LOG=str(log),
                   STATE=str(state), STUB_CHANGED=str(changed), DOCKER_HOST="unix:///nonexistent")
        if ssh:
            env["SSH_CONNECTION"] = "192.0.2.20 50000 100.64.0.5 22"
        if nb_down:
            env["NB_DOWN"] = "1"
        if undo_active:
            env["UNDO_ACTIVE"] = "1"
        # the first line answers the wizard's "Ready to start?"
        r = subprocess.run(["bash", str(box / "hardening-wizard.sh")], input="".join(a + "\n" for a in ["", *answers]),
                           capture_output=True, text=True, env=env, timeout=60)
        msgs = [json.loads(x[1:]) for x in r.stdout.split("\n") if x.startswith("\x1e")]  # splitlines() cuts at \x1e
        calls = log.read_text().splitlines() if log.exists() else []
        return msgs, calls, (box / ".env").read_text()


def asked(msgs: list[dict], kind: str) -> list[str]:
    return [m.get("text") or m.get("prompt") or "" for m in msgs if m["t"] == kind]


def said(msgs: list[dict]) -> str:
    return "\n".join(m.get("text", "") for m in msgs if m["t"] in ("say", "output", "check"))


DRY_RUN = "ansible-playbook -K ansible/site.yml --tags firewall --check --diff"
REAL_RUN = "ansible-playbook -K ansible/site.yml --tags firewall"
UNDO = ("sudo systemd-run --collect --unit=homelab-firewall-undo --on-active=15min /bin/sh -c "
        "firewall-cmd --set-default-zone=FedoraWorkstation; systemctl stop homelab-mesh-guard")


class Step(unittest.TestCase):
    def test_netbird_first(self):
        msgs, calls, _ = firewall_step([], nb_down=True)
        self.assertIn("NetBird isn't connected", said(msgs))
        self.assertFalse([c for c in calls if c.startswith(("ansible-playbook", "sudo"))], calls)
        self.assertEqual(asked(msgs, "input"), [])

    def test_no_after_the_dry_run_changes_nothing(self):
        msgs, calls, env = firewall_step(["samba-client transmission-client", "n"])
        self.assertEqual([c for c in calls if c.startswith("ansible-playbook")], [DRY_RUN])
        self.assertFalse([c for c in calls if c.startswith("sudo")], calls)
        self.assertIn("Make the change now?", asked(msgs, "confirm"))
        self.assertIn("22/tcp", said(msgs))      # what the LAN reaches today; not the loopback's 631
        self.assertNotIn("631", said(msgs))
        self.assertIn("LAN_ALLOW='samba-client transmission-client'", env)

    def test_a_wrong_name_is_asked_again(self):
        msgs, _, env = firewall_step(["smb", "samba-client 8080/tcp", "n"])
        self.assertEqual(len(asked(msgs, "input")), 2)
        self.assertIn("smb is neither a firewalld service", said(msgs))
        self.assertIn("LAN_ALLOW='samba-client 8080/tcp'", env)

    def test_over_ssh_the_undo_timer_goes_on_first_and_off_after_a_new_login(self):
        # LAN_ALLOW kept (Enter), make the change, the new login works, no check from the LAN, no logging
        msgs, calls, _ = firewall_step(["", "y", "y", "n", "n"], ssh=True)
        self.assertIn("sudo cp -a /etc/firewalld /root/firewalld.before-homelab", calls)
        undo, real, off = calls.index(UNDO), calls.index(REAL_RUN), calls.index("sudo systemctl stop homelab-firewall-undo.timer")
        self.assertLess(calls.index(DRY_RUN), undo)
        self.assertLess(undo, real)
        self.assertLess(real, off)
        self.assertIn({"t": "check", "label": "LAN closed: zone homelab-lan, wt0 in trusted, the mesh guard on", "ok": True},
                      [{k: m[k] for k in ("t", "label", "ok")} for m in msgs if m["t"] == "check"])

    def test_at_the_console_no_undo_timer_and_two_days_of_logging(self):
        msgs, calls, _ = firewall_step(["", "y", "n", "y"])
        self.assertIn(REAL_RUN, calls)
        self.assertFalse([c for c in calls if c.startswith("sudo") and "homelab-firewall-undo" in c], calls)
        self.assertIn("sudo firewall-cmd --set-log-denied=unicast", calls)
        self.assertIn("sudo systemd-run --collect --unit=homelab-firewall-log-off --on-active=48h "
                      "/usr/bin/firewall-cmd --set-log-denied=off", calls)

    def test_an_undo_timer_left_from_an_earlier_run_is_taken_over(self):
        # At the console, but a timer from an earlier run (over SSH) would still undo the change: the login check
        # turns it off
        msgs, calls, _ = firewall_step(["", "y", "y", "n", "n"], undo_active=True)
        self.assertFalse([c for c in calls if c.startswith("sudo systemd-run") and "undo" in c], calls)
        self.assertIn("Does the new login work?", asked(msgs, "confirm"))
        self.assertIn("sudo systemctl stop homelab-firewall-undo.timer", calls)

    def test_no_answer_stops_cleanly(self):
        # The front end went away at the question (end of input): no change, and the wizard ends normally
        msgs, calls, _ = firewall_step([])
        self.assertFalse([c for c in calls if c.startswith(("ansible-playbook", "sudo"))], calls)
        self.assertEqual(msgs[-1]["t"], "finish")

    def test_nothing_to_change_asks_nothing_more(self):
        msgs, calls, _ = firewall_step([""], applied=True, changed=0)
        self.assertIn("Nothing to change", said(msgs))
        self.assertEqual([c for c in calls if c.startswith("ansible-playbook")], [DRY_RUN])
        self.assertEqual(asked(msgs, "confirm"), [])


if __name__ == "__main__":
    unittest.main()
