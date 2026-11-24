from django.conf import settings
from rest_framework import serializers

from accounts.models import Role

from .models import Booth, Voter

PRIVATE_FIELDS = {"phone", "notes", "contact_consent"}


class BoothSerializer(serializers.ModelSerializer):
    cluster = serializers.CharField(source="cluster.name", default=None, read_only=True)
    constituency = serializers.CharField(source="constituency.__str__", read_only=True)

    class Meta:
        model = Booth
        fields = ["id", "number", "name", "address", "cluster", "constituency", "latitude", "longitude"]


class VoterSerializer(serializers.ModelSerializer):
    booth_number = serializers.IntegerField(source="booth.number", read_only=True)

    class Meta:
        model = Voter
        fields = [
            "id", "booth", "booth_number", "serial_no", "epic", "name", "relative_name",
            "house_no", "age", "gender", "status", "status_ts", "needs_assistance",
            "assistance_note", "contact_consent", "phone", "notes", "inclination",
            "details_ts", "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not settings.FEATURE_INCLINATION:
            data.pop("inclination", None)
        request = self.context.get("request")
        # Data minimisation: the read-only candidate view does not need
        # voters' phone numbers or canvass notes.
        if request and getattr(request.user, "role", None) == Role.CANDIDATE:
            for f in PRIVATE_FIELDS:
                data.pop(f, None)
        return data


class VoterChangeSerializer(serializers.Serializer):
    """Fields a device may change. Everything else comes from the roll."""

    event_id = serializers.UUIDField(required=False)
    voter_id = serializers.IntegerField(required=False)
    client_ts = serializers.DateTimeField(required=False)
    status = serializers.CharField(required=False)
    needs_assistance = serializers.BooleanField(required=False)
    assistance_note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    contact_consent = serializers.BooleanField(required=False)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    inclination = serializers.CharField(required=False)
