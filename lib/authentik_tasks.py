"""~/homelab/lib/authentik_tasks.py: the Authentik side of lib/authentik.py, one function per task.

Runs inside the worker, in `ak shell -c` (Authentik's Django models), never on the host: lib/authentik.py
sends this file's source with a call of run(), and the task's name and arguments as JSON in the
environment variable AK_TASK (not on a command line: they can hold a private key). Only lines starting
with "OUT:" reach the terminal, without the prefix; ak shell prints its own lines around them.
"""
import json
import os


def out(*parts):
    print("OUT:" + " ".join(str(p) for p in parts))


def user(name):
    # Your Authentik user may differ in case from the Linux user (e.g. JaneSmith vs janesmith);
    # SSSD matches case-insensitively (case_sensitive = false in ssh/sssd.conf).
    from authentik.core.models import User
    return User.objects.filter(username__iexact=name).first()


def rename_admin(name):
    u = user(name)
    if u:
        out("✓ user", u.username, "exists")
        return
    from authentik.core.models import User
    u = User.objects.get(username="akadmin", is_active=True)
    u.username = name
    u.save()
    out("✓ renamed akadmin →", u.username)


def cert_state():
    # Self-signed = issuer is the subject (Authentik's own generator writes "authentik <version>" as
    # issuer, which OpenSSL/SSSD can't verify)
    from authentik.crypto.models import CertificateKeyPair
    kp = CertificateKeyPair.objects.filter(name="ldap-ssh").first()
    out("ok" if kp and kp.certificate.issuer == kp.certificate.subject else "make")


def set_cert(cert, key):
    from authentik.crypto.models import CertificateKeyPair
    kp, _ = CertificateKeyPair.objects.get_or_create(name="ldap-ssh")
    kp.certificate_data = cert
    kp.key_data = key
    kp.save()
    out("✓ certificate ldap-ssh: self-signed, 10 years, SAN ldap.homelab.internal")


def list_additions(name, ssh_admins_only=False):
    """Additions to lists on default stages (a blueprint could only replace them), and you into
    ssh-admins once ssh-ldap.yaml made it."""
    from authentik.core.models import Group
    from authentik.flows.models import FlowStageBinding
    from authentik.policies.models import Policy
    from authentik.stages.prompt.models import Prompt, PromptStage
    if not ssh_admins_only:
        strong = Policy.objects.get(name="strong-password")
        e164 = Policy.objects.get(name="phone-e164")
        phone = Prompt.objects.get(name="homelab-field-phone")
        phone_settings = Prompt.objects.get(name="homelab-field-phone-settings")

        def add(stage_name, field=None, policy=None):
            s = PromptStage.objects.filter(name=stage_name).first()
            if not s:
                out(f"· {stage_name} not there yet (re-run after public-wizard.sh)")
                return
            if field:
                s.fields.add(field)
            if policy:
                s.validation_policies.add(policy)
            out(f"✓ {stage_name}")

        add("default-user-settings", field=phone_settings, policy=e164)
        add("default-enrollment-prompt-second", field=phone, policy=e164)
        add("default-password-change-prompt", policy=strong)
        add("default-enrollment-prompt-first", policy=strong)
        # Recovery: whichever prompt stage of default-recovery-flow asks for the password
        for b in FlowStageBinding.objects.filter(target__slug="default-recovery-flow"):
            s = PromptStage.objects.filter(pk=b.stage.pk).first()
            if s and s.fields.filter(field_key="password").exists():
                add(s.name, policy=strong)
    u = user(name)
    group = Group.objects.filter(name="ssh-admins").first()   # created by ssh-ldap.yaml, applied later
    if u and group:
        group.users.add(u)
        out("✓", u.username, "in ssh-admins")


def export_ca():
    from authentik.crypto.models import CertificateKeyPair
    for line in CertificateKeyPair.objects.get(name="ldap-ssh").certificate_data.strip().splitlines():
        out(line)


def outpost_token():
    from authentik.outposts.models import Outpost
    out(Outpost.objects.get(name="ldap-ssh").token.key)


# SSH keys: the attribute sshPublicKey, one line per key (the flows in authentik/ssh-sso.yaml change it too). Keys
# are compared by the key itself, not the line, and every change re-reads the user under a row lock, so the sheet
# and the command line never undo each other's change.
def _lines(u):
    v = u.attributes.get("sshPublicKey") or []
    return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)]


def _blob(line):
    import base64
    import binascii
    p = line.split()
    try:
        return base64.b64decode(p[1], validate=True) if len(p) >= 2 else None
    except (binascii.Error, ValueError):
        return None


def _fp(line):
    import base64
    import hashlib
    b = _blob(line)
    return "SHA256:" + base64.b64encode(hashlib.sha256(b).digest()).decode().rstrip("=") if b else None


def _change_keys(name, change):
    from django.db import transaction
    from authentik.core.models import User
    with transaction.atomic():
        u = User.objects.select_for_update().get(pk=user(name).pk)
        have = _lines(u)
        new = change(have)
        if new != have:
            u.attributes["sshPublicKey"] = new
            u.save(update_fields=["attributes"])
    return u, new


def add_keys(name, keys):
    def change(have):
        blobs = {_blob(x) for x in have}
        for k in keys:
            if _blob(k) not in blobs:
                have = have + [k]
                blobs.add(_blob(k))
                out("✓ added", " ".join(k.split()[2:]) or k.split()[0], _fp(k))
        return have
    u, have = _change_keys(name, change)
    out(len(have), "key(s) on", u.username)


def remove_keys(name, fps):
    def change(have):
        keep = [x for x in have if _fp(x) not in fps]
        for x in have:
            if x not in keep:
                out("✓ removed", " ".join(x.split()[2:]) or x.split()[0], _fp(x))
        return keep
    u, have = _change_keys(name, change)
    out(len(have), "key(s) on", u.username)


def list_keys(name):
    for k in _lines(user(name)):
        p = k.split()
        out(p[0], _fp(k) or "(not a key)", " ".join(p[2:]))


def keys(name):
    """One JSON line for lib/ssh_keys.py: the lines, and the latest change to the user in the last half hour (when,
    from which address, by whom: "authentik-shell" for setup-ssh.sh) for its mail. Authentik's events don't say what
    changed; the job asks right after it saw the keys change."""
    from datetime import timedelta
    from django.utils.timezone import now
    from authentik.events.models import Event
    u = user(name)
    e = (Event.objects.filter(action="model_updated", context__model__model_name="user", context__model__pk=u.pk,
                              created__gte=now() - timedelta(minutes=30))
         .order_by("-created").first())
    ev = {"time": e.created.isoformat(timespec="seconds"), "client_ip": e.client_ip,
          "by": (e.user or {}).get("username")} if e else None
    out(json.dumps({"user": u.username, "keys": _lines(u), "event": ev}))


def ldap_refresh():
    """The SSH outpost reloads its users within seconds when its provider is saved (it reloads only every few
    minutes otherwise)"""
    from authentik.providers.ldap.models import LDAPProvider
    p = LDAPProvider.objects.filter(name="ssh-ldap").first()
    if p:
        p.save()
        out("✓ outpost ldap-ssh reloads its users")
    else:
        out("· no provider ssh-ldap yet")


def status(name):
    # One KEY=VALUE per line: hardening-wizard.sh reads them (ak_is)
    from authentik.core.models import User
    from authentik.outposts.models import Outpost
    from authentik.stages.authenticator_totp.models import TOTPDevice
    from authentik.stages.authenticator_webauthn.models import WebAuthnDevice
    u = User.objects.get(username__iexact=name)
    out("totp=%d" % TOTPDevice.objects.filter(user=u, confirmed=True).count())
    out("webauthn=%d" % WebAuthnDevice.objects.filter(user=u).count())
    out("keys=%d" % len(u.attributes.get("sshPublicKey", [])))
    out("phone=%s" % ("yes" if u.attributes.get("phone") else "no"))
    out("ssh_admin=%s" % ("yes" if u.ak_groups.filter(name="ssh-admins").exists() else "no"))
    out("akadmin_active=%s" % ("yes" if User.objects.filter(username="akadmin", is_active=True).exists() else "no"))
    out("outpost=%s" % ("yes" if Outpost.objects.filter(name="ldap-ssh").exists() else "no"))


def brand(brand, slug, home_url):
    """What family.yaml can't do: the blueprint (API serializer) would trim the subject prefix's trailing
    space; the brand's title and logos only on a new instance (title still "authentik"), so an existing
    one keeps what was set; sign-up only with an address at DOMAIN / DOMAIN_ALIAS (a list addition)."""
    from authentik.brands.models import Brand
    from authentik.events.models import NotificationTransport
    from authentik.policies.models import Policy
    from authentik.stages.prompt.models import PromptStage
    NotificationTransport.objects.filter(name="default-email-transport").update(email_subject_prefix=brand + ": ")
    out(f"✓ alert mail subjects start with \"{brand}: \"")
    b = Brand.objects.filter(default=True).first()
    if b and b.branding_title == "authentik":
        b.branding_title = brand
        b.branding_logo = home_url + "/logo-%(theme)s.png"
        b.branding_favicon = home_url + "/favicon.png"
        b.save()
        out(f"✓ brand: title {brand}, logo and favicon from {home_url}")
    s = PromptStage.objects.filter(name="default-enrollment-prompt-second").first()
    p = Policy.objects.filter(name=slug + "-email-domain").first()
    if s and p:
        s.validation_policies.add(p)
        out("✓", p.name, "on", s.name)


def on_outpost(providers):
    # Added, not replaced: a blueprint would replace the outpost's provider list, so providers made
    # elsewhere would drop off
    from authentik.outposts.models import Outpost
    from authentik.providers.proxy.models import ProxyProvider
    o = Outpost.objects.get(managed="goauthentik.io/outposts/embedded")
    for name in providers:
        o.providers.add(ProxyProvider.objects.get(name=name))
        out("✓", name, "on the embedded outpost")


def join_groups(name, groups):
    # Added to the group's members (a blueprint would replace the list)
    from authentik.core.models import Group, User
    u = User.objects.get(username__iexact=name)
    for g in groups:
        Group.objects.get(name=g).users.add(u)
        out("✓", u.username, "in", g)


def kid(name):
    """A child's account: kids (home page, family app, Kavita's Kids library), never Paperless.
    Authentik's group list on the user is additive here, so nothing else changes."""
    from authentik.core.models import Group
    u = user(name)
    if not u:
        out("✗ no user", name)
        return
    Group.objects.get(name="kids").users.add(u)
    Group.objects.get(name="paperless-users").users.remove(u)
    out("✓", u.username, "in kids, not in paperless-users:", ", ".join(sorted(g.name for g in u.ak_groups.all())))


def members(group):
    """The active users of a group (its own members), one username a line: what an app takes over from Authentik's
    groups (Seerr's permissions, downloads-wizard.sh). Nothing for a group that doesn't exist."""
    from authentik.core.models import Group
    g = Group.objects.filter(name=group).first()
    if g:
        for u in g.users.filter(is_active=True).order_by("username"):
            out(u.username)


TASKS = {f.__name__: f for f in (rename_admin, cert_state, set_cert, list_additions, export_ca, outpost_token,
                                 add_keys, remove_keys, list_keys, keys, ldap_refresh, status, brand, on_outpost,
                                 join_groups, kid, members)}


def run():
    task = json.loads(os.environ["AK_TASK"])
    TASKS[task["task"]](**task.get("args", {}))
