from django.contrib import admin

from .models import CandidateRecord


@admin.register(CandidateRecord)
class CandidateRecordAdmin(admin.ModelAdmin):
    """
    Primarily a debugging view — checking what got stuck, what failed
    and why, and spot-checking extraction quality by session, before
    the stats app's aggregated endpoint exists (and useful alongside
    it afterward, for row-level detail stats won't show).
    """

    list_display = (
        "id",
        "session",
        "status",
        "email",
        "phone",
        "search_term",
        "page_number",
        "extracted_at",
        "submitted_at",
        "resolved_at",
    )

    list_filter = ("status", "session", "search_term")

    search_fields = ("email", "phone", "shine_profile_url")

    ordering = ("-extracted_at",)

    # raw_text can be large and isn't something you'd ever hand-edit —
    # keep it visible for debugging but not part of the editable form
    # flow that gets rendered on every row.
    readonly_fields = (
        "extracted_at",
        "submitted_at",
        "resolved_at",
        "raw_text",
    )

    # Candidate rows are only ever created by the crawler task, never
    # by hand — an admin-created row would have no session-scoped
    # dedup guarantee behind it in the same way CrawlSession's did,
    # and more importantly would just be fake data with no real
    # extraction behind it.
    def has_add_permission(self, request):
        return False