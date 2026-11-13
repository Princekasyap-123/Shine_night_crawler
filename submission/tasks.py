import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from candidates.models import CandidateRecord
from sessions.models import CrawlSession
from submission.client import (
    ExtensionSubmissionError,
    get_ats_queued_count,
    submit_batch,
    submit_candidate,
)

logger = logging.getLogger(__name__)


@shared_task(time_limit=1800, soft_time_limit=1740)
def auto_submit_queued_candidates():
    """
    Scheduled automatic submission (every AUTO_SUBMIT_INTERVAL_SECONDS):
    sends ONE batch of up to AUTO_SUBMIT_BATCH_SIZE (50) oldest QUEUED
    candidates to the ATS's /submit/batch per tick, paced to the ATS's
    own throughput rather than to our crawl speed.

    Pacing: before the batch, the ATS's GET /stats is read and the tick
    is skipped while jobs_by_status.queued is above
    AUTO_SUBMIT_ATS_BACKLOG_MAX (500) — the ATS's retry delays grow
    with its queue, so piling more on only slows it down. Whatever the
    crawler extracts faster than that simply stays QUEUED locally for
    the operator to send manually from the dashboard afterwards;
    crawling is not slowed down for it (see CRAWL_PAUSE_ON_BACKLOG).

    Only QUEUED rows are picked up, never FAILED ones, so a
    permanently-broken candidate (e.g. missing phone) can't retry itself
    forever unattended. Deliberately does not check is_paused, and
    keeps running for a session whose crawling has already stopped
    (NIGHT_END_HOUR) but hasn't been completed yet
    (SUBMISSION_END_HOUR). Whether a submitted candidate actually landed
    is decided later by polling.tasks, 3 minutes after submission.
    """
    sessions = list(CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING))
    if not sessions:
        return

    ats_queued = get_ats_queued_count()
    if ats_queued is not None and ats_queued > settings.AUTO_SUBMIT_ATS_BACKLOG_MAX:
        logger.info(
            "ATS backlog is %s (> %s), skipping this submit tick",
            ats_queued,
            settings.AUTO_SUBMIT_ATS_BACKLOG_MAX,
        )
        return

    for session in sessions:
        queued_count = session.candidates.filter(status=CandidateRecord.Status.QUEUED).count()
        if not queued_count or queued_count < settings.AUTO_SUBMIT_QUEUE_THRESHOLD:
            continue

        candidate_ids = list(
            session.candidates.filter(status=CandidateRecord.Status.QUEUED)
            .order_by("extracted_at")
            .values_list("id", flat=True)[: settings.AUTO_SUBMIT_BATCH_SIZE]
        )
        logger.info(
            "Session #%s: auto-submitting batch of %s (local queue was %s, ATS queue %s)",
            session.pk,
            len(candidate_ids),
            queued_count,
            ats_queued,
        )
        _submit_batch(candidate_ids)


def _claim(candidate_id, statuses) -> bool:
    """
    Atomically flips one candidate to SUBMITTED. A single UPDATE ...
    WHERE status IN (...) rather than a read-then-write: two overlapping
    submitters (double-click, two tabs, manual + automatic) can
    otherwise both read status=queued before either writes and both
    POST the same candidate. Only the caller whose UPDATE matches a row
    proceeds. Works on SQLite too, unlike select_for_update().
    """
    return bool(
        CandidateRecord.objects.filter(pk=candidate_id, status__in=statuses).update(
            status=CandidateRecord.Status.SUBMITTED,
            submitted_at=timezone.now(),
            failure_reason=None,
        )
    )


def _submit_batch(candidate_ids):
    """
    Claims each id (QUEUED only) and sends the claimed ones in a single
    submit_batch() call, then applies each item's own result.

    If the call fails as a whole (ATS down, bad key, response that can't
    be matched to the items), the claimed rows are put back to QUEUED
    rather than FAILED, so the next tick retries them on its own —
    marking 50 candidates FAILED because of one blip would leave them
    stranded until someone resent them by hand.
    """
    claimed_ids = [cid for cid in candidate_ids if _claim(cid, [CandidateRecord.Status.QUEUED])]
    if not claimed_ids:
        return

    by_id = CandidateRecord.objects.in_bulk(claimed_ids)
    candidates = [by_id[cid] for cid in claimed_ids if cid in by_id]

    try:
        results = submit_batch(candidates)
    except ExtensionSubmissionError as exc:
        logger.warning("Batch submission failed, returning %s to the queue: %s", len(candidates), exc)
        CandidateRecord.objects.filter(
            pk__in=[c.pk for c in candidates], status=CandidateRecord.Status.SUBMITTED
        ).update(status=CandidateRecord.Status.QUEUED, submitted_at=None)
        return

    for candidate, result in zip(candidates, results):
        # already_exists: the ATS has seen this one before — not a
        # failure; it stays SUBMITTED and polling confirms it is in the DB.
        if not (result.success or result.already_exists):
            candidate.mark_failed(reason=result.message)


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
    claimed = _claim(
        candidate_id, [CandidateRecord.Status.QUEUED, CandidateRecord.Status.FAILED]
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