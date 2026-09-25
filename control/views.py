from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import SessionAlreadyRunningError
from sessions.models import CrawlSession


class StartSessionView(APIView):
    """
    Manual "Start Crawling" button. Creates a MANUAL CrawlSession —
    the same guard against a second concurrent session that the
    nightly scheduled start already relies on.

    Deliberately does NOT dispatch a crawl itself. It just creates the
    session and lets the already-running control heartbeat
    (sessions.tasks.tick_control_loop, on Celery beat's own schedule)
    pick it up and call dispatch_next_crawl on its next tick — exactly
    how sessions.tasks.start_nightly_session already works for the
    scheduled 10pm start. That keeps this view a plain DB write with
    no Celery/Redis dependency of its own, so it works correctly
    whether this Django process is running in the same environment as
    the broker or not (e.g. the web server on Windows, Celery/Redis
    only reachable from WSL) — worst case, the first crawl starts up
    to CONTROL_TICK_INTERVAL_SECONDS later instead of instantly.
    """

    def post(self, request):
        try:
            session = CrawlSession.objects.create_session(
                triggered_by=CrawlSession.TriggeredBy.MANUAL
            )
        except SessionAlreadyRunningError as exc:
            return Response({"detail": str(exc)}, status=409)

        return Response({"id": session.pk, "status": session.status}, status=201)


class StopSessionView(APIView):
    """Manual "Stop Crawling" button. Ends whichever session is running."""

    def post(self, request):
        session = CrawlSession.objects.filter(
            status=CrawlSession.Status.RUNNING
        ).first()
        if session is None:
            return Response({"detail": "No session is currently running."}, status=404)

        session.mark_completed()
        return Response({"id": session.pk, "status": session.status})
