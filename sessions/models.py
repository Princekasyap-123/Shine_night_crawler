from django.db import IntegrityError, models, transaction
from django.utils import timezone

from common.exceptions import SessionAlreadyRunningError


class CrawlSessionManager(models.Manager):
    """
    Custom manager whose job is to guard session creation so two
    CrawlSessions can never be 'running' at the same time — whether
    the second one comes from a manual test trigger or the 10pm
    scheduled task.
    """

    def create_session(self, triggered_by, **overrides):
        """
        The only sanctioned way to start a new session. Rejects the
        attempt outright if a session is already running, rather than
        auto-completing the old one — silently closing an existing
        session could mask a forgotten manual test run and cut off a
        real night's crawl without anyone noticing.

        `overrides` allows batch_size / backlog thresholds / pause
        duration to be customized per session; anything not passed
        falls back to the model field defaults.

        The pre-check below is a fast path only, not the real
        guarantee — a plain "does one exist?" read followed by a
        separate create() is a classic check-then-act race: two
        near-simultaneous calls (a double-click on "Start Crawling",
        two browser tabs, or the 22:00 beat firing at the same moment
        as a manual start) can both pass the check before either has
        written a row. The actual enforcement is
        CrawlSession.Meta's UniqueConstraint on status='running', a
        real DB-level constraint that a second concurrent insert
        cannot violate even if it races past this check — caught below
        and turned into the same SessionAlreadyRunningError so callers
        don't need to know which layer caught it.
        """
        if self.filter(status=CrawlSession.Status.RUNNING).exists():
            raise SessionAlreadyRunningError(
                "A CrawlSession is already running. Stop it before "
                "starting a new one."
            )

        try:
            with transaction.atomic():
                return self.create(
                    triggered_by=triggered_by,
                    status=CrawlSession.Status.RUNNING,
                    **overrides,
                )
        except IntegrityError as exc:
            raise SessionAlreadyRunningError(
                "A CrawlSession is already running. Stop it before "
                "starting a new one."
            ) from exc


class CrawlSession(models.Model):
    """
    One row per nightly (or manual test) crawl run. Every other app's
    records — CandidateRecord, control/polling decisions — hang off
    the session that was active when they happened, which
    gives clean per-night history and lets config be tuned and
    compared night to night.
    """

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"

    class TriggeredBy(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
    )

    # Pause is a sub-state within a 'running' session, not a status of
    # its own — a session is 'running' for the whole night, and
    # is_paused toggles on/off as the control loop reacts to backlog.
    is_paused = models.BooleanField(default=False)
    paused_until = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    triggered_by = models.CharField(
        max_length=20,
        choices=TriggeredBy.choices,
    )

    # Per-session tunable config — stored here rather than in
    # settings.py so each night's numbers are visible in history and
    # can be adjusted without a redeploy.
    batch_size = models.PositiveIntegerField(default=40)
    backlog_pause_threshold = models.PositiveIntegerField(default=300)
    backlog_resume_threshold = models.PositiveIntegerField(default=230)
    pause_duration_minutes = models.PositiveIntegerField(default=10)

    objects = CrawlSessionManager()

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            # The real "only one running session" guarantee — see
            # CrawlSessionManager.create_session()'s docstring. A
            # partial unique index on status when it equals 'running':
            # works on SQLite (used locally) as well as Postgres
            # (production), unlike select_for_update() which is a
            # silent no-op on SQLite.
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="running"),
                name="unique_running_crawl_session",
            )
        ]

    def __str__(self):
        return f"CrawlSession #{self.pk} ({self.status}, {self.triggered_by})"

    def mark_completed(self):
        """
        Ends the session. Called at the 5am hard-stop or when a manual
        test run is deliberately stopped. Does not touch is_paused —
        a completed session simply stops being scheduled for further
        crawl/control ticks, regardless of what pause state it was in.
        """
        self.status = self.Status.COMPLETED
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at"])