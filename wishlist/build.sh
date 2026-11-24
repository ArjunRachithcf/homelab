#!/usr/bin/env bash
# ~/homelab/wishlist/build.sh: builds our Wishlist image, upstream's tagged source + claims-hidden.patch
# (RUNBOOK.md 7f, README.md here). Run by hand (or by family-wizard.sh) after changing WISHLIST_TAG.
#
#   ./wishlist/build.sh            build homelab-wishlist:<WISHLIST_TAG>-claims (skips if it exists)
#   ./wishlist/build.sh --force    build again
#
# 1. github.com/cmintey/wishlist at tag v<WISHLIST_TAG>, into ~/src/wishlist (shallow, re-cloned per tag).
# 2. claims-hidden.patch on top; if it no longer applies, it stops: the running image stays.
# 3. Upstream's own Dockerfile builds the image (its build stage installs, type-builds and prunes).
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=""; [[ -f .env ]] && TAG=$(sed -n "s/^WISHLIST_TAG=//p" .env | tail -n1 | tr -d "'\"")
TAG=${TAG:-0.69.0}
IMAGE=homelab-wishlist:$TAG-claims
SRC=$HOME/src/wishlist
PATCH=$PWD/wishlist/claims-hidden.patch

if [[ ${1:-} != --force ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "✓ $IMAGE exists (--force builds it again)"; exit 0
fi

echo "Wishlist v$TAG source…"
if [[ "$(git -C "$SRC" describe --tags --exact-match 2>/dev/null)" != "v$TAG" ]]; then
  rm -rf "$SRC"
  git clone -q --depth 1 --branch "v$TAG" https://github.com/cmintey/wishlist.git "$SRC"
fi
git -C "$SRC" reset -q --hard "v$TAG"
git -C "$SRC" clean -qfdx
git -C "$SRC" apply --check "$PATCH" || { echo "✗ claims-hidden.patch doesn't apply to v$TAG: update it first" >&2; exit 1; }
git -C "$SRC" apply "$PATCH"
echo "✓ patched v$TAG ($(git -C "$SRC" rev-parse --short HEAD))"

echo "Image…"
docker build -q --build-arg VERSION="v$TAG" --build-arg SHA="$(git -C "$SRC" rev-parse --short HEAD)-claims" \
  -t "$IMAGE" "$SRC" >/dev/null
echo "✓ built $IMAGE"
