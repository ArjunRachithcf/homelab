#!/usr/bin/env bash
# ~/homelab/backup-stage.sh: the root-only part of each backup. Run as root by homelab-backup.service
# (ExecStartPre=+), before backup.sh, which stays unprivileged and tars what lands here.
#   netbird/  /var/lib/netbird: the peer identity, so a rebuild keeps the same NetBird IP
#   plex/     Preferences.xml + the library DBs (SQLite backup API, Plex's own sqlite build)
# Target: $RUNTIME_DIRECTORY (/run/homelab-backup, 0700, owned by the backup user, gone after the run).
set -euo pipefail
out=${RUNTIME_DIRECTORY:?run by homelab-backup.service}
owner=$(stat -c %U "$out")

cp -a /var/lib/netbird "$out/netbird"

PLEX="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server"
if [[ -f "$PLEX/Preferences.xml" ]]; then
  mkdir -p "$out/plex/Databases"
  cp -a "$PLEX/Preferences.xml" "$out/plex/"
  for db in com.plexapp.plugins.library.db com.plexapp.plugins.library.blobs.db; do
    [[ -f "$PLEX/Plug-in Support/Databases/$db" ]] || continue
    "/usr/lib/plexmediaserver/Plex SQLite" "$PLEX/Plug-in Support/Databases/$db" \
      ".backup '$out/plex/Databases/$db'"
  done
fi

chown -R "$owner:" "$out"
