import logging

from celery import shared_task

from common.exceptions import SessionAlreadyRunningError
from control.tasks import dispatch_next_crawl, is_within_night_window, update_pause_state
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
def stop_nightly_session():
    """
    Hard stop at NIGHT_END_HOUR. Ends whatever session is currently
    running, scheduled or manual — 5am is a hard ceiling for both, not
    just for the scheduled run.
    """
    session = CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING).first()
    if session is None:
        return

    session.mark_completed()
    logger.info("Stopped CrawlSession #%s at night-end", session.pk)


@shared_task
def tick_control_loop():
    """
    The heartbeat: scheduled every CONTROL_TICK_INTERVAL_SECONDS for
    the whole night. Finds whatever session is currently running (if
    any) and drives it one step — re-evaluate backlog/pause state,
    then let dispatch_next_crawl decide whether to hand out the next
    search term.

    Also doubles as a backstop for stop_nightly_session(): if a
    SCHEDULED session is still marked running past the night window
    (e.g. the scheduled stop task never fired because the worker was
    down), this stops it itself rather than letting a stale session
    keep dispatching crawls into daylight hours on a shared VPS. A
    MANUAL session (started from the dashboard's "Start Crawling"
    button) is exempt from this backstop — an operator running a
    manual crawl during the day is deliberate, not a stale leftover,
    and is stopped only by the dashboard's own "Stop Crawling" button.
    """
    session = CrawlSession.objects.filter(status=CrawlSession.Status.RUNNING).first()
    if session is None:
        return

    if (
        session.triggered_by != CrawlSession.TriggeredBy.MANUAL
        and not is_within_night_window()
    ):
        session.mark_completed()
        logger.warning(
            "Session #%s was still running outside the night window; stopped it.",
            session.pk,
        )
        return

    update_pause_state.delay(session.pk)
    dispatch_next_crawl.delay(session.pk)
