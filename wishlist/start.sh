#!/bin/sh
# wishlist/start.sh: Wishlist's start (compose's entrypoint): its database migrated and seeded, then our settings
# (wishlist/settings.ts: Authentik only, no sign-up, no passwords) written into it, then upstream's own entrypoint.
# Wishlist never listens with its defaults (sign-up on, the first visitor its admin): a failed step stops here.
set -eu
cd /usr/src/app
export DATABASE_URL="file:/usr/src/app/data/prod.db"
pnpm prisma migrate deploy
pnpm prisma db seed
pnpm -s tsx prisma/homelab-settings.ts
# upstream's (it runs the migrations again: a no-op)
exec sh entrypoint.sh
