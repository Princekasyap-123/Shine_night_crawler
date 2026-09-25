from rest_framework.response import Response
from rest_framework.views import APIView

from candidates.models import CandidateRecord
from submission.tasks import _submit_one


class SubmitCandidatesView(APIView):
    """
    Manual, on-demand submission for a specific set of candidate ids —
    backs the dashboard's "Send to API" button, the only way
    candidates ever reach the extension API (submission is 100%
    manual by design; there is no automatic dispatch). Runs
    synchronously (not via Celery) since this is a small, explicit,
    human-initiated click where the dashboard wants an immediate
    per-candidate result to show back, not a fire-and-forget
    background dispatch. Reuses submission.tasks._submit_one(), which
    atomically claims each candidate (queued OR failed — a failed row
    can be re-selected and retried) before sending, so two overlapping
    submit requests for the same candidate can't both send it.
    """

    def post(self, request):
        candidate_ids = request.data.get("candidate_ids") or []
        if not isinstance(candidate_ids, list) or not candidate_ids:
            return Response(
                {"detail": "candidate_ids must be a non-empty list."}, status=400
            )

        results = []
        for candidate_id in candidate_ids:
            _submit_one(candidate_id)
            candidate = CandidateRecord.objects.filter(pk=candidate_id).first()
            results.append(
                {
                    "id": candidate_id,
                    "status": candidate.status if candidate else "not_found",
                    "failure_reason": candidate.failure_reason if candidate else None,
                }
            )

        return Response({"results": results})
