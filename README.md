# Shine Night Crawler

A backend service that logs into Shine (recruiter.shine.com) every night, searches for candidates by a rotating list of skill/role keywords, extracts each candidate card's raw HTML, and hands it off to the WhiteForce Chrome extension's backend for AI (Ollama) structuring and storage — all while staying gentle on CPU/memory, since it runs on a **shared VPS** alongside other unrelated projects.

It is the backend counterpart to the WhiteForce Chrome extension (`content.js`): where the extension lets a human recruiter manually click "Send Candidate" on a portal page, this project automates the same submission pipeline for Shine specifically, running unattended overnight (10pm–5am by default).

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | Django 5.0 | Models, admin, migrations, REST endpoint for stats |
| REST API | Django REST Framework | Thin JSON API for session/candidate stats |
| Browser automation | Playwright (sync API, Chromium headless) | Drives the actual Shine search/pagination |
| Task queue | Celery 5.4 | All background work — crawling, control loop, batching, submission, polling — runs as Celery tasks, never inline in a request |
| Scheduler | Celery Beat (`django-celery-beat` installed, static `CELERY_BEAT_SCHEDULE` used) | Fires the nightly start/stop and all periodic ticks |
| Broker / result backend / shared state | Redis (3 logical DBs: broker, results, crawler state) | Also backs the crawler's singleton-browser lock and the round-robin search-term cursor |
| HTTP client | `requests` | Submission and polling calls to the extension backend |
| Database | SQLite locally (`config/settings/local.py`), PostgreSQL in production (`config/settings/production.py`) | No Postgres needed for local dev/testing |
| Env config | `python-dotenv` | Loads `.env` in every entry point (`manage.py`, `wsgi.py`, `celery.py`) via `config/settings/base.py` |
| Dev environment | WSL2 (Ubuntu) + a WSL-local Python venv (`.venv-wsl/`) | Windows dev machine; Redis and Celery live inside WSL because WSL2 mirrored-networking port forwarding to Windows wasn't reliable in testing |

---

## The nightly pipeline, end to end

```
10:00 PM                                                              5:00 AM
   │                                                                     │
   ├─ start_nightly_session ── creates one CrawlSession (status=running) │
   │                                                                     │
   │   ┌─────────────────────── every CONTROL_TICK_INTERVAL_SECONDS ──┐  │
   │   │  tick_control_loop                                          │  │
   │   │    ├─ update_pause_state   (backlog too high? pause)        │  │
   │   │    └─ dispatch_next_crawl  (not paused, no crawl in-flight? │  │
   │   │         → pick next search term (round-robin) →              │
   │   │           crawl_search_term.delay(term))                     │  │
   │   └──────────────────────────────────────────────────────────────┘ │
   │                                                                     │
   │   crawl_search_term (one Celery task per search term):             │
   │     1. open ONE headless Chromium session (Redis-locked, singleton)│
   │     2. navigate to Shine's advanced search form                    │
   │     3. fill keyword, submit → Shine redirects to ?suid=<hash>      │
   │     4. for each results page:                                      │
   │          - extract every candidate card's raw HTML                 │
   │          - save one CandidateRecord per card (status=queued)       │
   │          - click next page (if any), with jittered delay           │
   │     5. close the browser fully (not kept warm across the night)    │
   │                                                                     │
   │   ┌─────────────── every BATCH_DISPATCH_INTERVAL_SECONDS ───────┐  │
   │   │  dispatch_pending_batches → submit_candidate_batch.delay()  │  │
   │   │    (pulls up to batch_size oldest QUEUED candidates)        │  │
   │   └────────────────────────────────────────────────────────────┘  │
   │       submit_candidate_batch → POST each candidate's raw HTML to   │
   │       the extension backend (astro-buddy.in/AI/candidate-extension)│
   │       → status=submitted (or failed)                               │
   │                                                                     │
   │   ┌─────────────── every POLLING_TICK_INTERVAL_SECONDS ─────────┐  │
   │   │  poll_submitted_candidates → checks the extension backend's │  │
   │   │  /status endpoint for each SUBMITTED candidate               │  │
   │   │    → status=success (Ollama structured it) or failed         │  │
   │   └────────────────────────────────────────────────────────────┘  │
   │                                                                     │
   └─ stop_nightly_session ── marks the CrawlSession completed ─────────┘
```

Two design choices worth calling out:

- **Extraction, submission, and polling are three independent Celery ticks**, not one big loop. A slow submission backend never blocks the crawler from moving to the next page, and a paused crawler (backlog too high) doesn't stop already-queued candidates from being submitted. This is the "queue decoupling" the original plan asked for.
- **This project does not run Ollama or any AI model itself.** It only extracts raw HTML and posts it to the extension's backend (`EXTENSION_API_BASE_URL`); that separate system does the structuring. `polling/` exists purely to find out the *result* of that structuring, not to do any of it.

---

## Module-by-module

### `config/` — Django & Celery wiring
- `settings/base.py` — the real settings file; everything else inherits from it. Holds every tunable (night window, delays, batch sizes, queue names) as an env-overridable value, plus the full `CELERY_BEAT_SCHEDULE`.
- `settings/local.py` — SQLite, `DEBUG=True`, for dev/testing.
- `settings/production.py` — PostgreSQL, HTTPS/HSTS settings, requires `DJANGO_ALLOWED_HOSTS` to be set. This is what the VPS uses.
- `celery.py` — creates the Celery app (`config.celery.app`), autodiscovers each app's `tasks.py`.
- `urls.py` / `wsgi.py` / `asgi.py` — standard Django plumbing; only real route is `/api/stats/`.

### `sessions/` — the nightly run itself
- `models.py` — `CrawlSession`: one row per night (or manual test run). Its own manager (`CrawlSession.objects.create_session()`) refuses to start a second session while one is already `running`. Holds per-night tunables (`batch_size`, `backlog_pause_threshold`, `backlog_resume_threshold`, `pause_duration_minutes`) so they're visible in history and adjustable without a redeploy.
- `tasks.py` — `start_nightly_session` / `stop_nightly_session` (fired by Celery Beat at `NIGHT_START_HOUR`/`NIGHT_END_HOUR`), and `tick_control_loop`, the heartbeat that drives `control/` every `CONTROL_TICK_INTERVAL_SECONDS`. Also acts as a backstop: if a session is somehow still `running` outside the night window, this tick stops it itself.

### `candidates/` — the data itself
- `models.py` — `CandidateRecord`: one row per extracted candidate card. Tracks its whole life: `queued → submitted → success/failed`. Deduplicated per session on `shine_profile_url`. Stores `raw_text` (the untouched card HTML), `search_term`/`page_number` for debugging, and `email`/`phone` (used for the extension backend's status-check matching).
- `admin.py` — read-heavy Django admin views for both models; rows are never created by hand (admin add is disabled), only ever by the crawler.

### `crawler/` — the part that actually talks to Shine
- `browser.py` — the resource-safety layer. `shine_browser_session()` is a context manager yielding one logged-in Playwright page; the whole Chromium process is launched fresh and torn down completely on exit (never kept warm across the night, to avoid a slow memory leak triggering an OOM kill that could take other processes on the shared VPS down too). Enforces **at most one Chromium instance at a time** via a Redis lock (`is_browser_session_active()` / `_browser_singleton_lock()`), blocks images/fonts/stylesheets/analytics at the network level, and exposes `throttle_between_pages()` (randomized delay) and `wait_for_acceptable_load()` (self-throttles on high system load average).
- `extractors/shine.py` — all Shine-specific DOM knowledge, kept separate from browser plumbing:
  - `submit_keyword_search()` — fills `#id_any_keyword`, commits it as a tag, clicks the real submit button `#id_advanced_search`, and waits for Shine's `?suid=<hash>` redirect (confirmed: Shine has no constructible search URL, every search — even brand new ones — gets a server-generated saved-search id).
  - `extract_page_info()` / `has_next_page()` / `go_to_next_page()` — pagination, parsed from Shine's own "Page: X of Y" text and the `cls_noclick` class on the disabled arrow.
  - `extract_candidate_cards_html()` — returns `(card_id, outer_html)` for every `[id^='cnd_div_']` card on the page, untouched (the extension backend does all parsing).
- `tasks.py` — `crawl_search_term(session_id, search_term)`: one Celery task = one full search term, start to finish (open browser → submit search → paginate → save `CandidateRecord`s → close browser). Retries per-page extraction up to `CRAWLER_MAX_RETRIES_PER_PAGE` times before skipping that page; treats an expired login (`SessionExpiredError`) as fatal-but-not-retried, since retrying against a login wall would burn CPU all night for nothing.
- `management/commands/shine_login.py` — one-time setup command (`python manage.py shine_login`). Opens a real, visible Chromium window, best-effort auto-fills login credentials (falling back to manual login if the form doesn't match), and saves the resulting session cookies to `SHINE_STORAGE_STATE_PATH` — this is the file `shine_browser_session()` reuses every night instead of logging in fresh each time.

### `control/` — the pacing brain
- `tasks.py`:
  - `next_search_term(session_id)` — round-robins `SHINE_SEARCH_TERMS` via an atomic Redis `INCR`.
  - `update_pause_state(session_id)` — hysteresis pause/resume: pauses once the "submitted but not yet resolved" backlog crosses `backlog_pause_threshold`, only resumes once it drops to the *lower* `backlog_resume_threshold` (prevents flapping if the backlog sits right at one line).
  - `dispatch_next_crawl(session_id)` — the actual per-tick decision: within the night window? session running and not paused? no crawl already in flight (`is_browser_session_active()`)? Only then does it advance the rotation and dispatch `crawl_search_term`.
- `redis_keys.py` — deliberately tiny; the only state that belongs in Redis here is the ephemeral rotation cursor. Backlog counts and pause state are read live from Postgres, not cached, since a stale backlog number is exactly the kind of bug that would let the crawler outrun submission.

### `batching/` — releases queued work at a controlled rate
- `tasks.py` — `dispatch_pending_batches()`: every `BATCH_DISPATCH_INTERVAL_SECONDS`, for each running session, pulls up to `batch_size` oldest `queued` candidates and hands them to `submission.tasks.submit_candidate_batch`. Runs on its own schedule, independent of the crawl/control heartbeat — submission pace (HTTP calls) and crawl pace (Playwright/CPU) are different bottlenecks.

### `submission/` — hands candidates to the extension backend
- `client.py` — `submit_candidate()` POSTs one candidate's raw HTML to `EXTENSION_API_BASE_URL`, using the exact payload shape the extension's own `sendShineCard()` in `content.js` uses (`portal`, `url`, `rawText`, `phone`, `extractionMethod`, `createdBy`). Recognizes the same "already extracted" wording the extension checks for and treats it as a harmless duplicate, not a failure.
- `tasks.py` — `submit_candidate_batch(candidate_ids)`: submits each one, records the outcome via `CandidateRecord.mark_submitted()`/`mark_failed()`. Not auto-retried at the Celery level — a failed row just stays `failed`, visible in admin.

### `polling/` — closes the loop
- `client.py` — `poll_candidate_status()` checks the extension backend's status endpoint, mirroring `content.js`'s own (dead/commented-out) `pollJobStatus()` request/response shape.
- `tasks.py` — `poll_submitted_candidates()`: every `POLLING_TICK_INTERVAL_SECONDS`, checks up to `POLLING_BATCH_SIZE` `submitted` candidates and resolves them to `success`/`failed`. Deliberately not scoped to any one session or the night window — Ollama structuring on the extension backend can take a while, and a candidate may resolve well after its own session has ended.

### `common/` — small shared pieces
- `exceptions.py` — `SessionAlreadyRunningError` (enforces one `CrawlSession` at a time) and `SessionExpiredError` (lets the crawler abort cleanly on an expired Shine login instead of retry-looping).
- `whiteforce_auth.py` — `resolve_created_by_id()`: the extension identifies the submitting recruiter via a plain `createdBy` id in the request body, not an auth header. This resolves that id either from `EXTENSION_CREATED_BY_ID` directly, or — if that's blank — by logging into the same endpoint the extension's own login panel uses (`.../plus/api/login-from-extension`) with `EXTENSION_LOGIN_EMAIL`/`PASSWORD`, caching the result for the life of the process.

### `stats/` — read-only API
- `views.py` / `serializers.py` / `urls.py` — three endpoints (`/api/stats/latest/`, `/sessions/`, `/sessions/<id>/`) returning a `CrawlSession`'s config plus a live per-status candidate count breakdown. This is a JSON API only — there is no dashboard UI in this repo; Django admin (`/admin/`) is the fastest way to actually look at data today.

---

## CPU / resource-safety design (this runs on a shared VPS)

- **One Chromium instance at a time, guaranteed twice over**: Celery's own `-Q crawl -c 1` worker concurrency, plus a Redis lock that holds even across separate worker processes.
- **Never kept warm**: the whole browser process is launched fresh and killed completely at the end of every `crawl_search_term` task, not just the page — avoids a multi-hour memory leak risking an OOM-kill that could take other processes on the box down too.
- **Resource blocking**: images, fonts, stylesheets, and known analytics domains are aborted at the network level — only HTML structure is needed.
- **Self-throttling**: `wait_for_acceptable_load()` checks the 1-minute load average before every page fetch and sleeps if it's over `CRAWLER_MAX_LOAD_AVERAGE`, on top of whatever hard `CPUQuota`/`MemoryMax` cgroup limits get set at the systemd level on the VPS (not yet written — still TODO).
- **Randomized jitter** between page loads (`CRAWLER_DELAY_MIN/MAX_SECONDS`) — protects both the Shine account (anti-bot-detection) and the CPU budget.
- **Decoupled queues**: crawling, submission, and polling run on separate Celery queues (`crawl`, `submit`, `poll`, plus `control`/`batch`) so a slow HTTP backend never blocks the browser, and vice versa.

---

## Setup

### 1. Environment variables
Copy `.env.example` to `.env` and fill in the blanks. Key ones:
- `SHINE_LOGIN_EMAIL` / `SHINE_LOGIN_PASSWORD` — Shine recruiter credentials, used once by `shine_login`.
- `SHINE_SEARCH_TERMS` — comma-separated keywords to rotate through (e.g. `delivery boy,picker,packer`).
- `EXTENSION_LOGIN_EMAIL` / `EXTENSION_LOGIN_PASSWORD` — white-force.com credentials, used to auto-resolve `EXTENSION_CREATED_BY_ID`.
- `DB_PASSWORD` / `DJANGO_ALLOWED_HOSTS` — only needed once you're running `config.settings.production` on the VPS; local dev uses SQLite and doesn't need them.

### 2. Redis
Needs a real Redis instance. On this project's dev machine (Windows), WSL2's mirrored networking didn't reliably forward the port to Windows, so Redis and the whole Python stack run **inside WSL**:
```bash
wsl
redis-server --daemonize yes --port 6379
redis-cli ping   # should print PONG
```

### 3. Python environment (inside WSL)
```bash
cd /mnt/c/Users/Document/Downloads/shine-night-crawler-structure/shine-night-crawler
python3 -m venv .venv-wsl
source .venv-wsl/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 4. Database
```bash
python manage.py migrate   # SQLite locally, no Postgres needed
```

### 5. Shine login (one-time, re-run whenever the session expires)
```bash
python manage.py shine_login
```

### 6. Run it
```bash
# terminal 1 — worker: must list every queue, or only "default" gets consumed
celery -A config worker -l info --pool=solo -Q default,crawl,control,batch,submit,poll

# terminal 2 — beat (the scheduler)
celery -A config beat -l info
```

`--pool=solo` is required on WSL/Windows — Celery's default prefork pool doesn't work reliably there.

---

## Known gaps / things to verify before relying on this in production

- **No systemd unit or cgroup limits yet** — the `CPUQuota`/`MemoryMax` enforcement described above is a code-level self-throttle only; the OS-level hard ceiling from the original plan still needs to be written for the VPS.
- **No cap on how long a candidate can sit in `submitted`** if the extension backend never resolves it — `CandidateRecord` has no attempt counter. Stuck rows are visible and filterable in Django admin for now.
- **`EXTENSION_STATUS_API_URL`** — `content.js` itself defines two different, disagreeing endpoints for this (a `STATUS_API_ENDPOINT` constant that's never actually used, and a different hardcoded URL inside the dead `pollJobStatus()` function). This project uses the one with an actual documented shape; worth double-checking against the real backend once available.
- **No frontend dashboard** — `stats/` is JSON-API only.
#   S h i n e _ n i g h t _ c r a w l e r  
 