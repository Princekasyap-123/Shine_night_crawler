import redis
from django.conf import settings
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from candidates.models import CandidateRecord
from crawler.tasks import SHINE_SESSION_CHECKED_AT_KEY, SHINE_SESSION_VALID_KEY
from sessions.models import CrawlSession

from .serializers import CandidateRecordSerializer, CrawlSessionStatsSerializer


class LatestSessionStatsView(APIView):
    """
    What the dashboard polls: the most recent CrawlSession (running or
    just-completed) plus its live candidate status breakdown. No
    session yet ever run returns null rather than a 404 — that's a
    normal pre-first-night state, not an error.
    """

    def get(self, request):
        session = CrawlSession.objects.first()
        if session is None:
            return Response(None)
        return Response(CrawlSessionStatsSerializer(session).data)


class SessionStatsDetailView(RetrieveAPIView):
    """Stats for one specific past night, by session id."""

    queryset = CrawlSession.objects.all()
    serializer_class = CrawlSessionStatsSerializer


class SessionStatsListView(ListAPIView):
    """History list — newest first (CrawlSession.Meta.ordering)."""

    queryset = CrawlSession.objects.all()
    serializer_class = CrawlSessionStatsSerializer


class LastNightCandidatesView(ListAPIView):
    """
    Every candidate (phone, email, status, search term, timestamps,
    raw_text) for one CrawlSession, newest first — the most recent
    session by default, or a specific past one via ?session=. Same
    underlying rows the Django admin shows, exposed as an API so the
    data can be pulled/filtered without a browser/admin login (and so
    the dashboard app can drive it).

    Optional query params:
      ?session=<id>                            — a specific night instead of the latest
      ?status=queued|submitted|success|failed  — filter by pipeline status
      ?phone=<digits>                          — exact phone lookup
      ?search_term=<term>                      — exact search-term match
    """

    serializer_class = CandidateRecordSerializer

    def get_queryset(self):
        session_id = self.request.query_params.get("session")
        if session_id:
            session = CrawlSession.objects.filter(pk=session_id).first()
        else:
            session = CrawlSession.objects.first()

        if session is None:
            return CandidateRecord.objects.none()

        queryset = session.candidates.all()

        status = self.request.query_params.get("status")
        if status:
            queryset = queryset.filter(status=status)

        phone = self.request.query_params.get("phone")
        if phone:
            queryset = queryset.filter(phone=phone)

        search_term = self.request.query_params.get("search_term")
        if search_term:
            queryset = queryset.filter(search_term=search_term)

        return queryset


class SubmittedPhonesView(APIView):
    """
    Flat, deduplicated list of phone numbers that were actually POSTed
    to the extension backend (status != queued — queued means it was
    only extracted from Shine and never sent). Built for cross-checking
    against the company's own candidate DB: "did this number we sent
    actually land there or not."

    Across all sessions by default (this is a cumulative check, not a
    single-night one); pass ?session=<id> to scope it to one night, and
    ?status=submitted|success|failed to narrow to one outcome instead
    of "anything that was ever sent".
    """

    def get(self, request):
        queryset = CandidateRecord.objects.exclude(
            status=CandidateRecord.Status.QUEUED
        ).exclude(phone__isnull=True).exclude(phone="")

        session_id = request.query_params.get("session")
        if session_id:
            queryset = queryset.filter(session_id=session_id)

        status = request.query_params.get("status")
        if status:
            queryset = queryset.filter(status=status)

        phones = list(
            queryset.order_by().values_list("phone", flat=True).distinct()
        )
        return Response({"count": len(phones), "phones": phones})


class ShineSessionHealthView(APIView):
    """
    Reads the cached result crawler.tasks.check_shine_session_health()
    writes to Redis on its own periodic schedule (a plain HTTP check,
    no browser) — lets the dashboard show "Shine login: valid" or
    "expired, re-login needed" at a glance, without ever launching a
    browser itself just to answer that question.
    """

    def get(self, request):
        client = redis.Redis.from_url(settings.CELERY_STATE_REDIS_URL)
        raw_valid = client.get(SHINE_SESSION_VALID_KEY)
        checked_at = client.get(SHINE_SESSION_CHECKED_AT_KEY)

        if raw_valid is None:
            valid = None
        else:
            valid = raw_valid.decode() == "1"

        return Response(
            {
                "valid": valid,
                "checked_at": checked_at.decode() if checked_at else None,
            }
        )
