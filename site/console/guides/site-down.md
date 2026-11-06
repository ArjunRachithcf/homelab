# Not reachable from outside

The heartbeat pings healthchecks.io every 5 minutes. When pings stop or report a problem, the
outside status on the home page turns red and the admin gets a mail.

## Check, in this order

1. **Are you on NetBird?** If this page loads, the machine and Caddy are up.
2. **The apps**: any red dot under **Apps**? Restart it, then wait 5 minutes for the next heartbeat.
3. **The internet at home**: is the router online? Nothing here can fix the router.
4. **NetBird**: the public sites go through NetBird's reverse proxy. If NetBird's own status page
   shows an incident, wait.
5. **The certificates** section: a red one means HTTPS fails for that name, see
   [Certificates](certificate.md).

## On the machine (admin)

```
netbird status
docker compose ps
./homelab logs heartbeat
```
