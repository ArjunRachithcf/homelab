# ~/homelab/lib/need-bash.sh: sourced first by the entry points (./homelab, lib/wizard.sh,
# lib/platform/launchd-run.sh). The scripts use bash 4.4+ (mapfile, associative arrays, ${a[-1]},
# ${x,,}); macOS ships bash 3.2, so there it's Homebrew's (ansible/site-macos.yml installs it). Under an
# older bash this re-runs the calling script with Homebrew's, or stops with what to install. Source it
# before any cd (it re-runs "$0"). Keep it bash 3.2 syntax: it's the one file that must run there.
# shellcheck shell=bash
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  for _b in /opt/homebrew/bin/bash /usr/local/bin/bash; do
    if [[ -x $_b ]] && "$_b" -c '(( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) ))'; then
      exec "$_b" "$0" ${1+"$@"}   # ${1+…}: bash 3.2 calls an empty "$@" unbound under set -u
    fi
  done
  echo "✗ bash $BASH_VERSION is too old for the homelab's scripts (4.4 or newer). macOS: brew install bash" >&2
  exit 1
fi
