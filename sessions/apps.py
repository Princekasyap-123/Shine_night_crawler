from django.apps import AppConfig


class SessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sessions"
    # Django's own django.contrib.sessions already owns the "sessions"
    # app label, so this app (crawl sessions, unrelated to HTTP
    # sessions) needs a distinct one to avoid a startup collision.
    label = "crawl_sessions"
