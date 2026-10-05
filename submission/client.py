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
    text = candidate.raw_text
    if candidate.phone and not text.lstrip().startswith(candidate.phone):
        text = f"{candidate.phone}\n{text}"

    payload = {
        "text": text,
        "portal": "recruiter.shine.com",
        "createdBy": settings.ATS_CREATED_BY,
    }

    try:
        response = requests.post(
            settings.ATS_SUBMIT_API_URL,
            json=payload,
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
