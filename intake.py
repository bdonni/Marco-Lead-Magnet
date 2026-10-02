"""Input hygiene, dedupe and the Smartlead -> Clay slim relay for the pre-call brief.

Why this exists (2026-10-02): Marco's briefs stopped firing for some booked calls.
- Clay's HTTP column treated all 12 inputs as required, so a single blank field (founded year,
  person name, location) stopped the call. Those inputs are optional now, so blanks reach us and
  are handled here.
- Pyrexar's owner summary came back from the Claygent as JSON text. We flatten JSON-shaped text
  into readable lines instead of printing raw JSON into the brief.
- Smartlead's "Booked Call" webhook was rejected by Clay with 413 Payload Too Large on long email
  threads (Ikor Industries / Paul Lesniak never reached Clay). The relay strips the thread history
  and forwards the rest, so the Clay table formulas keep working.
"""
import json
import re
import threading
import time
from typing import Any, Optional

import requests

GENERIC_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
                         "live.com", "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net"}

BLANKS = {"", "null", "none", "undefined", "n/a", "na", "[object object]"}


# ---------------------------------------------------------------------------
# Value hygiene
# ---------------------------------------------------------------------------

def _humanize_key(k: str) -> str:
    return re.sub(r"[_\s]+", " ", str(k)).strip().capitalize()


def flatten(value: Any) -> Optional[str]:
    """Any Clay value -> readable text. JSON objects and JSON-shaped strings become bullet lines."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in BLANKS:
            return None
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return flatten(json.loads(s))
            except (json.JSONDecodeError, ValueError):
                return s
        return s
    if isinstance(value, dict):
        # Clay action cells often wrap the answer as {"response": "..."}
        for k in ("response", "result", "text", "answer"):
            if k in value and len(value) <= 3:
                inner = flatten(value[k])
                if inner:
                    return inner
        lines = []
        for k, v in value.items():
            t = flatten(v)
            if t:
                lines.append(f"• {_humanize_key(k)}: {t}")
        return "\n".join(lines) or None
    if isinstance(value, (list, tuple)):
        parts = [flatten(v) for v in value]
        parts = [p for p in parts if p]
        return "; ".join(parts) or None
    return str(value).strip() or None


def domain_of(url_or_email: Optional[str]) -> str:
    s = (url_or_email or "").strip().lower()
    if "@" in s:
        s = s.split("@", 1)[1]
    s = re.sub(r"^https?://", "", s).split("/")[0].split("?")[0]
    return s[4:] if s.startswith("www.") else s


def company_from_domain(dom: str) -> Optional[str]:
    if not dom or dom in GENERIC_EMAIL_DOMAINS:
        return None
    root = dom.split(".")[0]
    words = [w for w in re.split(r"[-_]+", root) if w]
    return " ".join(w.capitalize() for w in words) or None


ROLE_LOCALS = {"info", "sales", "office", "admin", "contact", "hello", "support", "service", "team",
               "accounts", "billing", "inquiries", "enquiries", "mail", "orders", "help"}


def name_from_email(email: Optional[str]) -> Optional[str]:
    """jeff@x.com -> Jeff, mark.falkowski@x.com -> Mark Falkowski. Initial-style and role locals give nothing."""
    local = (email or "").split("@", 1)[0].lower()
    parts = [p for p in re.split(r"[._-]+", local) if p]
    if any(p in ROLE_LOCALS for p in parts):
        return None
    if not parts or any(len(p) < 2 or not p.isalpha() for p in parts) or len(parts) > 3:
        return None
    if len(parts) == 1 and len(parts[0]) > 12:
        return None
    return " ".join(p.capitalize() for p in parts)


TEXT_FIELDS = ("lead_name", "first_name", "email", "company_name", "website", "company_linkedin", "location",
               "company_type", "founded_year", "business_summary", "recent_news", "owner_summary", "title")


def normalize(req) -> list:
    """Clean every field in place and fill gaps from what we do have. Returns notes for the log."""
    notes = []
    for f in TEXT_FIELDS:
        if hasattr(req, f):
            setattr(req, f, flatten(getattr(req, f)))

    fy = req.founded_year or ""
    m = re.search(r"\b(1[6-9]\d\d|20\d\d)\b", fy)
    req.founded_year = m.group(1) if m else None

    if req.email and "@" not in req.email:
        req.email = None
    email_dom = domain_of(req.email)

    if not req.website and email_dom and email_dom not in GENERIC_EMAIL_DOMAINS:
        req.website = email_dom
        notes.append("website from email domain")
    if not req.company_name:
        c = company_from_domain(domain_of(req.website) or email_dom)
        if c:
            req.company_name = c
            notes.append("company from domain")
    if not req.lead_name:
        guess = name_from_email(req.email)
        if guess and req.first_name and not guess.lower().startswith(req.first_name.lower()):
            guess = None
        req.lead_name = guess or req.first_name
        if req.lead_name:
            notes.append("lead_name from email/first_name")
    return notes


# ---------------------------------------------------------------------------
# Our lead record wins on who the person is
# ---------------------------------------------------------------------------
# Clay's person enrichment named the Imex contact "Adam Said"; our Smartlead lead is Adam Zilberbaum.
# When SMARTLEAD_API_KEY is set, the lead record (looked up by email) supplies name, company and
# state. Without the key this is a no-op.

def smartlead_lead(email: Optional[str], api_key: str) -> dict:
    if not api_key or not email or "@" not in email:
        return {}
    try:
        r = requests.get("https://server.smartlead.ai/api/v1/leads/", params={"api_key": api_key, "email": email},
                         timeout=10, headers={"User-Agent": "curl/8.4.0"})
        data = r.json() if r.status_code == 200 else {}
        return data if isinstance(data, dict) and data.get("email") else {}
    except Exception:
        return {}


def apply_lead_record(req, rec: dict, notes: list) -> None:
    if not rec:
        return
    fn = (rec.get("first_name") or "").strip()
    ln = (rec.get("last_name") or "").strip()
    full = f"{fn} {ln}".strip()
    if fn and ln and (req.lead_name or "").strip().lower() != full.lower():
        notes.append(f"lead_name from Smartlead record (was {req.lead_name or 'blank'})")
        req.lead_name = full
    if fn and not req.first_name:
        req.first_name = fn
    cf = rec.get("custom_fields") or {}
    company = (cf.get("clean_company") or rec.get("company_name") or "").strip()
    if company and (not req.company_name or "company from domain" in notes):
        req.company_name = company
        notes.append("company from Smartlead record")
    site = domain_of(rec.get("website") or rec.get("company_url") or "")
    if site and (not req.website or "website from email domain" in notes):
        req.website = site
    state = (cf.get("state") or rec.get("location") or "").strip()
    if state and not req.location:
        req.location = state


# ---------------------------------------------------------------------------
# Dedupe (Clay retries and Smartlead redeliveries must not produce a second card)
# ---------------------------------------------------------------------------

_seen = {}
_seen_lock = threading.Lock()


def seen_recently(key: str, window_s: int) -> bool:
    """True when key was marked inside window_s. Marks it otherwise."""
    if not key:
        return False
    now = time.time()
    with _seen_lock:
        for k in [k for k, t in _seen.items() if now - t > 86400]:
            _seen.pop(k, None)
        t = _seen.get(key)
        if t and now - t < window_s:
            return True
        _seen[key] = now
        return False


def brief_key(req) -> str:
    who = (req.email or req.lead_name or "").strip().lower()
    co = (req.company_name or "").strip().lower()
    return f"brief:{who}|{co}" if (who or co) else ""


# ---------------------------------------------------------------------------
# Company overview when Clay's summary is blank
# ---------------------------------------------------------------------------

SITE_PATHS = ["", "/about", "/about-us", "/company", "/our-story"]


def site_text(site: str, limit: int = 7000) -> str:
    if not site:
        return ""
    chunks = []
    for path in SITE_PATHS:
        try:
            r = requests.get(f"https://{site}{path}", timeout=8,
                             headers={"User-Agent": "Mozilla/5.0 (Gamic brief bot)"})
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
        except Exception:
            continue
        t = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            chunks.append(f"[{site}{path or '/'}] {t[:2500]}")
        if sum(len(c) for c in chunks) > limit:
            break
    return "\n".join(chunks)[:limit]


def overview_from_site(client, model: str, company: str, site: str) -> Optional[str]:
    text = site_text(site)
    if not text:
        return None
    prompt = f"""Below is text from the website of {company or site} ({site}).

{text}

Write a short business overview for an M&A advisor's pre-call brief: what the company makes or does, who it sells to, and any size, age, location or ownership facts the text states. Use only facts stated in the text. Leave out anything it does not say. No marketing language, no em dashes, 90 to 150 words, plain prose, no heading."""
    try:
        msg = client.messages.create(model=model, max_tokens=400, messages=[{"role": "user", "content": prompt}])
        out = msg.content[0].text.strip()
        return out.replace("—", " - ").replace("–", " - ") or None
    except Exception as e:
        print(json.dumps({"event": "overview_error", "site": site, "error": str(e)[:200]}), flush=True)
        return None


# ---------------------------------------------------------------------------
# Smartlead -> Clay slim relay
# ---------------------------------------------------------------------------

CLAY_WEBHOOK_RE = re.compile(r"^https://api\.clay\.com/v3/sources/webhook/pull-in-data-from-a-webhook-[0-9a-f-]{36}$")
HEAVY_KEYS = ("history", "leadCorrespondence", "lead_correspondence")
MAX_BYTES = 90_000


def slim_payload(payload: dict) -> dict:
    """Drop the full thread (the 413 cause) and cap long strings. Keys Clay's formulas read are kept:
    app_url, last_reply.email_body, lead_data.*, description, campaign_name, to."""
    out = {k: v for k, v in payload.items() if k not in HEAVY_KEYS}

    def size(o):
        return len(json.dumps(o, ensure_ascii=False).encode("utf-8"))

    cap = 20_000
    while size(out) > MAX_BYTES and cap >= 500:
        out = _cap_strings(out, cap)
        cap //= 2
    return out


def _cap_strings(o, cap):
    if isinstance(o, str):
        return o if len(o) <= cap else o[:cap] + " [truncated]"
    if isinstance(o, dict):
        return {k: _cap_strings(v, cap) for k, v in o.items()}
    if isinstance(o, list):
        return [_cap_strings(v, cap) for v in o]
    return o


def forward_to_clay(target: str, payload: dict) -> None:
    body = slim_payload(payload)
    raw = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    slim = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    who = payload.get("to_email") or payload.get("sl_lead_email") or payload.get("to")
    for attempt in range(4):
        try:
            r = requests.post(target, json=body, timeout=20)
            print(json.dumps({"event": "relay_forward", "lead": who, "campaign": payload.get("campaign_name"),
                              "event_type": payload.get("event_type"), "status": r.status_code,
                              "bytes_in": raw, "bytes_out": slim, "attempt": attempt + 1}), flush=True)
            if r.status_code < 500 and r.status_code != 429:
                return
        except Exception as e:
            print(json.dumps({"event": "relay_error", "lead": who, "error": str(e)[:200],
                              "attempt": attempt + 1}), flush=True)
        time.sleep(5 * (attempt + 1))
