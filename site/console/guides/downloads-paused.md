# Downloads stopped

The downloads guard (every 10 minutes) stops every torrent when the disk with the media folder has less
free space than `DOWNLOADS_KEEP_FREE_PCT` (15 %), and starts them again once there's 5 points more. It
also brings qBittorrent, Prowlarr and Byparr back when the VPN container restarted: they share its network,
and a restarted VPN leaves them without one.

## Check, in this order

1. **The disk**: the **Disk** tile shows how full it is. Make room (old films, the cleanups there), then
   wait 10 minutes: the guard starts the torrents it stopped.
2. **The VPN**: a red dot at **Downloads** or **Search sites** under **Apps** means qBittorrent or
   Prowlarr has no network. Restart the VPN's container (gluetun) there; the guard brings them back.
3. **Still nothing?** The VPN's key may have expired (Proton's last about a year): the admin makes a new
   one and runs the step "Downloads on request → VPN key" again.

## On the machine (admin)

```
./homelab logs downloads-guard
docker compose ps gluetun qbittorrent prowlarr byparr
docker compose logs --tail 30 gluetun
```
