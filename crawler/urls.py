from django.urls import path

from . import views

urlpatterns = [
    path("search-terms/", views.SearchTermListCreateView.as_view(), name="search-terms"),
    path(
        "search-terms/<int:pk>/",
        views.SearchTermDestroyView.as_view(),
        name="search-term-detail",
    ),
]
