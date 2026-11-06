# A disk is almost full

Below 10 % free, a disk turns red here. Backups and copies start failing when a disk is full.

## From the home page

The admin's **Disk** sheet (home page) shows what uses the space and offers cleanups, each with
what it would free:

- **Unused Docker images and build cache**: safe, always.
- **Recycle bins of the second copies**: files deleted in Immich/OpenCloud more than 7 days ago.
- **Older hourly backups**: thins archives older than 24 h to one a day.

## If that's not enough (admin)

- Photos or files grew: the second disk may need to be larger.
- Look for a runaway log: `sudo du -xh / --max-depth=2 | sort -h | tail`.
- `docker system df` shows what Docker holds.
