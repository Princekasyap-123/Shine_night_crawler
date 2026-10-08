import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_timezone

import redis
from celery import shared_task
from django.conf import settings
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from candidates.models import CandidateRecord
from common.exceptions import SessionExpiredError
from crawler.browser import (
    check_shine_session_valid,
    is_login_page,
    shine_browser_session,
    throttle_between_pages,
    wait_for_acceptable_load,
)
from crawler.extractors import shine as shine_extractor
from sessions.models import CrawlSession
from submission.client import check_phone_exists

logger = logging.getLogger(__name__)

# Cached result of the lightweight session-health check, read by the
# dashboard via stats/'s API — kept in the same Redis DB as the
# browser singleton lock. TTL is generous relative to how often the
# check itself runs (SHINE_SESSION_CHECK_INTERVAL_SECONDS) so a
# temporarily-stopped beat/worker doesn't instantly make the dashboard
# show a stale "unknown" state.
SHINE_SESSION_VALID_KEY = "crawler:shine_session:valid"
SHINE_SESSION_CHECKED_AT_KEY = "crawler:shine_session:checked_at"
_SHINE_SESSION_CACHE_TTL_SECONDS = 30 * 60


def _redis_client():
    return redis.Redis.from_url(settings.CELERY_STATE_REDIS_URL)


# Per-session set of search terms whose results have been crawled to the
# end. control.tasks.next_search_term skips these, so a term that already
# ran to its last page is not re-run (and re-read page by page, with
# every card dropped by the session's dedup) for the rest of the night.
_DONE_TERMS_TTL_SECONDS = 24 * 60 * 60
# A pagination timeout before this page is treated as a transient failure
# (the term stays eligible for another run); at or after it, as the end
# of the results Shine is willing to show for one search.
_MIN_PAGES_TO_TRUST_END = 30


def _done_terms_key(session_id: int) -> str:
    return f"crawler:session:{session_id}:done_terms"


def _mark_term_done(session_id: int, term: str) -> None:
    client = _redis_client()
    key = _done_terms_key(session_id)
    client.sadd(key, term)
    client.expire(key, _DONE_TERMS_TTL_SECONDS)


def done_terms_for_session(session_id: int) -> set:
    return {t.decode() for t in _redis_client().smembers(_done_terms_key(session_id))}


@shared_task
def check_shine_session_health():
    """
    Lightweight periodic check (see
    crawler.browser.check_shine_session_valid — plain HTTP, no
    Chromium) that keeps a cached "is the saved Shine login still
    good?" status the dashboard can show at a glance, instead of an
    operator only finding out when a real crawl attempt fails with
    SessionExpiredError. Deliberately does not attempt to launch a
    browser or re-crawl anything itself — this task's only job is to
    check and record status.
    """
    valid = check_shine_session_valid()
    client = _redis_client()
    checked_at = datetime.now(dt_timezone.utc).isoformat()

    if valid is None:
        # Couldn't determine (no saved session file, or a transient
        # network error) — record "unknown" rather than leaving the
        # previous cached value looking falsely current.
        client.delete(SHINE_SESSION_VALID_KEY)
        client.set(
            SHINE_SESSION_CHECKED_AT_KEY, checked_at, ex=_SHINE_SESSION_CACHE_TTL_SECONDS
        )
        return

    client.set(
        SHINE_SESSION_VALID_KEY, "1" if valid else "0", ex=_SHINE_SESSION_CACHE_TTL_SECONDS
    )
    client.set(SHINE_SESSION_CHECKED_AT_KEY, checked_at, ex=_SHINE_SESSION_CACHE_TTL_SECONDS)

    if not valid:
        logger.warning(
            "Shine session health check: saved login has expired. "
            "Re-run `manage.py shine_login` before the next crawl."
        )


def _advanced_search_url() -> str:
    return f"{settings.SHINE_BASE_URL}/advancedsearch/"


def _profile_url_for_card(card_id: str) -> str:
    """
    TODO: the candidate card's real per-profile deep link hasn't been
    confirmed — content.js never needed one, since the extension reads
    contact info directly off the list-page card rather than
    navigating to a profile page. Until that's confirmed, the card's
    own stable "cnd_div_<hash>" id is used as a URL-shaped surrogate
    (a fragment on the search page itself) purely so it satisfies
    CandidateRecord.shine_profile_url's URLField and stays unique per
    candidate — it is NOT a real navigable profile link.
    """
    return f"{settings.SHINE_BASE_URL}/recruiter/search/advanced/#{card_id}"


def _phones_already_extracted(phones):
    unique = {phone for phone in phones if phone}
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=settings.ATS_CHECK_CONCURRENCY) as pool:
        return dict(zip(unique, pool.map(check_phone_exists, unique)))


def _save_candidate_cards(session, search_term, page_number, results_page_url, cards):
    saved = 0
    # Dedup checks run concurrently for the whole page up front — the
    # ATS /check call is slow (several seconds each), so doing them one
    # card at a time would dominate page time. Still before any card is
    # queued for submission; DB writes below stay on this thread.
    already_extracted = _phones_already_extracted(card[1] for card in cards)
    for card_id, phone, card_text, elements, images, files in cards:
        status = CandidateRecord.Status.QUEUED
        if already_extracted.get(phone):
            status = CandidateRecord.Status.ALREADY_EXTRACTED

        _, created = CandidateRecord.objects.get_or_create(
            session=session,
            shine_profile_url=_profile_url_for_card(card_id),
            defaults={
                "raw_text": card_text,
                "elements_json": elements,
                "images_json": images,
                "files_json": files,
                "phone": phone,
                "search_term": search_term,
                "page_number": page_number,
                "results_page_url": results_page_url,
                "status": status,
            },
        )
        if created:
            saved += 1
    return saved


@shared_task(bind=True, time_limit=4 * 3600, soft_time_limit=4 * 3600 - 60)
def crawl_search_term(self, session_id: int, search_term: str):
    """
    Runs one search term to completion: opens a single browser session,
    paginates through every results page for that term, and queues one
    CandidateRecord per card. One task = one term, so control/'s
    round-robin rotation and the backlog pause/resume check both
    operate between task runs rather than mid-crawl.
    """
    session = CrawlSession.objects.get(pk=session_id)

    if session.status != CrawlSession.Status.RUNNING:
        logger.info("Session #%s is not running, skipping %r", session_id, search_term)
        return

    if session.is_paused:
        logger.info("Session #%s is paused, skipping %r", session_id, search_term)
        return

    try:
        with shine_browser_session() as page:
            page.goto(_advanced_search_url(), wait_until="networkidle")

            if is_login_page(page):
                raise SessionExpiredError(
                    "Shine redirected to login while opening the search form "
                    f"for {search_term!r} — saved session has expired."
                )

            try:
                shine_extractor.submit_keyword_search(
                    page, search_term, timeout_ms=settings.CRAWLER_PAGE_LOAD_TIMEOUT_MS
                )
            except Exception:
                # Submission mechanism is a best-effort guess (see
                # submit_keyword_search's docstring) — a failure here
                # means this term just doesn't get crawled this cycle,
                # not that the whole session is broken. Logged loudly
                # so it's visible rather than silently skipped.
                logger.error(
                    "Failed to submit search for %r, skipping this cycle",
                    search_term,
                    exc_info=True,
                )
                return

            page_number = 1
            total_saved = 0
            retries = 0
            # True only when this term genuinely ran out of pages (not when it
            # was stopped, errored out or hit a transient failure).
            finished = False

            while True:
                # Re-checked every page, not just once at task start —
                # otherwise a "Stop Crawling" click mid-pagination has
                # no effect until this term finishes ALL its pages
                # (which could be hundreds), since this task loops
                # entirely on its own without control/'s dispatch loop
                # ever getting a chance to intervene.
                session.refresh_from_db(fields=["status"])
                if session.status != CrawlSession.Status.RUNNING:
                    logger.info(
                        "Session #%s stopped mid-crawl; halting %r after page %s",
                        session_id,
                        search_term,
                        page_number - 1,
                    )
                    break

                wait_for_acceptable_load()

                try:
                    cards = shine_extractor.extract_candidate_cards_html(page)
                except Exception:
                    retries += 1
                    logger.warning(
                        "Extraction failed on page %s for %r (attempt %s/%s)",
                        page_number,
                        search_term,
                        retries,
                        settings.CRAWLER_MAX_RETRIES_PER_PAGE,
                        exc_info=True,
                    )
                    if retries > settings.CRAWLER_MAX_RETRIES_PER_PAGE:
                        logger.error(
                            "Giving up on page %s for %r after %s retries",
                            page_number,
                            search_term,
                            retries,
                        )
                        break
                    throttle_between_pages()
                    continue

                retries = 0
                total_saved += _save_candidate_cards(
                    session, search_term, page_number, page.url, cards
                )

                if not shine_extractor.has_next_page(page):
                    finished = True
                    break

                throttle_between_pages()
                try:
                    shine_extractor.go_to_next_page(page)
                except (TimeoutError, PlaywrightTimeoutError):
                    # Shine stops advancing at the end of a search's results
                    # (observed at page ~44-48). Past _MIN_PAGES_TO_TRUST_END
                    # pages this is treated as the natural end of the term;
                    # earlier than that it is a transient failure and the
                    # term stays eligible for another run.
                    logger.warning(
                        "Pagination stopped advancing for %r after page %s",
                        search_term,
                        page_number,
                    )
                    finished = page_number >= _MIN_PAGES_TO_TRUST_END
                    break

                if is_login_page(page):
                    raise SessionExpiredError(
                        "Shine redirected to login while paginating "
                        f"{search_term!r} (page {page_number + 1}) — saved "
                        "session has expired."
                    )

                page_number += 1

            logger.info(
                "Finished %r: %s pages, %s new candidates queued",
                search_term,
                page_number,
                total_saved,
            )
            if finished:
                _mark_term_done(session_id, search_term)

    except SessionExpiredError:
        # Deliberately not retried: retrying against an expired login
        # wall would just burn CPU all night for nothing. Previously
        # this only logged and re-raised, with nothing actually
        # stopping the session — dispatch_next_crawl fired again on
        # the very next control tick (~60s later), launched a fresh
        # headless Chromium, hit the same expired login, and repeated
        # this every tick all night. Explicitly stopping the session
        # here is what actually prevents that: with no RUNNING session
        # left, dispatch_next_crawl and tick_control_loop both become
        # no-ops until an operator re-logs in (manage.py shine_login)
        # and starts a fresh session.
        logger.error(
            "Shine session expired during %r — stopping session #%s so it "
            "doesn't keep relaunching a browser against a dead login every "
            "tick. Re-run `manage.py shine_login` and start a new session.",
            search_term,
            session_id,
        )
        session.refresh_from_db(fields=["status"])
        if session.status == CrawlSession.Status.RUNNING:
            session.mark_completed()
        raise