from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/stats/", include("stats.urls")),
    path("api/crawler/", include("crawler.urls")),
    path("api/control/", include("control.urls")),
    path("api/submission/", include("submission.urls")),
    path("dashboard/", include("dashboard.urls")),
]
