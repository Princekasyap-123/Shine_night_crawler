from django.urls import path

from . import views

urlpatterns = [
    path("submit/", views.SubmitCandidatesView.as_view(), name="submission-submit"),
]
