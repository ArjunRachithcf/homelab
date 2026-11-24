#!/usr/bin/env bash
# wishlist/setup.sh: Wishlist's settings and admins in the running container (wishlist/README.md).
#   status        the settings wishlist/start.sh enforces, as they are in its database ("ok" last when they hold)
#   admin LOGIN   makes LOGIN (an Authentik username that has signed in once) a Wishlist admin
#   people        who has signed in and their own list, JSON (wishlist/draw.py reads it)
set -euo pipefail
cd "$(dirname "$0")/.."
# DATABASE_URL: upstream's entrypoint exports it for its own process only
run() { docker compose exec -T -e DATABASE_URL=file:/usr/src/app/data/prod.db wishlist pnpm -s tsx prisma/homelab-settings.ts "$@"; }
case "${1:-}" in
  status) run status ;;
  people) run people ;;
  admin)  [[ -n "${2:-}" ]] || { echo "usage: $0 admin LOGIN" >&2; exit 2; }; run admin "$2" ;;
  *)      echo "usage: $0 status | people | admin LOGIN" >&2; exit 2 ;;
esac
