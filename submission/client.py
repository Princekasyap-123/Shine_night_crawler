import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ExtensionSubmissionError(Exception):
    """
    Raised only for transport-level failures (network error, timeout,
    non-JSON response) — anything the ATS backend itself explicitly
    rejected comes back as a SubmissionResult instead, so callers can
    tell "couldn't reach the backend" apart from "backend said no".
    """


@dataclass(frozen=True)
class SubmissionResult:
    success: bool
    message: str
    already_exists: bool = False


def _ats_headers() -> dict:
    return {"x-api-key": settings.ATS_API_KEY, "Content-Type": "application/json"}


def check_phone_exists(phone: str) -> bool:
    """
    Pre-scrape dedup check against the ATS's own /check endpoint —
    POST {"phone": phone} -> {"exists": true/false}. Called from
    crawler.tasks._save_candidate_cards for every card that has a
    phone number, before it's queued for submission at all: a phone
    the ATS already has gets saved as an ALREADY_EXTRACTED row instead
    of QUEUED, so it's never sent again.

    This is a different backend and a different (working) endpoint
    from the old DUPLICATE_CHECK_API_URL that was removed earlier for
    being confirmed broken (it returned exists=True for an obviously
    fake number) — that one hit the original extension backend, this
    one hits the new ATS service.

    Fails open (returns False, i.e. "not a duplicate, go ahead and
    extract normally") on any network/parse error — a dedup check that
    can't be answered should never block real extraction.
    """
    if not phone:
        return False

    try:
        response = requests.post(
            settings.ATS_CHECK_API_URL,
            json={"phone": phone},
            headers=_ats_headers(),
            timeout=settings.ATS_CHECK_TIMEOUT_SECONDS,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "ATS duplicate-check failed for phone %s: %s — treating as not-duplicate",
            phone,
            exc,
        )
        return False

    return bool(data.get("exists"))


def _payload_for(candidate) -> dict:
    """
    {text, portal, createdBy} for one candidate. The ATS requires the
    candidate's phone number to be the first line of "text" (a resume
    with no phone on line 1 gets skipped server-side) — raw_text is the
    card's visible innerText and isn't guaranteed to start with the
    phone, so it's prepended explicitly rather than assumed.
    """
    text = candidate.raw_text
    if candidate.phone and not text.lstrip().startswith(candidate.phone):
        text = f"{candidate.phone}\n{text}"
    return {
        "text": text,
        "portal": settings.ATS_PORTAL_NAME,
        "createdBy": settings.ATS_CREATED_BY,
    }


def get_ats_queued_count() -> int | None:
    """
    GET /stats -> jobs_by_status.queued: how many jobs the ATS has
    accepted but not processed yet. Used to pace batch submission.
    Returns None when it can't be read — callers treat that as "unknown,
    don't block on it".
    """
    try:
        response = requests.get(
            settings.ATS_STATS_API_URL,
            headers=_ats_headers(),
            timeout=settings.ATS_STATS_TIMEOUT_SECONDS,
        )
        # /stats only lists statuses that currently have jobs, so an
        # empty ATS queue means the "queued" key is absent, not unknown.
        return int(response.json()["jobs_by_status"].get("queued", 0))
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("ATS /stats unreadable: %s — not pacing on it this tick", exc)
        return None


def submit_batch(candidates) -> list[SubmissionResult]:
    """
    POSTs up to 200 candidates in one call to ATS_BATCH_API_URL
    (/submit/batch) and returns one SubmissionResult per candidate, in
    the same order. Each item of the response carries its own "http"
    code and "status" (the ATS runs every item through the same intake
    as /submit): 202 queued is success, "already_parsed"/"duplicate" is
    already_exists, anything else is a per-item rejection (e.g. 422 no
    phone) and comes back as success=False.

    Raises ExtensionSubmissionError for transport-level problems (or a
    response that can't be matched to the items sent) — in which case
    the caller can't know which items were accepted.
    """
    if not candidates:
        return []
    if len(candidates) > 200:
        raise ValueError("ATS batch is capped at 200 items")

    try:
        response = requests.post(
            settings.ATS_BATCH_API_URL,
            json={"items": [_payload_for(c) for c in candidates]},
            headers=_ats_headers(),
            timeout=settings.ATS_SUBMIT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ExtensionSubmissionError(f"Batch request to ATS backend failed: {exc}") from exc

    if response.status_code == 401:
        raise ExtensionSubmissionError("ATS backend rejected the API key (401)")
    if response.status_code == 422:
        raise ExtensionSubmissionError(
            f"ATS rejected the whole batch (422, {len(candidates)} items): {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ExtensionSubmissionError(
            f"ATS backend returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    items = data
    if isinstance(data, dict):
        items = data.get("results", data.get("items"))
    if not isinstance(items, list) or len(items) != len(candidates):
        raise ExtensionSubmissionError(
            f"ATS batch response didn't match the {len(candidates)} items sent "
            f"(HTTP {response.status_code})"
        )

    results = []
    for candidate, item in zip(candidates, items):
        if not isinstance(item, dict):
            results.append(SubmissionResult(success=False, message="unreadable batch item"))
            continue
        http = item.get("http")
        status = item.get("status")
        if status == "queued" and http in (None, 202):
            results.append(SubmissionResult(success=True, message="queued"))
        elif status == "already_parsed" or item.get("duplicate"):
            results.append(
                SubmissionResult(success=False, message=status or "duplicate", already_exists=True)
            )
        else:
            message = str(item.get("message") or status or f"HTTP {http}")
            logger.warning("ATS backend rejected candidate %s: %s", candidate.pk, message)
            results.append(SubmissionResult(success=False, message=message))
    return results


def candidate_exists_in_db(phone: str, email: str | None = None) -> bool:
    """
    White Force check-candidate-exists: POST {"phone"[, "email"]} ->
    {"exists": bool, "matched_by": ...}. True means the candidate is
    already in the DB, i.e. the submission has landed — what polling
    treats as success. 404 (not found) and 422 (missing params) both
    come back as valid JSON and mean "not there"; only network errors,
    5xx and unparseable bodies raise, so the caller can retry later
    instead of misreading an outage as "not found".
    """
    payload = {"phone": phone}
    if email:
        payload["email"] = email

    try:
        response = requests.post(
            settings.DUPLICATE_CHECK_API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=settings.DUPLICATE_CHECK_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ExtensionSubmissionError(f"Exists check failed: {exc}") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise ExtensionSubmissionError(f"Exists check returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise ExtensionSubmissionError(
            f"Exists check returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    return isinstance(data, dict) and bool(data.get("exists"))


def submit_candidate(candidate) -> SubmissionResult:
    """
    POSTs one CandidateRecord to the ATS backend (ATS_SUBMIT_API_URL —
    ats.astro-buddy.in/submit), authenticated with a plain x-api-key
    header. Payload is just {text, portal, createdBy} — the ATS does
    its own parsing from plain text server-side, unlike the old
    extension backend which needed the structured elements/images/
    files arrays captured at extraction time.

    The ATS requires the candidate's phone number to be the first line
    of "text" (a resume with no phone on line 1 gets skipped
    server-side) — candidate.raw_text is the card's visible innerText
    and isn't guaranteed to start with the phone, so it's prepended
    explicitly here rather than assumed.
    """
    try:
        response = requests.post(
            settings.ATS_SUBMIT_API_URL,
            json=_payload_for(candidate),
            headers=_ats_headers(),
            timeout=settings.ATS_SUBMIT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ExtensionSubmissionError(
            f"Request to ATS backend failed: {exc}"
        ) from exc

    if response.status_code == 401:
        raise ExtensionSubmissionError("ATS backend rejected the API key (401)")

    try:
        data = response.json()
    except ValueError as exc:
        raise ExtensionSubmissionError(
            f"ATS backend returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    status = data.get("status")

    # 202 {"status": "queued", "id": N} — accepted, will be parsed in
    # the background.
    if response.status_code == 202 and status == "queued":
        return SubmissionResult(success=True, message="queued")

    # 200 {"status": "already_parsed"} or {"duplicate": true, ...} —
    # the ATS has already seen this exact text/phone before. Not a
    # failure, same as the old backend's "already extracted" wording.
    if status == "already_parsed" or data.get("duplicate"):
        return SubmissionResult(success=False, message=status or "duplicate", already_exists=True)

    message = str(data.get("message") or status or f"HTTP {response.status_code}")
    logger.warning("ATS backend rejected candidate %s: %s", candidate.pk, message)
    return SubmissionResult(success=False, message=message)