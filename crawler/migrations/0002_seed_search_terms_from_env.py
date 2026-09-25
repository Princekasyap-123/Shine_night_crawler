from django.conf import settings
from django.db import migrations


def seed_from_settings(apps, schema_editor):
    """
    One-time carry-over: the old SHINE_SEARCH_TERMS env list becomes
    the starting set of dashboard-managed rows, so switching to the DB
    doesn't silently drop whatever terms were already configured.
    Guarded on an empty table so re-running migrations never
    duplicates or re-adds terms an operator has since removed.
    """
    SearchTerm = apps.get_model("crawler", "SearchTerm")
    if SearchTerm.objects.exists():
        return

    SearchTerm.objects.bulk_create(
        [SearchTerm(term=term) for term in settings.SHINE_SEARCH_TERMS]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("crawler", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_from_settings, migrations.RunPython.noop),
    ]
