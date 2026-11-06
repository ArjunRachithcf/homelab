# ~/homelab/lib/platform/launchd.sh: ./homelab's job backend on macOS (plan Phase 5). The jobs are launchd
# agents org.homelab.<job> (lib/jobs.py plists, installed by ansible/site-macos.yml); each runs
# lib/platform/launchd-run.sh, which logs to ~/Library/Logs/homelab/org.homelab.<job>.log.
# shellcheck shell=bash

_launchd_label() { echo "org.homelab.${1#homelab-}" | sed 's/\.service$//'; }
plat_run() { launchctl kickstart "gui/$(id -u)/$(_launchd_label "$1")" && echo "started $(_launchd_label "$1")"; }
plat_logs() {
  local log; log="$HOME/Library/Logs/homelab/$(_launchd_label "$1").log"
  [[ -f $log ]] || { echo "no log yet: $log (it starts with the job's first run)" >&2; return 1; }
  if [[ $2 == 1 ]]; then tail -n 200 -f "$log"; else tail -n 200 "$log"; fi
}
plat_status() { python3 lib/jobs.py status --env .env; }
