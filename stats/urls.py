from django.urls import path

from . import views

urlpatterns = [
    path("latest/", views.LatestSessionStatsView.as_view(), name="stats-latest"),
    path("sessions/", views.SessionStatsListView.as_view(), name="stats-session-list"),
    path(
        "sessions/<int:pk>/",
        views.SessionStatsDetailView.as_view(),
        name="stats-session-detail",
    ),
    path(
        "candidates/last-night/",
        views.LastNightCandidatesView.as_view(),
        name="stats-candidates-last-night",
    ),
    path(
        "candidates/submitted-phones/",
        views.SubmittedPhonesView.as_view(),
        name="stats-candidates-submitted-phones",
    ),
    path(
        "shine-session/",
        views.ShineSessionHealthView.as_view(),
        name="stats-shine-session",
    ),
]
