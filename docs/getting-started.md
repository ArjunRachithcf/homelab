# Getting started

## What you need first

- **A computer that stays on**, with Fedora, Debian 13, Ubuntu 24.04 or Arch (macOS works, best effort:
  [platforms.md](platforms.md)). 8 GB of memory for a few apps; 16 GB or more with photos and local AI.
  An NVIDIA card only for the local AI component (Paperless titles and tags, Karakeep tags).
- **A domain** whose DNS you can edit (the sites become `home.<domain>`, `auth.<domain>`, …), and an API
  token for its DNS if you want Let's Encrypt certificates by DNS challenge (the default; Infomaniak today,
  others are a Caddy module away).
- **A NetBird account** (free tier is enough): the mesh every device joins.
- **A mail account** that can send (SMTP): sign-up confirmations, password resets, alerts.
- For backups off the machine: an **age** key pair on your laptop (the private key never touches the server)
  and, optionally, a Proton account.

## Four ways in

All four drive the same engine and the same wizards, write the same `.env`, and can be mixed: start in the
app, finish in a terminal, ask an assistant why a check fails.

### 1. The terminal

```bash
bash bootstrap.sh --repo https://github.com/<owner>/homelab.git     # git, Python 3.11+, the checkout
cd ~/homelab
./homelab setup --plan --preset minimal --add music                 # what that would do; changes nothing
./homelab setup --apply --preset minimal --add music                # writes .env (secrets generated there)
./homelab configure
./homelab setup                                                     # the wizards, one stage at a time
```

The wizards say what to click where (NetBird's dashboard, your DNS provider, Authentik), wait for you, and
check each result. Run any of them again: finished stages offer to skip. `./homelab check` re-runs every check.

### 2. The setup app

CI builds it for every release tag (Linux, macOS, Windows; the workflow run's artifacts; unsigned:
[setup-app/README.md](../setup-app/README.md)), or build it yourself from there.
Choose **This computer** or **Another computer** (its address, your user, password or key). It shows what's
there, downloads the homelab if needed, lets you pick a preset and switch apps on or off with a live plan,
then walks the same wizard stages as a checklist. Questions become fields; your sudo password is asked once.

### 3. An AI assistant

- **Claude Code**: open the checkout in Claude Code on the machine. It offers the `homelab-setup` MCP server
  (`.mcp.json`) and the skill `.claude/skills/homelab-setup/`. Ask it to set up what you want.
- **In the setup app**: the Assistant panel, with Anthropic's API (your key, kept in the OS keychain) or a
  local Ollama.

Either way the assistant can plan, save your choice after you say yes, run stages and read checks and logs.
It never sees or types a secret, a password or a destructive confirmation: those steps stop and tell you the
command to run yourself.

### 4. An answers file

For a second machine, or to rebuild: `./homelab setup --export-answers > answers.toml` on one machine (no
secrets in it), then `./homelab setup --apply --answers answers.toml` on the other. The setup app's
**Save answers…** writes the same file.

## After setup

- `./homelab status`: every scheduled job of the chosen components and when it runs next. `./homelab logs backup`, `./homelab run backup`.
- The home page at `https://home.<domain>`; the console at `https://home.<domain>/console/` for admins and the
  `operators` group, with a guide for everything that can go red.
- Updates: [the console's guide](../site/console/guides/update-apps.md).

## Your own instance notes

`INSTANCE.md` at the top of the repository (imported by `CLAUDE.md`, not in git) holds one instance's facts
and decisions: owner, domains, what's deliberately different. Make it a copy of
[instance/INSTANCE.example.md](instance/INSTANCE.example.md) filled in for yours, so an assistant working on
your machine knows your constraints. To keep it, your runbook and your logos in a private repository of your
own, see [instance/README.md](instance/README.md).
