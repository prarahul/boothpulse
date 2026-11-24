"""Integration test for the offline store + sync engine.

Needs a running backend seeded with demo data and OTP echo enabled:

    cd backend
    python manage.py seed_demo
    OTP_ECHO_IN_RESPONSE=1 python manage.py runserver

    cd mobile
    BOOTHPULSE_TEST_SERVER=http://127.0.0.1:8000 pytest tests/test_sync.py
"""

import os

import pytest

from boothpulse_app.api import Api, OfflineError
from boothpulse_app.store import Store
from boothpulse_app.sync import SyncEngine

SERVER = os.environ.get("BOOTHPULSE_TEST_SERVER")
pytestmark = pytest.mark.skipif(not SERVER, reason="set BOOTHPULSE_TEST_SERVER to run")


def login(store, phone):
    api = Api(SERVER, device_id=store.device_id())
    code = api.request_otp(phone)["dev_code"]
    r = api.verify_otp(phone, code)
    api.token = r["token"]
    return api, r["user"]


def test_offline_edit_then_sync(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    api, user = login(store, "8000020001")  # agent, booth 2
    booth = user["booth"]["id"]
    engine = SyncEngine(store, api)

    r = engine.sync_once([booth])
    assert r.ok and r.pulled > 0
    counts = store.counts(booth)
    assert counts["total"] == r.pulled

    voter = store.search(booth, limit=1)[0]
    target = "contacted" if voter["status"] != "contacted" else "pending"

    # Go offline: the change is saved locally and queued.
    api.base_url = "http://127.0.0.1:9"
    store.record_change(voter["id"], {"status": target, "notes": "met at home"})
    assert store.voter(voter["id"])["status"] == target
    r = engine.sync_once([booth])
    assert r.offline and store.outbox_count() == 1

    # Back online: queue drains and the server has the change.
    api.base_url = SERVER
    r = engine.sync_once([booth])
    assert r.ok and r.pushed == 1 and store.outbox_count() == 0
    server_copy = api.voters(booth=booth, q=voter["epic"])["results"][0]
    assert server_copy["status"] == target
    assert server_copy["notes"] == "met at home"


def test_second_phone_receives_change_via_pull(tmp_path):
    a, b = Store(str(tmp_path / "a.db")), Store(str(tmp_path / "b.db"))
    api_a, user = login(a, "8000030001")  # agent 1, booth 3
    api_b, _ = login(b, "8000030002")  # agent 2, booth 3
    booth = user["booth"]["id"]
    eng_a, eng_b = SyncEngine(a, api_a), SyncEngine(b, api_b)
    eng_a.sync_once([booth])
    eng_b.sync_once([booth])

    voter = a.search(booth, limit=1)[0]
    a.record_change(voter["id"], {"status": "vote_done"})
    assert eng_a.sync_once([booth]).pushed == 1

    r = eng_b.sync_once([booth])  # incremental pull
    assert r.ok and r.pulled >= 1
    assert b.voter(voter["id"])["status"] == "vote_done"


def test_out_of_scope_change_is_rejected(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    api, user = login(store, "8000010001")  # booth 1
    other = Api(SERVER, api.token).voters(booth=user["booth"]["id"], limit=1)["results"][0]
    store.record_change(999999, {"status": "vote_done"})
    store.record_change(other["id"], {"status": "pending"})
    r = SyncEngine(store, api).sync_once([user["booth"]["id"]])
    assert len(r.rejected) == 1 and r.pushed == 1
    assert store.outbox_count() == 0


def test_unreachable_server_raises_offline():
    with pytest.raises(OfflineError):
        Api("http://127.0.0.1:9").me()
