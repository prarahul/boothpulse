"""Business rules: who can see what, how device changes are merged, dashboard maths."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import Role, User

from .models import Booth, Voter, VoterChange, VoterStatus

# ---------------------------------------------------------------------------
# Scoping. Booth isolation is enforced here, on the server, for every query.
# ---------------------------------------------------------------------------


def booths_for(user: User):
    qs = Booth.objects.select_related("cluster", "constituency")
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    if user.is_superuser:
        return qs
    if user.role == Role.ADMIN:
        return qs.filter(constituency=user.constituency) if user.constituency_id else qs
    if user.role == Role.CANDIDATE:
        return qs.filter(constituency=user.constituency) if user.constituency_id else qs.none()
    if user.role == Role.COORDINATOR:
        return qs.filter(cluster=user.cluster) if user.cluster_id else qs.none()
    if user.role == Role.AGENT:
        return qs.filter(pk=user.booth_id) if user.booth_id else qs.none()
    return qs.none()


def voters_for(user: User):
    return Voter.objects.filter(booth__in=booths_for(user)).select_related("booth")


# ---------------------------------------------------------------------------
# Applying a change sent by a device (online edit or offline sync).
# ---------------------------------------------------------------------------

STATUS_FIELDS = {"status"}
DETAIL_FIELDS = {
    "needs_assistance", "assistance_note", "contact_consent", "phone", "notes", "inclination",
}
VALID_STATUSES = {s.value for s in VoterStatus}


class ChangeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_client_ts(value):
    now = timezone.now()
    ts = parse_datetime(value) if isinstance(value, str) else value
    if ts is None:
        return now
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.get_current_timezone())
    # A phone with a wrong clock must not "win" every future conflict.
    return min(ts, now)


def _clean(field, value):
    if field == "status":
        if value not in VALID_STATUSES:
            raise ChangeError("invalid", f"Unknown status '{value}'")
        return value
    if field in {"needs_assistance", "contact_consent"}:
        return bool(value)
    if field == "inclination":
        if value not in {"unknown", "supporter", "neutral", "opposed"}:
            raise ChangeError("invalid", f"Unknown inclination '{value}'")
        return value
    if field == "phone":
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return digits[-10:] if digits else ""
    limit = {"assistance_note": 200, "notes": 500}[field]
    return str(value or "")[:limit]


@transaction.atomic
def apply_change(user: User, change: dict, device_id: str = ""):
    """Merge one change into the voter record.

    Conflict rule: last write wins per group of fields, judged by the time
    the change was made on the phone (not when it reached the server), so a
    phone that was offline for an hour cannot overwrite a newer update.

    Returns (result, voter, applied_fields) where result is one of
    "applied", "stale" (nothing newer to apply) or "duplicate".
    """
    if not user.can_edit_voters:
        raise ChangeError("forbidden", "This role is read-only")

    event_id = change.get("event_id")
    if event_id:
        try:
            event_id = uuid.UUID(str(event_id))
        except ValueError:
            raise ChangeError("invalid", "event_id must be a UUID")
    if event_id and VoterChange.objects.filter(event_id=event_id).exists():
        voter = voters_for(user).filter(pk=change.get("voter_id")).first()
        return "duplicate", voter, []

    try:
        voter = voters_for(user).select_for_update().get(pk=change.get("voter_id"))
    except (Voter.DoesNotExist, ValueError, TypeError):
        raise ChangeError("not_found", "Voter not found in your booths")

    client_ts = _parse_client_ts(change.get("client_ts"))
    incoming = {k: v for k, v in change.items() if k in STATUS_FIELDS | DETAIL_FIELDS}
    if not settings.FEATURE_INCLINATION:
        incoming.pop("inclination", None)
    cleaned = {k: _clean(k, v) for k, v in incoming.items()}

    applied = []
    if "status" in cleaned and (voter.status_ts is None or client_ts >= voter.status_ts):
        voter.status = cleaned["status"]
        voter.status_ts = client_ts
        if voter.status == VoterStatus.NEEDS_ASSISTANCE:
            voter.needs_assistance = True
        applied.append("status")

    detail_keys = [k for k in cleaned if k in DETAIL_FIELDS]
    if detail_keys and (voter.details_ts is None or client_ts >= voter.details_ts):
        for key in detail_keys:
            setattr(voter, key, cleaned[key])
        voter.details_ts = client_ts
        applied.extend(detail_keys)

    # No consent, no phone number stored.
    if not voter.contact_consent and voter.phone:
        voter.phone = ""
        if "phone" not in applied:
            applied.append("phone")

    if applied:
        voter.updated_by = user
        voter.save()

    record = {"voter": voter, "user": user, "device_id": device_id[:64],
              "client_ts": client_ts, "payload": incoming, "applied": applied}
    if event_id:
        record["event_id"] = event_id
    VoterChange.objects.create(**record)
    return ("applied" if applied else "stale"), voter, applied


def touch_sync(user: User, device_id: str = ""):
    User.objects.filter(pk=user.pk).update(last_sync_at=timezone.now(), last_device_id=device_id[:64])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

S = VoterStatus


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def dashboard_for(user: User) -> dict:
    booths = booths_for(user)
    rows = list(
        booths.annotate(
            total=Count("voters"),
            voted=Count("voters", filter=Q(voters__status=S.VOTE_DONE)),
            pending=Count("voters", filter=Q(voters__status=S.PENDING)),
            contacted=Count("voters", filter=Q(voters__status=S.CONTACTED)),
            not_contacted=Count("voters", filter=Q(voters__status=S.NOT_CONTACTED)),
            not_available=Count("voters", filter=Q(voters__status=S.NOT_AVAILABLE)),
            assistance_open=Count(
                "voters", filter=Q(voters__needs_assistance=True) & ~Q(voters__status=S.VOTE_DONE)
            ),
        ).order_by("number")
    )
    # Separate query: joining agents into the count above would multiply
    # every voter by the number of agents on the booth.
    last_sync = dict(
        User.objects.filter(role=Role.AGENT, booth__in=booths)
        .values("booth").annotate(last=Max("last_sync_at")).values_list("booth", "last")
    )
    for b in rows:
        b.last_sync = last_sync.get(b.id)

    now = timezone.now()
    offline_after = timedelta(minutes=settings.AGENT_OFFLINE_ALERT_MINUTES)

    booth_rows, clusters = [], {}
    for b in rows:
        pct = _pct(b.voted, b.total)
        cname = b.cluster.name if b.cluster else "Unassigned"
        c = clusters.setdefault(cname, {"name": cname, "total": 0, "voted": 0, "booths": 0})
        c["total"] += b.total
        c["voted"] += b.voted
        c["booths"] += 1
        booth_rows.append({
            "id": b.id, "number": b.number, "name": b.name, "cluster": cname,
            "total": b.total, "voted": b.voted, "turnout_pct": pct,
            "pending": b.pending, "contacted": b.contacted, "not_contacted": b.not_contacted,
            "not_available": b.not_available, "assistance_open": b.assistance_open,
            "last_sync": b.last_sync.isoformat() if b.last_sync else None,
        })
    for c in clusters.values():
        c["turnout_pct"] = _pct(c["voted"], c["total"])

    alerts = []
    for r in booth_rows:
        cl = clusters[r["cluster"]]
        gap = cl["turnout_pct"] - r["turnout_pct"]
        if cl["booths"] > 1 and r["total"] and gap >= settings.LOW_TURNOUT_ALERT_POINTS:
            alerts.append({"level": "warning", "booth": r["number"], "booth_id": r["id"],
                           "message": f"Turnout {gap:.0f} points below cluster average"})
        if r["assistance_open"]:
            alerts.append({"level": "danger", "booth": r["number"], "booth_id": r["id"],
                           "message": f"{r['assistance_open']} open assistance request(s)"})
    # Agents who have gone quiet.
    for b in rows:
        if b.last_sync is None or now - b.last_sync > offline_after:
            mins = None if b.last_sync is None else int((now - b.last_sync).total_seconds() // 60)
            alerts.append({
                "level": "muted", "booth": b.number, "booth_id": b.id,
                "message": "Agent has never synced" if mins is None else f"Agent offline for {mins} min",
            })

    total = sum(r["total"] for r in booth_rows)
    voted = sum(r["voted"] for r in booth_rows)
    status_mix = {s.value: 0 for s in S}
    for row in Voter.objects.filter(booth__in=booths).values("status").annotate(n=Count("id")):
        status_mix[row["status"]] = row["n"]

    return {
        "generated_at": now.isoformat(),
        "scope": user.get_role_display() if hasattr(user, "get_role_display") else "",
        "totals": {
            "booths": len(booth_rows), "voters": total, "voted": voted,
            "turnout_pct": _pct(voted, total),
            "pending": status_mix[S.PENDING],
            "assistance_open": sum(r["assistance_open"] for r in booth_rows),
        },
        "status_mix": status_mix,
        "clusters": sorted(clusters.values(), key=lambda c: c["name"]),
        "booths": booth_rows,
        "alerts": sorted(alerts, key=lambda a: {"danger": 0, "warning": 1, "muted": 2}[a["level"]]),
    }
