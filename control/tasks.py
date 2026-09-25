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
    True between NIGHT_START_HOUR and NIGHT_END_HOUR, handling the
    overnight wrap (22 -> 5) rather than assuming start < end.
    """
    now = now or timezone.localtime()
    start, end = settings.NIGHT_START_HOUR, settings.NIGHT_END_HOUR
    if start <= end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


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
    Candidates already handed to the extension backend but not yet
    resolved by polling (success/failed). This is the number that
    matters for pause/resume: it measures how far submission+Ollama
    processing has fallen behind the crawler, not how much raw HTML
    the crawler has queued locally.
    """
    return session.candidates.filter(status=CandidateRecord.Status.SUBMITTED).count()


@shared_task
def update_pause_state(session_id: int):
    """
    Hysteresis pause/resume: pauses once backlog crosses
    backlog_pause_threshold, and only resumes once it has dropped back
    down to backlog_resume_threshold (a lower number) rather than the
    same threshold — without that gap, a backlog sitting right at the
    line would flip pause state on almost every tick.
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

            if backlog <= session.backlog_resume_threshold:
                session.is_paused = False
                session.paused_until = None
                session.save(update_fields=["is_paused", "paused_until"])
                logger.info(
                    "Session #%s resumed: backlog %s <= resume threshold %s",
                    session_id,
                    backlog,
                    session.backlog_resume_threshold,
                )
            else:
                # Pause window elapsed but backlog is still too high —
                # extend it rather than resuming into a submission
                # pipeline that clearly hasn't caught up yet.
                session.paused_until = now + timedelta(minutes=session.pause_duration_minutes)
                session.save(update_fields=["paused_until"])
                logger.warning(
                    "Session #%s stays paused: backlog %s still above resume threshold %s",
                    session_id,
                    backlog,
                    session.backlog_resume_threshold,
                )
        elif backlog >= session.backlog_pause_threshold:
            session.is_paused = True
            session.paused_until = now + timedelta(minutes=session.pause_duration_minutes)
            session.save(update_fields=["is_paused", "paused_until"])
            logger.warning(
                "Session #%s paused: backlog %s >= pause threshold %s",
                session_id,
                backlog,
                session.backlog_pause_threshold,
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
