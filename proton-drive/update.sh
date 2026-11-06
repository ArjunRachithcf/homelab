#!/usr/bin/env bash
# ~/homelab/proton-drive/update.sh: keep both Proton Drive CLIs current (README.md here).
# Run weekly by homelab-proton-drive-update.timer, or by hand. Exits non-zero (failed unit,
# visible in `systemctl --failed`) when an update was needed but couldn't be done; the
# installed binaries are only replaced after every check passed.
#
# 1. Official CLI (folder backup): Proton's version.json names the latest stable release and its
#    SHA-512; a newer one is downloaded, verified and installed to $OFFICIAL.
# 2. Patched CLI (Proton Photos: Live Photos + thumbnails): rebuilt from Proton's source at the
#    commit of that same release ("update changelog for cli/vX"), with live-photos.patch on top,
#    the exact Bun version the CLI pins (checksum-verified), Proton's test suite, then a smoke
#    test against the account. If the patch no longer applies, it stops and the old build stays.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PROTON_DRIVE_CREDENTIALS_STORE=pass
MANIFEST=https://proton.me/download/drive/cli/version.json
OPT=$HOME/.local/opt
OFFICIAL=$OPT/proton-drive/proton-drive
PATCHED=$OPT/proton-drive-homelab/proton-drive
SRC=$HOME/src/proton-drive-sdk
STATE=${STATE_DIRECTORY:-$HOME/.local/state/homelab}; mkdir -p "$STATE"
BUILT="$STATE/proton-drive-homelab.built"   # "<version> <upstream commit> <patch sha256>"
APP_NAME=external-drive-homelab_photos      # honest x-pm-appversion (Proton's SDK rules)

read -r VERSION URL SHA < <(curl -sSf -m 30 "$MANIFEST" | jq -r '.Releases[] | select(.CategoryName == "Stable")
  | .Version as $v | .Files[] | select(.Platform == "linux/x64") | "\($v) \(.Url) \(.Sha512CheckSum)"' | head -n1)
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$URL" == https://proton.me/* && ${#SHA} -eq 128 ]] \
  || { echo "unexpected version.json" >&2; exit 1; }
echo "Latest stable proton-drive: $VERSION"

# ── 1. Official ──────────────────────────────────────────────────────────
if [[ -x "$OFFICIAL" ]] && "$OFFICIAL" version 2>/dev/null | grep -q "cli-drive@$VERSION+"; then
  echo "official: up to date"
else
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -sSfL -m 300 -o "$tmp/proton-drive" "$URL"
  echo "$SHA  $tmp/proton-drive" | sha512sum -c --quiet - || { echo "official: checksum mismatch, not installed" >&2; exit 1; }
  chmod 0755 "$tmp/proton-drive"
  "$tmp/proton-drive" version | grep -q "cli-drive@$VERSION+" || { echo "official: wrong version inside" >&2; exit 1; }
  mkdir -p "${OFFICIAL%/*}"; mv -f "$tmp/proton-drive" "$OFFICIAL"
  echo "official: installed $VERSION"
fi

# ── 2. Patched ───────────────────────────────────────────────────────────
[[ -d "$SRC/.git" ]] || git clone -q --filter=blob:none https://github.com/ProtonDriveApps/sdk.git "$SRC"
git -C "$SRC" fetch -q origin main
COMMIT=$(git -C "$SRC" log origin/main -1 --format=%H --grep="update changelog for cli/v$VERSION\$")
[[ -n "$COMMIT" ]] || { echo "patched: no release commit for cli/v$VERSION in Proton's repo yet" >&2; exit 1; }
WANT="$VERSION ${COMMIT:0:7} $(sha256sum "$HERE/live-photos.patch" | cut -c1-12)"
if [[ -x "$PATCHED" && -f "$BUILT" && "$(cat "$BUILT")" == "$WANT" ]]; then
  echo "patched: up to date ($WANT)"; exit 0
fi

echo "patched: building $WANT…"
git -C "$SRC" checkout -q -f -B homelab-live-photos "$COMMIT"
git -C "$SRC" clean -qfdx -e node_modules
git -C "$SRC" -c user.name=homelab -c user.email=homelab@localhost am -q --3way "$HERE/live-photos.patch" \
  || { git -C "$SRC" am --abort || true; echo "patched: live-photos.patch no longer applies to cli/v$VERSION; old build kept" >&2; exit 1; }

# The exact Bun the CLI pins, from Bun's GitHub release, checksum-verified
BUN_V=$(jq -r .engines.bun "$SRC/cli/package.json")
BUN=$OPT/bun-$BUN_V/bun
if [[ ! -x "$BUN" ]]; then
  t=$(mktemp -d)
  gh release download "bun-v$BUN_V" -R oven-sh/bun -p bun-linux-x64.zip -p SHASUMS256.txt -D "$t"
  (cd "$t" && grep ' bun-linux-x64.zip$' SHASUMS256.txt | sha256sum -c --quiet - && unzip -q bun-linux-x64.zip)
  mkdir -p "${BUN%/*}"; install -m 0755 "$t/bun-linux-x64/bun" "$BUN"; rm -rf "$t"
fi
export PATH="${BUN%/*}:$PATH"

(cd "$SRC/client/js" && bun install --silent)
(cd "$SRC/incubating/account/js" && bun install --silent --frozen-lockfile)
cd "$SRC/cli"
bun install --silent --frozen-lockfile
node node_modules/jest/bin/jest.js src --silent >/dev/null 2>"$STATE/proton-drive-homelab.test.log" \
  || { echo "patched: Proton's CLI tests fail with the patch ($STATE/proton-drive-homelab.test.log); old build kept" >&2; exit 1; }
CLI_APP_VERSION_NAME=$APP_NAME bun run build >/dev/null
./release/proton-drive version | grep -q "$APP_NAME@$VERSION" || { echo "patched: unexpected version string" >&2; exit 1; }
./release/proton-drive photo upload --help | grep -q -- --live-photos || { echo "patched: --live-photos missing" >&2; exit 1; }
./release/proton-drive photo timeline >/dev/null || { echo "patched: can't reach Proton Photos (session? run auth login, README.md)" >&2; exit 1; }

mkdir -p "${PATCHED%/*}"; install -m 0755 ./release/proton-drive "$PATCHED.new"; mv -f "$PATCHED.new" "$PATCHED"
echo "$WANT" >"$BUILT"
echo "patched: installed $WANT"
