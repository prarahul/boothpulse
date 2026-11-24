from django.core.management.base import BaseCommand

from accounts.models import AuditLog
from booths.models import Voter, VoterChange


class Command(BaseCommand):
    help = (
        "After the election: erase the personal data the campaign collected "
        "(phone numbers, notes, assistance notes, inclination) and the change "
        "history that contains it. The public roll data is kept unless --all."
    )

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
        parser.add_argument("--all", action="store_true", help="Also delete the voters themselves")

    def handle(self, yes, all, **opts):
        if not yes and input("This cannot be undone. Type PURGE to continue: ") != "PURGE":
            self.stdout.write("Cancelled.")
            return
        changes = VoterChange.objects.all().delete()[0]
        if all:
            voters = Voter.objects.all().delete()[0]
            self.stdout.write(f"Deleted {voters} voter records.")
        else:
            voters = Voter.objects.update(phone="", notes="", assistance_note="", contact_consent=False,
                                          inclination="unknown")
            self.stdout.write(f"Cleared personal fields on {voters} voters.")
        AuditLog.objects.create(action="purge_personal_data", data={"changes_deleted": changes, "all": all})
        self.stdout.write(self.style.SUCCESS(f"Deleted {changes} change records."))
