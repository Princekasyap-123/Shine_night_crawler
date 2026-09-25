from django.db.models import Count
from rest_framework import serializers

from candidates.models import CandidateRecord
from sessions.models import CrawlSession


class CandidateRecordSerializer(serializers.ModelSerializer):
    """Every field extracted for one candidate, including raw_text."""

    class Meta:
        model = CandidateRecord
        fields = [
            "id",
            "session",
            "phone",
            "email",
            "status",
            "search_term",
            "page_number",
            "shine_profile_url",
            "results_page_url",
            "raw_text",
            "failure_reason",
            "extracted_at",
            "submitted_at",
            "resolved_at",
        ]


class CrawlSessionStatsSerializer(serializers.ModelSerializer):
    """
    Session config/state plus a live per-status candidate breakdown —
    the one payload the dashboard needs to show "what's happening
    right now" without the frontend stitching together multiple calls.
    """

    candidate_counts = serializers.SerializerMethodField()

    class Meta:
        model = CrawlSession
        fields = [
            "id",
            "status",
            "is_paused",
            "paused_until",
            "started_at",
            "ended_at",
            "triggered_by",
            "batch_size",
            "backlog_pause_threshold",
            "backlog_resume_threshold",
            "candidate_counts",
        ]

    def get_candidate_counts(self, session):
        return {
            row["status"]: row["count"]
            for row in session.candidates.values("status")
            .order_by()
            .annotate(count=Count("id"))
        }
