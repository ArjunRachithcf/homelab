# Adding a family member

People sign up themselves with an address at the family's own domains; they're accepted into the
family automatically and the admins get a mail.

1. Send them to the home page address. They choose **Sign up**, confirm their mail, set a password
   and an authenticator (or a passkey).
2. Documents: they're in `paperless-users` automatically. Budget: an admin adds them to the
   `budget` group.
3. A child: the admin runs `./authentik/setup-ssh.sh kid <username>`: their own home page, the
   kids' library only.
4. An operator (may open this console): an admin adds them to the `operators` group in the
   accounts admin (Directory → Groups → operators).

Someone with another address can't sign up; an admin creates the account in the accounts admin.
