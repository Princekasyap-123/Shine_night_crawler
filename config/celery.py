import os

from celery import Celery

# Matches manage.py's own default so `celery worker`/`celery beat` run
# against the same settings as `manage.py shell`/`runserver` locally
# (SQLite, no Postgres needed) unless DJANGO_SETTINGS_MODULE is set
# explicitly — which the VPS's systemd unit does, to production.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

# CONFIRMED (live test): Playwright's sync API uses asyncio internally
# in a way that makes Django's asgiref safety check believe ORM calls
# are happening inside an async context, even in an ordinary
# single-threaded Celery task — crawler.tasks.crawl_search_term raised
# SynchronousOnlyOperation on every CandidateRecord write otherwise.
# This is Django's own documented escape hatch for exactly this kind
# of false positive; scoped to the Celery worker process only (not
# manage.py/wsgi), since only the crawler ever mixes Playwright's sync
# API with the ORM in the same call stack.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

app = Celery("shine_night_crawler")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
