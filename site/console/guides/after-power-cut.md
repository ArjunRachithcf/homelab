# After a power cut

The machine starts by itself when power comes back (BIOS: restore on AC power loss). Everything
else starts on its own too. Give it 5 minutes, then check here.

1. **Apps**: all green? If one is red, **Restart** it.
2. **Jobs**: a job that was running when the power went may show as failed. **Run now** on
   *Backup* makes a fresh one.
3. **Second disk**: if photos or files are missing in the apps, the second disk didn't mount.
   Tell the admin (`findmnt /srv/nvme1`).
4. **Outside**: the next heartbeat (5 min) turns the outside status green again.

Plex runs outside Docker: if it doesn't play, the admin runs `sudo systemctl restart plexmediaserver`.
