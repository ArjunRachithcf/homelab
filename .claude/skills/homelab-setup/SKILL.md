---
name: homelab-setup
description: Guide someone through setting up or changing this homelab (choosing components, filling in .env options, running the wizard stages, checking the result) with the homelab-setup MCP server. Use when they ask to set up the homelab, add or remove an app, re-run a setup step, or find out why a check or job fails.
---

# Setting up the homelab with the person

The `homelab-setup` MCP server (`lib/setup/mcp.py`, started from `.mcp.json`) does the work on this
machine. It uses the same engine and wizards as `./homelab setup` and the setup app, so whatever you
do here, the terminal and the app see the same `.env` and the same stages.

## Order

1. **Find out what they want.** Which apps, whether family members get in from outside, and whether
   there's an NVIDIA card, a second disk, a Proton account. `list_components` shows everything, with
   presets (`minimal`, `family`, `everything`) as starting points.
2. **Plan, then show it.** `plan` with a preset or `components`, plus `add`/`remove`/`options`. Tell
   them in plain words what gets added and why (the `notes`), and what's still needed (`missing`).
   Non-secret values (domain, mesh IP, their name, mail address) go in `options`; ask for each one.
3. **Apply only after a yes.** `apply` writes `.env`, runs `./homelab configure` and refreshes a running console's backend. Secrets are
   generated on the machine and never shown to you.
4. **Stages, one at a time.** `stages` gives the order. For each one, say what it does, then
   `run_stage`. Read its messages to the person; for a question, ask them and `answer` with what they
   said (confirm: `y`/`n`; a pause or a terminal step: empty, once they say it's done). `poll` while
   it's still working.
5. **Check.** `run_check` at the end (or for one wizard), `status` for the scheduled jobs, `logs JOB`
   when one failed.

## What you never do

- **Never ask for a secret, a password or a token in the chat**, and never try to set one: `plan`,
  `apply` and `set_option` refuse secret options. When a stage needs one (an SMTP password, a DNS
  token), the sudo password, or a destructive confirmation (a disk wipe), `run_stage` returns
  `needs_person` with `person_runs`: give them that command to run in their own terminal (or tell them
  to use the setup app), and carry on with the next stage once they say it's done.
- Never answer a question the person hasn't answered, and never pick a disk, a domain or a mail
  address for them.
- Never edit `.env` or the generated files by hand; never touch DNS records other than the ones the
  wizard names (other mail records may be in the same zone).
- Respect the hard constraints in `CLAUDE.md` (no router changes, no public listeners, SELinux stays
  enforcing, the NetBird proxy stays TLS passthrough).

## When something fails

- A stage's `exit` isn't 0: read its last messages, explain, and offer to run it again after the fix
  (finished steps are skipped or offered to skip).
- A check fails: the check's label says what; the matching stage fixes it.
- A job fails: `logs JOB`, then the guide `site/console/guides/<guide>.md` named for the job in
  `jobs/jobs.toml`.

Everything you call is logged (redacted) in `~/.local/state/homelab/mcp-audit.log`.
