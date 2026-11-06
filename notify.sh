#!/usr/bin/env bash
# ~/homelab/notify.sh: mail NOTIFY_TO (.env) through Proton SMTP (the credentials Authentik uses, from .env).
#   notify.sh failed <unit>    the last 30 journal lines of that unit (homelab-notify@.service)
#   notify.sh send <subject>   the body from stdin (homelab-drift.service)
# Plain text only: Proton's SpamAssassin rejected richer mails (RUNBOOK.md "Family sign-up").
set -euo pipefail
cd "$(dirname "$0")"
umask 077

env_get() { sed -n "s/^$1=//p" .env | tail -n1 | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'; }
HOST=$(env_get AUTHENTIK_EMAIL__HOST); HOST=${HOST:-smtp.protonmail.ch}
PORT=$(env_get AUTHENTIK_EMAIL__PORT); PORT=${PORT:-587}
USER_=$(env_get AUTHENTIK_EMAIL__USERNAME)
PASS=$(env_get AUTHENTIK_EMAIL__PASSWORD)
FROM=$(env_get AUTHENTIK_EMAIL__FROM); FROM=${FROM:-$USER_}
FROM_ADDR=$(sed -n 's/.*<\(.*\)>.*/\1/p' <<<"$FROM"); FROM_ADDR=${FROM_ADDR:-$FROM}
TO=${NOTIFY_TO:-$(env_get NOTIFY_TO)}
[[ -n "$TO" ]] || { echo "NOTIFY_TO missing in .env" >&2; exit 1; }
[[ -n "$USER_" && -n "$PASS" ]] || { echo "AUTHENTIK_EMAIL__USERNAME/PASSWORD missing in .env" >&2; exit 1; }

case "${1:-}" in
  failed)
    unit=${2:?unit}
    subject="Homelab: $unit failed on $(hostname -s)"
    if command -v journalctl >/dev/null; then
    body=$(printf '%s failed at %s.\n\nLast 30 journal lines:\n\n%s\n\nMore: journalctl -u %s\n' \
      "$unit" "$(date '+%F %H:%M')" "$(journalctl -u "$unit" -n 30 --no-pager -o short-iso 2>&1)" "$unit")
    else
      # macOS: launchd jobs log to a file (lib/platform/launchd-run.sh)
      log="$HOME/Library/Logs/homelab/org.homelab.$(sed -e 's/^homelab-//' -e 's/\.service$//' <<<"$unit").log"
      body=$(printf '%s failed at %s.\n\nLast 30 log lines:\n\n%s\n\nMore: %s\n' \
        "$unit" "$(date '+%F %H:%M')" "$(tail -n 30 "$log" 2>&1)" "$log")
    fi ;;
  send)
    subject=${2:?subject}; body=$(cat) ;;
  *) echo "usage: notify.sh failed <unit> | send <subject> <body" >&2; exit 2 ;;
esac

msg=$(mktemp); trap 'rm -f "$msg"' EXIT
{
  printf 'From: %s\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\nMessage-ID: <%s.%s@%s>\r\n' \
    "$FROM" "$TO" "$subject" "$(date -R)" "$(date +%s)" "$RANDOM$RANDOM" "${FROM_ADDR#*@}"
  printf 'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n'
  sed 's/$/\r/' <<<"$body"
} >"$msg"

# Credentials go in through a curl config on a pipe, never on the command line
esc() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' <<<"$1"; }
curl -sS --fail -m 60 --ssl-reqd --url "smtp://$HOST:$PORT" \
  --mail-from "$FROM_ADDR" --mail-rcpt "$TO" -T "$msg" \
  -K <(printf 'user = "%s:%s"\n' "$(esc "$USER_")" "$(esc "$PASS")")
echo "mailed $TO: $subject"
