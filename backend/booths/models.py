import uuid

from django.conf import settings
from django.db import models


class Constituency(models.Model):
    name = models.CharField(max_length=120)
    number = models.PositiveIntegerField(help_text="Assembly constituency number, e.g. 44")
    state = models.CharField(max_length=60, default="Uttar Pradesh")
    poll_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "constituencies"
        unique_together = [("state", "number")]

    def __str__(self):
        return f"{self.number}-{self.name}"


class Cluster(models.Model):
    """A group of booths (ward or sector) looked after by one coordinator."""

    constituency = models.ForeignKey(Constituency, on_delete=models.CASCADE, related_name="clusters")
    name = models.CharField(max_length=120)

    class Meta:
        unique_together = [("constituency", "name")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class Booth(models.Model):
    """A polling station (part number on the electoral roll)."""

    constituency = models.ForeignKey(Constituency, on_delete=models.CASCADE, related_name="booths")
    cluster = models.ForeignKey(
        Cluster, null=True, blank=True, on_delete=models.SET_NULL, related_name="booths"
    )
    number = models.PositiveIntegerField(help_text="Part / polling station number")
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        unique_together = [("constituency", "number")]
        ordering = ["number"]

    def __str__(self):
        return f"Booth {self.number} - {self.name}"


class VoterStatus(models.TextChoices):
    NOT_CONTACTED = "not_contacted", "Not contacted"
    CONTACTED = "contacted", "Contacted"
    PENDING = "pending", "Pending"
    VOTE_DONE = "vote_done", "Vote done"
    NEEDS_ASSISTANCE = "needs_assistance", "Needs assistance"
    NOT_AVAILABLE = "not_available", "Not available"


class Inclination(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    SUPPORTER = "supporter", "Supporter"
    NEUTRAL = "neutral", "Neutral"
    OPPOSED = "opposed", "Opposed"


class Voter(models.Model):
    """One entry on the electoral roll plus what the campaign has recorded.

    The system records WHETHER a voter has voted, never whom they voted for.
    """

    booth = models.ForeignKey(Booth, on_delete=models.CASCADE, related_name="voters")
    serial_no = models.PositiveIntegerField(help_text="Serial number in the part's roll")
    epic = models.CharField("EPIC number", max_length=20, db_index=True)
    name = models.CharField(max_length=120, db_index=True)
    relative_name = models.CharField(max_length=120, blank=True, help_text="Father's / husband's name")
    house_no = models.CharField(max_length=40, blank=True, db_index=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    gender = models.CharField(max_length=1, blank=True, choices=[("M", "Male"), ("F", "Female"), ("T", "Third gender")])

    # Campaign data (personal data under the DPDP Act 2023: consent + deletion).
    status = models.CharField(max_length=20, choices=VoterStatus.choices, default=VoterStatus.NOT_CONTACTED, db_index=True)
    status_ts = models.DateTimeField(null=True, blank=True, help_text="Client time of the latest status change")
    needs_assistance = models.BooleanField(default=False)
    assistance_note = models.CharField(max_length=200, blank=True)
    contact_consent = models.BooleanField(default=False)
    phone = models.CharField(max_length=15, blank=True)
    notes = models.TextField(blank=True, max_length=500)
    inclination = models.CharField(max_length=10, choices=Inclination.choices, default=Inclination.UNKNOWN)
    details_ts = models.DateTimeField(null=True, blank=True, help_text="Client time of the latest notes/contact change")

    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        unique_together = [("booth", "serial_no")]
        ordering = ["booth__number", "serial_no"]
        indexes = [models.Index(fields=["booth", "status"]), models.Index(fields=["booth", "updated_at"])]

    def __str__(self):
        return f"{self.name} ({self.epic})"


class VoterChange(models.Model):
    """One change sent by a device. `event_id` makes sync retries idempotent."""

    event_id = models.UUIDField(unique=True, default=uuid.uuid4)
    voter = models.ForeignKey(Voter, on_delete=models.CASCADE, related_name="changes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    device_id = models.CharField(max_length=64, blank=True)
    client_ts = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField()
    applied = models.JSONField(default=list, help_text="Fields actually applied (others were stale)")

    class Meta:
        ordering = ["-received_at"]
