from django.contrib import admin

from .models import SearchTerm


@admin.register(SearchTerm)
class SearchTermAdmin(admin.ModelAdmin):
    list_display = ("term", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("term",)
