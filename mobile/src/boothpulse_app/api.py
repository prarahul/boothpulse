"""Thin HTTP client for the BoothPulse Django API.

Uses only the standard library (urllib) so the app packages cleanly for
Android and iOS without native wheels.
"""

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 15


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


class OfflineError(ApiError):
    """No network or the server could not be reached."""


class AuthError(ApiError):
    """The session token is missing, expired or was replaced by a new login."""


def _ssl_context():
    try:
        import certifi  # optional, bundled on most Flet builds

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Api:
    def __init__(self, base_url: str = "", token: str = "", device_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self._ssl = _ssl_context()

    # -- plumbing ----------------------------------------------------------

    def _request(self, method, path, body=None, params=None, raw=False):
        if not self.base_url:
            raise ApiError("Server address is not set")
        url = f"{self.base_url}/api{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Token {self.token}")
        if self.device_id:
            req.add_header("X-Device-Id", self.device_id)
        try:
            ctx = self._ssl if url.startswith("https") else None
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ctx) as resp:
                payload = resp.read()
                return payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            message = detail.get("detail") if isinstance(detail, dict) else None
            if isinstance(detail, dict) and not message:
                first = next(iter(detail.values()), None)
                message = first[0] if isinstance(first, list) and first else (first if isinstance(first, str) else None)
            message = message or f"Server error {e.code}"
            if e.code == 401:
                raise AuthError(message, 401)
            raise ApiError(message, e.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise OfflineError(f"Cannot reach server ({getattr(e, 'reason', e)})")

    # -- endpoints ---------------------------------------------------------

    def request_otp(self, phone):
        return self._request("POST", "/auth/otp/request/", {"phone": phone})

    def verify_otp(self, phone, code):
        return self._request("POST", "/auth/otp/verify/", {"phone": phone, "code": code, "device_id": self.device_id})

    def logout(self):
        return self._request("POST", "/auth/logout/", {})

    def me(self):
        return self._request("GET", "/me/")

    def booths(self):
        return self._request("GET", "/booths/")

    def voters(self, booth=None, since=None, offset=0, limit=500, q=None):
        return self._request("GET", "/voters/", params={
            "booth": booth, "since": since, "offset": offset, "limit": limit, "q": q,
        })

    def sync(self, changes):
        return self._request("POST", "/sync/", {"changes": changes})

    def dashboard(self):
        return self._request("GET", "/dashboard/")
