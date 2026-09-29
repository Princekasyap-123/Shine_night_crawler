from rest_framework.response import Response
from rest_framework.views import APIView

from candidates.models import CandidateRecord
from submission.tasks import _submit_one


class SubmitCandidatesView(APIView):
    """
    Manual, on-demand submission for a specific set of candidate ids —
    backs the dashboard's "Send to API" button. Candidates also get
    submitted automatically on their own schedule (see
    submission.tasks.auto_submit_queued_candidates), but only for
    QUEUED rows; this manual path is the only way to (re)send a
    specific FAILED row, or to jump a specific candidate ahead of the
    automatic batch. Runs synchronously (not via Celery) since this is
    a small, explicit, human-initiated click where the dashboard wants
    an immediate per-candidate result to show back, not a
    fire-and-forget background dispatch. Reuses
    submission.tasks._submit_one() — the same function the automatic
    path calls — which atomically claims each candidate before
    sending, so two overlapping submit requests (manual+manual, or
    manual+automatic) for the same candidate can't both send it.
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
