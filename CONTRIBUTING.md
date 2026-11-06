# Contributing

Thanks for looking. The project is early (README): it runs one family's homelab and is being made ready for others,
so expect rough edges in a first run.

- **Before a change:** `./homelab selftest` and `tests/golden.sh`. Golden needs `docker compose` (no daemon), and
  for all of it `ansible-playbook` and a Caddy built from `tests/caddy/build/`. A change that should keep behaviour
  keeps the golden files identical; a deliberate change runs `tests/golden.sh --update` and says in the PR what
  changed and why.
- **Checks:** shellcheck at warning level for shell scripts; `cargo clippy -- -D warnings` and `cargo test` in
  `setup-app/src-tauri` and `family-app/src-tauri` when you touch them.
- **No instance data in code:** a domain, a name, an address or a path of one homelab belongs in `.env`
  ("Your instance" in `.env.example`); `./homelab configure` renders what comes from it. `tests/unit/test_public_tree.py`
  catches the obvious ones.
- **A new app:** `setup/components/<id>.toml`, its compose service with `profiles: [<id>]`, its Caddy files in
  `caddy/components/<id>/` and a tile; then `python3 lib/setup/engine.py docs > docs/components.md`. The unit tests
  name what's still missing.
- **Licence:** what you contribute is under Apache-2.0 (its section 5); no sign-off needed.

Security reports: SECURITY.md, never a public issue.
