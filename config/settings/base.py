import os
import socket
from pathlib import Path

from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Loaded here (the one module every entry point — manage.py, wsgi.py,
# celery.py — imports through) so .env takes effect the same way
# whether this is run locally or under systemd on the VPS. Vars
# already set in the real environment (e.g. by systemd's own
# EnvironmentFile=) take precedence over .env, since override=False
# never clobbers an existing os.environ value.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")

DEBUG = False

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_celery_beat",
    "candidates",
    "sessions",
    "crawler",
    "control",
    "submission",
    "polling",
    "stats",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "shine_crawler"),
        "USER": os.environ.get("DB_USER", "shine_crawler"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# --- Redis / Celery ---------------------------------------------------
# Single Redis instance, three logical DBs so the crawler's queues never
# share keyspace/eviction pressure with anything else that might already
# be using Redis DB 0 on this shared VPS.
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_BROKER_DB = os.environ.get("REDIS_BROKER_DB", "1")
REDIS_RESULT_DB = os.environ.get("REDIS_RESULT_DB", "2")
REDIS_STATE_DB = os.environ.get("REDIS_STATE_DB", "3")

CELERY_BROKER_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_BROKER_DB}"
CELERY_RESULT_BACKEND = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_RESULT_DB}"
CELERY_STATE_REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_STATE_DB}"

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True

# --- Connection resilience -------------------------------------------
# Local dev runs Redis inside WSL2 with mirrored networking, which is
# known to drop/reset TCP connections intermittently even though the
# broker itself is fine — these settings make the worker retry and
# keep the connection alive through that instead of dying. Harmless on
# the production VPS too, where Redis and the worker are on the same
# host and drops should be rare anyway.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = None  # retry indefinitely, never give up
CELERY_BROKER_CONNECTION_TIMEOUT = 30

_socket_keepalive_options = {}
if hasattr(socket, "TCP_KEEPIDLE"):
    _socket_keepalive_options[socket.TCP_KEEPIDLE] = 60
if hasattr(socket, "TCP_KEEPINTVL"):
    _socket_keepalive_options[socket.TCP_KEEPINTVL] = 10
if hasattr(socket, "TCP_KEEPCNT"):
    _socket_keepalive_options[socket.TCP_KEEPCNT] = 5

CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_keepalive": True,
    "socket_keepalive_options": _socket_keepalive_options,
    "retry_on_timeout": True,
}

# If the broker connection drops mid-task, don't cancel whatever's
# currently running (e.g. a crawl mid-pagination) just because of a
# transient network blip.
CELERY_WORKER_CANCEL_LONG_RUNNING_TASKS_ON_CONNECTION_LOSS = False

# If a worker process is killed mid-task (OOM, cgroup MemoryMax,
# server restart), the task goes back on the queue for another worker
# instead of silently vanishing and leaving a CandidateRecord stuck in
# an intermediate status forever.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Each worker process reserves only one task at a time rather than
# prefetching several — crawl_search_term tasks can run for many
# minutes across pages, so a process shouldn't hoard multiple long
# crawls in its local buffer while another sits idle.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Hard ceiling per task — if a crawl or submission hangs past this
# despite the timeouts already set at the Playwright/requests level,
# the task is killed outright rather than blocking a worker slot
# indefinitely. (crawl_search_term and auto_submit_queued_candidates
# override this with their own, longer limits on the task decorator.)
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540

# Dedicated, low-concurrency queues so the crawler never competes for
# worker slots with any other Celery workload that might already be
# running on this box. Concurrency is enforced at the worker command
# line (`-Q crawl -c 1`), these names just keep routing explicit.
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "crawler.tasks.*": {"queue": "crawl"},
    "control.tasks.*": {"queue": "control"},
    "submission.tasks.*": {"queue": "submit"},
    "polling.tasks.*": {"queue": "poll"},
    "sessions.tasks.*": {"queue": "control"},
}

# --- Shine crawler ------------------------------------------------------
# Confirmed against the WhiteForce extension's own portal registry: the
# recruiter search/candidate-database UI lives on recruiter.shine.com,
# not www.shine.com.
SHINE_BASE_URL = os.environ.get("SHINE_BASE_URL", "https://recruiter.shine.com")
# `or` (not just os.environ.get's default arg) so a blank-but-present
# SHINE_STORAGE_STATE_PATH= line in .env still falls through to the
# real default — os.environ.get's default only applies when the key is
# missing entirely, not when it's present-but-empty, and Path("")
# resolves to the current directory, which Playwright then tries to
# open as if it were a file.
SHINE_STORAGE_STATE_PATH = os.environ.get("SHINE_STORAGE_STATE_PATH") or str(
    BASE_DIR / "shine_storage_state.json"
)
# Used only by the one-time `manage.py shine_login` command to produce
# SHINE_STORAGE_STATE_PATH — crawler/browser.py itself never logs in,
# it only ever reuses the saved cookies. See that command for how
# these are used (best-effort auto-fill with a manual fallback, since
# the real login form's selectors haven't been confirmed).
SHINE_LOGIN_EMAIL = os.environ.get("SHINE_LOGIN_EMAIL", "")
SHINE_LOGIN_PASSWORD = os.environ.get("SHINE_LOGIN_PASSWORD", "")

# CONFIRMED (live test): Shine's advanced search does NOT work via a
# constructible URL — submitting a search redirects to a server-
# generated "?suid=<hash>" results URL every time, even for a brand
# new search. So instead of a URL template, the crawler navigates to
# <SHINE_BASE_URL>/advancedsearch/ and submits the form directly — see
# crawler/extractors/shine.py's submit_keyword_search().

# Dynamic, backend-owned search terms the crawler rotates through each
# night. Kept in settings (env-overridable) rather than hardcoded so
# the term list can be tuned without touching code; control/ owns the
# rotation logic that decides which term goes next each tick.
SHINE_SEARCH_TERMS = [
    t.strip()
    for t in os.environ.get("SHINE_SEARCH_TERMS", "").split(",")
    if t.strip()
]

# Night window the scheduler is allowed to run crawl/control ticks in.
# Enforced by sessions/tasks.py, not just by cron timing, so a task that
# fires late (e.g. worker was down) never starts a fresh crawl past
# NIGHT_END_HOUR. Minute-level (not just hour) so a window like
# 15:40-16:30 can be expressed exactly — see
# control.tasks.is_within_night_window.
NIGHT_START_HOUR = int(os.environ.get("NIGHT_START_HOUR", "23"))
NIGHT_START_MINUTE = int(os.environ.get("NIGHT_START_MINUTE", "0"))
NIGHT_END_HOUR = int(os.environ.get("NIGHT_END_HOUR", "5"))
NIGHT_END_MINUTE = int(os.environ.get("NIGHT_END_MINUTE", "0"))

# Later boundary submission and polling keep running until, well past
# NIGHT_END_HOUR where crawling itself stops — the queue still has
# candidates sitting in it that were extracted overnight, and the
# extension backend takes time to structure/resolve each one. The
# CrawlSession stays 'running' (not completed) between NIGHT_END_HOUR
# and this time specifically so auto_submit_queued_candidates keeps
# draining it. See control.tasks.is_within_submission_window and
# sessions.tasks.stop_nightly_session.
SUBMISSION_END_HOUR = int(os.environ.get("SUBMISSION_END_HOUR", "10"))
SUBMISSION_END_MINUTE = int(os.environ.get("SUBMISSION_END_MINUTE", "0"))

# --- CPU / resource protection ------------------------------------------
# Hard ceiling on concurrent Playwright browser instances. Must stay 1
# on a shared VPS — this is enforced in code (crawler/browser.py), the
# cgroup CPUQuota/MemoryMax on the systemd unit is the second, OS-level
# line of defense.
CRAWLER_MAX_CONCURRENT_BROWSERS = int(
    os.environ.get("CRAWLER_MAX_CONCURRENT_BROWSERS", "1")
)
CRAWLER_PAGE_LOAD_TIMEOUT_MS = int(
    os.environ.get("CRAWLER_PAGE_LOAD_TIMEOUT_MS", "30000")
)
# Randomized jitter between page loads (seconds) — protects the Shine
# account from bot-detection as much as it protects VPS CPU.
CRAWLER_DELAY_MIN_SECONDS = float(os.environ.get("CRAWLER_DELAY_MIN_SECONDS", "2"))
CRAWLER_DELAY_MAX_SECONDS = float(os.environ.get("CRAWLER_DELAY_MAX_SECONDS", "5"))
CRAWLER_MAX_RETRIES_PER_PAGE = int(os.environ.get("CRAWLER_MAX_RETRIES_PER_PAGE", "2"))

# Self-throttle: if 1-minute load average exceeds this, the crawl task
# sleeps and rechecks before fetching the next page, on top of the
# cgroup's hard CPUQuota cap.
CRAWLER_MAX_LOAD_AVERAGE = float(os.environ.get("CRAWLER_MAX_LOAD_AVERAGE", "2.0"))

# How often the control heartbeat re-evaluates backlog/pause state and
# considers dispatching the next search term. Deliberately much
# shorter than a typical crawl_search_term run (control.tasks
# .dispatch_next_crawl skips dispatch entirely while a crawl is still
# in progress) so pause/resume reacts quickly without spamming the
# crawl queue with redundant dispatches.
CONTROL_TICK_INTERVAL_SECONDS = int(os.environ.get("CONTROL_TICK_INTERVAL_SECONDS", "60"))

# How often polling checks the extension backend for a resolution on
# submitted candidates, and how many it checks per tick. Not scoped to
# any one CrawlSession, but IS scoped to the submission window (see
# control.tasks.is_within_submission_window) — Ollama structuring can
# take a while so a candidate may resolve well after its own session
# has ended, but polling itself still only runs 23:00-10:00.
POLLING_TICK_INTERVAL_SECONDS = int(os.environ.get("POLLING_TICK_INTERVAL_SECONDS", "180"))
POLLING_BATCH_SIZE = int(os.environ.get("POLLING_BATCH_SIZE", "50"))

# How often automatic submission ticks (6 min), how big of a queue has
# to build up before it starts submitting at all, and how many
# candidates go out per batch once it does. Restored per explicit
# instruction — see submission.tasks.auto_submit_queued_candidates's
# docstring for why it was removed and why this version is safe to
# bring back, and for how the threshold/batch gating works.
AUTO_SUBMIT_INTERVAL_SECONDS = int(os.environ.get("AUTO_SUBMIT_INTERVAL_SECONDS", "60"))
AUTO_SUBMIT_QUEUE_THRESHOLD = int(os.environ.get("AUTO_SUBMIT_QUEUE_THRESHOLD", "300"))
AUTO_SUBMIT_BATCH_SIZE = int(os.environ.get("AUTO_SUBMIT_BATCH_SIZE", "100"))

# How often the lightweight Shine-session health check runs (plain
# HTTP request with saved cookies, no browser launch — see
# crawler.browser.check_shine_session_valid). Deliberately much less
# frequent than the other ticks: this exists purely so an expired
# login shows up on the dashboard proactively, not to drive any
# actual crawling, so there's no reason to check it more often than an
# operator would realistically notice/react to it.
SHINE_SESSION_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("SHINE_SESSION_CHECK_INTERVAL_SECONDS", "300")
)

# --- Search terms from the positions API --------------------------------
# Before each night session, every position_name from the White Force weekly
# positions API becomes a search term (control.tasks.sync_search_terms_from_api).
POSITIONS_API_URL = os.environ.get(
    "POSITIONS_API_URL", "https://white-force.com/plus/api/get-position-weekly"
)
# Blank = the Monday of the current week (falls back to the Monday before it
# when this week's list is empty). Set YYYY-MM-DD to pin a specific week.
POSITIONS_API_DATE = os.environ.get("POSITIONS_API_DATE", "")
POSITIONS_API_TIMEOUT_SECONDS = int(os.environ.get("POSITIONS_API_TIMEOUT_SECONDS", "30"))
# "A / B" position names become two terms, "A" and "B".
POSITIONS_SPLIT_ALTERNATIVES = (
    os.environ.get("POSITIONS_SPLIT_ALTERNATIVES", "true").lower() == "true"
)
# When true, active terms that are not in the API list are switched off
# (never deleted), so the term list mirrors the API.
POSITIONS_DEACTIVATE_OTHERS = (
    os.environ.get("POSITIONS_DEACTIVATE_OTHERS", "true").lower() == "true"
)
# Must be before NIGHT_START_HOUR/MINUTE so the terms are in place when the
# session starts.
POSITIONS_SYNC_HOUR = int(os.environ.get("POSITIONS_SYNC_HOUR", "22"))
POSITIONS_SYNC_MINUTE = int(os.environ.get("POSITIONS_SYNC_MINUTE", "30"))

# "expires" on every entry: if a worker/beat was down and a task was
# queued while nobody was consuming, that task is DROPPED when the
# worker comes back instead of running late. Without this, a stale
# stop_nightly_session (or start_nightly_session) sitting in the queue
# will fire the moment a worker starts and can end a session someone
# just started by hand.
CELERY_BEAT_SCHEDULE = {
    "sync-search-terms-from-api": {
        "task": "control.tasks.sync_search_terms_from_api",
        "schedule": crontab(hour=POSITIONS_SYNC_HOUR, minute=POSITIONS_SYNC_MINUTE),
        "options": {"expires": 1800},
    },
    "start-nightly-crawl-session": {
        "task": "sessions.tasks.start_nightly_session",
        "schedule": crontab(hour=NIGHT_START_HOUR, minute=NIGHT_START_MINUTE),
        "options": {"expires": 600},
    },
    "stop-nightly-crawl-session": {
        # Fires at SUBMISSION_END_HOUR (10am), not NIGHT_END_HOUR (5am)
        # — crawling itself already stops being dispatched at 5am via
        # is_within_night_window(), this task closes the session out
        # once submission has also had its full window to drain the
        # queue. See sessions.tasks.stop_nightly_session's docstring.
        "task": "sessions.tasks.stop_nightly_session",
        "schedule": crontab(hour=SUBMISSION_END_HOUR, minute=SUBMISSION_END_MINUTE),
        "options": {"expires": 600},
    },
    "tick-control-loop": {
        "task": "sessions.tasks.tick_control_loop",
        "schedule": CONTROL_TICK_INTERVAL_SECONDS,
        "options": {"expires": 120},
    },
    # Auto-submit restored per explicit instruction — see
    # submission.tasks.auto_submit_queued_candidates's docstring.
    # Candidates can still also be sent manually anytime via the
    # dashboard's "Send selected to API" button
    # (submission.views.SubmitCandidatesView); both paths share the
    # same underlying _submit_one().
    "auto-submit-queued-candidates": {
        "task": "submission.tasks.auto_submit_queued_candidates",
        "schedule": AUTO_SUBMIT_INTERVAL_SECONDS,
        "options": {"expires": 120},
    },
    "poll-submitted-candidates": {
        "task": "polling.tasks.poll_submitted_candidates",
        "schedule": POLLING_TICK_INTERVAL_SECONDS,
        "options": {"expires": 120},
    },
    "check-shine-session-health": {
        "task": "crawler.tasks.check_shine_session_health",
        "schedule": SHINE_SESSION_CHECK_INTERVAL_SECONDS,
        "options": {"expires": 120},
    },
}

# --- Extension backend (Ollama structuring + storage) --------------------
# Same endpoint the WhiteForce Chrome extension itself posts to
# (content.js: SSE_API_ENDPOINT = "https://astro-buddy.in/AI/candidate-extension").
EXTENSION_API_BASE_URL = os.environ.get("EXTENSION_API_BASE_URL", "")
EXTENSION_API_KEY = os.environ.get("EXTENSION_API_KEY", "")
EXTENSION_SUBMIT_TIMEOUT_SECONDS = int(
    os.environ.get("EXTENSION_SUBMIT_TIMEOUT_SECONDS", "15")
)
# The extension identifies the submitting recruiter via a plain
# "createdBy" id in the payload body (chrome.storage.local's logged-in
# user id) rather than an auth header. The crawler has no logged-in
# recruiter behind it, so this must resolve to whichever backend user
# id candidates from the night crawl should be attributed to.
#
# Two ways to provide it — see common/whiteforce_auth.py:
#   1. Set EXTENSION_CREATED_BY_ID directly if the id is already known.
#   2. Otherwise, set EXTENSION_LOGIN_EMAIL/EXTENSION_LOGIN_PASSWORD
#      (the same recruiter credentials used to log into the extension
#      itself) and the id is resolved automatically via the same
#      endpoint content.js's own login panel calls
#      (renderLoginPanel(): POST {email, password} to
#      ".../plus/api/login-from-extension" -> {status, data: {id}}).
EXTENSION_CREATED_BY_ID = os.environ.get("EXTENSION_CREATED_BY_ID", "")
EXTENSION_LOGIN_URL = os.environ.get(
    "EXTENSION_LOGIN_URL", "https://white-force.com/plus/api/login-from-extension"
)
EXTENSION_LOGIN_EMAIL = os.environ.get("EXTENSION_LOGIN_EMAIL", "")
EXTENSION_LOGIN_PASSWORD = os.environ.get("EXTENSION_LOGIN_PASSWORD", "")

# --- ATS backend (submission.client.submit_candidate / check_phone_exists) --
# Separate backend from EXTENSION_API_BASE_URL above — this is the new
# ats.astro-buddy.in service candidates are actually POSTed to now,
# authenticated with a plain x-api-key header rather than a resolved
# createdBy login. EXTENSION_API_BASE_URL/EXTENSION_STATUS_API_URL are
# left as-is since polling.client still checks status against that
# older backend; only the submit step was moved to this one.
ATS_SUBMIT_API_URL = os.environ.get("ATS_SUBMIT_API_URL", "https://ats.astro-buddy.in/submit")
ATS_CHECK_API_URL = os.environ.get("ATS_CHECK_API_URL", "https://ats.astro-buddy.in/check")
ATS_API_KEY = os.environ.get("ATS_API_KEY", "")
# Duplicate-check responses took ~6-7s in practice per the API's own
# documentation, and submit itself does a similar check before
# queueing — kept generous (60s) rather than tight so a slow backend
# moment fails a submission over a false timeout.
ATS_SUBMIT_TIMEOUT_SECONDS = int(os.environ.get("ATS_SUBMIT_TIMEOUT_SECONDS", "60"))
ATS_CHECK_TIMEOUT_SECONDS = int(os.environ.get("ATS_CHECK_TIMEOUT_SECONDS", "30"))
# Parallel /check calls per results page — see crawler.tasks._phones_already_extracted.
ATS_CHECK_CONCURRENCY = int(os.environ.get("ATS_CHECK_CONCURRENCY", "8"))
# The ATS's own save endpoint always records 1 regardless of what's
# sent here ("createdBy kuch bhi bhejo, save API ko hamesha 1 jata
# hai") — sent as 1 literally so the submitted payload matches what
# actually gets stored.
ATS_CREATED_BY = int(os.environ.get("ATS_CREATED_BY", "1"))
# Portal label sent to the ATS with every submission (the "portal" field
# of /submit). Override from .env with ATS_PORTAL_NAME if it needs to change.
ATS_PORTAL_NAME = os.environ.get("ATS_PORTAL_NAME", "Shine_night_crawler.com")

# NOTE: content.js defines a STATUS_API_ENDPOINT constant
# (".../candidate-extension/list") but its own pollJobStatus()
# function — otherwise commented out/unused there — actually calls a
# different, hardcoded ".../candidate-extension/status" URL. The two
# never agree in the reference source. This uses the literal URL
# pollJobStatus() calls, since that's the one with a documented
# request/response shape ({createdBy, email, phone} ->
# {success, data: {status, statusMessage}}); override via env if this
# turns out to be the wrong one.
EXTENSION_STATUS_API_URL = os.environ.get(
    "EXTENSION_STATUS_API_URL", "https://astro-buddy.in/AI/candidate-extension/status"
)