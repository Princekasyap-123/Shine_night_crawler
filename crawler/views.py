from rest_framework import generics

from .models import SearchTerm
from .serializers import SearchTermSerializer


class SearchTermListCreateView(generics.ListCreateAPIView):
    """
    Backs the dashboard's search-term panel: GET the full rotation
    list, POST a new term. Uniqueness is enforced by the model field
    (ModelSerializer auto-validates it), so adding a term that already
    exists returns a clean 400 instead of a duplicate row.
    """

    queryset = SearchTerm.objects.all()
    serializer_class = SearchTermSerializer


class SearchTermDestroyView(generics.DestroyAPIView):
    """DELETE removes a term from the rotation entirely."""

    queryset = SearchTerm.objects.all()
    serializer_class = SearchTermSerializer
