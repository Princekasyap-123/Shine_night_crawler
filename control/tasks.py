import logging
from datetime import timedelta

import redis
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from candidates.models import CandidateRecord
from control.redis_keys import ROTATION_CURSOR_TTL_SECONDS, rotation_cursor_key
from crawler.browser import is_browser_session_active
from crawler.models import SearchTerm
from crawler.tasks import crawl_search_term
from sessions.models import CrawlSession

logger = logging.getLogger(__name__)


def _redis_client():
    return redis.Redis.from_url(settings.CELERY_STATE_REDIS_URL)


def is_within_night_window(now=None) -> bool:
    """
    True between the configured start and end time, to the minute —
    handling the overnight wrap (23:00 -> 05:00) rather than assuming
    start < end. Minute precision (not just hour) matters for windows
    like 15:40-16:30 that start/end mid-hour; comparing only .hour
    would treat the whole of both hours as in-window instead of just
    the configured range within them.

    This is the CRAWLING window only (NIGHT_START_HOUR/MINUTE ->
    NIGHT_END_HOUR/MINUTE, 23:00-05:00 by default) — it gates whether
    dispatch_next_crawl is allowed to hand out a new search term. It is
    deliberately shorter than the session's own lifetime: see
    is_within_submission_window() for the later boundary submission/
    polling run until.
    """
    now = now or timezone.localtime()
    start = settings.NIGHT_START_HOUR * 60 + settings.NIGHT_START_MINUTE
    end = settings.NIGHT_END_HOUR * 60 + settings.NIGHT_END_MINUTE
    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def is_within_submission_window(now=None) -> bool:
    """
    True between NIGHT_START_HOUR/MINUTE and the later
    SUBMISSION_END_HOUR/MINUTE (23:00-10:00 by default) — the window
    submission and polling are allowed to keep working in, five hours
    past NIGHT_END_HOUR where crawling itself stops. Same overnight-
    wrap handling as is_within_night_window(); kept as a separate
    function (not a parameterized end time) so each call site's intent
    stays obvious at a glance.
    """
    now = now or timezone.localtime()
    start = settings.NIGHT_START_HOUR * 60 + settings.NIGHT_START_MINUTE
    end = settings.SUBMISSION_END_HOUR * 60 + settings.SUBMISSION_END_MINUTE
    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def next_search_term(session_id: int) -> str | None:
    """
    Round-robins through the dashboard-managed SearchTerm rows
    (is_active=True only) using a Redis counter scoped to this
    session. INCR is atomic, so this is safe even if two control ticks
    somehow overlap. Terms are ordered by SearchTerm.Meta.ordering
    (alphabetical) so the rotation order stays stable across ticks
    even as terms are added/removed mid-session.
    """
    terms = list(
        SearchTerm.objects.filter(is_active=True).values_list("term", flat=True)
    )
    if not terms:
        return None

    client = _redis_client()
    key = rotation_cursor_key(session_id)
    index = client.incr(key) - 1
    client.expire(key, ROTATION_CURSOR_TTL_SECONDS)

    return terms[index % len(terms)]


def _backlog_count(session: CrawlSession) -> int:
    """
    Candidates extracted but not yet submitted to the extension
    backend. This is the number the new flow's pause/resume cycle
    reacts to: how far the crawler has raced ahead of submission,
    which is a local-queue-size concern, not a "has Ollama caught up"
    concern (that used to be measured via SUBMITTED count, before this
    was repurposed — see update_pause_state()'s docstring).
    """
    return session.candidates.filter(status=CandidateRecord.Status.QUEUED).count()


@shared_task
def update_pause_state(session_id: int):
    """
    Fixed-duration pause/resume: pauses crawling once the QUEUED count
    reaches backlog_pause_threshold (1000 by default), then resumes
    unconditionally once pause_duration_minutes (10 by default) has
    elapsed — no hysteresis against backlog_resume_threshold anymore.
    That field is kept on the model (existing sessions/history still
    reference it, and removing it isn't worth a schema churn) but is no
    longer read here: the new flow explicitly wants a flat "pause for
    exactly 10 minutes, then go again" cycle rather than waiting for
    the queue to drain back down to some lower number, since crawling
    and queueing are meant to run continuously otherwise.
    """
    with transaction.atomic():
        session = CrawlSession.objects.select_for_update().get(pk=session_id)

        if session.status != CrawlSession.Status.RUNNING:
            return

        backlog = _backlog_count(session)
        now = timezone.now()

        if session.is_paused:
            if session.paused_until and now < session.paused_until:
                return

            session.is_paused = False
            session.paused_until = None
            session.save(update_fields=["is_paused", "paused_until"])
            logger.info(
                "Session #%s resumed after pause window elapsed (queued was %s)",
                session_id,
                backlog,
            )
        elif backlog >= session.backlog_pause_threshold:
            session.is_paused = True
            session.paused_until = now + timedelta(minutes=session.pause_duration_minutes)
            session.save(update_fields=["is_paused", "paused_until"])
            logger.warning(
                "Session #%s paused: queued %s >= pause threshold %s, resuming in %s min",
                session_id,
                backlog,
                session.backlog_pause_threshold,
                session.pause_duration_minutes,
            )


@shared_task
def dispatch_next_crawl(session_id: int):
    """
    The control loop's main tick: decides whether tonight's crawl
    should advance at all right now, and if so, hands the next
    round-robin search term to crawler.tasks.crawl_search_term.

    Deliberately checks is_paused directly rather than calling
    update_pause_state() first — this task decides whether to dispatch
    work, it isn't responsible for (re)evaluating the pause decision
    itself; that's a separate periodic tick so the two concerns don't
    get tangled into one task that both judges and acts on the same
    read.

    The night window only gates SCHEDULED sessions — it exists to keep
    the automated 10pm-5am run from burning CPU/bot-detection risk
    outside its intended hours, not to block an operator's explicit
    manual "Start Crawling" click from the dashboard, which is
    deliberately allowed to run any time of day.
    """
    session = CrawlSession.objects.get(pk=session_id)

    if (
        session.triggered_by != CrawlSession.TriggeredBy.MANUAL
        and not is_within_night_window()
    ):
        logger.info("Outside night window, skipping dispatch for session #%s", session_id)
        return

    if session.status != CrawlSession.Status.RUNNING:
        logger.info("Session #%s is not running, skipping dispatch", session_id)
        return

    if session.is_paused:
        logger.info("Session #%s is paused, skipping dispatch", session_id)
        return

    if is_browser_session_active():
        # A crawl_search_term run can take several minutes across many
        # pages; without this check a tick firing every
        # CONTROL_TICK_INTERVAL_SECONDS would queue up a redundant
        # dispatch for every tick that overlaps one still-running
        # crawl, burning through the rotation cursor far faster than
        # terms actually get crawled.
        logger.debug("Session #%s: a crawl is already in progress, skipping dispatch", session_id)
        return

    term = next_search_term(session_id)
    if term is None:
        logger.error(
            "Session #%s: no active search terms configured, nothing to crawl",
            session_id,
        )
        return

    crawl_search_term.delay(session_id, term)
