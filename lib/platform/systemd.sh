# ~/homelab/lib/platform/systemd.sh: ./homelab's job backend on Linux (systemd units from jobs/jobs.toml).
# shellcheck shell=bash

# plat_run UNIT POLKIT(true|false): start now, without waiting for it to finish
plat_run() {
  if [[ $2 == true ]]; then
    systemctl start --no-block "$1"        # the polkit rule lets the homelab user do this
  else
    echo "sudo systemctl start --no-block $1"
    sudo systemctl start --no-block "$1"
  fi
}

# plat_logs UNIT FOLLOW(0|1)
plat_logs() {
  local args=(-u "$1" -n 200 --no-pager -o short-iso)
  [[ $2 == 1 ]] && args+=(-f)
  journalctl "${args[@]}" || return
  if ! id -nG | grep -qwE 'systemd-journal|adm|wheel'; then
    echo "(Only part of the journal may be visible: sudo usermod -aG systemd-journal $USER, then sign in again.)" >&2
  fi
}

plat_status() { python3 lib/jobs.py status --env .env; }
