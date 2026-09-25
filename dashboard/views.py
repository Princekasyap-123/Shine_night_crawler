from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView


@method_decorator(never_cache, name="dispatch")
class DashboardView(TemplateView):
    """
    Serves the single-page operator dashboard. All real data comes
    from stats/'s existing REST endpoints via client-side fetch() —
    this view has nothing to do but render the static shell.

    never_cache so the browser never serves a stale copy of this page
    (and its embedded JS + CSRF token) after a code update — a cached
    pre-fix page silently re-triggers bugs like a missing CSRF header
    even though the server side is already fixed.
    """

    template_name = "dashboard/index.html"
