# Proton Drive CLIs (since 2026-10-23)

Two builds of Proton's Drive CLI, both kept current by `update.sh` (weekly,
`ansible/roles/timers/templates/homelab-proton-drive-update.{service,timer}.j2`, Sundays 02:30):

| Binary | Used for | Session in `pass` |
|---|---|---|
| `~/.local/opt/proton-drive/proton-drive`: Proton's official release, SHA-512 from `version.json` | folder backup (`backup-photos.sh`: library, DB dumps, age tars → `/my-files/homelab`) | `ch.proton.drive/drive-sdk-cli/auth-session` |
| `~/.local/opt/proton-drive-homelab/proton-drive`: built from Proton's source ([ProtonDriveApps/sdk](https://github.com/ProtonDriveApps/sdk), MIT) at the same release + `live-photos.patch` | Proton Photos + album Family (`backup-photos.sh`, after the folder backup) | `homelab/proton-photos/auth-session` |

Why the patch: the official `photo upload` uploads `IMG_x.HEIC` and `IMG_x.MOV` as two unrelated
items (the SDK can link them, the CLI doesn't), and makes thumbnails only for JPEG/PNG/…, so HEIC
photos and videos show blank tiles. The patch (2 files) adds:
- `--live-photos`: still + `.MOV` with the same name in one folder → still (tag Live Photo), then
  the video with `mainPhotoNodeUid` = the still. A still already in Proton gets its video attached.
- `HOMELAB_PREVIEW_DIR`: no thumbnail of its own → `<dir>/<sha1>.jpg` (Immich's preview) is used.
- its own credentials path, so it's a separate session you can revoke on its own.

It identifies honestly as `external-drive-homelab_photos@<version>` (Proton's SDK rules: never
the official `cli-drive`).

`update.sh`: reads `https://proton.me/download/drive/cli/version.json`; installs a newer official
binary after the SHA-512 check; rebuilds the patched one at Proton's commit "update changelog for
cli/vX" with the exact Bun version the CLI pins (checksum-verified), runs Proton's test suite and a
smoke test, and only then swaps the binary. If the patch no longer applies or tests fail, the unit
fails (`systemctl --failed`, `journalctl -u homelab-proton-drive-update`) and the old build stays.
Then: update the patch in `~/src/proton-drive-sdk` (branch `homelab-live-photos`),
`git format-patch -1 --stdout > live-photos.patch`, run `./update.sh`.

Re-login: `PROTON_DRIVE_CREDENTIALS_STORE=pass <binary> auth login` (each has its own session).
When Proton ships Live Photos and HEIC/video thumbnails in the official CLI, drop the patched
build and use the official one for Photos too.
