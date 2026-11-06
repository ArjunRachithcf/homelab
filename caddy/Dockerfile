# Caddy with the Infomaniak DNS module, for Let's Encrypt DNS-01 (RUNBOOK.md "HTTPS").
# Keep both tags on the same Caddy version.
FROM docker.io/library/caddy:2.11.7-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/infomaniak@v1.0.2

FROM docker.io/library/caddy:2.11.7
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
