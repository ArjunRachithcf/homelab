# Instance: <your homelab's name>

<!-- Copy this to INSTANCE.md at the top of the repository (CLAUDE.md imports it) and fill it in. It tells an
     assistant working on your machine what's yours: who decides, what's fixed, what not to touch. -->

You are working on <owner>'s homelab, on <machine and OS>, in `~/homelab`. <Owner> is at the keyboard for
the parts only they can do. The design is decided; don't redesign it.

## Goal

- Which components run (`COMPONENTS` in `.env`) and anything about them that isn't obvious from
  `docs/components.md` (who uses what, what's public, which devices).

## Hard constraints

- What must never change: e.g. no router access; the DNS zone also carries mail records nobody may touch;
  SELinux stays enforcing; Docker, not Podman.
- Anything that was considered and rejected, so it isn't proposed again.

## Ports

- The ports that are open and to whom (the mesh groups), if they differ from the defaults.
- With the downloads: the VPN provider's forwarded port, open on the VPN's address (not this machine's, not the
  router's).

## What <owner> does themselves (stop and ask, then wait)

- Dashboards (NetBird, the DNS provider), the first admin account, hardware keys, BIOS settings, any sudo
  password prompt.
- With the downloads: the VPN's key; with the request page, Jellyfin's own admin password once (Seerr's first admin,
  never stored).

## How to work

- Say what you'll change before each step, run its check after it.
- Secrets go straight into `.env`; never print them.

## Done when

- `./homelab check` passes, the home page signs in, the first backup exists and <owner> knows where.
