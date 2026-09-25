import logging
import threading

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached_created_by_id = None


class ExtensionLoginError(Exception):
    """Raised when the extension backend rejects login, or credentials are missing entirely."""


def resolve_created_by_id() -> str:
    """
    Returns the backend user id crawled candidates should be
    attributed to as "createdBy" in submission/status requests.

    If EXTENSION_CREATED_BY_ID is set explicitly, that's used as-is —
    a manual override for when the id is already known. Otherwise,
    this logs into the same endpoint the extension's own login panel
    uses (content.js's renderLoginPanel(): POST {email, password} to
    ".../plus/api/login-from-extension" -> {status, data: {id}}) using
    EXTENSION_LOGIN_EMAIL/EXTENSION_LOGIN_PASSWORD, and caches the
    resulting id for the lifetime of this process — a recruiter
    account's id doesn't change, so there's no reason to log in again
    on every submission or poll.
    """
    global _cached_created_by_id

    if settings.EXTENSION_CREATED_BY_ID:
        # os.environ.get() always returns a str, but content.js's real
        # createdBy is a plain JSON number (chrome.storage.local's
        # cached login response id) — the login-fallback path below
        # naturally produces an int too, since it comes straight out of
        # response.json(). Cast here so both paths agree on type;
        # sending "1" (string) instead of 1 (number) risks a strict
        # backend comparison silently failing to match a real account.
        try:
            return int(settings.EXTENSION_CREATED_BY_ID)
        except ValueError as exc:
            raise ExtensionLoginError(
                f"EXTENSION_CREATED_BY_ID={settings.EXTENSION_CREATED_BY_ID!r} "
                "is not a valid integer user id."
            ) from exc

    if _cached_created_by_id is not None:
        return _cached_created_by_id

    with _lock:
        if _cached_created_by_id is not None:
            return _cached_created_by_id

        if not settings.EXTENSION_LOGIN_EMAIL or not settings.EXTENSION_LOGIN_PASSWORD:
            raise ExtensionLoginError(
                "Neither EXTENSION_CREATED_BY_ID nor EXTENSION_LOGIN_EMAIL/"
                "EXTENSION_LOGIN_PASSWORD are set — cannot determine which "
                "backend user to attribute crawled candidates to."
            )

        try:
            response = requests.post(
                settings.EXTENSION_LOGIN_URL,
                json={
                    "email": settings.EXTENSION_LOGIN_EMAIL,
                    "password": settings.EXTENSION_LOGIN_PASSWORD,
                },
                headers={"Content-Type": "application/json"},
                timeout=settings.EXTENSION_SUBMIT_TIMEOUT_SECONDS,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ExtensionLoginError(f"Extension backend login failed: {exc}") from exc

        if not data.get("status"):
            raise ExtensionLoginError(
                data.get("message") or "Extension backend rejected login credentials."
            )

        created_by_id = (data.get("data") or {}).get("id")
        if not created_by_id:
            raise ExtensionLoginError(
                "Extension backend login succeeded but the response had no user id."
            )

        _cached_created_by_id = created_by_id
        logger.info("Resolved extension backend createdBy id via login.")
        return _cached_created_by_id
