#!/usr/bin/env bash
# ~/homelab/backup-retention.sh HOURS DAYS: reads archive names (homelab-YYYY-MM-DD-HHMM.tar.age) on
# stdin and prints the ones to drop: keep everything younger than HOURS, then only the first
# archive of each day younger than DAYS. Used for backups/ (backup.sh) and Proton (backup-photos.sh).
set -euo pipefail
hours=${1:?hours}; days=${2:?days}
recent=$(date -d "-$hours hours" +%F-%H%M)
oldest=$(date -d "-$days days" +%F)
sort | awk -v recent="$recent" -v oldest="$oldest" '
  match($0, /^homelab-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}\.tar\.age$/) {
    stamp = substr($0, 9, 15); day = substr($0, 9, 10)
    if (stamp >= recent) next                      # recent: keep all
    if (day >= oldest && !(day in first)) { first[day]; next }   # first of its day: keep
    print
  }'
