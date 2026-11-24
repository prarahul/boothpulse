"""Demo mode: the full app with generated sample data, no server needed.

Everything is stored on the phone. The DemoApi answers the calls the sync
engine and dashboard make, using the local store instead of the network,
so all screens behave exactly as they do against the real backend.
"""

import random
from datetime import datetime, timezone

from .api import Api
from .store import Store

FIRST_M = ["Ramesh", "Suresh", "Arjun", "Vikram", "Imran", "Rahul", "Amit", "Sanjay", "Mohit", "Deepak", "Faizan", "Ajay", "Ravi", "Naveen", "Pankaj"]
FIRST_F = ["Meena", "Salma", "Lakshmi", "Sunita", "Pooja", "Anita", "Neha", "Kavita", "Rekha", "Shabnam", "Priya", "Geeta", "Seema", "Nazia", "Rita"]
LAST = ["Kumar", "Singh", "Yadav", "Sharma", "Khan", "Verma", "Ali", "Gupta", "Saini", "Tyagi", "Chauhan", "Rana", "Ansari", "Jatav"]

DEMO_BOOTHS = [
    {"id": 1, "number": 112, "name": "Govt School Room 1", "cluster": "Ward 1"},
    {"id": 2, "number": 113, "name": "Govt School Room 2", "cluster": "Ward 1"},
    {"id": 3, "number": 114, "name": "Panchayat Bhawan", "cluster": "Ward 2"},
    {"id": 4, "number": 115, "name": "Primary School", "cluster": "Ward 2"},
]
VOTERS_PER_BOOTH = 150

DEMO_USERS = {
    "agent": {
        "id": 0, "name": "Demo Agent", "phone": "demo", "role": "agent", "role_label": "Booth agent (demo)",
        "can_edit": True, "constituency": "Demo Constituency", "cluster": "Ward 1",
        "booth": {k: DEMO_BOOTHS[0][k] for k in ("id", "number", "name")}, "features": {"inclination": False},
    },
    "coordinator": {
        "id": 0, "name": "Demo Coordinator", "phone": "demo", "role": "coordinator",
        "role_label": "Coordinator (demo)", "can_edit": True, "constituency": "Demo Constituency",
        "cluster": "All wards", "booth": None, "features": {"inclination": False},
    },
}


def seed_demo(store: Store, seed=7):
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc).isoformat()
    voters, vid = [], 1
    for b in DEMO_BOOTHS:
        share_voted = rnd.uniform(0.35, 0.6)
        house = 0
        for s in range(1, VOTERS_PER_BOOTH + 1):
            if s == 1 or rnd.random() < 0.3:
                house += 1
                family = rnd.choice(LAST)
            gender = rnd.choice("MF")
            age = rnd.randint(18, 90)
            r = rnd.random()
            status = ("vote_done" if r < share_voted else "pending" if r < share_voted + 0.3
                      else "contacted" if r < share_voted + 0.38 else "not_contacted")
            voters.append({
                "id": vid, "booth": b["id"], "booth_number": b["number"], "serial_no": s,
                "epic": f"DEM{b['number']:03d}{s:04d}",
                "name": f"{rnd.choice(FIRST_M if gender == 'M' else FIRST_F)} {family}",
                "relative_name": f"{rnd.choice(FIRST_M)} {family}", "house_no": str(house),
                "age": age, "gender": gender, "status": status,
                "needs_assistance": age > 78 and status != "vote_done" and rnd.random() < 0.5,
                "assistance_note": "", "contact_consent": False, "phone": "", "notes": "",
                "inclination": "unknown", "updated_at": now,
            })
            vid += 1
    store.upsert_voters(voters, overwrite_pending=True)
    store.set("demo_seeded", True)


class DemoApi(Api):
    """Answers API calls from the phone's own store."""

    def __init__(self, store: Store, user: dict):
        super().__init__("demo://local", "demo", store.device_id())
        self.store = store
        self.user = user

    def _request(self, *args, **kwargs):
        return {}

    def me(self):
        return self.user

    def logout(self):
        return {}

    def booths(self):
        return DEMO_BOOTHS

    def voters(self, **kwargs):
        return {"results": [], "next": None}

    def sync(self, changes):
        # Changes are already applied to the local store; just acknowledge them.
        return {"results": [
            {"event_id": c["event_id"], "result": "applied", "voter": self.store.voter(c["voter_id"])}
            for c in changes
        ]}

    def dashboard(self):
        now = datetime.now(timezone.utc).isoformat()
        rows, clusters, alerts = [], {}, []
        mix = {k: 0 for k in ("not_contacted", "contacted", "pending", "vote_done", "needs_assistance", "not_available")}
        for b in DEMO_BOOTHS:
            c = self.store.counts(b["id"])
            total, voted = c.get("total", 0), c.get("vote_done", 0)
            for k in mix:
                mix[k] += c.get(k, 0)
            pct = round(100 * voted / total, 1) if total else 0.0
            rows.append({"id": b["id"], "number": b["number"], "name": b["name"], "cluster": b["cluster"],
                         "total": total, "voted": voted, "turnout_pct": pct, "pending": c.get("pending", 0),
                         "contacted": c.get("contacted", 0), "not_contacted": c.get("not_contacted", 0),
                         "not_available": c.get("not_available", 0), "assistance_open": c["assistance_open"],
                         "last_sync": now})
            cl = clusters.setdefault(b["cluster"], {"name": b["cluster"], "total": 0, "voted": 0, "booths": 0})
            cl["total"] += total
            cl["voted"] += voted
            cl["booths"] += 1
        for cl in clusters.values():
            cl["turnout_pct"] = round(100 * cl["voted"] / cl["total"], 1) if cl["total"] else 0.0
        for r in rows:
            gap = clusters[r["cluster"]]["turnout_pct"] - r["turnout_pct"]
            if gap >= 5:
                alerts.append({"level": "warning", "booth": r["number"], "booth_id": r["id"],
                               "message": f"Turnout {gap:.0f} points below cluster average"})
            if r["assistance_open"]:
                alerts.append({"level": "danger", "booth": r["number"], "booth_id": r["id"],
                               "message": f"{r['assistance_open']} open assistance request(s)"})
        total = sum(r["total"] for r in rows)
        voted = sum(r["voted"] for r in rows)
        return {
            "generated_at": now,
            "totals": {"booths": len(rows), "voters": total, "voted": voted,
                       "turnout_pct": round(100 * voted / total, 1) if total else 0.0,
                       "pending": mix["pending"], "assistance_open": sum(r["assistance_open"] for r in rows)},
            "status_mix": mix, "clusters": list(clusters.values()), "booths": rows,
            "alerts": sorted(alerts, key=lambda a: a["level"] != "danger"),
        }
