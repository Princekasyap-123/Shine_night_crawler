import os

from .base import *  # noqa: F401,F403

DEBUG = False

if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError(
        "DJANGO_ALLOWED_HOSTS must be set in production (comma-separated hosts)."
    )

SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "true").lower() == "true"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 7
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
