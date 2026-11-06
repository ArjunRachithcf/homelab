"""The tree that becomes the public repo carries nobody's own data: no e-mail address but the examples, no NetBird
address but the 100.64.0.0/16 examples, no disk serial, no UUID but made-up ones, no key. An instance's own files
live in its own repository (docs/instance/README.md). Names and domains are that repository's check: a public
list of them would publish them."""
import ipaddress
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid", "localhost")  # and their subdomains
EMAIL_OK = {"106335968+ArjunRachithcf@users.noreply.github.com",  # the project's commit identity
            "demo@dawarich.app"}                                   # Dawarich's own first user, which the wizard removes
NOT_EMAIL = re.compile(r"\.(png|svg|md|js|json|toml|ya?ml|txt|html|sh|py)$")  # icon@2x.png, @INSTANCE.md
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
NETBIRD = ipaddress.ip_network("100.64.0.0/10")
NETBIRD_EXAMPLES = ipaddress.ip_network("100.64.0.0/16")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
MADE_UP_UUID = re.compile(r"^0{8}-0{4}-4000-8000-0{9}[0-9a-f]{3}$")
BY_ID = re.compile(r"/dev/disk/by-id/([A-Za-z0-9_.:-]+)")
KEY = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|BEGIN CERTIFICATE|\bssh-(ed25519|rsa) AAAA|\bage1[0-9a-z]{50,}")


def public_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    mine = str(Path(__file__).resolve().relative_to(ROOT))  # its own bad examples
    return [ROOT / f for f in out if f != mine and (ROOT / f).is_file()]


def problems(text: str) -> list[str]:
    found = []
    for m in EMAIL.finditer(text):
        addr, domain = m.group(0), m.group(1).lower()
        if NOT_EMAIL.search(domain) or addr in EMAIL_OK or any(domain == d or domain.endswith("." + d) for d in EMAIL_DOMAINS):
            continue
        found.append(f"e-mail {addr}")
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip in NETBIRD and ip not in NETBIRD_EXAMPLES:
            found.append(f"NetBird address {ip}")
    found += [f"UUID {u}" for u in UUID.findall(text) if not MADE_UP_UUID.match(u.lower())]
    found += [f"disk {d}" for d in BY_ID.findall(text) if "EXAMPLE" not in d]
    found += [f"key or certificate ({m.group(0)})" for m in KEY.finditer(text)]
    return found


class PublicTree(unittest.TestCase):
    def test_nobodys_own_data(self):
        bad = []
        for f in public_files():
            try:
                text = f.read_text()
            except UnicodeDecodeError:
                continue  # images, fonts
            bad += [f"{f.relative_to(ROOT)}: {p}" for p in problems(text)]
        self.assertEqual(bad, [], "replace with the documented examples (example.org, 100.64.0.x, EXAMPLE_…)")

    def test_the_projects_address_is_in_one_place(self):
        # setup/project.toml; docs may repeat it, and the files GitHub and REUSE read can't read it
        with open(ROOT / "setup/project.toml", "rb") as f:
            repo = tomllib.load(f)["repo"].split("://", 1)[1]
        allowed = {"setup/project.toml", "REUSE.toml", ".github/ISSUE_TEMPLATE/config.yml"}
        where = {str(f.relative_to(ROOT)) for f in public_files() if f.suffix != ".md" and repo in f.read_text(errors="replace")}
        self.assertEqual(where - allowed, set())

    def test_the_check_finds_them(self):
        self.assertEqual(problems("mail me@home.example.org or a@example.net, icon@2x.png, @INSTANCE.md"), [])
        self.assertEqual(problems("NETBIRD_IP=100.64.0.10, the resolver 9.9.9.9, a version 3.3.1.0"), [])
        self.assertEqual(problems("00000000-0000-4000-8000-000000000001 /dev/disk/by-id/nvme-EXAMPLE_SSD_0001"), [])
        self.assertEqual(problems("NOTIFY_TO=someone@somewhere.ch"), ["e-mail someone@somewhere.ch"])
        self.assertEqual(problems("NETBIRD_IP=100.101.102.103"), ["NetBird address 100.101.102.103"])
        self.assertEqual(problems("PHOTOS_UUID=9f0e1d2c-3b4a-4596-8778-695a4b3c2d1e"),
                         ["UUID 9f0e1d2c-3b4a-4596-8778-695a4b3c2d1e"])
        self.assertEqual(problems("/dev/disk/by-id/nvme-Some_Disk_S3R1AL"), ["disk nvme-Some_Disk_S3R1AL"])
        self.assertEqual(len(problems("-----BEGIN OPENSSH PRIVATE KEY-----")), 1)


if __name__ == "__main__":
    unittest.main()
