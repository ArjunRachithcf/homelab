# The photo copy failed

`photos-local.sh` keeps a second copy of the photo library on the primary SSD (and, nightly,
deduplicates both). The photos themselves are safe in Immich; this is the local spare.

## From the console

1. **Jobs → Photos: copy to the SSD → Logs**.
2. If Immich isn't running (**Apps → immich-server**), restart it first.
3. **Run now** on *Photos: copy to the SSD*.

## Common causes

- **The second disk isn't mounted** (`/srv/nvme1`): after a power cut the disk may need a check.
  The admin runs `findmnt /srv/nvme1` and `sudo mount -a`.
- **The SSD is full**: see [Disk full](disk-full.md). The recycle bin of the copy can be emptied
  from the home page's **Disk** sheet.

## On the machine (admin)

```
./homelab logs photos-copy
./homelab logs photos-local
```
