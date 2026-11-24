import csv
import uuid

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, audit

from .models import Constituency
from .roll_import import RollImportError, import_roll
from .serializers import BoothSerializer, VoterChangeSerializer, VoterSerializer
from .services import ChangeError, apply_change, booths_for, dashboard_for, touch_sync, voters_for

MAX_SYNC_BATCH = 500


def device_id(request):
    return request.headers.get("X-Device-Id", "")[:64]


class BoothViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BoothSerializer
    pagination_class = None

    def get_queryset(self):
        return booths_for(self.request.user)


class VoterViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List / search voters in the caller's booths, and edit one voter.

    Query params: booth, q (name, EPIC, house no. or serial no.), status,
    needs_assistance=1, since=<ISO time> (only voters changed after it).
    """

    serializer_class = VoterSerializer

    def get_queryset(self):
        qs = voters_for(self.request.user)
        p = self.request.query_params
        if p.get("booth"):
            qs = qs.filter(booth_id=p["booth"])
        if p.get("status"):
            qs = qs.filter(status__in=p["status"].split(","))
        if p.get("needs_assistance") in {"1", "true"}:
            qs = qs.filter(needs_assistance=True)
        if p.get("since"):
            since = parse_datetime(p["since"])
            if since is None:
                raise ValidationError({"since": "Use an ISO 8601 date-time"})
            qs = qs.filter(updated_at__gt=since)
        q = p.get("q", "").strip()
        if q:
            cond = Q(name__icontains=q) | Q(epic__iexact=q) | Q(house_no__iexact=q) | Q(relative_name__icontains=q)
            if q.isdigit():
                cond |= Q(serial_no=int(q))
            qs = qs.filter(cond)
        return qs.order_by("booth__number", "serial_no")

    def partial_update(self, request, pk=None):
        ser = VoterChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        change = {**ser.validated_data, "voter_id": pk}
        change.setdefault("event_id", uuid.uuid4())
        try:
            result, voter, applied = apply_change(request.user, change, device_id(request))
        except ChangeError as e:
            code = {"forbidden": 403, "not_found": 404}.get(e.code, 400)
            return Response({"detail": e.message, "code": e.code}, status=code)
        return Response({"result": result, "applied": applied,
                         "voter": VoterSerializer(voter, context={"request": request}).data})


class SyncView(APIView):
    """Offline-first sync. The app sends its queued changes; each one carries a
    unique event_id so a retry after a dropped connection is never applied twice.

    POST {"changes": [{"event_id", "voter_id", "client_ts", "status", ...}]}
    """

    def post(self, request):
        changes = request.data.get("changes", [])
        if not isinstance(changes, list):
            raise ValidationError({"changes": "Must be a list"})
        if len(changes) > MAX_SYNC_BATCH:
            raise ValidationError({"changes": f"Send at most {MAX_SYNC_BATCH} changes per request"})

        results = []
        for raw in changes:
            event_id = raw.get("event_id") if isinstance(raw, dict) else None
            ser = VoterChangeSerializer(data=raw)
            if not ser.is_valid() or not ser.validated_data.get("event_id") or not ser.validated_data.get("voter_id"):
                results.append({"event_id": event_id, "result": "error", "code": "invalid",
                                "detail": ser.errors or "event_id and voter_id are required"})
                continue
            try:
                result, voter, applied = apply_change(request.user, ser.validated_data, device_id(request))
                results.append({
                    "event_id": event_id, "result": result, "applied": applied,
                    "voter": VoterSerializer(voter, context={"request": request}).data if voter else None,
                })
            except ChangeError as e:
                results.append({"event_id": event_id, "result": "error", "code": e.code, "detail": e.message})

        touch_sync(request.user, device_id(request))
        return Response({"server_time": timezone.now().isoformat(), "results": results})


class DashboardView(APIView):
    def get(self, request):
        return Response(dashboard_for(request.user))


class ImportRollView(APIView):
    """Admin only: upload a booth-wise roll CSV (multipart field `file`)."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if request.user.role != Role.ADMIN and not request.user.is_superuser:
            raise PermissionDenied("Only the control room can import rolls")
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Attach the roll CSV as 'file'"})
        constituency_id = request.data.get("constituency") or request.user.constituency_id
        constituency = get_object_or_404(Constituency, pk=constituency_id)
        try:
            summary = import_roll(upload, constituency)
        except (RollImportError, UnicodeDecodeError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        audit(request, "roll_import", constituency, summary)
        return Response(summary, status=status.HTTP_201_CREATED)


class BoothReportView(APIView):
    """CSV export of booth-wise turnout. Coordinators get their cluster."""

    def get(self, request):
        if request.user.role not in {Role.ADMIN, Role.COORDINATOR, Role.CANDIDATE} and not request.user.is_superuser:
            raise PermissionDenied("Reports are not available to booth agents")
        data = dashboard_for(request.user)
        audit(request, "export_booth_report", f"{len(data['booths'])} booths")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="booth-report-{timezone.now():%Y%m%d-%H%M}.csv"'
        writer = csv.writer(response)
        cols = ["number", "name", "cluster", "total", "voted", "turnout_pct", "pending",
                "contacted", "not_contacted", "not_available", "assistance_open", "last_sync"]
        writer.writerow(cols)
        for row in data["booths"]:
            writer.writerow([row[c] for c in cols])
        return response


def dashboard_page(request):
    """Web dashboard for the candidate and control room (logs in with OTP)."""
    return render(request, "dashboard.html")
