import logging

from django.utils import timezone

from candidates.models import CandidateRecord
from submission.client import ExtensionSubmissionError, submit_candidate

logger = logging.getLogger(__name__)


def _submit_one(candidate_id):
    """
    Claims and submits one candidate. QUEUED and FAILED rows are both
    eligible — FAILED is included so a transient backend hiccup
    doesn't strand a candidate permanently; the dashboard's "Send
    selected to API" lets an operator re-select a failed row and try
    again.

    No pre-submission duplicate check: DUPLICATE_CHECK_API_URL
    (check_candidate_exists) was CONFIRMED broken — it returned
    exists=True for an obviously fake, never-real phone number, which
    meant every real candidate was being silently marked "submitted"
    without ever actually reaching the extension API. Per explicit
    instruction, every candidate is now sent straight to
    submit_candidate() and the extension backend's OWN response is
    trusted instead — its "already extracted"/"already in queue"
    wording (matched by submission/client.py's _ALREADY_EXISTS_PATTERN)
    is what now decides whether a candidate was a genuine duplicate.

    The claim itself is a single atomic UPDATE ... WHERE status IN
    (...) rather than a read-then-write: two overlapping submit
    requests for the same candidate (e.g. a double-click, or two
    browser tabs) can otherwise both read status=queued before either
    writes, and both go on to POST the same candidate. Only the
    request whose UPDATE actually matches a row (affected count == 1)
    proceeds; the other sees 0 rows affected and backs off. This works
    on SQLite too, unlike select_for_update() (a silent no-op there).
    """
    claimed = CandidateRecord.objects.filter(
        pk=candidate_id,
        status__in=[CandidateRecord.Status.QUEUED, CandidateRecord.Status.FAILED],
    ).update(
        status=CandidateRecord.Status.SUBMITTED,
        submitted_at=timezone.now(),
        failure_reason=None,
    )

    if not claimed:
        if not CandidateRecord.objects.filter(pk=candidate_id).exists():
            logger.warning(
                "CandidateRecord #%s no longer exists, skipping submission",
                candidate_id,
            )
        else:
            logger.info(
                "CandidateRecord #%s was already claimed by another submit "
                "(not queued/failed anymore), skipping",
                candidate_id,
            )
        return

    candidate = CandidateRecord.objects.get(pk=candidate_id)

    try:
        result = submit_candidate(candidate)
    except ExtensionSubmissionError as exc:
        # Transport-level failure (network/timeout/bad response) — not
        # retried automatically. Marked failed so it's visible in the
        # admin/dashboard and can be re-selected and sent again by an
        # operator; nothing retries it on its own anymore.
        logger.warning("Submission failed for CandidateRecord #%s: %s", candidate_id, exc)
        candidate.mark_failed(reason=str(exc))
        return

    if result.success or result.already_exists:
        # already_exists means the extension backend has already seen
        # this candidate (e.g. a recruiter submitted it by hand
        # earlier) — that's not a crawler failure. Already claimed as
        # submitted above; nothing further to do.
        return

    candidate.mark_failed(reason=result.message)
