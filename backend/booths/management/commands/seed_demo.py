"""Create a demo constituency with fake voters and one user per role.

    python manage.py seed_demo            # 6 booths x 120 voters
    python manage.py seed_demo --booths 20 --voters 750

All names are randomly generated. Never use real voter data for demos.
"""

import csv
import io
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Role, User
from booths.models import Booth, Cluster, Constituency, Voter, VoterStatus
from booths.roll_import import import_roll

FIRST_M = ["Ramesh", "Suresh", "Arjun", "Vikram", "Imran", "Rahul", "Amit", "Sanjay", "Mohit", "Deepak", "Faizan", "Ajay", "Ravi", "Naveen", "Pankaj"]
FIRST_F = ["Meena", "Salma", "Lakshmi", "Sunita", "Pooja", "Anita", "Neha", "Kavita", "Rekha", "Shabnam", "Priya", "Geeta", "Seema", "Nazia", "Rita"]
LAST = ["Kumar", "Singh", "Yadav", "Sharma", "Khan", "Verma", "Ali", "Gupta", "Saini", "Tyagi", "Chauhan", "Rana", "Ansari", "Jatav"]
CLUSTERS = ["Ward 1", "Ward 2", "Ward 3"]

DEMO_USERS = [
    ("9000000001", "Control", "Room", Role.ADMIN),
    ("9000000002", "Candidate", "Demo", Role.CANDIDATE),
    ("9000000003", "Coordinator", "Ward 1", Role.COORDINATOR),
]


class Command(BaseCommand):
    help = "Seed a demo constituency, booths, fake voters and demo users."

    def add_arguments(self, parser):
        parser.add_argument("--booths", type=int, default=6)
        parser.add_argument("--voters", type=int, default=120, help="Voters per booth")
        parser.add_argument("--simulate", action="store_true",
                            help="Also mark a random share of voters as contacted / voted")

    def handle(self, booths, voters, simulate, **opts):
        rnd = random.Random(42)
        c, _ = Constituency.objects.get_or_create(
            state="Uttar Pradesh", number=999,
            defaults={"name": "Demo Constituency", "poll_date": timezone.localdate() + timedelta(days=30)},
        )

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["part_no", "booth_name", "booth_address", "cluster", "serial_no", "epic", "name",
                    "relative_name", "house_no", "age", "gender"])
        for b in range(1, booths + 1):
            house = 0
            for s in range(1, voters + 1):
                if s == 1 or rnd.random() < 0.3:
                    house += 1
                    family = rnd.choice(LAST)
                gender = rnd.choice("MF")
                first = rnd.choice(FIRST_M if gender == "M" else FIRST_F)
                w.writerow([b, f"Govt School Room {b}", f"Demo Village {b}", CLUSTERS[(b - 1) % len(CLUSTERS)],
                            s, f"DEM{b:03d}{s:04d}", f"{first} {family}",
                            f"{rnd.choice(FIRST_M)} {family}", str(house), rnd.randint(18, 90), gender])
        summary = import_roll(io.StringIO(buf.getvalue()), c)
        self.stdout.write(f"Roll: {summary}")

        for phone, first, last, role in DEMO_USERS:
            u, _ = User.objects.get_or_create(username=phone, defaults={"phone": phone})
            u.phone, u.first_name, u.last_name, u.role, u.constituency = phone, first, last, role, c
            if role == Role.COORDINATOR:
                u.cluster = Cluster.objects.get(constituency=c, name="Ward 1")
            if role == Role.ADMIN:
                u.is_staff = True
            u.set_unusable_password()
            u.save()

        for booth in Booth.objects.filter(constituency=c):
            for n in (1, 2):  # two agents per booth
                phone = f"8{booth.number:05d}{n:04d}"[:10]
                u, _ = User.objects.get_or_create(username=phone, defaults={"phone": phone})
                u.phone, u.role, u.constituency, u.booth = phone, Role.AGENT, c, booth
                u.first_name, u.last_name = "Agent", f"B{booth.number}-{n}"
                u.set_unusable_password()
                u.save()

        if simulate:
            now = timezone.now()
            for v in Voter.objects.filter(booth__constituency=c):
                r = rnd.random()
                status = (VoterStatus.VOTE_DONE if r < 0.45 else VoterStatus.PENDING if r < 0.8
                          else VoterStatus.CONTACTED if r < 0.9 else VoterStatus.NOT_CONTACTED)
                v.status, v.status_ts = status, now
                v.needs_assistance = (v.age or 0) > 80 and rnd.random() < 0.4
                v.save()

        self.stdout.write(self.style.SUCCESS(
            "Demo ready. Log in with OTP (shown in the server log):\n"
            "  9000000001 control room admin\n  9000000002 candidate\n  9000000003 coordinator (Ward 1)\n"
            "  8000010001 agent, booth 1   (pattern 8BBBBB000N: booth number, agent 1 or 2)"
        ))
