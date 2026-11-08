"""tests/paperless/check.py: paperless/family.py's cases against the test's real Paperless (tests/paperless/run.sh sets up
the project and this environment). Each case one line, ✓ or ✗; exits 1 when any failed."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
SRC = Path(__file__).resolve().parents[2]
passed = failed = 0


def check(what, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {what}")
    else:
        failed += 1
        print(f"  ✗ {what}{': ' + str(detail)[:400] if detail else ''}")


def shell(code=None, stdin=None, sharing=None):
    """Paperless' Django shell: `code` (-c) or a script on stdin; its "R:" lines as JSON, its "✓/✗" lines, its exit code"""
    env_args = ["-e", f"PAPERLESS_FAMILY_SHARING={sharing}"] if sharing is not None else []
    cmd = ["docker", "compose", "exec", "-T", *env_args, "paperless", "python3", "manage.py", "shell"]
    r = subprocess.run(cmd + (["-c", code] if code else []), input=stdin, text=True, capture_output=True, cwd=REPO,
                       timeout=600)
    lines = (r.stdout + r.stderr).splitlines()
    data = [json.loads(x[2:]) for x in lines if x.startswith("R:")]
    return data, [x for x in lines if x.startswith(("✓", "✗"))], r.returncode, (r.stdout + r.stderr)[-800:]


def family(sharing):
    _, said, rc, tail = shell(stdin=(SRC / "paperless/family.py").read_text(), sharing=sharing)
    return said, rc, tail


# What mia (a family member) may do with each test document, the group, the workflow
STATE = r'''
import json
from django.contrib.auth.models import Group, User
from documents.models import Document, Workflow
from documents.permissions import has_perms_owner_aware
mia = User.objects.get(username="mia")
g = Group.objects.filter(name="paperless-users").first()
w = Workflow.objects.filter(name="Share with the family").first()
docs = {d.title: {a: has_perms_owner_aware(mia, f"{a}_document", d) for a in ("view", "change", "delete")}
        for d in Document.objects.order_by("title")}
print("R:" + json.dumps({"group": bool(g), "rights": g.permissions.count() if g else 0,
    "members": sorted(u.username for u in g.user_set.all()) if g else [],
    "mia_ui": mia.has_perm("documents.view_uisettings"), "mia_admin": mia.has_perm("auth.change_user"),
    "workflow": None if w is None else w.enabled, "docs": docs}))
'''


def state():
    data, _, rc, tail = shell(STATE)
    return data[0] if data else {"error": tail}


def main():
    print("The test's data: a family member (mia), a disabled account (olaf), three documents of the admin's")
    data, _, rc, tail = shell(r'''
import json
from django.contrib.auth.models import User
from documents.models import Document
admin = User.objects.filter(is_superuser=True).first()
User.objects.get_or_create(username="mia", defaults={"is_active": True})
User.objects.get_or_create(username="olaf", defaults={"is_active": False})
for i in range(3):
    Document.objects.get_or_create(checksum=f"{i:032x}", defaults={"title": f"Test {i}", "mime_type": "application/pdf", "owner": admin})
print("R:" + json.dumps({"admin": admin.username, "docs": Document.objects.count()}))
''')
    check("users and documents made", bool(data) and data[0]["docs"] == 3, tail)

    print("PAPERLESS_FAMILY_SHARING=false")
    said, rc, tail = family("false")
    check("the script runs", rc == 0, tail)
    s = state()
    check("the group, with a family member's rights", s.get("group") and s.get("rights", 0) >= 40, s)
    check("every active account in it, not the disabled one", s.get("members") == ["mia"], s.get("members"))
    check("mia's UI works (her UI settings), no admin", s.get("mia_ui") is True and s.get("mia_admin") is False, s)
    check("no sharing workflow", s.get("workflow") is None, s.get("workflow"))
    check("mia sees none of the admin's documents", not any(d["view"] for d in s.get("docs", {}).values()), s.get("docs"))
    check("the outcome said", any("each person's documents are their own" in x for x in said), said)

    print("PAPERLESS_FAMILY_SHARING=true")
    said, rc, tail = family("true")
    s = state()
    check("the workflow made, on", s.get("workflow") is True, s.get("workflow"))
    check("the documents already there shared once: mia views and edits all three, deletes none",
          s.get("docs") == {f"Test {i}": {"view": True, "change": True, "delete": False} for i in range(3)}, s.get("docs"))
    check("the outcome said", any("and the 3 already here" in x for x in said), said)

    print("A document made private, then the script again: it stays private")
    shell(r'''
from documents.models import Document
from documents.permissions import set_permissions_for_object
d = Document.objects.get(title="Test 0")
set_permissions_for_object({"view": {"users": [], "groups": []}, "change": {"users": [], "groups": []}}, d)
''')
    said, rc, tail = family("true")
    s = state()
    check("Test 0 stays private", s.get("docs", {}).get("Test 0", {}).get("view") is False, s.get("docs"))
    check("the others still shared", all(s["docs"][f"Test {i}"]["view"] for i in (1, 2)), s.get("docs"))
    check("nothing added the second time", rc == 0 and not any("added" in x for x in said), said)

    print("A new document through Paperless' consumer: the workflow shares it")
    data, _, rc, tail = shell(r'''
import json
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from documents.data_models import ConsumableDocument, DocumentMetadataOverrides, DocumentSource
from documents.tasks import consume_file
text = b"BT /F1 18 Tf 20 70 Td (Family test letter) Tj ET"
objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
pdf, offsets = b"%PDF-1.4\n", []
for i, o in enumerate(objs, 1):
    offsets.append(len(pdf))
    pdf += b"%d 0 obj\n" % i + o + b"\nendobj\n"
xref = len(pdf)
pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offsets)
pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
p = Path(settings.SCRATCH_DIR) / "family-test.pdf"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(pdf)
admin = User.objects.filter(is_superuser=True).first()
r = consume_file.apply(args=(ConsumableDocument(source=DocumentSource.ApiUpload, original_file=p),
                             DocumentMetadataOverrides(title="Test new", owner_id=admin.id)))
print("R:" + json.dumps({"status": r.status, "result": str(r.result)[:200]}))
''')
    check("consumed", bool(data) and data[0]["status"] == "SUCCESS", data or tail)
    s = state()
    check("the new one: mia views and edits it, deletes it not",
          s.get("docs", {}).get("Test new") == {"view": True, "change": True, "delete": False}, s.get("docs"))

    print("PAPERLESS_FAMILY_SHARING=false again")
    said, rc, tail = family("false")
    s = state()
    check("the workflow switched off", s.get("workflow") is False, s.get("workflow"))
    check("what was shared stays shared", s.get("docs", {}).get("Test 1", {}).get("view") is True, s.get("docs"))
    check("the outcome said", any("switched off" in x for x in said), said)

    print("The settings and the wizard's check")
    data, _, rc, tail = shell('from django.conf import settings; import json; print("R:" + json.dumps(settings.SOCIAL_ACCOUNT_DEFAULT_GROUPS))')
    check("a new sign-in joins paperless-users (PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS)", data == [["paperless-users"]], data or tail)
    wizard = (SRC / "wizard.sh").read_text()
    code = wizard.split("paperless_family_ok() {", 1)[1].split("manage.py shell -c '", 1)[1].split("' 2>/dev/null", 1)[0]
    _, _, rc, out = shell(code)
    check("wizard.sh's check says FAMILY-OK", "FAMILY-OK" in out, out)
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
