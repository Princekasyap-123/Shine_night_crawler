from dataclasses import dataclass

import requests
from django.conf import settings

from common.whiteforce_auth import ExtensionLoginError, resolve_created_by_id


class PollingRequestError(Exception):
    """Transport-level failure only — mirrors ExtensionSubmissionError."""


@dataclass(frozen=True)
class PollResult:
    resolved: bool
    status: str | None = None  # "success" | "error", only meaningful when resolved
    status_message: str = ""


def poll_candidate_status(candidate) -> PollResult:
    """
    Checks the extension backend's status endpoint for one candidate,
    matching the request/response shape of content.js's own (dead,
    commented-out) pollJobStatus(): POST {createdBy, email, phone} to
    EXTENSION_STATUS_API_URL, expecting back
    {success, data: {status, statusMessage}}.

    A response with success=false means "not resolved yet / not
    found" in that reference implementation, not a hard error — this
    returns PollResult(resolved=False) the same way, so the caller
    just tries again on the next tick rather than treating it as a
    failure.
    """
    try:
        created_by_id = resolve_created_by_id()
    except ExtensionLoginError as exc:
        raise PollingRequestError(str(exc)) from exc

    payload = {
        "createdBy": created_by_id,
        "email": candidate.email,
        "phone": candidate.phone,
    }

    try:
        response = requests.post(
            settings.EXTENSION_STATUS_API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=settings.EXTENSION_SUBMIT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PollingRequestError(
            f"Request to extension status backend failed: {exc}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise PollingRequestError(
            f"Extension status backend returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    if not data.get("success"):
        return PollResult(resolved=False)

    status_data = data.get("data") or {}
    status = status_data.get("status")
    status_message = status_data.get("statusMessage") or ""

    if status == "success":
        return PollResult(resolved=True, status="success")
    if status == "error":
        return PollResult(resolved=True, status="error", status_message=status_message)

    # Any other status value (e.g. a "pending"/"processing" state) —
    # not resolved yet.
    return PollResult(resolved=False)
