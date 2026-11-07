# Homelab Setup (the app)

A small desktop app (Tauri 2, Linux, macOS and Windows) that sets the homelab up, on the computer
it runs on, or on another one at home over SSH. It chooses; the machine itself does the work with the
same commands a person would type, so the app, the terminal (`./homelab setup`) and an AI assistant
(Phase 3c) all end up with the same `.env` and the same running stack.

## What it does

1. **Connect.** *This computer* (Linux, macOS), or *another computer* by its address, user and password
   or key. On the first connection it shows the computer's host key fingerprint to compare (as `ssh`
   would ask), remembers it, and refuses a changed key unless you confirm it again. A password with a
   one-time code goes in as `password;123456` (this homelab's own sshd asks that way).
2. **This machine.** What's there (system, Python, git, the homelab, `.env`). Without the homelab it
   runs `setup/bootstrap.sh` (embedded in the app): installs git and Python 3.11+ if needed and clones
   the release the app was built from, checking the exact commit.
3. **Choose.** A preset (`setup/presets/`), then any component on or off (`setup/components/`), with
   what each needs added by itself. **Basic** asks only for what's still needed; **Advanced** shows
   every option and the graphics card. The panel on the right is `./homelab setup --plan --json`, live.
   **Save answers…** writes the choice as an answers file for `./homelab setup --apply --answers FILE`
   on another machine (never a secret in it). **Apply** writes `.env` there (secrets are generated
   there and never shown; a secret you type, like a DNS token, goes over stdin with `--set-secret`), runs
   `./homelab configure` and gives a running console the new choice (`./homelab refresh-console`: its
   backend reads the components when it starts). A machine that is already set up, with nothing chosen differently, goes on
   with **Set up without changes**. On a machine with a `.env` the panel lists what the choice switches on
   and off against it (a preset replaces the whole choice), and Apply asks before it switches anything off:
   that leaves the home page and Caddy (once recreated), backups and timed jobs may leave it out, and its
   containers keep running. After Apply the Set up page shows what `./homelab configure` and the console said and the
   command that stops what was switched off.
4. **Set up.** `./homelab setup --protocol`: the wizards' stages of what you chose. The page lists them by
   part (wizard), titled as in the wizards: **Start everything** runs them all in order, a part's **Start**
   only that wizard, a step's **Run** only that stage (`--stages=ID`); **Check everything** or a part's
   **Check** runs its `--check`, and each part then says whether all its checks pass. Questions become
   fields and buttons; steps elsewhere (DNS, a phone, a dashboard) show their link and wait; a command
   that needs its own terminal is shown to copy; `sudo` asks here once (kept in the app's memory until it
   closes, handed only to sudo). The wizards' bold and colours show as such.

## The assistant (optional)

**Assistant** in the sidebar opens a chat that does the same with you: it lists the components, works out
a plan, saves the choice, runs the setup steps one by one and reads checks and logs. It uses the tools of
`lib/setup/mcp.py` (the MCP server Claude Code uses through `.mcp.json`), started on the machine like
everything else (`./homelab mcp`, which finds a Python 3.11+ as the other commands do). Whatever changes something (saving the choice, an option, running or answering a step)
waits for your **Allow**. The server never lets it see or set a secret, the sudo password, or confirm a
step that destroys data: those steps stop and name the command you run yourself. Results are redacted
(each secret becomes `‹NAME›`) and every call is logged on the machine (`~/.local/state/homelab/mcp-audit.log`).

- **Anthropic (Claude)**: an API key from console.anthropic.com, kept in the OS keychain (macOS Keychain,
  Windows Credential Manager, the Secret Service on Linux), never in a file. Default model `claude-opus-5-5`;
  a refused request is retried on the model the API picks (`fallbacks: "default"`). What the assistant reads
  goes to Anthropic.
- **Ollama**: a model with tool use on your own network (e.g. `ollama pull qwen3`; the homelab's own Ollama
  is reachable only from inside, so a laptop's Ollama or another one). Nothing leaves your network.
- **Claude Code** instead: open the repository in Claude Code on the machine; it offers the `homelab-setup`
  MCP server from `.mcp.json` and the skill `.claude/skills/homelab-setup/SKILL.md` (also the assistant's
  instructions here).

## How it works

- `src-tauri/src/transport.rs`: a local child process or an SSH channel (`russh`), the same interface.
  Every command is an argv; over SSH each word is quoted. Tests: `cargo test` (quoting, local run and
  stream) and `tests/setup-app/ssh.sh` (a throwaway sshd on 127.0.0.1: host key, key login, quoting,
  exit codes, streaming).
- `src-tauri/src/main.rs`: the commands the window may call (also listed in `build.rs` and
  `capabilities/default.json`; `tests/unit/test_setup_app.py` checks the three agree).
- `src-tauri/src/assistant.rs`: the MCP client (JSON-RPC over the same transport), the two providers
  (Anthropic Messages API over raw HTTP, `anthropic-version: 2026-10-21`; Ollama `/api/chat`), the keychain.
  Tests: request/response shapes against recorded fixtures, errors in words, and a round trip with the real
  `lib/setup/mcp.py`.
- `ui/`: the window, plain HTML/CSS/JS in the home page's look. `tests/setup-app/ui.test.js` drives it
  in Chromium with Tauri mocked: the real engine for plan/apply and a protocol session recorded through
  `lib/setup/run.py` and `lib/wizard.sh` (every kind of step), then checks every answer and that no
  secret ever shows.
- sudo without a terminal: under `--protocol`, `lib/setup/bin/sudo` and `lib/setup/bin/ansible-playbook`
  come first on PATH and ask through `lib/setup/askpass.py`, which reaches `run.py` over a private Unix
  socket; `run.py` sends `{"t":"sudo"}` and passes the answer back to sudo alone.

## Building

```
cd setup-app/src-tauri
cargo install tauri-cli --version '^2' --locked   # once
HOMELAB_REPO=https://github.com/<you>/homelab.git HOMELAB_REF=v1.0.0 HOMELAB_COMMIT=<sha of v1.0.0> cargo tauri build
```

Linux needs the WebKitGTK 4.1 development packages (Fedora `webkit2gtk4.1-devel`, Debian/Ubuntu
`libwebkit2gtk-4.1-dev`). Without the three variables the app clones `main` of the upstream repository
and checks no commit. `.github/workflows/setup-app.yml` builds all three systems for every tag (and on
demand) with them set from the tag.

The builds are **not signed**. macOS: right-click the app → Open the first time (Gatekeeper). Windows:
SmartScreen → More info → Run anyway. Linux: the AppImage or the .deb/.rpm.
