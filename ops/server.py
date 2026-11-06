"""~/homelab/ops/server.py: the home page's detail sheets talk to this (RUNBOOK.md 7e, "Details").

Container `ops` (python stdlib only, read-only rootfs, no ports: Caddy reaches it on the compose
network, and only for admins and operators, see Caddyfile `(home)`). The console (site/console/)
and the home page's detail sheets use it.

  GET  /ops/data/<name>.json   a report from /data (written on the host by ops-report.py,
                               storage-report.py, console-report.py, ops-run.sh)
  POST /ops/run/<name>         drops the file /requests/<name> (its content: who asked); on the host
                               homelab-ops.path starts ops-run.sh, which maps the name to a fixed command

Nothing from a request is used but the name, checked against ops/access.py (the registry's actions,
run-/logs-job-<job>, restart-/logs-app-<service> of the chosen components), and the signed-in
username, only for the restart mail. Only Caddy's address (OPS_TRUSTED_PROXY) is answered. A POST must come from the home page itself: Origin is
home.<domain> and the header X-Homelab-Ops is set (a cross-site form can't set it, a cross-site
fetch can't without CORS, which this never answers).
"""
import json
import os
import re
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import access  # noqa: E402  (ops/access.py, mounted next to this file)

DATA = "/data"
REQUESTS = "/requests"
# What may be asked for: the job registry (jobs/jobs.toml) and the components (setup/components/),
# both mounted read-only, for the chosen components (COMPONENTS from .env)
with open(os.environ.get("OPS_REGISTRY", "/app/jobs.toml"), "rb") as _f:
    _ALLOWED = access.allowed(tomllib.load(_f), Path(os.environ.get("OPS_COMPONENTS_DIR", "/app/components")),
                              [c for c in os.environ.get("OPS_COMPONENTS", "").split(",") if c])
JOBS = frozenset(access.names(_ALLOWED))
REPORTS = frozenset({"outside", "backups", "storage", "disk-history", "jobs", "console"}
                    | {f"logs-job-{j}" for j in _ALLOWED["logs-job"]} | {f"logs-app-{s}" for s in _ALLOWED["logs-app"]})
USERNAME = re.compile(r"[A-Za-z0-9._@+-]{1,64}")
# The home page with and without the public port (NetBird peers may use :443)
ORIGINS = set(filter(None, os.environ.get("OPS_ORIGINS", "").split()))
# Only Caddy (its fixed compose address): every other container on the network could otherwise send
# the forward-auth headers itself
TRUSTED = set(filter(None, os.environ.get("OPS_TRUSTED_PROXY", "").split()))


class Handler(BaseHTTPRequestHandler):
    server_version = "ops"
    sys_version = ""

    def reply(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def from_caddy(self):
        if self.client_address[0] in TRUSTED:
            return True
        self.reply(403, {"error": "only through Caddy"})
        return False

    def do_GET(self):
        if not self.from_caddy():
            return
        m = re.fullmatch(r"/ops/data/([a-z0-9-]+)\.json", self.path.split("?", 1)[0])
        if not m or m[1] not in REPORTS:
            return self.reply(404, {"error": "unknown report"})
        try:
            with open(os.path.join(DATA, m[1] + ".json"), "rb") as f:
                return self.reply(200, f.read())
        except FileNotFoundError:
            return self.reply(200, {"missing": True})

    def do_POST(self):
        if not self.from_caddy():
            return
        m = re.fullmatch(r"/ops/run/([a-z0-9-]+)", self.path)
        if not m or m[1] not in JOBS:
            return self.reply(404, {"error": "unknown job"})
        if self.headers.get("X-Homelab-Ops") != "1" or self.headers.get("Origin") not in ORIGINS:
            return self.reply(403, {"error": "not from the home page"})
        if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return self.reply(403, {"error": "not from the home page"})
        job = m[1]
        # Set by forward-auth (client-sent copies are stripped in Caddy); only for the restart mail
        who = self.headers.get("X-Authentik-Username", "")
        who = who if USERNAME.fullmatch(who) else "?"
        tmp = os.path.join(REQUESTS, "." + job)  # dot files are invisible to the host's glob
        with open(tmp, "w") as f:
            f.write(who + "\n")
        os.replace(tmp, os.path.join(REQUESTS, job))
        print(f"queued {job} for {who}", file=sys.stderr, flush=True)
        return self.reply(202, {"queued": job})

    def log_message(self, fmt, *args):
        # The access log lives in Caddy; only problems here
        if args and str(args[1]).startswith(("4", "5")):
            print(fmt % args, file=sys.stderr, flush=True)


if __name__ == "__main__":
    if not ORIGINS:
        sys.exit("OPS_ORIGINS is empty")
    if not TRUSTED:
        sys.exit("OPS_TRUSTED_PROXY is empty")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
