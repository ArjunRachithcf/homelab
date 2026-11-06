# ~/homelab/lib/portable.sh: GNU vs BSD differences, sourced by ./homelab (plan Phase 2).
# On macOS it also puts Homebrew's GNU tools first in PATH (coreutils, gnu-sed, findutils, util-linux's
# flock), so the scripts' GNU options work; `./homelab doctor` says which are missing.
# shellcheck shell=bash

HOMELAB_OS=$(uname -s)
if [[ $HOMELAB_OS == Darwin ]] && command -v brew >/dev/null; then
  for _pkg in coreutils gnu-sed findutils; do
    _p="$(brew --prefix "$_pkg" 2>/dev/null)/libexec/gnubin"
    [[ -d $_p ]] && PATH="$_p:$PATH"
  done
  _p="$(brew --prefix util-linux 2>/dev/null)/bin"
  [[ -d $_p ]] && PATH="$_p:$PATH"
  unset _pkg _p
  export PATH
fi

# python3 3.11+ in PATH (lib/jobs.py and friends need tomllib): an activated conda/mamba env or venv with
# an older python3 would otherwise shadow the system's. Its folder goes just ahead of the older python3's,
# not to the front: what comes earlier (a test's stub docker, say) must keep winning over /usr/bin.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  _old=$(command -v python3 2>/dev/null || true); _old=${_old%/*}
  for _py in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if [[ -x $_py ]] && "$_py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      _p=":$PATH:"
      if [[ -n $_old && $_p == *":$_old:"* ]]; then _p=${_p/":$_old:"/":${_py%/*}:$_old:"}; else _p=":${_py%/*}$_p"; fi
      _p=${_p#:}; PATH=${_p%:}; export PATH; break
    fi
  done
  unset _py _old _p
fi

# p_sed_i EXPR FILE…: sed -i on GNU and BSD sed
p_sed_i() { if sed --version >/dev/null 2>&1; then sed -i "$@"; else sed -i '' "$@"; fi; }
# p_mtime FILE: modification time in epoch seconds
p_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }
# p_realpath PATH: absolute path with symlinks resolved
p_realpath() { readlink -f "$1" 2>/dev/null || python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
# p_epoch "YYYY-MM-DD HH:MM:SS": epoch seconds (local time)
p_epoch() { date -d "$1" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$1" +%s; }
# p_have CMD…: all present?
p_have() { local c; for c in "$@"; do command -v "$c" >/dev/null || return 1; done; }
