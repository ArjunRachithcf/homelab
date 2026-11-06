# Paperless AI workflow (RUNBOOK.md step 6b), through Paperless' own serializer (same checks as the UI).
# docker compose exec -T paperless python3 manage.py shell < paperless/ai-workflow.py
from documents.models import Workflow, WorkflowAction as A, WorkflowTrigger as T
from documents.serialisers import WorkflowSerializer
F = A.AISuggestionField
data = {
  "name": "AI: fill in new documents",
  "order": 0, "enabled": True,
  "triggers": [{"type": T.WorkflowTriggerType.DOCUMENT_ADDED, "sources": [1,2,3,4], "matching_algorithm": 0}],
  "actions": [
    # Scanner names ("Adobe Scan 22 Jun 2026") are never worth keeping
    {"type": A.WorkflowActionType.APPLY_AI_SUGGESTIONS, "order": 0,
     "ai_suggestion_fields": [F.TITLE], "ai_overwrite_existing": True, "ai_create_missing": False},
    # Only where empty, only existing items; created date stays with Paperless' parser
    {"type": A.WorkflowActionType.APPLY_AI_SUGGESTIONS, "order": 1,
     "ai_suggestion_fields": [F.TAGS, F.CORRESPONDENT, F.DOCUMENT_TYPE],
     "ai_overwrite_existing": False, "ai_create_missing": False},
  ],
}
if Workflow.objects.filter(name=data["name"]).exists():
    print("workflow exists, unchanged"); raise SystemExit
s = WorkflowSerializer(data=data)
if s.is_valid():
    w = s.save(); print("created workflow", w.pk, [(a.type, a.ai_suggestion_fields, a.ai_overwrite_existing) for a in w.actions.all()], [(t.type, t.sources) for t in w.triggers.all()])
else:
    print("INVALID", s.errors)
