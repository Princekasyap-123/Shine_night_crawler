import logging

from celery import shared_task
from django.conf import settings

from candidates.models import CandidateRecord
from control.tasks import is_within_submission_window
from polling.client import PollingRequestError, poll_candidate_status

logger = logging.getLogger(__name__)


@shared_task
def poll_submitted_candidates():
    """
    Periodic tick (every 3 min), independent of the crawl/control
    heartbeats and the manual submit endpoint, and not scoped to any
    one session — a candidate may resolve well after its own crawl
    session has ended, so this checks across all sessions' SUBMITTED
    rows, not just the currently-running one.

    Gated on the submission window (23:00-10:00) per the "all process
    stops at 10am" requirement: outside that window this is a no-op,
    even though nothing here is otherwise tied to a specific session.
    Without this check, a candidate submitted late one night and still
    unresolved would keep getting polled all day until the next
    night's window reopened, which the new flow explicitly doesn't
    want.

    NOTE: there's no cap on how long a candidate can sit in SUBMITTED
    if the extension backend never resolves it before the window
    closes — CandidateRecord has no attempt counter to age one out
    automatically. Such a row simply waits for the next window and
    resumes being polled then. Stuck rows are visible in the admin
    (filterable by status) for manual follow-up; adding an automatic
    give-up-after-N-attempts would need a schema change, left out here
    rather than guessed at.
    """
    if not is_within_submission_window():
        return

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
