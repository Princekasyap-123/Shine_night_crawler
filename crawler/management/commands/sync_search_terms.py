from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from crawler.positions import PositionsAPIError, sync_from_api


class Command(BaseCommand):
    help = (
        "Fetch the weekly positions from the White Force API and make each "
        "position_name an active search term (the same job beat runs before "
        "the night session)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="YYYY-MM-DD to query instead of the Monday of the current week.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and print the terms without changing the database.",
        )

    def handle(self, *args, **options):
        days = None
        if options["date"]:
            try:
                days = [datetime.strptime(options["date"], "%Y-%m-%d").date()]
            except ValueError as exc:
                raise CommandError("--date must look like 2026-10-05") from exc

        try:
            result = sync_from_api(days=days, dry_run=options["dry_run"])
        except PositionsAPIError as exc:
            raise CommandError(str(exc)) from exc

        for term in result.pop("term_list", []):
            self.stdout.write(f"  {term}")
        for key, value in result.items():
            self.stdout.write(f"{key}: {value}")