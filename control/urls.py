from django.urls import path

from . import views

urlpatterns = [
    path("sessions/start/", views.StartSessionView.as_view(), name="control-start"),
    path("sessions/stop/", views.StopSessionView.as_view(), name="control-stop"),
]
