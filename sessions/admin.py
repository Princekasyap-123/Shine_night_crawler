from django.contrib import admin

from .models import CrawlSession


@admin.register(CrawlSession)
class CrawlSessionAdmin(admin.ModelAdmin):
    """
    Read-heavy admin view — this is your at-a-glance night-by-night
    history before the stats app exists, and stays useful afterward
    for checking a session's config/pause state directly.
    """

    list_display = (
        "id",
        "status",
        "is_paused",
        "triggered_by",
        "started_at",
        "ended_at",
        "batch_size",
        "backlog_pause_threshold",
        "backlog_resume_threshold",
    )

    list_filter = ("status", "is_paused", "triggered_by")

    ordering = ("-started_at",)

    # Sessions are created via CrawlSession.objects.create_session(...)
    # so the "only one running at a time" guard is always enforced —
    # never through the admin's own "Add" form, which would bypass it.
    def has_add_permission(self, request):
        return False

    readonly_fields = (
        "started_at",
        "ended_at",
    )