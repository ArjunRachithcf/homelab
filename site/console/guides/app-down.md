# An app isn't running

A red dot under **Apps** means its container stopped, keeps restarting, or reports itself unhealthy.

## From the console (over NetBird)

1. **Logs** first: the last lines often say why (a full disk, a database that isn't up yet).
2. **Restart**. The admin gets a mail naming you. Wait a minute, then reload this page.
3. Apps that come in parts (Immich: server, machine learning, database, redis): restart the
   database first, then the server.

## If it keeps failing

- **"permission denied"** on a file: usually a label after files were copied in by hand. The admin
  runs `restorecon -R` on that folder (SELinux stays on).
- **Database errors after an update**: tell the admin; don't restart it in a loop.
- **Out of memory**: the machine's memory is shared with the AI model (Ollama). Restarting
  `ollama` frees it.

## On the machine (admin)

```
docker compose ps
docker compose logs --tail 100 <service>
docker compose up -d <service>
```
