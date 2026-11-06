# The backup failed

The hourly backup (`backup.sh`) packs the apps' databases, settings and `.env` into one encrypted
archive in `backups/`. One failed run is not an emergency: the next one is at most an hour away.
Two failed runs in a row are.

## From the console

1. Open **Jobs → Backup → Logs** and read the last lines. The cause is usually in the last ten.
2. If it says a container wasn't running (Paperless, Authentik, Immich), look at **Apps** for a red
   dot, restart that app, then **Backup → Run now**.
3. If **Run now** succeeds, you're done. The admin got a mail about the failure; nothing else to do.

## Common causes

- **"No space left on device"**: see [Disk full](disk-full.md).
- **A database dump failed**: the app's database container was restarting. Run it again.
- **"another backup holds the lock"**: a backup was already running. Wait and look again.
- **"age: … recipient"**: `BACKUP_AGE_RECIPIENT` in `.env` is wrong. Only the admin can fix that.

## On the machine (admin)

```
./homelab logs backup
./homelab run backup
ls -lt backups/ | head
```
