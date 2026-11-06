#!/usr/bin/env bash
# ~/homelab/ansible/live-safe.sh <home of the homelab user> [wait seconds]: exit 0 when the box is
# idle enough for a real Ansible run, 1 (with the reason) when it isn't. Used by site.yml's
# pre_tasks and as ExecCondition of homelab-drift.service, which waits up to 30 min (the backup
# watcher runs every 5 min) and then skips instead of failing.
set -uo pipefail
home=${1:?usage: live-safe.sh <home> [wait seconds]}
deadline=$(( $(date +%s) + ${2:-0} ))
# shellcheck source=../lib/portable.sh
source "$(dirname "$0")/../lib/portable.sh"   # a python3 with tomllib (lib/jobs.py)

busy() {

hm=$(date +%H%M)
if (( 10#$hm >= 200 && 10#$hm < 530 )); then echo "busy: nightly window 02:00–05:30"; return 0; fi

# A job is a oneshot, so it's "activating" while it runs. Oneshots with RemainAfterExit (the mesh guard)
# stay "active (exited)" for good and must not make the box look busy forever.
active=$(systemctl list-units 'homelab-*.service' --state=activating,deactivating --no-legend --plain | awk '$1 !~ /^homelab-(drift|heartbeat)/ {print $1}')
if [[ -n "$active" ]]; then echo "busy: ${active//$'\n'/ } running"; return 0; fi

if pgrep -x proton-drive >/dev/null; then echo "busy: a proton-drive process is running"; return 0; fi

# The scripts run by hand hold these locks too (backup-photos.sh also between proton-drive calls)
# The locks come from the job registry; can't read it = assume busy rather than run into a job
locks=$(python3 "$(dirname "$0")/../lib/jobs.py" locks 2>/dev/null) || { echo "busy: can't read jobs/jobs.toml"; return 0; }
[[ -n "$locks" ]] || { echo "busy: no locks in jobs/jobs.toml"; return 0; }
for name in $locks; do
  lock=$home/.local/state/homelab/$name.lock
  if [[ -e $lock ]] && ! flock -n -s "$lock" true; then echo "busy: $name.sh holds its lock"; return 0; fi
done
return 1
}

while reason=$(busy); do
  (( $(date +%s) < deadline )) || { echo "$reason"; exit 1; }
  sleep 30
done
echo "idle"
