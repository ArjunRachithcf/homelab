# ~/homelab/ansible/lookup_plugins/homelab_env.py: read .env the way the scripts and wizards do.
# The ini lookup keeps quotes (KEY='value', as the wizards' write_env writes it, came back as 'value').
from __future__ import annotations

import re

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

DOCUMENTATION = r"""
name: homelab_env
short_description: values from the homelab's .env
description:
  - One value per key. The last KEY= line wins (like the scripts' tail -n1), and one pair of
    surrounding single or double quotes is removed (single-quoted '\'' becomes ').
options:
  _terms:
    description: Keys to read.
    required: true
  file:
    description: The .env file (vars.yml passes homelab_env).
    type: path
    required: true
  default:
    description: Value for a key that is missing or empty.
    type: str
    default: ""
"""

LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)
        path, default = self.get_option("file"), self.get_option("default")
        values = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = LINE.match(line.rstrip("\r\n"))
                    if m:
                        values[m.group(1)] = unquote(m.group(2))
        except OSError as e:
            raise AnsibleError(f"homelab_env: can't read {path}: {e}") from e
        return [values.get(t) or default for t in terms]
