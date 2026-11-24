from django.core.management.base import BaseCommand, CommandError

from booths.models import Constituency
from booths.roll_import import RollImportError, import_roll


class Command(BaseCommand):
    help = "Import a booth-wise electoral roll CSV into a constituency."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--constituency", type=int, required=True, help="Constituency id")

    def handle(self, csv_path, constituency, **opts):
        try:
            c = Constituency.objects.get(pk=constituency)
        except Constituency.DoesNotExist:
            raise CommandError(f"No constituency with id {constituency}")
        try:
            with open(csv_path, "rb") as fh:
                summary = import_roll(fh, c)
        except RollImportError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(str(summary)))
