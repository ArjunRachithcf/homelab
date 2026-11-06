# The files snapshot or copy failed

`files-local.sh` takes a snapshot of OpenCloud's files every night (OpenCloud stops for a few
seconds) and copies it to the primary SSD. People's files in OpenCloud are not affected.

## From the console

1. **Jobs → Files: snapshot + second copy → Logs**.
2. Check that **opencloud** is running under **Apps**. The script starts it again after the
   snapshot; if it didn't, **Restart** it. People can't sync until it's back.

## Common causes

- **The second disk isn't mounted**: as for photos, the admin checks `findmnt /srv/nvme1`.
- **Reflink copy failed**: the files folder must be on the XFS second disk. Only the admin can fix
  the layout (RUNBOOK.md 7e).
- **The SSD is full**: see [Disk full](disk-full.md).

## On the machine (admin)

```
./homelab logs files-local
./homelab run files-local     # prints the sudo command: this job isn't started from the console
```
