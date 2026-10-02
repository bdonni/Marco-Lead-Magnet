"""Calendly bookings as the trigger (ABC: Caleb books calls through Calendly; ABC's Smartlead has no Booked tag).

Calendly posts invitee.created / invitee.canceled to /hooks/calendly. The payload already carries the invitee, the
booking-form answers and the exact start time, so no Calendly token is needed on the server. Deliveries are
checked against the signing key chosen when the subscription was created (settings: calendly_signing_key).
"""
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from typing import Optional

TOLERANCE_S = 600


def verify(raw: bytes, header: Optional[str], key: Optional[str], now: Optional[float] = None) -> bool:
    """Calendly-Webhook-Signature: t=<unix>,v1=<hex hmac-sha256 of '<t>.<body>'>."""
    if not header or not key:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return False
    try:
        if abs((now or time.time()) - int(t)) > TOLERANCE_S:
            return False
    except ValueError:
        return False
    want = hmac.new(key.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, v1)


def _iso(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


COMPANY_Q = re.compile(r"\b(agency|company|business|firm|organi[sz]ation)\b(?!.*\b(size|revenue|how many)\b)", re.I)


def booking_from_invitee(payload: dict) -> dict:
    ev = payload.get("scheduled_event") or {}
    qa = [(str(q.get("question") or "").strip(), str(q.get("answer") or "").strip())
          for q in payload.get("questions_and_answers") or [] if q.get("answer")]
    company = next((a for q, a in qa if COMPANY_Q.search(q) and len(a) < 120), None)
    name = (payload.get("name") or f"{payload.get('first_name') or ''} {payload.get('last_name') or ''}").strip()
    return {
        "email": (payload.get("email") or "").strip().lower(),
        "lead_name": name or None,
        "first_name": payload.get("first_name") or (name.split()[0] if name else None),
        "company": company,
        "booked_at": _iso(payload.get("created_at")),
        "meeting": {"at": _iso(ev.get("start_time")), "text": (ev.get("name") or "Calendly booking")[:120],
                    "quote": None, "source": "calendar", "uid": ev.get("uri")},
        "hidden": False,
        "qa": qa,
        "invitee_uri": payload.get("uri"),
        "rescheduled": bool(payload.get("rescheduled")),
    }


def qa_text(qa: list) -> str:
    return "\n".join(f"{q}: {a}" for q, a in qa)
