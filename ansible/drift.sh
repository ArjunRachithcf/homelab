#!/usr/bin/env bash
# ~/homelab/ansible/drift.sh: weekly drift check (homelab-drift.service, as root).
# Runs site.yml with --check --diff, which changes nothing. Mails a summary only when a task
# would change; a failed run exits non-zero so homelab-notify@ mails the journal instead.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export ANSIBLE_CONFIG=$PWD/ansible.cfg ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0
log=$(mktemp); trap 'rm -f "$log"' EXIT

ansible-playbook ansible/site.yml --check --diff >"$log" 2>&1
rc=$?
changed=$(grep -oE '^localhost +:.* changed=[0-9]+' "$log" | grep -oE 'changed=[0-9]+' | cut -d= -f2)
if (( rc != 0 )) || [[ -z "$changed" ]]; then tail -n 40 "$log"; echo "drift check failed (rc=$rc)"; exit 1; fi
if (( changed == 0 )); then echo "no drift"; exit 0; fi

{
  echo "This machine differs from ansible/site.yml in $changed place(s). Nothing was changed."
  echo "Review:  ansible-playbook -K ansible/site.yml --check --diff"
  echo "Apply:   ansible-playbook -K ansible/site.yml   (outside 02:00–05:30)"
  echo
  echo "Would change:"
  awk '/^TASK \[/ {t=$0; sub(/^TASK \[/, "", t); sub(/\] \**$/, "", t)} /^changed: / {print "  - " t}' "$log"
  echo
  echo "Diff (secret tasks are censored):"
  grep -vE '^(ok|skipping): |^$' "$log" | head -n 300
} | "$PWD/notify.sh" send "Homelab: drift on $(hostname -s) ($changed change(s))"
echo "drift: $changed change(s), mailed"
