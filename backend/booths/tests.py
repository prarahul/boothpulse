import io
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuditLog, Role, User
from booths.models import Booth, Constituency, Voter, VoterChange


class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", booths=4, voters=20, stdout=io.StringIO())
        cls.c = Constituency.objects.get(number=999)
        cls.b1 = Booth.objects.get(constituency=cls.c, number=1)
        cls.b2 = Booth.objects.get(constituency=cls.c, number=2)

    def setUp(self):
        cache.clear()

    def login(self, phone):
        client = APIClient()
        r = client.post("/api/auth/otp/request/", {"phone": phone}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = client.post("/api/auth/otp/verify/", {"phone": phone, "code": r.data["dev_code"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        client.credentials(HTTP_AUTHORIZATION="Token " + r.data["token"], HTTP_X_DEVICE_ID="test-device")
        return client


@override_settings(OTP_ECHO_IN_RESPONSE=True)
class AuthTests(BaseCase):
    def test_otp_login_and_me(self):
        client = self.login("8000010001")
        me = client.get("/api/me/").data
        self.assertEqual(me["role"], Role.AGENT)
        self.assertEqual(me["booth"]["number"], 1)
        self.assertTrue(AuditLog.objects.filter(action="login").exists())

    def test_wrong_code_rejected_and_unknown_number_gives_same_answer(self):
        client = APIClient()
        client.post("/api/auth/otp/request/", {"phone": "8000010001"}, format="json")
        r = client.post("/api/auth/otp/verify/", {"phone": "8000010001", "code": "000000"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = client.post("/api/auth/otp/request/", {"phone": "9876543210"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("dev_code", r.data)

    def test_code_cannot_be_reused(self):
        client = APIClient()
        code = client.post("/api/auth/otp/request/", {"phone": "+91 80000 10001"}, format="json").data["dev_code"]
        self.assertEqual(client.post("/api/auth/otp/verify/", {"phone": "8000010001", "code": code}, format="json").status_code, 200)
        self.assertEqual(client.post("/api/auth/otp/verify/", {"phone": "8000010001", "code": code}, format="json").status_code, 400)

    def test_otp_requests_limited_per_phone(self):
        client = APIClient()
        codes = [client.post("/api/auth/otp/request/", {"phone": "8000010001"}, format="json").status_code
                 for _ in range(6)]
        self.assertEqual(codes[-1], 429)
        # A different agent on the same IP is not affected.
        r = client.post("/api/auth/otp/request/", {"phone": "8000010002"}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_invalid_phone(self):
        r = APIClient().post("/api/auth/otp/request/", {"phone": "12345"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_blocked(self):
        self.assertEqual(APIClient().get("/api/voters/").status_code, 401)


@override_settings(OTP_ECHO_IN_RESPONSE=True)
class IsolationTests(BaseCase):
    def test_agent_sees_only_own_booth(self):
        client = self.login("8000010001")
        r = client.get("/api/voters/?limit=1000")
        self.assertEqual(r.data["count"], 20)
        self.assertTrue(all(v["booth"] == self.b1.id for v in r.data["results"]))
        other = Voter.objects.filter(booth=self.b2).first()
        self.assertEqual(client.get(f"/api/voters/{other.id}/").status_code, 404)
        r = client.patch(f"/api/voters/{other.id}/", {"status": "vote_done"}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(client.get("/api/booths/").data[0]["number"], 1)

    def test_coordinator_sees_cluster(self):
        client = self.login("9000000003")  # Ward 1 = booths 1 and 4
        numbers = sorted(b["number"] for b in client.get("/api/booths/").data)
        self.assertEqual(numbers, [1, 4])

    def test_candidate_read_only_and_no_private_fields(self):
        client = self.login("9000000002")
        self.assertEqual(client.get("/api/voters/?limit=1").data["count"], 80)
        v = client.get("/api/voters/?limit=1").data["results"][0]
        self.assertNotIn("phone", v)
        self.assertNotIn("notes", v)
        r = client.patch(f"/api/voters/{v['id']}/", {"status": "vote_done"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_agent_cannot_import_or_export(self):
        client = self.login("8000010001")
        self.assertEqual(client.get("/api/reports/booths.csv").status_code, 403)
        f = io.BytesIO(b"part_no,serial_no,epic,name\n1,1,X,Y\n")
        f.name = "roll.csv"
        self.assertEqual(client.post("/api/import/roll/", {"file": f}, format="multipart").status_code, 403)


@override_settings(OTP_ECHO_IN_RESPONSE=True)
class VoterAndSyncTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.client_ = self.login("8000010001")
        self.voter = Voter.objects.filter(booth=self.b1).first()

    def test_search(self):
        c = self.client_
        self.assertEqual(c.get(f"/api/voters/?q={self.voter.epic}").data["count"], 1)
        self.assertGreaterEqual(c.get(f"/api/voters/?q={self.voter.serial_no}").data["count"], 1)
        self.assertGreaterEqual(c.get(f"/api/voters/?q={self.voter.name.split()[0]}").data["count"], 1)

    def test_patch_status(self):
        r = self.client_.patch(f"/api/voters/{self.voter.id}/", {"status": "vote_done"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["voter"]["status"], "vote_done")
        r = self.client_.patch(f"/api/voters/{self.voter.id}/", {"status": "bogus"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_sync_is_idempotent(self):
        event = str(uuid.uuid4())
        payload = {"changes": [{"event_id": event, "voter_id": self.voter.id, "status": "pending",
                                "client_ts": timezone.now().isoformat()}]}
        r1 = self.client_.post("/api/sync/", payload, format="json")
        r2 = self.client_.post("/api/sync/", payload, format="json")
        self.assertEqual(r1.data["results"][0]["result"], "applied")
        self.assertEqual(r2.data["results"][0]["result"], "duplicate")
        self.assertEqual(VoterChange.objects.filter(event_id=event).count(), 1)
        self.assertIsNotNone(User.objects.get(phone="8000010001").last_sync_at)

    def test_older_offline_change_does_not_overwrite_newer(self):
        now = timezone.now()
        newer = {"event_id": str(uuid.uuid4()), "voter_id": self.voter.id, "status": "vote_done",
                 "client_ts": now.isoformat()}
        older = {"event_id": str(uuid.uuid4()), "voter_id": self.voter.id, "status": "pending",
                 "client_ts": (now - timedelta(hours=1)).isoformat()}
        self.client_.post("/api/sync/", {"changes": [newer]}, format="json")
        r = self.client_.post("/api/sync/", {"changes": [older]}, format="json")
        self.assertEqual(r.data["results"][0]["result"], "stale")
        self.voter.refresh_from_db()
        self.assertEqual(self.voter.status, "vote_done")

    def test_future_client_clock_is_clamped(self):
        future = {"event_id": str(uuid.uuid4()), "voter_id": self.voter.id, "status": "pending",
                  "client_ts": (timezone.now() + timedelta(days=1)).isoformat()}
        self.client_.post("/api/sync/", {"changes": [future]}, format="json")
        self.voter.refresh_from_db()
        self.assertLessEqual(self.voter.status_ts, timezone.now())

    def test_phone_needs_consent(self):
        self.client_.patch(f"/api/voters/{self.voter.id}/", {"phone": "9876543210"}, format="json")
        self.voter.refresh_from_db()
        self.assertEqual(self.voter.phone, "")
        self.client_.patch(f"/api/voters/{self.voter.id}/", {"phone": "9876543210", "contact_consent": True}, format="json")
        self.voter.refresh_from_db()
        self.assertEqual(self.voter.phone, "9876543210")

    def test_inclination_ignored_when_feature_off(self):
        self.client_.patch(f"/api/voters/{self.voter.id}/", {"inclination": "supporter"}, format="json")
        self.voter.refresh_from_db()
        self.assertEqual(self.voter.inclination, "unknown")

    def test_bad_sync_items_reported_individually(self):
        r = self.client_.post("/api/sync/", {"changes": [
            {"event_id": "not-a-uuid", "voter_id": self.voter.id, "status": "pending"},
            {"event_id": str(uuid.uuid4()), "voter_id": 99999999, "status": "pending"},
            {"event_id": str(uuid.uuid4()), "voter_id": self.voter.id, "status": "contacted"},
        ]}, format="json")
        self.assertEqual([x["result"] for x in r.data["results"]], ["error", "error", "applied"])

    def test_since_filter(self):
        mark = timezone.now()
        self.client_.patch(f"/api/voters/{self.voter.id}/", {"status": "contacted"}, format="json")
        r = self.client_.get(f"/api/voters/?since={mark.isoformat().replace('+', '%2B')}")
        self.assertEqual([v["id"] for v in r.data["results"]], [self.voter.id])


@override_settings(OTP_ECHO_IN_RESPONSE=True)
class DashboardAndImportTests(BaseCase):
    def test_dashboard_counts(self):
        agent = self.login("8000010001")
        v = Voter.objects.filter(booth=self.b1).first()
        agent.patch(f"/api/voters/{v.id}/", {"status": "vote_done"}, format="json")
        d = self.login("9000000002").get("/api/dashboard/").data
        self.assertEqual(d["totals"]["voters"], 80)
        self.assertEqual(d["totals"]["voted"], Voter.objects.filter(status="vote_done").count())
        self.assertEqual(len(d["booths"]), 4)

    def test_admin_import_and_reimport_keeps_campaign_data(self):
        admin = self.login("9000000001")
        csv_bytes = ("part_no,booth_name,serial_no,epic,name,relative_name,house_no,age,gender,cluster\n"
                     "50,Primary School,1,ABC1234567,सीता देवी,राम लाल,12,45,महिला,Ward 9\n"
                     "50,Primary School,2,ABC1234568,Ram Lal,Shyam Lal,12,50,M,Ward 9\n"
                     "50,Primary School,,BAD,Bad Row,,,,,\n").encode("utf-8")
        f = io.BytesIO(csv_bytes); f.name = "roll.csv"
        r = admin.post("/api/import/roll/", {"file": f}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data["created"], r.data["skipped"]), (2, 1))
        v = Voter.objects.get(epic="ABC1234567")
        self.assertEqual((v.gender, v.booth.cluster.name), ("F", "Ward 9"))
        v.status = "vote_done"; v.save()
        f = io.BytesIO(csv_bytes); f.name = "roll.csv"
        r = admin.post("/api/import/roll/", {"file": f}, format="multipart")
        self.assertEqual(r.data["updated"], 2)
        v.refresh_from_db()
        self.assertEqual(v.status, "vote_done")

    def test_report_csv(self):
        r = self.login("9000000003").get("/api/reports/booths.csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content.decode().strip().splitlines()), 3)  # header + 2 booths in Ward 1

    def test_purge(self):
        Voter.objects.update(phone="9876543210", contact_consent=True, notes="x")
        call_command("purge_personal_data", yes=True, stdout=io.StringIO())
        self.assertFalse(Voter.objects.exclude(phone="").exists())
        self.assertFalse(Voter.objects.exclude(notes="").exists())
