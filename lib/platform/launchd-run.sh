#!/usr/bin/env bash
# ~/homelab/lib/platform/launchd-run.sh JOB: one job of jobs/jobs.toml under launchd (macOS, plan Phase 5),
# what homelab-JOB.service is on Linux. Runs the job's script from the repo with its arguments, appends
# its output to ~/Library/Logs/homelab/org.homelab.JOB.log (./homelab logs, the console), and on failure
# mails it like homelab-notify@ (notify_on_failure in the registry). Also run synchronously by ops-run.sh.
set -uo pipefail
# shellcheck source=lib/need-bash.sh
source "$(dirname "$0")/../need-bash.sh"   # bash 4.4+: Homebrew's (launchd starts /bin/bash 3.2's world)
cd "$(dirname "$0")/../.." || exit 1
job=${1:?job}
script=$(python3 lib/jobs.py script "$job") || exit 2
read -ra argv <<<"$script"
log="$HOME/Library/Logs/homelab/org.homelab.$job.log"
mkdir -p "$(dirname "$log")"
# Keep the log short: the last 5000 lines
if [[ -f "$log" ]] && (( $(wc -l <"$log") > 6000 )); then tail -n 5000 "$log" >"$log.tmp" && mv -f "$log.tmp" "$log"; fi
echo "── $(date '+%F %T') $job: start" >>"$log"
"./${argv[0]}" "${argv[@]:1}" >>"$log" 2>&1
rc=$?
echo "── $(date '+%F %T') $job: exit $rc" >>"$log"
if (( rc != 0 )) && [[ "$(python3 lib/jobs.py field "$job" notify_on_failure)" == true ]]; then
  ./notify.sh failed "homelab-$job.service" >>"$log" 2>&1 || true
fi
exit "$rc"
