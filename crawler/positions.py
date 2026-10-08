"""
Keeps the dashboard's SearchTerm table in step with the White Force
"weekly positions" API, so the night crawl searches for the positions
that are actually open instead of a hand-maintained keyword list.

API: GET <POSITIONS_API_URL>?date=YYYY-MM-DD ->
    {"status": true, "date": ..., "count": N,
     "data": [{"position_name": ..., "software_category": ..., "PipelineCount": ...}, ...]}
Every position_name becomes one search term.

Runs from control.tasks.sync_search_terms_from_api (scheduled by beat
shortly before the night session starts) and from
`manage.py sync_search_terms` for a manual run.
"""

import logging
import re
from datetime import date, datetime, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from crawler.models import SearchTerm

logger = logging.getLogger(__name__)

_MAX_TERM_LENGTH = 255  # SearchTerm.term max_length


class PositionsAPIError(Exception):
    """The positions API could not be reached or returned something unusable."""


def candidate_days(today=None) -> list:
    """
    Dates to try, in order. The endpoint is weekly and the sample date
    (2026-10-05) is a Monday, so the Monday of the current week comes first,
    then the Monday before it in case this week's list hasn't been generated
    yet. POSITIONS_API_DATE (YYYY-MM-DD) overrides both.
    """
    fixed = (settings.POSITIONS_API_DATE or "").strip()
    if fixed:
        return [datetime.strptime(fixed, "%Y-%m-%d").date()]
    today = today or timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    return [monday, monday - timedelta(days=7)]


def fetch_position_names(day: date) -> list:
    try:
        response = requests.get(
            settings.POSITIONS_API_URL,
            params={"date": day.isoformat()},
            headers={"Accept": "application/json"},
            timeout=settings.POSITIONS_API_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PositionsAPIError(f"Request to positions API failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise PositionsAPIError(
            f"Positions API refused the request (HTTP {response.status_code}) — "
            "it may need authentication."
        )
    if response.status_code != 200:
        raise PositionsAPIError(f"Positions API returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PositionsAPIError("Positions API returned a non-JSON response") from exc

    if not isinstance(payload, dict) or not payload.get("status"):
        raise PositionsAPIError("Positions API reported status=false")

    rows = payload.get("data") or []
    return [row.get("position_name") for row in rows if isinstance(row, dict)]


def clean_terms(names, split_alternatives: bool = True) -> list:
    """
    Normalises position names into unique search terms (case-insensitive,
    first spelling wins, order kept). With split_alternatives, a name like
    "Billing Executive / Billing Engineer" becomes two terms, because Shine
    would otherwise look for that exact slash-separated phrase.
    """
    unique = {}
    for name in names:
        if not isinstance(name, str):
            continue
        parts = re.split(r"\s*/\s*", name) if split_alternatives else [name]
        for part in parts:
            term = " ".join(part.split())
            if not term or len(term) > _MAX_TERM_LENGTH:
                continue
            unique.setdefault(term.casefold(), term)
    return list(unique.values())


@transaction.atomic
def apply_terms(terms, deactivate_others: bool) -> dict:
    """
    Makes every term in `terms` an active SearchTerm. Rows are never deleted;
    with deactivate_others, active rows that are not in `terms` are switched
    off (is_active=False), which the dashboard can undo. Matching ignores case
    so "Packer" does not create a duplicate of an existing "packer".
    """
    existing = {row.term.casefold(): row for row in SearchTerm.objects.all()}
    wanted = set()
    created = activated = deactivated = 0

    for term in terms:
        key = term.casefold()
        wanted.add(key)
        row = existing.get(key)
        if row is None:
            SearchTerm.objects.create(term=term, is_active=True)
            created += 1
        elif not row.is_active:
            row.is_active = True
            row.save(update_fields=["is_active"])
            activated += 1

    if deactivate_others:
        for key, row in existing.items():
            if key not in wanted and row.is_active:
                row.is_active = False
                row.save(update_fields=["is_active"])
                deactivated += 1

    return {"created": created, "activated": activated, "deactivated": deactivated}


def sync_from_api(days=None, dry_run: bool = False) -> dict:
    """
    Fetches positions and updates SearchTerm. If the API fails or returns no
    usable positions for every date tried, nothing in the database is touched
    — an outage must never wipe the term list.
    """
    last_error = None
    for day in days or candidate_days():
        try:
            names = fetch_position_names(day)
        except PositionsAPIError as exc:
            last_error = exc
            logger.warning("Positions API failed for %s: %s", day, exc)
            continue

        terms = clean_terms(names, settings.POSITIONS_SPLIT_ALTERNATIVES)
        if not terms:
            logger.warning("Positions API returned no usable positions for %s", day)
            continue

        result = {
            "date": day.isoformat(),
            "positions_fetched": len(names),
            "terms": len(terms),
            "dry_run": dry_run,
        }
        if dry_run:
            result["term_list"] = terms
        else:
            result.update(apply_terms(terms, settings.POSITIONS_DEACTIVATE_OTHERS))
            logger.info("Search terms synced from positions API: %s", result)
        return result

    if last_error is not None:
        raise last_error
    raise PositionsAPIError("Positions API returned no usable positions; terms left unchanged")