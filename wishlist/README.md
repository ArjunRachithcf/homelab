# wishlist/

Wishlist (gifts.<domain>, RUNBOOK.md 7f): the family's wish lists. Our build of it.

## The build: `build.sh` + `claims-hidden.patch`

Upstream Wishlist hides claims from a list's owner in its interface only: the list page's data and its live updates
still carry every claim, with who claimed it and whether it was bought. Anyone who opens the browser's developer tools
on their own list sees what they're getting. `claims-hidden.patch` changes that:

- the list page sends the owner no claims at all, before filtering and sorting, so neither `?filter=` nor the order
  gives them away;
- the list's live updates (`/lists/<id>/events`) do the same for the owner;
- list managers still see claims (a parent managing a kid's list), and so does the owner of a list marked
  "not for me" or when the admin setting "show claims to the owner" is on: upstream behaviour.

`./wishlist/build.sh` clones upstream's tag `v$WISHLIST_TAG` to `~/src/wishlist`, applies the patch and builds the
image with upstream's own Dockerfile: `homelab-wishlist:<WISHLIST_TAG>-claims` (compose never pulls it). To upgrade:
set WISHLIST_TAG in `.env`, `./wishlist/build.sh`, `docker compose up -d wishlist`. If the patch no longer applies,
the build stops and the running image stays; refresh it in `~/src/wishlist` and save it with
`git add -N tests/ui/claims-hidden.spec.ts && git diff > ~/homelab/wishlist/claims-hidden.patch`.

## The test: `tests/ui/claims-hidden.spec.ts` (in the patch)

In upstream's Playwright suite: the owner's list data (`/lists/<id>/__data.json`) and live updates carry no claim,
while another member's do (the control). It fails on upstream's image and passes on ours. Run it against a throwaway
container on `127.0.0.1:3280` (password login on, never the `homelab` project), with a Playwright config that drops
upstream's `webServer` (its compose runs on the host network): `pnpm exec playwright test -c <that config>
tests/ui/claims-hidden.spec.ts`.

## Settings: `start.sh` + `settings.ts`, `setup.sh`

Wishlist reads no setting from its environment: sign-up, password login, OIDC and the rest live in its database
(table `system_config`, its admin UI), with sign-up on by default and the first account its admin. So compose starts
it with `start.sh`: its migrations and seed, then `settings.ts` (run with the image's own tsx and Prisma client)
writes ours, then upstream's entrypoint. A failed step stops the start: Wishlist never listens with its defaults.

At every start, whatever the admin UI changed:

- sign-up **off**, password login **off**, Authentik only (OIDC from compose's `OIDC_*`, auto-redirect,
  auto-register, profile sync);
- Claims: **show to the owner off** (the patch relies on it);
- Lists: **public lists off** until the household decides who may make one (kids' lists: only with an adult's
  approval, which Wishlist can't enforce yet);
- the default group: everyone who signs in joins it and gets a list (without it, only the first one would);
- an inactive placeholder user (`homelab-placeholder`, like Authentik's `akadmin`): while Wishlist has no user, its
  `/login` sends everyone to the setup wizard before it reads Authentik's answer, so the first sign-in could never
  finish. The placeholder has no password, no Authentik link, no group and no list.

Once, then the admin's: Suggestions **on, "Approval required"**: others can suggest a wish, the list's owner
approves it. Not "Surprise me": that mode hides from the owner every item someone else added, judged at every read,
so wishes imported or added for them by someone else would vanish from their own list.

Caddy refuses Wishlist's own account routes (`/signup` with or without an invite, password reset and recovery) and
sends its setup wizard to Authentik (`caddy/components/gifts/sites.caddy`).

Wishlist has no admin group: everyone signs in as a member. `./wishlist/setup.sh admin LOGIN` makes LOGIN (an
Authentik username that has signed in once) an admin; `family-wizard.sh` does it for you after your first sign-in.
`./wishlist/setup.sh status` shows the enforced settings as they are (never the client secret), `ok` last when they
hold.

## Draw names: `draw.py`

Wishlist has no gift exchange ("Secret Santa"; upstream cmintey/wishlist#797), so `draw.py` draws names beside it:
each giver gets a mail (through `notify.sh`, the box's Proton SMTP) naming the one person they give to, with a
link to that person's Wishlist list, the budget and a note. Nobody sees the whole draw, the one who runs it
included: no pair is printed or logged, and the pairs are kept only encrypted to `BACKUP_AGE_RECIPIENT`
(`~/.local/state/homelab/draws/<event>.age`), readable with the backup's private key, which isn't on this box.

1. An event file per exchange, in your instance's own folder (household names never go into the public repo):
   copy `draw.example.toml`; participants are Authentik usernames, `exclude` pairs never draw each other.
2. Everyone taking part signs in to Wishlist once (that makes their list).
3. `./wishlist/draw.py plan EVENT.toml`: who's missing, whether the exclusions allow a draw.
4. `./wishlist/draw.py draw EVENT.toml`: draws and mails everyone. `--redraw` starts over (everyone gets a new mail).
5. Someone lost their mail: `./wishlist/draw.py resend EVENT.toml LOGIN --identity <age key file>`.

What someone claims on their person's list stays hidden from that person (`claims-hidden.patch`); the others in the
family see the claim, so two people don't buy the same thing.
