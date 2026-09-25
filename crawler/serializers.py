from rest_framework import serializers

from .models import SearchTerm


class SearchTermSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchTerm
        fields = ["id", "term", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_term(self, value):
        return value.strip()
