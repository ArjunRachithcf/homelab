# SSH keys

SSH into this PC takes a key listed on the home page's **SSH keys** sheet, then your password and authenticator
code. The sheet's list is what Authentik holds; adding and removing a key goes through Authentik, with your
passkey every time, and only over NetBird. Every change mails the admin, however it was made.

## A device is lost or stolen

1. Over NetBird, open **SSH keys** on the home page and **Remove** that device's key. SSH stops taking it
   within a minute; the sheet says so.
2. In **Accounts**, sign out that device's sessions and remove its passkey if it had one.
3. Didn't you make a change you got a mail about? Remove the key, then change your password.

## The SSH keys job failed

It follows the keys for the sheet and sends the mails (every 15 minutes, and when the sheet opens). Look at its
log under **Jobs**; most often Authentik wasn't running. SSH itself doesn't depend on it.

## On the machine (admin)

```
./authentik/setup-ssh.sh list-keys                 # type, fingerprint, title
./authentik/setup-ssh.sh remove-key SHA256:…       # the same as Remove on the sheet
./authentik/setup-ssh.sh add-key < laptop.pub      # one key per line, its comment as title
./homelab logs ssh-keys
sss_ssh_authorizedkeys "$USER"                     # what SSH takes right now
```
