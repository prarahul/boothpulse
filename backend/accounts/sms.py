"""Pluggable SMS delivery for OTP codes.

Set SMS_BACKEND to the dotted path of a class with a `send(phone, message)`
method. Write one for your gateway (MSG91, Twilio, AWS SNS, Gupshup ...).
Note: Indian SMS gateways require a DLT-registered sender ID and template.
"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

log = logging.getLogger("boothpulse")


class ConsoleSMSBackend:
    """Development backend: writes the message to the server log."""

    outbox: list = []  # handy in tests

    def send(self, phone: str, message: str) -> None:
        ConsoleSMSBackend.outbox.append((phone, message))
        log.info("SMS to %s: %s", phone, message)
        print(f"[SMS to {phone}] {message}", flush=True)


def send_sms(phone: str, message: str) -> None:
    backend = import_string(settings.SMS_BACKEND)()
    backend.send(phone, message)
