from rest_framework.throttling import SimpleRateThrottle


class PhoneRateThrottle(SimpleRateThrottle):
    """Rate-limit OTP endpoints per phone number, not per IP.

    Many agents on mobile data share one carrier IP (CGNAT), so an IP limit
    would lock out whole areas on polling day. A wide per-IP limit is still
    applied separately by IPBurstThrottle against brute force.
    """

    def get_cache_key(self, request, view):
        digits = "".join(ch for ch in str(request.data.get("phone", "")) if ch.isdigit())[-10:]
        if not digits:
            return None
        return self.cache_format % {"scope": self.scope, "ident": digits}


class OTPRequestThrottle(PhoneRateThrottle):
    scope = "otp_request"


class OTPVerifyThrottle(PhoneRateThrottle):
    scope = "otp_verify"


class IPBurstThrottle(SimpleRateThrottle):
    scope = "otp_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
