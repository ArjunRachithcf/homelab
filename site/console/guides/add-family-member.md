# Adding a family member

People sign up themselves with an address at the family's own domains; they're accepted into the
family automatically and the admins get a mail.

1. Send them to the home page address. They choose **Sign up**, confirm their mail, set a password
   and an authenticator (or a passkey).
2. Documents: they're in `paperless-users` automatically, and their first Paperless sign-in puts them into Paperless'
   group of the same name (their own documents, tags, saved views; never the admin's rights). Whether they see the
   family's documents too is `PAPERLESS_FAMILY_SHARING` (step "Paperless: the family's accounts and sharing").
   Budget: an admin adds them to the `budget` group.
3. A child: the admin runs `./authentik/setup-ssh.sh kid <username>`: their own home page, the
   kids' library only.
4. An operator (may open this console): an admin adds them to the `operators` group in the
   accounts admin (Directory → Groups → operators).
5. Requests (Seerr), if it's on: once they've signed in there, the admin runs the step "Downloads on
   request → Requests" again: an adult's requests are then approved by themselves, a child's still wait.

Someone with another address can't sign up; an admin creates the account in the accounts admin.
