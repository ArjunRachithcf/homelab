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

## Settings

Sign-in is set from compose (Authentik only, no passwords, no sign-up; `authentik Admins` = Wishlist's admins, at
every sign-in). The rest lives in Wishlist's admin settings (its database), set once by `family-wizard.sh`'s first
sign-in stage:

- Claims: **show to the owner off** (the default; the patch relies on it);
- Suggestions: **on, "Approval required"**: others can suggest a wish, the list's owner approves it. Not "Surprise
  me": that mode hides from the owner every item someone else added, judged at every read, so wishes imported or
  added for them by someone else would vanish from their own list;
- Lists: **public lists off** until the household decides who may make one (kids' lists: only with an adult's
  approval, which Wishlist can't enforce yet).
