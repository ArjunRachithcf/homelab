// tests/caddy/build: Caddy as caddy/Dockerfile builds it (2.11.7 + caddy-dns/infomaniak), for
// tests/caddy/matrix.py outside Docker (CI): go build -o caddy . (keep both versions in step)
package main

import (
	caddycmd "github.com/caddyserver/caddy/v2/cmd"
	_ "github.com/caddy-dns/infomaniak"
	_ "github.com/caddyserver/caddy/v2/modules/standard"
)

func main() { caddycmd.Main() }
