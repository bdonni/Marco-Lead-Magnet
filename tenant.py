"""Which client this service runs for. One Railway service per client, same code.

TENANT (env) names the client; everything client-specific is read from the service's own database
(settings key "tenant_config", set through the Gamic-only ingest API), so no client details or secrets
live in this public repo. A service with no config runs as Carrara, which is how this bot started.
"""
import base64
import json
import os
import re
from typing import Optional

import store

TENANT = os.environ.get("TENANT", "carrara").strip().lower() or "carrara"

CARRARA = {
    "firm": "Carrara Strategy Group",
    "firm_short": "Carrara Strategy",
    "sender_label": "Carrara",
    "caller": "Marco",
    "caller_context": "a sell-side M&A advisor at Carrara Strategy Group",
    "confirm_focus": "ownership and decision makers, size (revenue or EBITDA, headcount), timeline, anything unverified",
    "accent": "#144e83",
    "accent_dark": "#8fb3e0",
    "campaign_prefixes": ["CRR", "Marco"],
    "mailbox_hint": "carrara",
    "booked_category_ids": [96272],
    "slack_enabled": True,
}

GENERIC = {
    "firm": "Pre-Call Briefs",
    "firm_short": "Pre-Call Briefs",
    "sender_label": "Us",
    "caller": "the advisor",
    "caller_context": "an M&A advisor",
    "confirm_focus": "ownership and decision makers, size, timeline, anything unverified",
    "accent": "#1f4fd8",
    "accent_dark": "#8fb3e0",
    "campaign_prefixes": [],
    "mailbox_hint": "",
    "booked_category_ids": [96272],
    "slack_enabled": False,  # a new client posts nothing to Slack until Ben switches it on
}

_cache = {"raw": None, "cfg": None}


def cfg() -> dict:
    raw = store.get_setting("tenant_config") or ""
    if _cache["cfg"] is not None and _cache["raw"] == raw:
        return _cache["cfg"]
    base = dict(CARRARA if TENANT == "carrara" else GENERIC)
    try:
        base.update({k: v for k, v in json.loads(raw).items() if v not in (None, "")})
    except (ValueError, AttributeError):
        pass
    _cache.update(raw=raw, cfg=base)
    return base


def get(key: str, default=None):
    return cfg().get(key, default)


def campaign_regex() -> Optional[re.Pattern]:
    pre = [p for p in (get("campaign_prefixes") or []) if p]
    if not pre:
        return None
    return re.compile(r"^\s*(" + "|".join(re.escape(p) + r"\b" for p in pre) + r")", re.I)


def logo_bytes() -> Optional[bytes]:
    b64 = store.get_setting("tenant_logo_b64")
    if b64:
        try:
            return base64.b64decode(b64)
        except ValueError:
            return None
    if TENANT == "carrara":
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "carrara-logo.png")
        try:
            return open(p, "rb").read()
        except OSError:
            return None
    return None


def logo_b64() -> Optional[str]:
    b = logo_bytes()
    return base64.b64encode(b).decode() if b else None


def possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def calendar_owner() -> str:
    """'Marco's calendar', 'Kory's calendar'."""
    return possessive(get("caller") or "the advisor")
