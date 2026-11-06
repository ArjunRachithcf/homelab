# Certificates

Caddy gets a certificate for every name from Let's Encrypt (DNS challenge at the DNS provider)
and renews it about 30 days before it expires. Fewer than 14 days left means renewals are failing.

## What to check (admin)

1. Caddy's log: `docker compose logs --tail 200 caddy | grep -i -E 'acme|certificate|error'`.
2. The DNS API token (`INFOMANIAK_API_TOKEN` in `.env`) may have expired or lost its scope.
3. The DNS zone: the wildcard record and `_acme-challenge` must not be blocked by a CAA record
   that leaves out Let's Encrypt.

"No valid certificate" can also mean the name isn't served on :443 at all (a component was turned
off): then it's expected.
