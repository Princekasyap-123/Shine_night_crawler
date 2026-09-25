from django.db import models


class SearchTerm(models.Model):
    """
    Dynamic replacement for the old settings.SHINE_SEARCH_TERMS env
    list — lets the dashboard add/remove keywords the crawler rotates
    through without touching .env or redeploying.
    control.tasks.next_search_term() reads only is_active=True rows,
    ordered by term for a stable, predictable rotation order.
    """

    term = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["term"]

    def __str__(self):
        return self.term
