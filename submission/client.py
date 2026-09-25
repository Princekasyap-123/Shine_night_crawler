import logging
import re
from dataclasses import dataclass

import requests
from django.conf import settings

from common.whiteforce_auth import ExtensionLoginError, resolve_created_by_id

logger = logging.getLogger(__name__)

# Matches the same "already extracted / already in queue" wording the
# WhiteForce extension's own sendShineCard() checks for (content.js) —
# a candidate the extension backend has already seen isn't a
# submission failure, it's effectively a no-op success.
_ALREADY_EXISTS_PATTERN = re.compile(r"already extracted|already in queue", re.IGNORECASE)


class ExtensionSubmissionError(Exception):
    """
    Raised only for transport-level failures (network error, timeout,
    non-JSON response) — anything the extension backend itself
    explicitly rejected comes back as a SubmissionResult instead, so
    callers can tell "couldn't reach the backend" apart from "backend
    said no".
    """


@dataclass(frozen=True)
class SubmissionResult:
    success: bool
    message: str
    already_exists: bool = False


def submit_candidate(candidate) -> SubmissionResult:
    """
    POSTs one CandidateRecord's raw card text to the same endpoint the
    Chrome extension itself submits to (EXTENSION_API_BASE_URL —
    astro-buddy.in/AI/candidate-extension in content.js), using the
    same payload shape the extension's per-card Shine sender uses
    (rawText + portal + phone + createdBy), so a submission from this
    crawler looks like any other recruiter's extension submission to
    the backend.
    """
    try:
        created_by_id = resolve_created_by_id()
    except ExtensionLoginError as exc:
        raise ExtensionSubmissionError(str(exc)) from exc

    payload = {
        "portal": "recruiter.shine.com",
        # Matches content.js's own sendShineCard(): "url" is the real
        # results page the candidate was found on (window.location.href
        # there), not a per-candidate link — falls back to the
        # synthetic per-card URL only for older rows crawled before
        # results_page_url existed.
        "url": candidate.results_page_url or candidate.shine_profile_url,
        # CONFIRMED (live test against the real backend's own logs):
        # Ollama's structuring returned null for every field except
        # phone when these were empty arrays, despite a clean rawText —
        # the backend's structuring is built around this structured
        # per-element DOM data, captured at extraction time by running
        # content.js's own buildPageElementsArray()/buildImagesArray()/
        # buildFilesArray() live in-page. See
        # crawler/extractors/shine.py's _BUILD_STRUCTURED_DATA_JS.
        "elements": candidate.elements_json,
        "images": candidate.images_json,
        # CONFIRMED via a real captured network request from the
        # actual Chrome extension (DevTools, sendShineCard() live):
        # "files" and "attachments" are both always present as empty
        # arrays for Shine cards (no downloadable resume link in the
        # list view) — "attachments" was missing from this payload
        # entirely before.
        "files": candidate.files_json,
        "attachments": [],
        "phone": candidate.phone,
        # CONFIRMED via that same real captured request: rawText is
        # plain text (cardElement.innerText), not JSON — reverting the
        # earlier JSON-encoding, which was based on a request to
        # deviate from content.js without a confirmed reference at the
        # time. This capture is definitive: real payload had
        # "rawText": "Shakeel Shaikh\nImmediate Joiner\n...".
        "rawText": candidate.raw_text,
        # Literal value from that same real capture —
        # "attribute-scope+card-scope+recruiter-email" — matches
        # content.js's own sendShineCard() extractionMethod exactly.
        "extractionMethod": "attribute-scope+card-scope+recruiter-email",
        "createdBy": created_by_id,
    }

    headers = {"Content-Type": "application/json"}
    if settings.EXTENSION_API_KEY:
        headers["Authorization"] = f"Bearer {settings.EXTENSION_API_KEY}"

    try:
        response = requests.post(
            settings.EXTENSION_API_BASE_URL,
            json=payload,
            headers=headers,
            timeout=settings.EXTENSION_SUBMIT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ExtensionSubmissionError(
            f"Request to extension backend failed: {exc}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ExtensionSubmissionError(
            f"Extension backend returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    message = data.get("message") or ""

    if data.get("success"):
        return SubmissionResult(success=True, message=message)

    already_exists = bool(_ALREADY_EXISTS_PATTERN.search(message))
    if not already_exists:
        logger.warning(
            "Extension backend rejected candidate %s: %s",
            candidate.pk,
            message or f"HTTP {response.status_code}",
        )

    return SubmissionResult(
        success=False,
        message=message or f"HTTP {response.status_code}",
        already_exists=already_exists,
    )
