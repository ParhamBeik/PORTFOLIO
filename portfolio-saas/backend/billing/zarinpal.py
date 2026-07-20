"""Thin client over the Zarinpal v4 REST API.

Two calls only: `request_payment` (issue an authority / start a payment) and
`verify_payment` (confirm a paid authority). Both use `requests` with a hard
timeout — no SDK, no retries beyond what `requests` does on connection errors.

Zarinpal returns an `authority` token; the URL the user visits is built from it
(`start_pay_url`). Verify response codes: 100 = paid now, 101 = already verified
(both success — 101 is what a callback replay sees).

Amounts are in Rial. Our price is in Toman (1 Toman = 10 Rial); the view does
the ×10, the gateway never sees Toman.
"""
import requests
from django.conf import settings

REQUEST_TIMEOUT = 15  # seconds, hard cap on both request and verify

_API_BASE = "https://api.zarinpal.com/pg/v4"
_START_PAY_LIVE = "https://www.zarinpal.com/pg/StartPay/{authority}"
_START_PAY_SANDBOX = "https://sandbox.zarinpal.com/pg/StartPay/{authority}"


class ZarinpalError(Exception):
    """Raised when Zarinpal refuses to issue an authority (code != 100)."""

    def __init__(self, data):
        self.data = data
        super().__init__(f"zarinpal refused: {data}")


def start_pay_url(authority: str) -> str:
    """The hosted payment URL the user is redirected to (sandbox vs live)."""
    tmpl = _START_PAY_SANDBOX if settings.ZARINPAL_SANDBOX else _START_PAY_LIVE
    return tmpl.format(authority=authority)


def request_payment(*, amount_rial: int, description: str, callback_url: str) -> str:
    """Ask Zarinpal for an authority token. Returns it; raises ZarinpalError on refusal."""
    resp = requests.post(
        f"{_API_BASE}/payment/request.json",
        json={
            "merchant_id": settings.ZARINPAL_MERCHANT_ID,
            "amount": int(amount_rial),
            "description": description,
            "callback_url": callback_url,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    if data.get("code") != 100 or not data.get("authority"):
        raise ZarinpalError(data)
    return data["authority"]


def verify_payment(*, authority: str, amount_rial: int) -> tuple[bool, str, int]:
    """Verify a payment. Returns (ok, ref_id, code).

    code 100 = paid this call, 101 = already verified (idempotent success).
    Any other code (or a missing ref_id on success) is a failure.
    """
    resp = requests.post(
        f"{_API_BASE}/payment/verify.json",
        json={
            "merchant_id": settings.ZARINPAL_MERCHANT_ID,
            "amount": int(amount_rial),
            "authority": authority,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    code = int(data.get("code") or 0)
    ref_id = str(data.get("ref_id") or data.get("reference_id") or "")
    ok = code in (100, 101)
    return ok, ref_id, code
