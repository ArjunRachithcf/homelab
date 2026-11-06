# memos/

Memos (notes.<domain>, RUNBOOK.md 7f). Two parts: our build of it, and its settings.

## The build: `build.sh` + `sso-admin.patch`

Upstream Memos lets admins sign in with a password even when password sign-in is off, lets the
first password sign-up become admin, and never makes an SSO user an admin. That would leave one
login on the internet without Authentik and its second step. `sso-admin.patch` changes that:

- password auth off → no password sign-in for anyone (admins too) and no first-user password sign-up;
- `MEMOS_SSO_ADMIN_GROUP` (compose: `authentik Admins`) → members of that group in the `groups`
  claim are ADMIN, everyone else USER, re-checked at every SSO sign-in. Unset: upstream behaviour;
- web: a fresh instance with password auth off shows the identity provider's button instead of the
  "create admin account" password form; the first SSO sign-in ends the setup;
- tests for both (`server/api/v1/test/sso_admin_group_test.go`).

`./memos/build.sh` clones upstream's tag `v$MEMOS_TAG` to `~/src/memos`, applies the patch, runs
upstream's tests for the touched packages, builds the web frontend and the image with upstream's
own Dockerfile: `homelab-memos:<MEMOS_TAG>-sso` (compose never pulls it). To upgrade: set MEMOS_TAG
in `.env`, `./memos/build.sh`, `docker compose up -d memos`. If the patch no longer applies, the
build stops and the running image stays; refresh it in `~/src/memos` and save it with
`git add -N server/api/v1/test/sso_admin_group_test.go && git diff -- . ':(exclude)server/frontend/dist' > ~/homelab/memos/sso-admin.patch`.

## Settings: the JSON files

Memos reads its settings from JSON files in `/etc/secrets` at every start; the compose command copies
them there. File settings are locked in its UI.

- `memos-instance-setting-general.json`: no password sign-in or sign-up (Authentik only; SSO users
  are created on first sign-in), usernames fixed, weeks start on Monday.
- `memos-instance-setting-access.json`: private instance, nothing visible without signing in.
- `memos-instance-setting-ai.json`: no AI providers.
- `idp-authentik.json.in`: the Authentik login; `__SECRET__`, `__AUTH__` and `__BRAND__` are filled from the
  environment (`MEMOS_OIDC_CLIENT_SECRET`, `BRAND`), so no secret is in this folder.

Changes need `docker compose up -d --force-recreate memos`.
