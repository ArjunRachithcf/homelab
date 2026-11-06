# The machine drifted from its setup

Every Sunday `ansible/drift.sh` asks Ansible what it *would* change on this machine. A mail means
something differs from `ansible/site.yml`: a package, a service, a setting someone changed by hand.

Nothing is broken by this on its own. It's a note for the admin.

## What to do

- Operators: nothing, except tell the admin if you changed something on the machine yourself.
- Admin: read the mail's diff. Either run the playbook to put it back, or change the playbook to
  match what you want:

```
ansible-playbook -K ansible/site.yml --check --diff
ansible-playbook -K ansible/site.yml
```

Real runs refuse while a backup or a Proton upload runs, and at night (02:00–05:30).
