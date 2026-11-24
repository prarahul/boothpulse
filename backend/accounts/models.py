import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    ADMIN = "admin", "Control room admin"
    CANDIDATE = "candidate", "Candidate / core team"
    COORDINATOR = "coordinator", "Cluster coordinator"
    AGENT = "agent", "Booth agent"


class BoothPulseUserManager(UserManager):
    def create_user_for_phone(self, phone, role, **extra):
        user = self.model(username=phone, phone=phone, role=role, **extra)
        user.set_unusable_password()
        user.save()
        return user


class User(AbstractUser):
    """A campaign user. Signs in to the apps with phone number + OTP.

    `username` mirrors the phone number so Django admin keeps working; staff
    who use /admin/ also get a normal password.
    """

    phone = models.CharField(max_length=15, unique=True, null=True, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT)
    constituency = models.ForeignKey(
        "booths.Constituency", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="users",
    )
    cluster = models.ForeignKey(
        "booths.Cluster", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="coordinators",
        help_text="Required for cluster coordinators.",
    )
    booth = models.ForeignKey(
        "booths.Booth", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="agents",
        help_text="Required for booth agents.",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_device_id = models.CharField(max_length=64, blank=True)

    objects = BoothPulseUserManager()

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def can_edit_voters(self) -> bool:
        # The candidate dashboard is read-only by design.
        return self.role in {Role.ADMIN, Role.COORDINATOR, Role.AGENT}


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}:{settings.SECRET_KEY}".encode()).hexdigest()


class OTPCode(models.Model):
    phone = models.CharField(max_length=15, db_index=True)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, phone: str) -> str:
        """Invalidate earlier codes for this phone and create a new 6-digit one."""
        cls.objects.filter(phone=phone, used=False).update(used=True)
        code = f"{secrets.randbelow(1_000_000):06d}"
        cls.objects.create(
            phone=phone,
            code_hash=_hash_code(phone, code),
            expires_at=timezone.now() + timedelta(seconds=settings.OTP_TTL_SECONDS),
        )
        return code

    @classmethod
    def verify(cls, phone: str, code: str) -> bool:
        otp = cls.objects.filter(phone=phone, used=False).first()
        if otp is None or otp.expires_at < timezone.now():
            return False
        if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            otp.used = True
            otp.save(update_fields=["used"])
            return False
        otp.attempts += 1
        ok = secrets.compare_digest(otp.code_hash, _hash_code(phone, code.strip()))
        if ok:
            otp.used = True
        otp.save(update_fields=["attempts", "used"])
        return ok


class AuditLog(models.Model):
    """Every login, import, export and voter edit, with user, time and device."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=40, db_index=True)
    target = models.CharField(max_length=120, blank=True)
    device_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.user} {self.action} {self.target}"


def audit(request, action, target="", data=None, user=None, device_id=""):
    ip = None
    if request is not None:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR")
        device_id = device_id or request.headers.get("X-Device-Id", "")
        if user is None and getattr(request, "user", None) and request.user.is_authenticated:
            user = request.user
    AuditLog.objects.create(
        user=user, action=action, target=str(target)[:120], device_id=device_id[:64],
        ip=ip or None, data=data or {},
    )
