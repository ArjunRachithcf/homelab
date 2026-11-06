# Homelab

> Early: this runs one family's homelab every day and is being made ready for others. A first run on a
> new machine isn't smooth yet; see [docs/getting-started.md](docs/getting-started.md).

A family's own cloud on one computer at home: documents, photos, films, music, books, files, a budget,
bookmarks and a home page that ties them together, each behind one sign-in with passkeys. Nothing is open to
the internet unless you choose it, and then only through an encrypted mesh, never your router.

It runs on Fedora, Debian, Ubuntu or Arch (macOS best effort), with Docker for the apps and Ansible for the
machine. It sets itself up with you, step by step, in a terminal, in a desktop app, or with an AI assistant.

## What you get

- **One sign-in** (Authentik): passkeys or password + authenticator, sign-up limited to your family's
  addresses, every app behind it.
- **Apps you choose** from 35 components: Paperless-ngx (documents, with local AI), Immich (photos),
  Jellyfin or Plex, Navidrome (music), Audiobookshelf, Kavita (books), OpenCloud (files), Actual (budget),
  Karakeep (bookmarks), BentoPDF, Sharry (share links), Home Assistant + Matter, AdGuard Home, a web terminal,
  ntfy (push notifications), n8n (automations) and the family apps (chores, things,
  family tree, notes, wiki, news, games, places).
  The full list: [docs/components.md](docs/components.md).
- **A home page** for everyone (only the apps each person may open), and a **console** for whoever looks
  after the machine: jobs, apps, logs, what to do when something is red.
- **Backups that leave the house**: encrypted hourly archives, a second copy of photos and files, optionally
  Proton Drive, with a weekly restore test.
- **Private by default**: everything is reachable over NetBird (WireGuard) only; the public sites, if you want
  them, go through NetBird's reverse proxy in TLS passthrough, so certificates and data stay on your machine.

## Start

On the machine that will run it:

```bash
curl -fsSLO https://raw.githubusercontent.com/ArjunRachithcf/homelab/main/setup/bootstrap.sh
bash bootstrap.sh --repo https://github.com/ArjunRachithcf/homelab.git
cd ~/homelab && ./homelab setup
```

Or use the **setup app** (built for Linux, macOS and Windows by CI for every release tag; `setup-app/README.md`)
and point it at that machine. All the ways in, and what you need
first (a domain, a NetBird account, a mail account for sign-up mails):
[docs/getting-started.md](docs/getting-started.md).

## Find your way

| | |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Before you start, and the four ways to set up |
| [docs/components.md](docs/components.md) | Every component, what it needs and its settings (generated) |
| [docs/architecture.md](docs/architecture.md) | How the parts fit: mesh, Caddy, Authentik, the apps, jobs, the setup engine |
| [docs/platforms.md](docs/platforms.md) | Fedora, Debian/Ubuntu, Arch, macOS: what differs |
| [docs/instance/README.md](docs/instance/README.md) | Where your own instance's notes, runbook and logos go (a private repository of yours) |
| `./homelab` | The command line: `status`, `run`, `logs`, `check`, `setup`, `components`, `selftest` |

## Contributing

`./homelab selftest` and `tests/golden.sh` before a change; a change that should keep behaviour keeps the
golden files identical. CONTRIBUTING.md has the rest, CLAUDE.md the conventions (for people and for Claude Code
alike). Security problems: SECURITY.md, privately.

## Licence

Apache-2.0 (LICENSE). The app icons, the Inter font and two patches to other projects keep their own licences
(NOTICE, REUSE.toml).
