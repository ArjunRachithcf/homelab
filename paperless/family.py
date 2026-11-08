# The family's Paperless accounts (wizard.sh stage paperless_family, RUNBOOK.md step 6), run in Paperless' shell:
#   docker compose exec -T -e PAPERLESS_FAMILY_SHARING paperless python3 manage.py shell < paperless/family.py
# Idempotent, and it only adds, never takes away:
# - the group paperless-users (Authentik's name for the same people) with a family member's rights: their own
#   documents, tags, correspondents, document types, storage paths, custom fields, notes, saved views and UI settings;
#   the task list; the list of users and groups (to share a document with someone). Not: users, groups, workflows,
#   mail, the app's settings, public share links. Without these rights Paperless shows a new account an error and
#   nothing else; docker-compose.yml puts every new sign-in into the group (PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS);
# - every account that already signs in (not the superusers, who see everything anyway) into the group;
# - PAPERLESS_FAMILY_SHARING=true: the workflow "Share with the family" (every new document: the group may view and
#   edit it; deleting stays with its owner), and once, when it's made, the documents already there likewise. Run
#   again, it never shares a document someone has made private since. false: the workflow is switched off (what was
#   shared stays shared); each person's documents are their own, shared by hand (select → Permissions).
import os

from django.contrib.auth.models import Group, Permission, User
from documents.models import Document, Workflow, WorkflowAction as A, WorkflowTrigger as T
from documents.permissions import set_permissions_for_object
from documents.serialisers import WorkflowSerializer

GROUP, WORKFLOW = "paperless-users", "Share with the family"
OWN = ["document", "correspondent", "documenttype", "tag", "storagepath", "customfield", "customfieldinstance", "note",
       "savedview", "savedviewfilterrule", "uisettings"]

g, made = Group.objects.get_or_create(name=GROUP)
rights = set(Permission.objects.filter(content_type__app_label="documents", content_type__model__in=OWN))
rights |= set(Permission.objects.filter(content_type__app_label="documents",
                                        codename__in=["view_paperlesstask", "change_paperlesstask"]))
rights |= set(Permission.objects.filter(content_type__app_label="auth", codename__in=["view_user", "view_group"]))
missing = rights - set(g.permissions.all())
g.permissions.add(*missing)
print(f"✓ group {GROUP}: {'made, ' if made else ''}{len(rights)} rights" + (f" ({len(missing)} added)" if missing else ""))

people = User.objects.filter(is_active=True, is_superuser=False).exclude(username="AnonymousUser")
new = [u.username for u in people if not u.groups.filter(pk=g.pk).exists()]
g.user_set.add(*people)
print(f"✓ {people.count()} account(s) in {GROUP}" + (f", added: {', '.join(new)}" if new else ""))

share = os.environ.get("PAPERLESS_FAMILY_SHARING", "false").strip().lower() == "true"
w = Workflow.objects.filter(name=WORKFLOW).first()
if share and w is None:
    s = WorkflowSerializer(data={
        "name": WORKFLOW, "order": 1, "enabled": True,
        "triggers": [{"type": T.WorkflowTriggerType.DOCUMENT_ADDED, "sources": [1, 2, 3, 4], "matching_algorithm": 0}],
        "actions": [{"type": A.WorkflowActionType.ASSIGNMENT, "order": 0,
                     "assign_view_groups": [g.pk], "assign_change_groups": [g.pk]}],
    })
    if not s.is_valid():
        print(f"✗ workflow {WORKFLOW}: {s.errors}")
        raise SystemExit(1)
    s.save()
    both = {"view": {"users": [], "groups": [g.pk]}, "change": {"users": [], "groups": [g.pk]}}
    docs = list(Document.objects.all())
    for d in docs:
        set_permissions_for_object(both, d, merge=True)
    print(f"✓ shared with the family: every new document (workflow {WORKFLOW}), and the {len(docs)} already here")
elif share:
    was_off = not w.enabled
    if was_off:
        w.enabled = True
        w.save()
    print(f"✓ shared with the family: every new document (workflow {WORKFLOW}{', switched on again' if was_off else ''})")
elif w is not None and w.enabled:
    w.enabled = False
    w.save()
    print(f"✓ sharing switched off for new documents (workflow {WORKFLOW}); what was shared stays shared")
else:
    print("✓ each person's documents are their own (PAPERLESS_FAMILY_SHARING is not true); share by hand")
