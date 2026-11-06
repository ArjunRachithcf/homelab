#!/usr/bin/env bash
# ~/homelab/memos/build.sh: builds our Memos image, upstream's tagged source + sso-admin.patch
# (RUNBOOK.md 7f, README.md here). Run by hand (or by family-wizard.sh) after changing MEMOS_TAG.
#
#   ./memos/build.sh            build homelab-memos:<MEMOS_TAG>-sso (skips if it exists)
#   ./memos/build.sh --force    build again
#
# 1. github.com/usememos/memos at tag v<MEMOS_TAG>, into ~/src/memos (shallow, re-cloned per tag).
# 2. sso-admin.patch on top; if it no longer applies, it stops: the running image stays.
# 3. Upstream's tests for the touched packages, in golang (the version upstream's Dockerfile pins).
# 4. The web frontend (pnpm release, the Node and pnpm versions upstream's CI uses), then
#    upstream's scripts/Dockerfile.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=$(sed -n "s/^MEMOS_TAG=//p" .env | tail -n1 | tr -d "'\""); TAG=${TAG:-0.31.0}
IMAGE=homelab-memos:$TAG-sso
SRC=$HOME/src/memos
PATCH=$PWD/memos/sso-admin.patch

if [[ ${1:-} != --force ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "✓ $IMAGE exists (--force builds it again)"; exit 0
fi

echo "Memos v$TAG source…"
if [[ "$(git -C "$SRC" describe --tags --exact-match 2>/dev/null)" != "v$TAG" ]]; then
  rm -rf "$SRC"
  git clone -q --depth 1 --branch "v$TAG" https://github.com/usememos/memos.git "$SRC"
fi
git -C "$SRC" reset -q --hard "v$TAG"
git -C "$SRC" clean -qfdx
git -C "$SRC" apply --check "$PATCH" || { echo "✗ sso-admin.patch doesn't apply to v$TAG: update it first" >&2; exit 1; }
git -C "$SRC" apply "$PATCH"
echo "✓ patched v$TAG ($(git -C "$SRC" rev-parse --short HEAD))"

GO_IMAGE=$(sed -n 's/^FROM --platform=\$BUILDPLATFORM \(golang:[^ ]*\) AS backend$/\1/p' "$SRC/scripts/Dockerfile")
[[ -n "$GO_IMAGE" ]] || { echo "✗ no golang image in upstream's Dockerfile" >&2; exit 1; }
NODE=$(sed -n 's/^  NODE_VERSION: "\(.*\)"$/\1/p' "$SRC/.github/workflows/upgrade-smoke.yml"); NODE=${NODE:-24}
PNPM=$(sed -n 's/^  PNPM_VERSION: "\(.*\)"$/\1/p' "$SRC/.github/workflows/upgrade-smoke.yml"); PNPM=${PNPM:-11.0.1}

echo "Tests ($GO_IMAGE)…"
docker run --rm -v "$SRC":/src:z -v homelab-memos-gomod:/go/pkg/mod -w /src "docker.io/library/$GO_IMAGE" \
  sh -c "go test ./provider/idp/... ./server/api/v1/... >/dev/null; rc=\$?; chown -R $(id -u):$(id -g) /src; exit \$rc" \
  || { echo "✗ upstream tests fail with the patch" >&2; exit 1; }
echo "✓ tests pass"

echo "Frontend (node $NODE, pnpm $PNPM)…"
docker run --rm -v "$SRC":/src:z -v homelab-memos-pnpm:/pnpm -w /src/web -e PNPM_HOME=/pnpm -e npm_config_store_dir=/pnpm/store \
  "docker.io/library/node:$NODE-alpine" sh -c "npm i -g -s pnpm@$PNPM && pnpm i -s --frozen-lockfile && pnpm -s release; rc=\$?; chown -R $(id -u):$(id -g) /src; exit \$rc" >/dev/null
[[ -f "$SRC/server/frontend/dist/index.html" ]] || { echo "✗ no frontend build" >&2; exit 1; }

echo "Image…"
docker build -q -f "$SRC/scripts/Dockerfile" --build-arg VERSION="$TAG" \
  --build-arg COMMIT="$(git -C "$SRC" rev-parse --short HEAD)-sso" -t "$IMAGE" "$SRC" >/dev/null
echo "✓ built $IMAGE"
