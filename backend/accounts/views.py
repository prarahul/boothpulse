from django.conf import settings
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import OTPCode, User, audit
from .sms import send_sms
from .throttles import IPBurstThrottle, OTPRequestThrottle, OTPVerifyThrottle


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise serializers.ValidationError({"phone": "Enter a 10-digit Indian mobile number"})
    return digits


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.get_full_name() or user.phone or user.username,
        "phone": user.phone,
        "role": user.role,
        "role_label": user.get_role_display(),
        "can_edit": user.can_edit_voters,
        "constituency": str(user.constituency) if user.constituency_id else None,
        "cluster": user.cluster.name if user.cluster_id else None,
        "booth": ({"id": user.booth.id, "number": user.booth.number, "name": user.booth.name}
                  if user.booth_id else None),
        "features": {"inclination": settings.FEATURE_INCLINATION},
    }


class OTPRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [OTPRequestThrottle, IPBurstThrottle]

    def post(self, request):
        phone = normalize_phone(request.data.get("phone"))
        body = {"detail": "If this number is registered, an OTP has been sent."}
        user = User.objects.filter(phone=phone, is_active=True).first()
        # Same answer whether or not the number exists, so the endpoint cannot
        # be used to discover who works for the campaign.
        if user is not None:
            code = OTPCode.issue(phone)
            send_sms(phone, f"{code} is your BoothPulse login code. Valid for {settings.OTP_TTL_SECONDS // 60} minutes. Do not share it.")
            if settings.OTP_ECHO_IN_RESPONSE:
                body["dev_code"] = code
        return Response(body)


class OTPVerifyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [OTPVerifyThrottle, IPBurstThrottle]

    def post(self, request):
        phone = normalize_phone(request.data.get("phone"))
        code = str(request.data.get("code", ""))
        user = User.objects.filter(phone=phone, is_active=True).first()
        if user is None or not OTPCode.verify(phone, code):
            audit(request, "login_failed", phone)
            return Response({"detail": "Wrong or expired code"}, status=400)
        # One active session per user: a new login signs out the old phone.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)
        audit(request, "login", phone, user=user, device_id=str(request.data.get("device_id", "")))
        return Response({"token": token.key, "user": user_payload(user)})


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        audit(request, "logout")
        return Response({"detail": "Signed out"})


class MeView(APIView):
    def get(self, request):
        return Response(user_payload(request.user))
