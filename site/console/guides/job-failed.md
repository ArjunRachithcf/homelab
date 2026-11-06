# A job failed

Every scheduled job mails the admin when it fails (with its last journal lines), and shows a red
dot here.

1. Open its **Logs** in the **Jobs** list. The last lines usually say why.
2. If the job has **Run now**, try once. Two failures in a row: tell the admin.
3. Jobs without **Run now** need the admin: they run as root or take long (the nightly ones).

Each job's own guide (when there is one) is linked from **Needs a look**.

```
./homelab status
./homelab logs <job>
```
