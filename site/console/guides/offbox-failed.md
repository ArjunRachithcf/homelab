# The off-box copy (Proton Drive) failed

The encrypted archives, the photos and the files are copied to Proton Drive: every 4 hours
(quick) and every night (full, with a restore test on Sundays). The local backups are still
there; what's missing is the copy away from this house.

## From the console

1. **Jobs → Off-box copy → Logs** (quick) or **Off-box copy (nightly) → Logs**.
2. Try **Off-box copy (quick) → Run now** once. Proton's service has bad hours.

## Common causes

- **"not logged in" / "session expired"**: the Proton Drive login on this machine expired. Only the
  admin can log in again: `proton-drive auth login` on the machine.
- **Network**: check the home page's outside status. If the internet is down, it catches up later.
- **The CLI updated and broke**: *Proton Drive CLI update* runs on Sundays. Its log says which
  version it installed; the admin can pin the previous one (`proton-drive/README.md`).
- **Restore test failed** (Sunday): a file came back different. Tell the admin the same day.

## On the machine (admin)

```
./homelab logs offbox
./homelab logs photos-backup
```
