from django.db import models
from django.utils import timezone

from sessions.models import CrawlSession


class CandidateRecord(models.Model):
    """
    One row per extracted candidate card. This is the single source of
    truth for a candidate's journey through the pipeline:

        queued -> submitted -> success / failed

    Written by crawler (insert, status=queued), updated by submission
    (status=submitted), updated again by polling (status=success/failed
    + failure_reason). stats reads aggregates off this table, and
    control reads status=submitted counts to drive the backlog
    pause/resume decision.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SUBMITTED = "submitted", "Submitted"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        # Set at extraction time, before submission, when the ATS's own
        # /check endpoint (submission.client.check_phone_exists) says
        # this phone was already parsed by a previous submission — this
        # candidate is deliberately never queued for sending, so it
        # needs its own terminal status to stay visible in the
        # dashboard rather than silently never appearing there.
        ALREADY_EXTRACTED = "already_extracted", "Already Extracted"

    session = models.ForeignKey(
        CrawlSession,
        on_delete=models.CASCADE,
        related_name="candidates",
    )

    # Dedup key: checked before insert so re-crawling the same Shine
    # page (or a page that appears again after pagination quirks)
    # doesn't create duplicate rows. Unique per session, not globally —
    # the same candidate profile could legitimately reappear across
    # different nights' searches. This is a per-card synthetic
    # identifier (not necessarily the shared results-page URL), since
    # every card on one page would otherwise collide on the same value.
    shine_profile_url = models.URLField(max_length=1000)

    # The real Shine results page URL (with its live "?suid=<hash>")
    # this candidate was found on. Kept separate from
    # shine_profile_url because content.js's own sendShineCard() sends
    # window.location.href — the actual page URL, shared by every
    # candidate on that page — as the submission payload's "url"
    # field; matching that convention exactly needs a value that isn't
    # forced to be unique per candidate.
    results_page_url = models.URLField(max_length=1000, blank=True, null=True)

    # Matching keys for the polling module — senior's API returns
    # candidates keyed by email/phone (same fields popup.js uses to
    # match createdBy/email/phone), not by our internal Shine URL or
    # row ID, so these need to be stored as extracted rather than
    # derived later.
    email = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)

    raw_text = models.TextField()

    # Structured DOM data captured at extraction time (page.evaluate()
    # running content.js's OWN buildPageElementsArray/buildImagesArray/
    # buildFilesArray functions live against the real card element) —
    # confirmed via the backend's own logs that Ollama's structuring
    # returns null for every field when this is empty, despite a clean
    # rawText: the backend's extraction is built around this structured
    # "elements" array as its real signal, not rawText alone. Captured
    # here (not regenerated at submission time) because the live
    # Playwright page no longer exists by the time submission runs,
    # often minutes or hours later.
    elements_json = models.JSONField(default=list, blank=True)
    images_json = models.JSONField(default=list, blank=True)
    files_json = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
    )

    # Which Shine search this came from — useful for debugging (e.g.
    # "did term X ever get past page 3 last night?") without needing
    # to inspect raw_text.
    search_term = models.CharField(max_length=255)
    page_number = models.PositiveIntegerField()

    failure_reason = models.TextField(blank=True, null=True)

    extracted_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["extracted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "shine_profile_url"],
                name="unique_candidate_per_session",
            )
        ]
        indexes = [
            # Matches the two real query patterns: control's backlog
            # count and the dashboard's ?session=&status= candidate
            # filter are both filtered by (session, status).
            models.Index(fields=["session", "status"]),
        ]

    def __str__(self):
        return f"CandidateRecord #{self.pk} ({self.status})"

    def mark_submitted(self):
        self.status = self.Status.SUBMITTED
        self.submitted_at = timezone.now()
        self.save(update_fields=["status", "submitted_at"])

    def mark_success(self):
        self.status = self.Status.SUCCESS
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at"])

    def mark_failed(self, reason=""):
        self.status = self.Status.FAILED
        self.failure_reason = reason
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "failure_reason", "resolved_at"])