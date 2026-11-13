import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import connection
from django.utils import timezone

from candidates.models import CandidateRecord
from control.tasks import is_within_submission_window
from sessions.models import CrawlSession
from polling.client import PollingRequestError, poll_candidate_status

logger = logging.getLogger(__name__)


@shared_task
def poll_submitted_candidates():
    """
    Periodic tick (every POLLING_TICK_INTERVAL_SECONDS), independent of
    the crawl/control heartbeats and the submit task, and not scoped to
    any one session — a candidate may resolve well after its own crawl
    session has ended, so this checks across all sessions' SUBMITTED
    rows.

    A candidate is first checked SUCCESS_CHECK_DELAY_SECONDS (3 min)
    after its own submission, via the White Force exists-by-phone API:
    found -> SUCCESS. Not found yet -> left SUBMITTED and checked again
    next tick. Still not found SUCCESS_CHECK_GIVE_UP_MINUTES after
    submission -> FAILED, so it surfaces for a manual resend instead of
    being polled forever.

    Gated on the submission window (23:00-10:00), unless a manual
    session is running: outside it this is a no-op. A row still unresolved when the window closes simply waits
    for the next one.
    """
    # Same exemption crawling has: a MANUAL session (started by hand,
    # possibly during the day) is not bound to the night window.
    manual_running = CrawlSession.objects.filter(
        status=CrawlSession.Status.RUNNING, triggered_by=CrawlSession.TriggeredBy.MANUAL
    ).exists()
    if not (manual_running or is_within_submission_window()):
        return

    cutoff = timezone.now() - timedelta(seconds=settings.SUCCESS_CHECK_DELAY_SECONDS)
    candidate_ids = list(
        CandidateRecord.objects.filter(
            status=CandidateRecord.Status.SUBMITTED, submitted_at__lte=cutoff
        )
        .order_by("submitted_at")
        .values_list("id", flat=True)[: settings.POLLING_BATCH_SIZE]
    )
    if not candidate_ids:
        return

    # The checks are network-bound; DB writes in _poll_one are per-row
    # and short, so threads are fine here.
    with ThreadPoolExecutor(max_workers=settings.SUCCESS_CHECK_CONCURRENCY) as pool:
        list(pool.map(_poll_one, candidate_ids))


def _poll_one(candidate_id):
    # Runs in a pool thread, which gets its own DB connection — close it
    # when done so connections don't pile up tick after tick.
    try:
        _poll_one_inner(candidate_id)
    except Exception:
        logger.exception("Polling crashed for CandidateRecord #%s", candidate_id)
    finally:
        connection.close()


def _poll_one_inner(candidate_id):
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

    if result.resolved:
        if result.status == "success":
            candidate.mark_success()
        return

    give_up_at = candidate.submitted_at + timedelta(
        minutes=settings.SUCCESS_CHECK_GIVE_UP_MINUTES
    )
    if timezone.now() >= give_up_at:
        candidate.mark_failed(
            reason=f"Not found in DB {settings.SUCCESS_CHECK_GIVE_UP_MINUTES} min after submission"
        )
