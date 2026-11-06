"""Ansible per distro family (plan Phase 4): every role that installs something has a task file per
family, the family variable files agree, site.yml loads them, Fedora keeps its exact package list."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "ansible/roles"
FAMILIES = ["RedHat", "Debian", "Archlinux"]


class Distros(unittest.TestCase):
    def test_task_files_per_family(self):
        for role in ("base", "firewall", "docker", "gpu", "netbird", "plex"):
            main = (ROLES / role / "tasks/main.yml").read_text()
            for fam in FAMILIES:
                f = ROLES / role / "tasks" / f"{fam}.yml"
                if f.exists():
                    self.assertIn(f"import_tasks: {fam}.yml", main, (role, fam))
                    self.assertIn(f'ansible_os_family == "{fam}"', main, (role, fam))
                else:
                    # A family without a file must be handled (refused) in main.yml itself
                    self.assertIn(f'ansible_os_family == "{fam}"', main, (role, fam))
        # Package tasks live only in the family files
        for role in ("base", "firewall", "docker", "gpu", "netbird", "plex"):
            main = (ROLES / role / "tasks/main.yml").read_text()
            self.assertNotRegex(main, r"ansible\.builtin\.(dnf5|apt):|community\.general\.pacman:", role)

    def test_family_vars(self):
        keys = None
        for fam in FAMILIES:
            text = (ROOT / "ansible/distro" / f"{fam}.yml").read_text()
            k = set(re.findall(r"^([a-z_]+):", text, re.M))
            keys = keys or k
            self.assertEqual(k, keys, fam)
        site = (ROOT / "ansible/site.yml").read_text()
        self.assertIn("distro/{{ ansible_os_family }}.yml", site)
        self.assertIn("homelab_selinux", site)

    def test_fedora_packages_unchanged(self):
        # The list the first box always had (it moved from base/tasks/main.yml to ansible/distro/RedHat.yml)
        text = (ROOT / "ansible/distro/RedHat.yml").read_text()
        pkgs = re.findall(r"^  - ([a-z0-9-]+)", text, re.M)
        self.assertEqual(pkgs, ["age", "zstd", "pass", "duperemove", "rsync", "git", "bind-utils", "policycoreutils-python-utils",
                                "jq", "curl", "unzip", "gh", "nodejs", "btrfs-progs", "xfsprogs", "ansible-core",
                                "ansible-collection-ansible-posix", "ansible-collection-community-general"])

    def test_selinux_only_where_on(self):
        for f in (ROLES / "timers/templates").glob("*.j2"):
            for m in re.finditer(r"^SELinuxContext=", f.read_text(), re.M):
                before = f.read_text()[:m.start()].rstrip().splitlines()[-1]
                self.assertEqual(before, "{% if homelab_selinux | default(true) %}", f.name)
        self.assertIn("when: homelab_selinux", (ROLES / "docker/tasks/main.yml").read_text())
        self.assertIn("when: homelab_selinux", (ROLES / "netbird/tasks/main.yml").read_text())

    def test_ssh_sso_fedora_only(self):
        self.assertIn('ansible_distribution == "Fedora"', (ROLES / "ssh/tasks/main.yml").read_text())  # not RHEL/Alma/Rocky
        self.assertIn('platforms = ["fedora"]', (ROOT / "setup/components/ssh-sso.toml").read_text())


if __name__ == "__main__":
    unittest.main()
