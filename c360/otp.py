"""One-time passcodes for login 2FA and password reset.

CSPRNG codes (``secrets``, never ``random``), short expiry, single-use, delivered by
email. In local dev the email backend is the console, so the code is printed to the
server log; when ``DEBUG`` is on the code is also returned to the caller (``dev_otp``)
so the flow can be exercised without a mail server. Mirrors the HF Group OTP flow.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import OTP

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 5


def _brand() -> str:
    return getattr(settings, 'APP_BRAND_NAME', 'HFCB Customer 360')


def issue_otp(user, purpose: str) -> str:
    """Create a fresh 6-digit OTP for the user+purpose (superseding any prior one)
    and email it. Returns the code (callers only expose it in DEBUG)."""
    code = ''.join(secrets.choice('0123456789') for _ in range(6))
    OTP.objects.filter(user=user, purpose=purpose).delete()
    OTP.objects.create(user=user, code=code, purpose=purpose,
                       expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES))

    label = 'sign-in' if purpose == OTP.PURPOSE_LOGIN else 'password reset'
    if user.email:
        try:
            send_mail(
                subject=f'Your {_brand()} {label} code',
                message=(f'Your {_brand()} {label} code is: {code}\n\n'
                         f'It expires in {OTP_TTL_MINUTES} minutes. '
                         f"If you didn't request this, you can ignore this email."),
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@hfcb.co.ke'),
                recipient_list=[user.email],
                fail_silently=True,
            )
        except Exception:
            logger.exception('OTP email failed for %s', user.email)

    # Console backend prints the mail; also log explicitly so it's easy to find.
    logger.info('[OTP] %s code for %s: %s', label, user.username, code)
    return code


def verify_otp(user, purpose: str, code: str) -> bool:
    """Validate and consume the latest OTP for user+purpose. Single-use."""
    otp = OTP.objects.filter(user=user, purpose=purpose).order_by('-created_at').first()
    if not otp or otp.is_expired() or otp.code != (code or '').strip():
        return False
    otp.delete()   # consume on success
    return True


def dev_code(code: str) -> dict:
    """In DEBUG, expose the code so the flow can be tested without SMTP; empty otherwise."""
    return {'dev_otp': code} if settings.DEBUG else {}
