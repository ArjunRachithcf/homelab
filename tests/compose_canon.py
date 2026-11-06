#!/usr/bin/env python3
"""tests/compose_canon.py: `docker compose config --format json` (stdin) in a form that doesn't depend on
the Compose version: keys sorted, and Compose's own defaults that some versions print and others don't
(bind create_host_path, an empty ipam, the null fields of JSON) left out. List order is kept: it means something. tests/golden.sh."""
import json
import sys


NOISE = {("create_host_path", True), ("ipam", "{}")}


def canon(v):
    if isinstance(v, dict):
        # null = unset (JSON prints every field); an empty ipam is no ipam
        return {k: canon(x) for k, x in v.items()
                if x is not None and (k, x if x is True else json.dumps(x)) not in NOISE}
    if isinstance(v, list):
        return [canon(x) for x in v]
    return v


json.dump(canon(json.load(sys.stdin)), sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
sys.stdout.write("\n")
