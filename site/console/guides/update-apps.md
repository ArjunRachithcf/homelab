# Updating the apps

Versions are pinned in `.env` (`*_TAG`) and `docker-compose.yml`. Updating is the admin's job,
one app at a time, after a fresh backup.

1. **Backup → Run now** and wait for *Done*.
2. Read the app's release notes for breaking changes.
3. Raise its tag, then:

```
docker compose pull <service>
docker compose up -d <service>
```

4. Watch **Apps** here for a minute; open the app once.
5. Authentik: diff our compose against the upstream `compose.yml` first, and after the upgrade run
   `./authentik/setup-ssh.sh hardening` (upgrades re-apply its default blueprints).

If an update goes wrong: put the old tag back and `docker compose up -d <service>`; the database
dumps in the backup restore older data if the app migrated it.
