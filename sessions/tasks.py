import logging

from celery import shared_task

from common.exceptions import SessionAlreadyRunningError
from control.tasks import (
    dispatch_next_crawl,
    is_within_submission_window,
    update_pause_state,
)
from sessions.models import CrawlSession

logger = logging.getLogger(__name__)


@shared_task
def start_nightly_session():
    """
    Scheduled via Celery beat at NIGHT_START_HOUR. If a session is
    already running — a manual test run left going, or this firing
    twice after a worker restart — this is a no-op rather than an
    error: CrawlSession.objects.create_session() already refuses a
    second concurrent session, so there's nothing more to do here.
    """
    try:
        session = CrawlSession.objects.create_session(
            triggered_by=CrawlSession.TriggeredBy.SCHEDULED
        )
    except SessionAlreadyRunningError:
        logger.info("Nightly session start skipped: a session is already running.")
        return

    logger.info("Started nightly CrawlSession #%s", session.pk)


@shared_task
def start_manual_session_once():
    """
    One-off ad-hoc test window support — scheduled via a single
    current_app.send_task(..., eta=...) call for a specific one-time
    start, NOT registered in CELERY_BEAT_SCHEDULE (that would make it
    recurring daily, which is explicitly not wanted for ad-hoc test
    windows). Creates a MANUAL-triggered session deliberately, not a
    SCHEDULED one: only MANUAL sessions are exempt from
    is_within_night_window() in dispatch_next_crawl/tick_control_loop,
    so a SCHEDULED session started outside NIGHT_START_HOUR/
    NIGHT_END_HOUR would have every crawl dispatch skipped, and
    tick_control_loop's own backstop would immediately auto-stop it
    again. No-ops (like start_nightly_session) if a session is already
    running.
    """
    try:
        session = CrawlSession.objects.create_session(
            triggered_by=CrawlSession.TriggeredBy.MANUAL
        )
    except SessionAlreadyRunningError:
        logger.info("One-off session start skipped: a session is already running.")
        return

    logger.info("Started one-off test CrawlSession #%s", session.pk)


@shared_task
def stop_nightly_session():
    """
    Hard stop at SUBMISSION_END_HOUR (10am by default). Ends whatever
    session is currently running, scheduled or manual.

    Crawling itself already stops much earlier, at NIGHT_END_HOUR
    (5am) — dispatch_next_crawl refuses to hand out new search terms
    past that time (is_within_night_window()), so no new extraction
    happens after 5am even though the session stays 'running'. That
    gap is deliberate: auto_submit_queued_candidates only processes
    RUNNING sessions, and the new flow wants submission to keep
    draining the queue for 5 more hours after crawling stops. This
    task is what finally closes the session out and, by extension,
    stops auto-submit from picking it up on the next tick.
    """
    session = CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING).first()
    if session is None:
        return

    session.mark_completed()
    logger.info("Stopped CrawlSession #%s at submission-end", session.pk)


@shared_task
def tick_control_loop():
    """
    The heartbeat: scheduled every CONTROL_TICK_INTERVAL_SECONDS for
    the whole night. Finds whatever session is currently running (if
    any) and drives it one step — re-evaluate backlog/pause state,
    then let dispatch_next_crawl decide whether to hand out the next
    search term.

    Also doubles as a backstop for stop_nightly_session(): if a
    SCHEDULED session is still marked running past the *submission*
    window (e.g. the scheduled stop task never fired because the
    worker was down), this stops it itself rather than letting a stale
    session keep the submission/polling pipeline alive indefinitely on
    a shared VPS. Deliberately checks is_within_submission_window()
    (23:00-10:00), not is_within_night_window() (23:00-05:00) — the
    session is meant to stay RUNNING for those extra 5 hours so
    auto_submit_queued_candidates keeps draining the queue after
    crawling itself has already stopped; using the shorter crawl
    window here would prematurely end the session (and submission with
    it) at 5am. A MANUAL session (started from the dashboard's "Start
    Crawling" button) is exempt from this backstop — an operator
    running a manual crawl during the day is deliberate, not a stale
    leftover, and is stopped only by the dashboard's own "Stop
    Crawling" button.
    """
    session = CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING).first()
    if session is None:
        return

    if (
        session.triggered_by != CrawlSession.TriggeredBy.MANUAL
        and not is_within_submission_window()
    ):
        session.mark_completed()
        logger.warning(
            "Session #%s was still running outside the submission window; stopped it.",
            session.pk,
        )
        return

    update_pause_state.delay(session.pk)
    dispatch_next_crawl.delay(session.pk)
