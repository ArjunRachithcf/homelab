#!/usr/bin/env bash
# ~/homelab/setup/bootstrap.sh: from a fresh machine to `./homelab setup` (plan Phase 3b).
# The setup app runs it (over SSH or locally) when the machine has no homelab yet; by hand:
#
#   curl -fsSLO https://raw.githubusercontent.com/<owner>/homelab/<tag>/setup/bootstrap.sh
#   bash bootstrap.sh --repo https://github.com/<owner>/homelab.git --ref <tag> [--commit <sha>]
#
#   --repo URL      the git repository (https; file:// for a local mirror)
#   --ref REF       a tag or branch to check out (default: main)
#   --commit SHA    refuse unless the checkout is exactly this commit (the setup app passes the
#                   commit its release was built from, so a moved tag can't hand it other code)
#   --dir DIR       where (default: ~/homelab); an existing checkout there is left alone
#   --sudo-stdin    the first line of stdin is the sudo password (the setup app: no terminal over
#                   its SSH channel); it stays in this shell and only ever goes to `sudo -S`
#
# Installs what it needs to go on (git, python3 ≥ 3.11) with the distro's package manager, nothing
# else; Docker, NetBird and the rest come later from Ansible (./homelab setup). Prints one line
# `HOMELAB_DIR=<dir>` at the end for the app.
set -euo pipefail

repo="" ref="main" commit="" dir="$HOME/homelab" sudo_stdin=0
while (( $# )); do
  case "$1" in
    --repo) repo=$2; shift 2 ;;
    --ref) ref=$2; shift 2 ;;
    --commit) commit=$2; shift 2 ;;
    --dir) dir=$2; shift 2 ;;
    --sudo-stdin) sudo_stdin=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "bootstrap: unknown option $1" >&2; exit 2 ;;
  esac
done
[[ "$repo" == https://* || "$repo" == file://* ]] || { echo "bootstrap: --repo must be an https URL" >&2; exit 2; }
[[ "$ref" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "bootstrap: odd --ref" >&2; exit 2; }
[[ -z "$commit" || "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "bootstrap: --commit must be a full SHA-1" >&2; exit 2; }

pw=""
if (( sudo_stdin )); then IFS= read -r pw || true; fi
as_root() {
  if (( EUID == 0 )); then "$@"
  elif (( sudo_stdin )); then printf '%s\n' "$pw" | sudo -S -p '' "$@"
  else sudo "$@"
  fi
}

# ── The distro ──
os=$(uname -s) id="" like=""
if [[ -r /etc/os-release ]]; then
  # shellcheck source=/dev/null
  id=$(. /etc/os-release; echo "${ID:-}")
  # shellcheck source=/dev/null
  like=$(. /etc/os-release; echo "${ID_LIKE:-}")
fi
family=""
case " $id $like " in
  *" fedora "*|*" rhel "*|*" centos "*) family=fedora ;;
  *" debian "*|*" ubuntu "*) family=debian ;;
  *" arch "*) family=arch ;;
esac
[[ "$os" == Darwin ]] && family=macos
echo "• This machine: ${id:-$os} (${family:-unknown family})"
[[ -n "$family" ]] || { echo "✗ Not a distro this homelab knows (Fedora, Debian/Ubuntu, Arch, macOS)." >&2; exit 1; }

py_ok() { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; }
need=()
command -v git >/dev/null || need+=(git)
py_ok || need+=(python3)
command -v curl >/dev/null || need+=(curl)
# The scripts need bash 4.4+; macOS's own is 3.2 (lib/need-bash.sh then switches to Homebrew's)
bash -c '(( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) ))' 2>/dev/null \
  || [[ -x /opt/homebrew/bin/bash || -x /usr/local/bin/bash ]] || need+=(bash)
if (( ${#need[@]} )); then
  echo "• Installing: ${need[*]}"
  case "$family" in
    fedora) as_root dnf -y -q install "${need[@]}" ;;
    debian) as_root apt-get update -qq && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" ;;
    arch) as_root pacman -Sy --noconfirm --needed "${need[@]/python3/python}" ;;
    macos)
      command -v brew >/dev/null || { echo "✗ Needs Homebrew first: https://brew.sh" >&2; exit 1; }
      brew install "${need[@]/python3/python@3.13}" ;;
  esac
fi
py_ok || { echo "✗ python3 is older than 3.11 here; the homelab needs 3.11+ (tomllib)." >&2; exit 1; }
echo "✓ git $(git --version | awk '{print $3}'), $(python3 --version)"

# ── The checkout ──
if [[ -d "$dir/.git" ]]; then
  echo "✓ $dir is already a checkout ($(git -C "$dir" rev-parse --short HEAD)); left as it is"
  [[ -x "$dir/homelab" ]] || { echo "✗ but it has no ./homelab: not this repository?" >&2; exit 1; }
elif [[ -e "$dir" && -n "$(ls -A "$dir" 2>/dev/null)" ]]; then
  echo "✗ $dir exists and isn't empty; pass --dir" >&2; exit 1
else
  echo "• Cloning $repo ($ref) into $dir"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$ref" -- "$repo" "$dir"
  if [[ -n "$commit" ]]; then
    got=$(git -C "$dir" rev-parse HEAD)
    if [[ "$got" != "$commit" ]]; then
      echo "✗ $ref is $got, not the expected $commit: removed again, nothing runs from it" >&2
      rm -rf -- "$dir"; exit 1
    fi
    echo "✓ $ref is commit $commit, as expected"
  fi
fi
pw=""
echo "✓ Ready. Next: cd $dir && ./homelab setup"
echo "HOMELAB_DIR=$dir"
