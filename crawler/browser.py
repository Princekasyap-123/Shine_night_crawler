import json
import logging
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import redis
import requests
from django.conf import settings
from playwright.sync_api import sync_playwright

from common.exceptions import SessionExpiredError

logger = logging.getLogger(__name__)

# We only need candidate-card HTML, not a rendered page, so anything
# that isn't structural markup is dropped at the network layer. This
# is the single biggest CPU/bandwidth lever available — typically
# cuts page load cost by well over half.
#
# CONFIRMED (live test): stylesheets are deliberately NOT in this set,
# even though they're not needed for HTML extraction either. Shine's
# pages contain elements hidden purely via CSS (e.g. a "Change
# Password" modal present in the DOM on every logged-in page) — with
# stylesheets blocked, the browser has no rule to hide them, so they
# render as visible by default and break any visibility-based check
# (is_login_page()'s real password-field detection, in particular).
# CSS is a small fraction of page weight next to images/fonts, so this
# trades a little of the CPU/bandwidth saving for correctness.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "hotjar",
    "clarity.ms",
    "criteo",
)

_LAUNCH_ARGS = [
    "--disable-gpu",
    # Shared VPS /dev/shm is usually small; Chromium crashes under
    # memory pressure without this rather than falling back cleanly.
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--no-first-run",
    "--mute-audio",
]

_BROWSER_LOCK_KEY = "crawler:browser:lock"
# Covers the whole night window as a ceiling; released explicitly the
# moment the session ends, so this TTL only matters if the process is
# killed hard enough to skip the `finally`.
_BROWSER_LOCK_TTL_SECONDS = 8 * 60 * 60


def _redis_client():
    return redis.Redis.from_url(settings.CELERY_STATE_REDIS_URL)


def is_browser_session_active() -> bool:
    """
    Non-blocking peek at the same lock _browser_singleton_lock() holds
    for the full duration of a crawl_search_term run (page loads,
    pagination, everything). control.tasks.dispatch_next_crawl uses
    this to skip dispatching a new term while one is still in
    progress, rather than queuing up redundant dispatches that would
    otherwise pile up in the crawl queue every tick a multi-minute
    crawl is still running.
    """
    return bool(_redis_client().exists(_BROWSER_LOCK_KEY))


@contextmanager
def _browser_singleton_lock():
    """
    Guarantees at most one Chromium instance runs at a time. Celery's
    own `-Q crawl -c 1` worker concurrency is the first line of
    defense; this Redis lock is the second, and the one that actually
    holds if two separate worker processes are ever started by
    mistake on this box (an in-memory flag wouldn't span processes).
    """
    client = _redis_client()
    acquired = client.set(_BROWSER_LOCK_KEY, "1", nx=True, ex=_BROWSER_LOCK_TTL_SECONDS)
    if not acquired:
        raise RuntimeError(
            "A crawler browser session is already running (lock held). "
            "Refusing to start a second Chromium instance on this VPS."
        )
    try:
        yield
    finally:
        client.delete(_BROWSER_LOCK_KEY)


def _block_unneeded_requests(route, request):
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        return route.abort()
    if any(s in request.url for s in _BLOCKED_URL_SUBSTRINGS):
        return route.abort()
    return route.continue_()


def _storage_state_path() -> Path:
    return Path(settings.SHINE_STORAGE_STATE_PATH)


def has_saved_session() -> bool:
    return _storage_state_path().exists()


def is_login_page(page) -> bool:
    """
    Best-effort check for "the saved session got logged out". Call
    this after any navigation that's supposed to land on a logged-in
    page — catches an expired storageState within one page load
    instead of after N retries spent hammering a login wall.

    CONFIRMED (live test): a logged-in Shine page can still contain
    hidden <input type="password"> elements elsewhere in the DOM (a
    "Change Password" modal, present-but-hidden on every page) — so
    counting *any* password input, visible or not, produces a false
    positive on perfectly valid sessions. Only a *visible* password
    field counts as evidence of an actual login form.

    CONFIRMED (live test, recurred multiple times): an expired session
    navigating to an authenticated deep link like /advancedsearch/
    does NOT land on a classic login form at all — it silently bounces
    to the public marketing homepage at
    "recruiter.shine.com/recruiter/?next=<original-path>", which has
    no password field anywhere on it. Without this check, that bounce
    was invisible here and crawl_search_term would only discover the
    expired session several steps later, as a confusing "search
    keyword input not found" ValueError instead of a clear,
    immediately-raised SessionExpiredError.
    """
    if _url_indicates_login_required(page.url):
        return True

    return page.locator("input[type='password']:visible").count() > 0


def _url_indicates_login_required(url: str) -> bool:
    """
    The URL-pattern half of is_login_page()'s check, factored out so
    check_shine_session_valid() (a plain HTTP request, no browser, so
    no DOM to inspect) can reuse the same confirmed detection logic
    instead of duplicating it.
    """
    url = url.lower()
    if "login" in url or "signin" in url:
        return True

    parsed_path = urlparse(url).path.rstrip("/")
    return parsed_path == "/recruiter" and "next=" in url


_SESSION_CHECK_TIMEOUT_SECONDS = 10


def check_shine_session_valid() -> bool | None:
    """
    Lightweight "is the saved session still good?" check: a single
    plain HTTP GET with the saved session's cookies attached — no
    Chromium launch, no Playwright, orders of magnitude cheaper than
    discovering expiry by attempting a real crawl. Meant to run on its
    own frequent, low-cost periodic schedule (see
    crawler.tasks.check_shine_session_health) so an expired session
    shows up on the dashboard almost immediately instead of only being
    discovered whenever the next crawl attempt happens to fire.

    Returns True (session looks valid), False (confirmed expired —
    same URL-bounce pattern is_login_page() detects), or None (the
    check itself couldn't complete — no saved session file, or a
    network error — which is "unknown", not "confirmed expired", and
    is treated conservatively: the dashboard should show this as a
    warning rather than a hard failure).
    """
    if not has_saved_session():
        return None

    try:
        with open(_storage_state_path(), encoding="utf-8") as f:
            storage_state = json.load(f)
    except (OSError, ValueError):
        return None

    cookies = {
        cookie["name"]: cookie["value"]
        for cookie in storage_state.get("cookies", [])
        if "shine.com" in cookie.get("domain", "")
    }
    if not cookies:
        return None

    try:
        response = requests.get(
            f"{settings.SHINE_BASE_URL}/advancedsearch/",
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
            timeout=_SESSION_CHECK_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None

    return not _url_indicates_login_required(response.url)


@contextmanager
def shine_browser_session():
    """
    Yields a single logged-in Playwright Page, backed by one headless
    Chromium instance for the whole `with` block. The browser process
    is launched fresh and torn down completely on exit — including on
    exception — rather than kept warm across the night: a headless
    Chrome instance left running for hours accumulates memory, and an
    eventual OOM-kill on a shared VPS risks taking other processes
    down with it, not just the crawler.
    """
    if not has_saved_session():
        raise SessionExpiredError(
            f"No saved Shine session at {_storage_state_path()}. "
            "Log in manually once and save storage state before running the crawler."
        )

    with _browser_singleton_lock():
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            try:
                context = browser.new_context(storage_state=str(_storage_state_path()))
                context.set_default_timeout(settings.CRAWLER_PAGE_LOAD_TIMEOUT_MS)
                context.route("**/*", _block_unneeded_requests)

                page = context.new_page()
                try:
                    yield page
                finally:
                    context.close()
            finally:
                browser.close()


def throttle_between_pages():
    """
    Randomized delay between page loads. Serves two purposes at once:
    keeps Shine's bot-detection from seeing fixed-interval requests,
    and caps how fast the crawler can burn CPU regardless of how
    quickly Shine itself responds.
    """
    delay = random.uniform(
        settings.CRAWLER_DELAY_MIN_SECONDS, settings.CRAWLER_DELAY_MAX_SECONDS
    )
    time.sleep(delay)


def wait_for_acceptable_load(check_interval_seconds: float = 5.0):
    """
    Self-throttle on top of the systemd cgroup's hard CPUQuota: before
    fetching the next page, if the box's 1-minute load average is
    already above CRAWLER_MAX_LOAD_AVERAGE, sleep and recheck instead
    of adding more load on top of whatever else is running on this
    shared VPS. No-ops on platforms without os.getloadavg() (Windows
    dev machines) since this only matters on the Linux VPS.
    """
    while True:
        try:
            load_1min = os.getloadavg()[0]
        except (AttributeError, OSError):
            return

        if load_1min <= settings.CRAWLER_MAX_LOAD_AVERAGE:
            return

        logger.warning(
            "Load average %.2f exceeds CRAWLER_MAX_LOAD_AVERAGE=%.2f, "
            "pausing before next page fetch.",
            load_1min,
            settings.CRAWLER_MAX_LOAD_AVERAGE,
        )
        time.sleep(check_interval_seconds)
