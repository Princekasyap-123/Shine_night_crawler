import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from candidates.models import CandidateRecord
from sessions.models import CrawlSession
from submission.client import ExtensionSubmissionError, submit_candidate

logger = logging.getLogger(__name__)


@shared_task
def auto_submit_queued_candidates():
    """
    Scheduled automatic submission, restored per explicit instruction
    after an earlier session where auto-submit was deliberately
    removed entirely — that removal was because of a real, since-fixed
    bug: the old pipeline gated every submission behind
    check_candidate_exists(), which was CONFIRMED broken (returned
    exists=True for an obviously fake, never-real phone number),
    silently blocking every real submission. This version has no such
    gate — it just calls _submit_one() directly, exactly like the
    dashboard's manual "Send selected to API" button does, so there's
    only one submission code path total, not two that could drift
    apart again.

    Gated on queue size, per the new flow: a session's QUEUED
    candidates aren't touched at all until they first reach
    AUTO_SUBMIT_QUEUE_THRESHOLD (300) — this tick is scheduled every
    AUTO_SUBMIT_INTERVAL_SECONDS (6 min), so on any tick where a
    session hasn't reached the threshold yet, it's skipped entirely
    and its queue keeps building; once at/above threshold, one batch of
    AUTO_SUBMIT_BATCH_SIZE (100) oldest QUEUED candidates goes out that
    tick — not FAILED ones, so a permanently-broken candidate (e.g.
    missing phone) can't retry itself forever unattended; a failed row
    still needs an operator to re-select and resend it from the
    dashboard. Deliberately does not check is_paused: pausing only
    throttles NEW crawling (extraction) when the queued-count pause
    threshold trips, it was never meant to also stop already-extracted
    candidates from being submitted — the two are meant to run
    independently so submission keeps draining even while crawling is
    paused.

    Runs for every currently RUNNING session — including one whose
    crawling has already hard-stopped for the night (NIGHT_END_HOUR)
    but hasn't yet been marked completed (SUBMISSION_END_HOUR), which
    is exactly how the new flow keeps submission going for 5 hours
    after crawling itself stops. See sessions.tasks.stop_nightly_session
    for that timing.
    """
    for session in CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING):
        queued_count = session.candidates.filter(status=CandidateRecord.Status.QUEUED).count()
        if queued_count < settings.AUTO_SUBMIT_QUEUE_THRESHOLD:
            continue

        candidate_ids = list(
            session.candidates.filter(status=CandidateRecord.Status.QUEUED)
            .order_by("extracted_at")
            .values_list("id", flat=True)[: settings.AUTO_SUBMIT_BATCH_SIZE]
        )
        if not candidate_ids:
            continue

        logger.info(
            "Session #%s: auto-submitting %s queued candidates (queue was %s)",
            session.pk,
            len(candidate_ids),
            queued_count,
        )
        for candidate_id in candidate_ids:
            _submit_one(candidate_id)


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
