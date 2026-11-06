#!/usr/bin/env python3
"""~/homelab/lib/setup/askpass.py: a password for sudo -A and ansible-playbook, from the setup app.

Only under `./homelab setup --protocol`: run.py listens on the Unix socket HOMELAB_ASKPASS_SOCK (in a
private 0700 directory), asks the front end ({"t":"sudo","prompt":…}) and sends back its answer, which
this prints for sudo (SUDO_ASKPASS) or Ansible (--become-password-file, run as a script). The password
goes from the front end to run.py to here to sudo; nothing writes or logs it.
"""
import os
import socket
import sys

path = os.environ.get("HOMELAB_ASKPASS_SOCK")
if not path:
    sys.exit("askpass: only for ./homelab setup --protocol")
prompt = sys.argv[1] if len(sys.argv) > 1 else "sudo password"
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(path)
    s.sendall(prompt.encode()[:200] + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
if not data.endswith(b"\n"):
    sys.exit(1)  # no answer: sudo counts it as a wrong password
sys.stdout.write(data[:-1].decode())
