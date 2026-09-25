import logging

from celery import shared_task
from django.conf import settings

from candidates.models import CandidateRecord
from polling.client import PollingRequestError, poll_candidate_status

logger = logging.getLogger(__name__)


@shared_task
def poll_submitted_candidates():
    """
    Periodic tick, independent of the crawl/control heartbeats and the
    manual submit endpoint, and not scoped to any one session or the
    night window
    — Ollama structuring on the extension backend can take a while,
    and a candidate may resolve well after its own crawl session (or
    the whole night) has ended.

    NOTE: there's no cap on how long a candidate can sit in SUBMITTED
    if the extension backend never resolves it — CandidateRecord has
    no attempt counter to age one out automatically. Stuck rows are
    visible in the admin (filterable by status) for manual follow-up;
    adding an automatic give-up-after-N-attempts would need a schema
    change, left out here rather than guessed at.
    """
    candidate_ids = list(
        CandidateRecord.objects.filter(status=CandidateRecord.Status.SUBMITTED)
        .order_by("submitted_at")
        .values_list("id", flat=True)[: settings.POLLING_BATCH_SIZE]
    )

    for candidate_id in candidate_ids:
        _poll_one(candidate_id)


def _poll_one(candidate_id):
    try:
        candidate = CandidateRecord.objects.get(pk=candidate_id)
    except CandidateRecord.DoesNotExist:
        return

    if candidate.status != CandidateRecord.Status.SUBMITTED:
        return

    try:
        result = poll_candidate_status(candidate)
    except PollingRequestError as exc:
        logger.warning("Polling failed for CandidateRecord #%s: %s", candidate_id, exc)
        return

    if not result.resolved:
        return

    if result.status == "success":
        candidate.mark_success()
    else:
        candidate.mark_failed(
            reason=result.status_message or "Extension backend reported failure"
        )
