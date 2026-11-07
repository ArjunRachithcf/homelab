# Platforms

| | Fedora | Debian 13 / Ubuntu 24.04 | Arch | macOS (best effort) |
|---|---|---|---|---|
| Host playbook | `ansible/site.yml` | `ansible/site.yml` | `ansible/site.yml` | `ansible/site-macos.yml` |
| Packages | dnf5 + Docker, NetBird, Plex, NVIDIA repos | apt + the same vendors' repos | pacman (NetBird from the AUR first) | Homebrew, Docker Desktop |
| Updates | dnf5-automatic, security only | unattended-upgrades | by hand (`pacman -Syu`) | by hand |
| SELinux | enforcing; labels, ports, unit contexts | not used (AppArmor) | not used | n/a |
| Jobs | systemd timers | systemd timers | systemd timers | launchd agents |
| Local AI | Ollama container, NVIDIA via CDI | same | same | Ollama native (Metal), `OLLAMA_URL` |
| Data folders | `/srv/…` | `/srv/…` | `/srv/…` | `~/homelab-data/…` |
| Tested | CI (packages), one real instance | CI (packages); base + Docker by hand on Ubuntu | CI (packages) | CI (generation, syntax) |

## Not everywhere

Each component lists where it runs (`platforms` in `setup/components/<id>.toml`; [components.md](components.md)),
and `./homelab setup --plan` refuses one that doesn't run on the machine (`--platform` plans for another).

- **SSH with your sign-in** (`ssh-sso`, and the web terminal that needs it): Fedora only. It changes PAM for
  sshd, written for Fedora's `/etc/pam.d/sshd`; a wrong PAM change locks people out, so other distros keep their
  own sshd until someone tests theirs.
- **Plex**: not on Arch (AUR only) or macOS here; Jellyfin plays the same library.
- **macOS**: no Home Assistant (needs the host network), AdGuard, the downloads (gluetun's kernel WireGuard), the
  second disk, the Proton copy, or the jobs that need btrfs/XFS (`macos = false` in `jobs/jobs.toml`). No
  `firewall` role: macOS's own firewall (System Settings → Network → Firewall) is yours to set.

## Distro notes

- **Debian/Ubuntu**: NetworkManager is needed for Home Assistant's Thread routes (Ubuntu Server uses netplan
  with systemd-networkd by default; the playbook says so if it's missing or too old).
- **Arch**: install NetBird from the AUR before the playbook (`yay -S netbird-bin`).
- **NVIDIA**: install the driver yourself (Fedora: akmod-nvidia from RPM Fusion; Debian/Ubuntu: nvidia-driver;
  Arch: nvidia); the playbook adds the container toolkit and the CDI spec.
- **macOS**: install Docker Desktop and start it once before the playbook; the Mac must not sleep (`pmset`).
  The scripts need bash 4.4+: Homebrew's (`brew install bash`; the playbook and `setup/bootstrap.sh` install it).
  macOS's own `/bin/bash` is 3.2; `./homelab`, the wizards and the jobs switch to Homebrew's by themselves
  (`lib/need-bash.sh`), and `./homelab doctor` checks it.
