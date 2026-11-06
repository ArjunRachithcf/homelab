#!/usr/bin/env bash
# ~/homelab/ops-run.sh: runs what the home page's detail sheets and the console ask for (RUNBOOK.md 7e,
# "Details"; the console: RUNBOOK.md "Console"). Started by homelab-ops.path whenever ops/server.py
# drops a file into ops/requests/. A request is a file NAME, checked again here against
# ops/access.py; each kind maps to one fixed command below, anything else is deleted unread. The
# file's content is only the signed-in username, for the restart mail. One job at a time; progress
# goes to ops/data/jobs.json, which the sheets and the console poll.
#
#   backup           homelab-backup, then homelab-offbox (Proton); the polkit rule allows exactly these
#   clean-docker     images no container uses, older than 7 days, and the build cache
#   clean-bins       the second copies' recycle bins older than 7 days
#   clean-backups    hourly archives older than 24 h thinned to one a day
#   run-<job>        systemctl start of a job the polkit rule allows (jobs/jobs.toml polkit_start)
#   restart-<svc>    docker compose restart of an app's service; mails NOTIFY_TO who did it
#   logs-job-<job>   the job's last 200 journal lines → ops/data/logs-job-<job>.json (the user
#                    needs journal access: wheel or systemd-journal)
#   logs-app-<svc>   the service's last 200 log lines → ops/data/logs-app-<svc>.json
set -uo pipefail
cd "$(dirname "$0")" || exit 1
REQ=ops/requests
DATA=ops/data
mkdir -p "$DATA" "$HOME/.local/state/homelab"
exec 9>"$HOME/.local/state/homelab/ops.lock"
flock -w 7200 9 || { echo "another ops-run.sh holds the lock" >&2; exit 1; }

# jobs.json: job, step, state (running | done | failed), started, finished, output (last lines)
status() { # job step state started [output]
  JOB="$1" STEP="$2" STATE="$3" STARTED="$4" OUT="${5:-}" python3 - "$DATA/jobs.json" <<'PY'
import json, os, sys, time
done = os.environ["STATE"] != "running"
obj = {"job": os.environ["JOB"], "step": os.environ["STEP"], "state": os.environ["STATE"],
       "started": int(os.environ["STARTED"]), "finished": int(time.time()) if done else None,
       "output": os.environ["OUT"].splitlines()[-12:]}
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as f:
    json.dump(obj, f)
os.chmod(tmp, 0o644)
os.replace(tmp, sys.argv[1])
PY
}

# logs-*.json: name, time, lines (the last 200; the console shows them as text)
write_logs() { # name, the lines on stdin
  python3 -c '
import json, os, sys, time
lines = sys.stdin.read().splitlines()[-200:]
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as f:
    json.dump({"name": sys.argv[2], "time": int(time.time()), "lines": lines}, f)
os.chmod(tmp, 0o640)
os.replace(tmp, sys.argv[1])' "$DATA/$1.json" "$1"
}

# The unit of a job id (jobs/jobs.toml)
job_unit() { python3 lib/jobs.py unit "$1"; }

# Start a job's unit and wait for it, and its log since a time: systemd, or on macOS the launchd
# wrapper (lib/platform/launchd-run.sh) and its log file
start_unit() { # unit
  if command -v systemctl >/dev/null; then systemctl start "$1"
  else local j=${1#homelab-}; bash lib/platform/launchd-run.sh "${j%.service}"; fi
}
unit_log() { # unit since(epoch) lines
  if command -v journalctl >/dev/null; then
    journalctl -u "$1" --since "@$2" -o cat --no-pager 2>/dev/null | tail -n "$3"
  else
    local j=${1#homelab-}; tail -n "$3" "$HOME/Library/Logs/homelab/org.homelab.${j%.service}.log" 2>/dev/null
  fi
}

run_job() {
  local job=$1 who=${2:-?} started out rc=0 unit svc
  started=$(date +%s)
  case "$job" in
    backup)
      status backup "Local backup" running "$started"
      out=$(start_unit homelab-backup.service 2>&1) || rc=$?
      out+=$'\n'$(unit_log homelab-backup.service "$started" 4)
      if (( rc == 0 )); then
        status backup "Proton copy" running "$started" "$out"
        out+=$'\n'$(start_unit homelab-offbox.service 2>&1) || rc=$?
        out+=$'\n'$(unit_log homelab-offbox.service "$started" 4)
      fi
      ;;
    clean-docker)
      status "$job" "Removing unused images" running "$started"
      out=$(docker image prune -a -f --filter until=168h 2>&1 | tail -n 3) || rc=$?
      out+=$'\n'$(docker builder prune -f 2>&1 | tail -n 2) || rc=$?
      ;;
    clean-bins)
      status "$job" "Emptying recycle bins" running "$started"
      out=$(find "${PHOTOS_COPY_DIR:-/srv/photos-copy}/deleted" "${FILES_COPY_DIR:-/srv/files-copy}/deleted" -mindepth 1 -maxdepth 1 -type d -mtime +7 \
              -printf 'removed %p\n' -exec rm -rf {} + 2>&1) || rc=$?
      [[ -n "$out" ]] || out="Nothing older than 7 days."
      ;;
    clean-backups)
      status "$job" "Thinning backups" running "$started"
      # backup.sh's lock: never while it writes or prunes
      exec 8>"$HOME/.local/state/homelab/backup.lock"
      if flock -w 600 8; then
        out=$(./backup-retention.sh 24 14 < <(cd backups && ls homelab-*.tar.age) | sed 's|^|backups/|' \
                | xargs -r rm -vf -- 2>&1 | sed 's|^removed .backups/|removed |') || rc=$?
        [[ -n "$out" ]] || out="Nothing to thin."
      else
        rc=1; out="backup.sh is busy, try again later"
      fi
      exec 8>&-
      ;;
    run-*)
      unit=$(job_unit "${job#run-}") || { echo "no unit for $job"; return; }
      status "$job" "Running $unit" running "$started"
      out=$(start_unit "$unit" 2>&1) || rc=$?
      out+=$'\n'$(unit_log "$unit" "$started" 6)
      ;;
    restart-*)
      svc=${job#restart-}
      status "$job" "Restarting $svc" running "$started"
      out=$(docker compose restart "$svc" 2>&1 | tail -n 4) || rc=$?
      # Whoever restarts an app, the admin hears of it (and a failed restart all the more)
      printf '%s restarted %s at %s from the console: %s.\n\n%s\n' "$who" "$svc" "$(date '+%F %H:%M')" \
        "$( (( rc == 0 )) && echo "done" || echo "failed ($rc)")" "$out" \
        | ./notify.sh send "Homelab: $who restarted $svc" >/dev/null 2>&1 || echo "restart mail failed" >&2
      ;;
    logs-job-*)
      unit=$(job_unit "${job#logs-job-}") || { echo "no unit for $job"; return; }
      status "$job" "Reading the journal" running "$started"
      if command -v journalctl >/dev/null; then
        journalctl -u "$unit" -n 200 -o short-iso --no-pager 2>&1 | write_logs "$job" || rc=$?
      else
        unit_log "$unit" 0 200 | write_logs "$job" || rc=$?
      fi
      out="ops/data/$job.json"
      ;;
    logs-app-*)
      svc=${job#logs-app-}
      status "$job" "Reading the logs" running "$started"
      docker compose logs --no-color --no-log-prefix --timestamps --tail 200 "$svc" 2>&1 | write_logs "$job" || rc=$?
      out="ops/data/$job.json"
      ;;
  esac
  if [[ "$job" == clean-* ]]; then
    status "$job" "Measuring" running "$started" "$out"
    nice -n 10 python3 storage-report.py || true
  fi
  status "$job" "" "$( (( rc == 0 )) && echo "done" || echo failed)" "$started" "$out"
  echo "$job: $( (( rc == 0 )) && echo "done" || echo "failed ($rc)")"
}

# What may be asked for (ops/access.py: the chosen components); the server checked it too
mapfile -t allowed < <(python3 ops/access.py names --env .env)
is_allowed() { local a; for a in "${allowed[@]}"; do [[ "$a" == "$1" ]] && return 0; done; return 1; }

# Until the inbox is empty (requests that arrive meanwhile run next)
while :; do
  shopt -s nullglob
  reqs=("$REQ"/*)
  shopt -u nullglob
  (( ${#reqs[@]} )) || break
  for f in "${reqs[@]}"; do
    name=$(basename "$f")
    # Who asked: the username ops/server.py wrote (already checked there); only for the mail
    who=$(head -c 64 -- "$f" 2>/dev/null | head -n 1 | tr -cd 'A-Za-z0-9._@+-')
    rm -f -- "$f"
    if is_allowed "$name"; then
      run_job "$name" "${who:-?}"
    else
      echo "ignored unknown request: ${name//[^A-Za-z0-9._-]/?}"
    fi
  done
done
