# This repository: a homelab you can set up and run yourself

Read `README.md` for what it is, `docs/architecture.md` for how the parts fit, `docs/getting-started.md` for the
ways to set it up. The machine this checkout runs on, its owner and their decisions are in `INSTANCE.md`
(imported below; per machine, not in git: `docs/instance/INSTANCE.example.md` is the template); where the
two differ, `INSTANCE.md` wins.

## Working in this repository

- **The instance is data.** Everything specific to one homelab lives in `.env` ("Your instance" in
  `.env.example`), the chosen components (`COMPONENTS`) and `branding/`. Code reads it; never write a domain,
  a name or a path of one instance into code. `./homelab configure` renders what comes from `.env`.
- **Byte-identical unless deliberate.** `tests/golden.sh` renders compose, Ansible's values, the units, the
  blueprints, everything `configure` writes and Caddy's behaviour for each instance under test
  (`tests/fixtures/full.env`, an example with every component), and compares them with `tests/golden/<name>/`. A
  change meant to keep behaviour must keep it green; a deliberate change runs `tests/golden.sh --update` (it needs
  ansible-playbook and caddy) and explains the diff.
- **Tests:** `./homelab selftest` (unit tests), `tests/golden.sh`, shellcheck at warning level, Rust in
  `setup-app/src-tauri` (`cargo clippy -- -D warnings`, `cargo test`). CI: `.github/workflows/`.
- **Components:** a new app is a `setup/components/<id>.toml`, its compose service with `profiles: [<id>]`,
  its Caddy files in `caddy/components/<id>/` and a tile; `docs/components.md` is generated
  (`python3 lib/setup/engine.py docs > docs/components.md`).
- **Secrets** never appear in output, logs, commits, answers files, assistant results or the setup app's
  window; they go into `.env` on the machine (generated, typed into a wizard, or over stdin).

## Files here

- `RUNBOOK.md` (an instance's own, linked from `instance/`: docs/instance/README.md): the procedure. Follow its
  numbered steps in order (1–11, including 6b for Paperless AI, 7b for Jellyfin,
  7c for Immich, 7d for Navidrome and the other apps of 2026-10-25, 7e for Kavita, OpenCloud, Sharry and kids,
  7f for the family apps, 7g for ntfy, 7h for n8n).
- `docker-compose.yml`, `Caddyfile`, `.env.example`: the stack. Already validated with
  `docker compose config`. Caddyfile is a single-file bind mount: after editing it,
  `docker compose up -d --force-recreate --no-deps caddy` (a `caddy reload` reads the old inode). It holds the
  globals, snippets and core sites; each app's sites/status/Authentik lines are in `caddy/components/<id>/`,
  copied into `caddy/enabled/` by `./homelab configure` (the chosen components only).
- `site/`: landing page (`index.html`, Caddy templates); its logos and icons come from `BRANDING_DIR`
  (`branding/default/` unless `.env` names another) and `site-src/manifest.webmanifest`, written by `./homelab configure`.
- `authentik/jellyfin-ldap.yaml`: blueprint for Jellyfin's LDAP login (applied by hand).
- `public-wizard.sh`: the walkthrough for the public-access UI/DNS steps; `--check` re-runs checks.
- `ssh/`: `10-homelab.conf` (sshd drop-in, `__NETBIRD_IP__` / `__SSH_USER__`; LogLevel VERBOSE: which key a login
  used), `sssd.conf` template (keys re-read after 60 s), `setup-host.sh` (sudo; SSSD, PAM for sshd only, sshd),
  `ldap-ssh.pem` (outpost CA, generated).
- SSH keys (component ssh-sso): the home page's sheet `#keys` (`site/index.html`, tile in ssh-sso.toml) lists them
  (GET `/ops/keys`: `ops/server.py`, ssh-admins only in Caddy, X-Homelab-Mesh from Caddy); adding and removing
  happens in Authentik's flows `ssh-key-add`/`ssh-key-remove` (`authentik/ssh-sso.yaml`: your passkey every time,
  only from NetBird peers or this PC, also refused per request in `caddy/components/ssh-sso/authentik.caddy`, the form's
  policy re-checks everything and writes only the one change under a row lock, never showing a paste; the flows end
  at `?next=/ssh-keys/done` → the sheet). `lib/ssh_keys.py` (the checks, shared with the policy by
  `tests/unit/test_ssh_keys.py`'s cases; the job `ssh-keys`: `homelab-ssh-keys.{service,timer,path}`, triggered by
  `ops/keys/refresh`) follows Authentik and `sss_ssh_authorizedkeys`, keeps "last used" from sshd's journal, writes
  `ops/data/ssh-keys.json`, mails every change and has the outpost reload (saving its provider: seconds).
  `setup-ssh.sh add-key|remove-key|list-keys` do the same from a terminal. Authentik takes X-Forwarded-For only from
  Caddy (`AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS`). Tests: `tests/unit/test_ssh_keys.py`, `tests/authentik/run.sh` (a
  real Authentik set up by `lib/authentik.py all` in project homelab-aktest, never `homelab`; `KEEP=1` leaves it up;
  CI `.github/workflows/authentik.yml`; run it on every `AUTHENTIK_TAG` bump).
- `authentik/ssh-ldap.yaml`, `authentik/hardening.yaml`, `authentik/family.yaml` (core; each app's sign-in is in its
  component's own `authentik/<component>.yaml`, `setup-ssh.sh component ID`): blueprints,
  applied by `authentik/setup-ssh.sh` (also `hardening` after every Authentik upgrade, which
  includes `family`; `add-key`, `status`). Its logic is `lib/authentik.py`; what runs inside Authentik (`ak shell`) is
  `lib/authentik_tasks.py`; what a component needs beyond its blueprint (outpost providers, groups) is `[authentik]`
  in its `setup/components/<id>.toml`. Tests that stub `docker` set `DOCKER_HOST` to nowhere: the compose project is
  named `homelab` from any folder, so a real `docker` would act on the live stack.
- `authentik/templates-src/`: Authentik mail templates with `__KEY__` placeholders; `./homelab configure` renders
  them into `authentik/templates/<BRAND_SLUG>/` (bind-mounted at `/templates`, `:ro,z`) and copies the mail logo
  from `BRANDING_DIR`. The blueprints carry the same placeholders, filled by `setup-ssh.sh`
  (`lib/render.py`, only the instance keys it allows, never secrets). Run `./homelab configure` after every pull.
- `ansible/` + `ansible.cfg`: the host layer (`site.yml`, roles base/firewall/docker/gpu/netbird/plex/storage/smarthome/ssh/timers;
  per distro family `ansible/distro/<family>.yml` + `roles/*/tasks/{RedHat,Debian,Archlinux}.yml`, this PC = RedHat;
  `vars.yml` reads every fact from `.env`, section "Your instance", via `lookup('homelab_env')`). Run from `~/homelab`:
  `ansible-playbook -K ansible/site.yml --check --diff`; real runs refuse while a homelab job or Proton upload
  runs or 02:00–05:30 (`ansible/live-safe.sh`). Ansible never touches compose. All `homelab-*` units are
  templates in `ansible/roles/timers/templates/` (edit there, then `--tags timers`), each with
  `OnFailure=homelab-notify@%n.service` → `notify.sh` mails `NOTIFY_TO`; `homelab-drift.timer`
  (Sun 12:00) mails only when `--check` finds drift.
- `ansible/roles/firewall/` (RUNBOOK.md "Firewall: the LAN"): zone `homelab-lan` as firewalld's default zone
  (`LAN_ALLOW` in `.env`), policy `homelab-no-route` (the LAN is never routed), `homelab-mesh-guard` (nftables:
  NETBIRD_IP answers only wt0, lo and Docker's bridges), wt0 → `trusted` for good. `lib/firewall.sh`
  (`lan_closed`, for the heartbeat and hardening-wizard), `tests/firewall/guard.sh` (the guard in namespaces).
- `backup.sh` + `homelab-backup.{service,timer}`: age-encrypted backup, hourly + after every change (step 9):
  Paperless export, Authentik/Immich dumps (`.sql.zst`), Jellyfin config, Navidrome DB, Caddy certs, `.env`/config, and via
  `backup-stage.sh` (root, `ExecStartPre=+`) NetBird identity + Plex DBs. `backup-retention.sh`: 48 h all, then daily.
- `backup-watch.sh` + `homelab-backup-watch.timer` (5 min): fingerprints Immich/Paperless/Authentik/.env; after a
  change (quiet 10 min, max 30) starts `homelab-backup`, `homelab-photos-copy`, `homelab-offbox` (polkit rule allows
  exactly these). Off-box fallback `homelab-offbox.timer` every 4 h (`backup-photos.sh quick`).
- `backup-photos.sh` + `homelab-photos-backup.{service,timer}`: nightly: age tars to Proton
  Drive (60-day prune), photos to Proton Photos + album Family (`photos-only [N]` by hand), OpenCloud's files, and
  Paperless' documents as plain files (each original, `homelab/paperless/<owner>/`, also in the 4-hourly quick runs;
  deletions not mirrored), Sunday restore tests.
- `immich/restore-from-proton.sh`: rebuild the Immich library from Proton Photos + Immich's DB (`plan`, `verify`, `restore`).
- `proton-drive/`: `update.sh` (weekly: official CLI from Proton's version.json + SHA-512, patched
  build from Proton's source + `live-photos.patch`), `README.md`. Patched build = Proton Photos only.
- `photos-local.sh` + `homelab-photos-local.{service,timer}`: nightly duperemove of the Immich
  library (two accounts, same photos) + rsync copy to `/srv/photos-copy` on the primary SSD.
- `paperless/`: run in Paperless' own shell (`docker compose exec -T paperless python3 manage.py shell < FILE`):
  `ai-workflow.py` (step 6b), `originals.py` (not in the shell: the export's originals by owner, hard links, for
  backup-photos.sh's plain copy in Proton Drive), `family.py` (wizard.sh stage `paperless_family`: the group `paperless-users` with a
  family member's rights, every account in it, `PAPERLESS_FAMILY_SHARING` = the workflow "Share with the family",
  existing documents shared once; new SSO sign-ins join the group by `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS`; test
  `tests/paperless/run.sh`, a real Paperless in project homelab-pltest, CI `.github/workflows/paperless.yml`).
- `immich/setup.sh`: Immich settings over its API (`init`, `sso-only`, `status`; uses `IMMICH_API_KEY`).
- `immich-wizard.sh`: the walkthrough for Immich (disk, NetBird, logins, Proton Drive, local copy); `--check`.
- `music-wizard.sh`: the walkthrough for Navidrome (zones, first login = admin, public services,
  app password, copying music); `--check`.
- `hardening-wizard.sh`: the walkthrough for SSH/hardening/backups and the LAN firewall (stage `firewall`: the
  firewall role's dry run, then the change, with an undo timer over SSH); `--check` re-runs checks.
- `apps-wizard.sh`: the walkthrough for step 7d (secrets, Ansible, Authentik apps, NetBird zones/services/
  nameserver, AdGuard, Home Assistant + Matter, first sign-ins, heartbeat); `--check`.
- `authentik/{audiobooks,bookmarks,budget,pdf,adblock}.yaml`: the 7d apps, one blueprint per component (`setup-ssh.sh apps`
  applies the chosen ones; also run by `hardening`).
- `homeassistant/setup.sh`: HA's first start without a LAN listener (`init`, `drop-http`, `status`).
- `heartbeat.sh` + `homelab-heartbeat.{service,timer}`: outside view every 5 min → healthchecks.io (`HEARTBEAT_URL`);
  also writes `site/status/heartbeat.json` for the home page (read-only key `HEARTBEAT_API_KEY_RO`).
- `files-wizard.sh`: the walkthrough for step 7e (secrets, disk restructure, Authentik, Kavita UI, NetBird,
  OpenCloud desktop app, Sharry test, Navidrome app passwords, Termix, ntfy (7g), n8n (7h), kids, home status); `--check`.
- `authentik/{reading,files,share}.yaml`: Kavita, OpenCloud (4 clients), Sharry, one blueprint per component
  (`setup-ssh.sh files` applies the chosen ones; also run by `hardening`); `setup-ssh.sh kid <user>` puts a child into `kids`,
  `setup-ssh.sh members GROUP` lists a group's users (Seerr's permissions).
- `files-local.sh` + `homelab-files-local.{service,timer}` (03:50): OpenCloud snapshot (reflink) + copy to `/srv/files-copy`.
- `sharry/sharry.conf`: Sharry's config (secrets via env).
- `site/apps.json` (generated by `./homelab configure` from `site-src/apps.json` + the components' `[[tiles]]`),
  `site/index.html`: home page (who sees what; layout). Design sheet in `.taste-skill/` (not committed).
- `ops-report.py`, `storage-report.py` (+ `homelab-storage-report.timer`), `ops/server.py` (container `ops`),
  `ops-run.sh` (+ `homelab-ops.path`): the home page's detail sheets for admins (graphs, Back up now, cleanups);
  reports in `ops/data/`, job requests in `ops/requests/`, the SSH keys sheet's in `ops/keys/` (all untracked).
  RUNBOOK.md 7e "Details".
- Console (`site/console/`, guides in `site/console/guides/`, RUNBOOK.md "Console"): admins + group `operators`;
  `console-report.py` (from `heartbeat.sh`) → `ops/data/console.json`; `ops/access.py` is the one list of what
  may be asked (run-/logs-job-<job>, restart-/logs-app-<service>); restarts and runs only from NetBird IPs.
- `authentik/terminal.yaml`: Termix's OIDC app, admins only (`setup-ssh.sh terminal`; also run by `hardening`).
- `family-wizard.sh`: the walkthrough for step 7f (secrets, Memos build, Authentik apps, start, NetBird zones,
  first sign-ins as admin, public services, phones, backup); `--check`.
- `authentik/{chores,things,family-tree,notes,wiki,news,games,places}.yaml`: the family apps' OIDC clients, one per component
  (`setup-ssh.sh family-apps` applies the chosen ones; also run by `hardening`).
- `downloads-wizard.sh`: the walkthrough for downloads on request (components downloads, movies-tv, albums, subtitles,
  book-requests, byparr, requests; opt-in: never part of "all"): the VPN's key (no password: Authentik is the one
  sign-in, the apps' own are generated), WireGuard at boot and the guard's timer (Ansible), start (the volumes to
  HOST_UID, then the VPN, then the apps; never Seerr or Bindery), Authentik apps, NetBird zones, connecting them,
  search sites (+ Byparr's tag), the library imported unmonitored, Recyclarr, a first film, then subtitles, albums,
  books (Bindery started and its Authentik provider pushed back to back), requests (Seerr: started, its first admin
  from Jellyfin's own admin, typed once, then the family's permissions from Authentik's groups), its public services,
  backup; `--check`.
- `downloads/`: `setup.py` (stdlib: `init`, `apply`, `status`, `unmonitor-new-seasons`, `guard`, `bindery-init`,
  `seerr-init`, `seerr-users`), run by `run.sh` in a throwaway container on the compose project's downloads network (and
  requests, Seerr's) (not a compose service: heartbeat.sh counts those), `guard.sh` (`homelab-downloads-guard.timer`:
  torrents stopped when the media disk runs low, the apps in gluetun recreated after it restarts),
  `prowlarr-ready.sh` (s6's readiness check) and `gluetun-post-rules.txt` (its kill switch while it restarts the
  tunnel: an established connection's packets would leave by eth0 from the tunnel's address; plain rules only, gluetun
  parses its chains). gluetun's namespace holds qBittorrent, Prowlarr (bound to gluetun's
  address only: that loopback counts as local) and Byparr (kill switch). Authentik is the only login: forward-auth on
  every path, then Radarr, Sonarr, Lidarr, Prowlarr let in Caddy's forwarded client (Forms + DisabledForLocalAddresses
  + TrustCgnat + TrustedNetworks = Caddy's fixed addresses, the networks on documentation ranges DOWNLOADS_NET and
  REQUESTS_NET so a neighbour is never local), qBittorrent Caddy's address (its whitelist), Caddy fills in Bazarr's
  Basic login (BAZARR_AUTH), Bindery has its own OIDC client, Seerr signs in through Jellyfin (LDAP → Authentik); each
  app's own password and API key generated (External would hand out the API keys). Networks `arr` = gluetun and the
  apps with Caddy (not #8's `downloads`: compose couldn't re-range that one under its apps), `requests` = Seerr, Radarr,
  Sonarr, Caddy; no IPv6. Caddy lets anyone reach Seerr only once `site/status/requests-ready` exists (the wizard's
  requests step, after its admin proved ours), and never passes its API key. `authentik/{downloads,movies-tv,albums,subtitles,
  book-requests}.yaml`, `recyclarr/recyclarr.yml` (TRaSH Guides' profiles). Tests: `tests/unit/test_downloads.py`,
  `tests/downloads/run.sh` (the real images in project homelab-dltest, never `homelab`, on other ranges).
- `memos/`: Memos' settings (JSON, read at start), `sso-admin.patch` + `build.sh` (our image), `README.md`.
- `ntfy/setup.sh` (`token NAME TOPIC KEY`: an app's write-only token, `n8n-token`, `user NAME [TOPIC]`, `status`): ntfy's
  users in its `user.db` (deny-all otherwise; RUNBOOK.md 7g).
- `authentik/n8n.yaml`: n8n's forward-auth (admins; `setup-ssh.sh n8n`, also run by `hardening`; RUNBOOK.md 7h).
- `homelab`: the CLI (`status`, `list`, `run JOB`, `logs JOB [-f]`, `check`, `configure`, `doctor`, `selftest`, `mcp`,
  `refresh-console`: the console's backend with the choice after an Apply); `jobs/jobs.toml`
  is the job registry it reads (also Ansible's unit list and polkit rule, `live-safe.sh`'s locks, ops' actions);
  `lib/` holds `render.py`, `configure.py`, `jobs.py`, `portable.sh` (GNU/BSD), `platform/{systemd,launchd}.sh`.
- `setup/components/*.toml` (42 components: services + compose profile, `.env` options, jobs, blueprints, wizard
  stages) and `setup/presets/` (`minimal`, `family` = this box, `everything`); `lib/setup/engine.py` resolves them
  (`./homelab components`, `./homelab setup --plan|--apply|--export-answers`); `COMPONENTS` / `COMPOSE_PROFILES` in `.env`.
  A new app = a component file plus its compose service (with `profiles: [<id>]`).
- `lib/wizard.sh`: the wizard library every `*-wizard.sh` sources (`HOMELAB_UI=tty|protocol`), with
  `lib/wizard-checks.sh` (the checks several wizards share: NetBird zones, the public path, `code`, `gen`, …);
  `lib/setup/run.py` is `./homelab setup [WIZARD…] [--check] [--protocol]` (the protocol: JSON lines, secrets only inward).
  Under `--protocol`, `lib/setup/bin/{sudo,ansible-playbook}` (first on PATH) ask for the sudo password through
  `lib/setup/askpass.py` → run.py's private socket → the front end (`{"t":"sudo"}`).
- `setup-app/` (Tauri 2, `README.md`): the guided setup app, local or over SSH (`russh`), driving the commands above
  (`--json`, `--set-secret`, `--protocol`); embeds `setup/bootstrap.sh` (fresh machine → checkout of a tag at its
  commit). `.github/workflows/setup-app.yml` checks it and builds Linux/macOS/Windows for tags (unsigned).
- `lib/setup/mcp.py` (+ `.mcp.json`, skill `.claude/skills/homelab-setup/SKILL.md`): the setup as an MCP server
  (plan, apply, stages one at a time via `./homelab setup WIZARD --stages=ID`, checks, logs); refuses secrets, sudo
  and `danger` steps, redacts every result, audit log `~/.local/state/homelab/mcp-audit.log`. The setup app's
  Assistant (`setup-app/src-tauri/src/assistant.rs`: Anthropic API with the key in the OS keychain, or Ollama)
  drives the same server and asks the person before every change.
- `.github/workflows/ci.yml`: shellcheck, golden, unit tests, Ansible syntax, `tests/distro/install.yml` in Fedora/Debian/
  Ubuntu/Arch containers, a macOS job; `setup-app.yml` for the app.
- macOS (best effort): `ansible/site-macos.yml` (Homebrew, Docker Desktop, native Ollama via `OLLAMA_URL`, `~/homelab-data`),
  launchd agents from `lib/jobs.py plists` running `lib/platform/launchd-run.sh JOB`; `macos` per job in `jobs.toml`.
- `tests/unit/` (`./homelab selftest`): render, configure, the registry against the unit templates, ops/server.py, the CLI,
  the wizard library (terminal output byte-identical to the old copies; protocol), the setup runner.
- `tests/golden.sh` (+ `tests/render.yml`, `tests/fixtures/full.env`, `tests/golden/`): compose config, Ansible's
  values, the units, the rendered blueprints, everything `./homelab configure` writes and (with a local `caddy`)
  `tests/caddy/matrix.py`'s request table, rendered for each instance under test (`--env FILE --golden DIR` for
  one, e.g. an instance's own in `instance/`, docs/instance/README.md), must equal the committed baseline;
  `--update` after a deliberate change.
- `family-app/`: the family desktop app (Tauri 2; `src-tauri/`, sidebar in `ui/`; name, bundle id and icons from `.env`
  via `./homelab configure` and `build.rs`), signs in through the browser via
  `authentik/family-app.yaml` (`setup-ssh.sh app`); RUNBOOK.md "Family app".

@INSTANCE.md
