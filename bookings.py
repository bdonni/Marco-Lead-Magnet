"""Smartlead booking events -> booking records, readable email threads and the agreed call time.

The "Booked" tag on a Marco (Carrara) campaign lead is the trigger for the pre-call brief. The webhook
payload already carries the lead record and the whole email thread, so the brief and the site need no
Smartlead API key.
"""
import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BOOKED_CATEGORY_IDS = {96272}
CARRARA_CAMPAIGN_RE = re.compile(r"^\s*(CRR\b|Marco\b)", re.I)

STATE_TZ = {
    "ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "PT": "America/Los_Angeles",
}
_STATES = {
    "America/New_York": "ct de fl ga in ky me md ma mi nh nj ny nc oh pa ri sc vt va wv dc connecticut delaware "
                        "florida georgia indiana kentucky maine maryland massachusetts michigan new-hampshire "
                        "new-jersey new-york north-carolina ohio pennsylvania rhode-island south-carolina vermont "
                        "virginia west-virginia",
    "America/Chicago": "al ar il ia ks la mn ms mo ne nd ok sd tn tx wi alabama arkansas illinois iowa kansas "
                       "louisiana minnesota mississippi missouri nebraska north-dakota oklahoma south-dakota "
                       "tennessee texas wisconsin",
    "America/Denver": "co id mt nm ut wy colorado idaho montana new-mexico utah wyoming",
    "America/Phoenix": "az arizona",
    "America/Los_Angeles": "ca nv or wa california nevada oregon washington",
}
STATE_TO_TZ = {s: tz for tz, names in _STATES.items() for s in names.split()}


def state_tz(state: Optional[str]) -> Optional[str]:
    s = (state or "").strip().lower().replace(" ", "-")
    return STATE_TO_TZ.get(s)


def is_booked_event(payload: dict) -> bool:
    cat = payload.get("lead_category") or {}
    new_id = cat.get("new_id") if isinstance(cat, dict) else None
    new_name = (cat.get("new_name") if isinstance(cat, dict) else "") or ""
    try:
        if int(new_id) in BOOKED_CATEGORY_IDS:
            return True
    except (TypeError, ValueError):
        pass
    return new_name.strip().lower() == "booked"


def is_marco_campaign(name: Optional[str]) -> bool:
    return bool(CARRARA_CAMPAIGN_RE.match(name or ""))


def is_marco_event(payload: dict, known_campaign_ids=()) -> bool:
    """Marco's lead? Campaign named CRR/Marco, a Carrara sending inbox on the thread, or a campaign id the sync
    listed as Carrara's. POS REPLY follow-ups are just named "POS REPLY", so the name alone misses them."""
    if is_marco_campaign(payload.get("campaign_name")):
        return True
    try:
        if int(payload.get("campaign_id")) in set(known_campaign_ids):
            return True
    except (TypeError, ValueError):
        pass
    boxes = " ".join(str(payload.get(k) or "") for k in ("sl_senders_mailbox", "from_email", "to_email", "from"))
    return "carrara" in boxes.lower()


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

QUOTE_CUT = re.compile(
    r"(\n\s*On\s.{0,200}?\bwrote:|\n\s*-{2,}\s*Original Message|\n\s*From:\s.+\n\s*(Sent|Date):|\n>)", re.I | re.S)


def html_to_text(body: Optional[str]) -> str:
    s = body or ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|tr|h\d)>", "\n", s)
    s = re.sub(r"(?is)<blockquote.*?</blockquote>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s).replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def strip_quoted(text: str) -> str:
    m = QUOTE_CUT.search("\n" + text)
    if m and m.start() > 20:
        text = ("\n" + text)[:m.start()].strip()
    return text.strip()


def _iso(t) -> Optional[str]:
    if not t:
        return None
    s = str(t).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return s


def normalize_thread(history) -> list:
    """Smartlead message-history entries (API or webhook) -> [{type, time, from, to, subject, text}] oldest first."""
    out = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        body = m.get("email_body") or m.get("body") or m.get("html") or m.get("text") or ""
        text = strip_quoted(html_to_text(body))
        if not text:
            continue
        t = (m.get("type") or "").upper()
        out.append({
            "type": "REPLY" if t in ("REPLY", "RECEIVED", "INBOUND") else "SENT",
            "time": _iso(m.get("time") or m.get("sent_time") or m.get("reply_time")),
            "from": m.get("from") or m.get("from_email") or "",
            "to": m.get("to") or m.get("to_email") or "",
            "subject": m.get("subject") or "",
            "text": text[:6000],
        })
    out.sort(key=lambda x: x.get("time") or "")
    return out


def booking_from_payload(payload: dict) -> dict:
    """Webhook payload -> booking record + the inputs a brief needs."""
    ld = payload.get("lead_data") or {}
    cf = ld.get("custom_fields") or {}
    email = (ld.get("email") or payload.get("sl_lead_email") or payload.get("to_email") or "").strip().lower()
    first = (ld.get("first_name") or "").strip()
    last = (ld.get("last_name") or "").strip()
    company = (cf.get("clean_company") or ld.get("company_name") or "").strip()
    history = payload.get("history")
    if not isinstance(history, list):
        corr = payload.get("leadCorrespondence") or {}
        history = corr.get("history") if isinstance(corr, dict) else None
    return {
        "email": email,
        "lead_name": f"{first} {last}".strip() or None,
        "first_name": first or None,
        "company": company or None,
        "website": (ld.get("website") or ld.get("company_url") or "").strip() or None,
        "location": (cf.get("state") or ld.get("location") or "").strip() or None,
        "campaign_id": payload.get("campaign_id"),
        "campaign_name": payload.get("campaign_name"),
        "lead_id": str(payload.get("sl_email_lead_id") or ld.get("id") or payload.get("lead_id") or "") or None,
        "booked_at": _iso(payload.get("event_timestamp")),
        "thread": normalize_thread(history) if isinstance(history, list) else None,
        "linkedin": ld.get("linkedin_profile") or None,
    }


def prospect_words(thread: list, limit: int = 2500) -> str:
    """What the prospect side actually wrote, newest last, for the brief."""
    parts = [f"[{(m.get('time') or '')[:10]}] {m.get('from')}: {m['text']}" for m in thread if m.get("type") == "REPLY"]
    s = "\n\n".join(parts)
    return s[-limit:]


# ---------------------------------------------------------------------------
# Agreed call time
# ---------------------------------------------------------------------------

def extract_meeting(client, model: str, thread: list, state: Optional[str]) -> Optional[dict]:
    """Ask the model whether a specific call time was agreed in the thread. -> {at, text, quote, source} or None."""
    if not thread or client is None:
        return None
    lines = []
    for m in thread[-8:]:
        who = "PROSPECT" if m.get("type") == "REPLY" else "US"
        try:
            wd = datetime.fromisoformat(str(m.get("time")).replace("Z", "+00:00")).strftime("%A")
        except ValueError:
            wd = ""
        lines.append(f"--- {who} at {m.get('time')} UTC ({wd})\n{m['text'][:1500]}")
    tz_hint = state_tz(state) or "unknown"
    try:
        start = datetime.fromisoformat(str(thread[max(0, len(thread) - 8)].get("time")).replace("Z", "+00:00")).date()
    except ValueError:
        start = datetime.now(timezone.utc).date()
    calendar = ", ".join((start + timedelta(days=i)).strftime("%a %Y-%m-%d") for i in range(28))
    prompt = f"""Below is the end of an email thread between us (an M&A advisor's team) and a business owner who booked a call.
Message times are UTC. The prospect's state timezone is {tz_hint}.

{chr(10).join(lines)}

Calendar for working out dates: {calendar}

Has a specific date and time for the call been agreed (proposed by one side and accepted, or a calendar booking confirmed)?
A list of options is not agreement; use the option the other side accepted. Resolve weekday names with the calendar
above, counting forward from the date of the message they appear in. Return ONLY JSON:
{{"agreed": true or false,
  "local_datetime": "YYYY-MM-DDTHH:MM" (24h clock, in the timezone the time was stated in),
  "timezone": IANA name the time was stated in, e.g. "America/New_York" for ET, "America/Chicago" for CT; use the prospect's state timezone if none is stated,
  "stated_as": "the time as written, e.g. Friday at 10am ET",
  "quote": "the sentence that confirms it, max 160 chars"}}
If no specific time was agreed, return {{"agreed": false}}."""
    try:
        msg = client.messages.create(model=model, max_tokens=300, messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception as e:
        print(json.dumps({"event": "meeting_extract_error", "error": str(e)[:200]}), flush=True)
        return None
    if not d.get("agreed") or not d.get("local_datetime"):
        return None
    try:
        tzname = d.get("timezone") or state_tz(state) or "America/Chicago"
        local = datetime.fromisoformat(str(d["local_datetime"])[:16])
        at = local.replace(tzinfo=ZoneInfo(tzname)).astimezone(timezone.utc)
    except Exception:
        return None
    # a stated weekday must match the date we resolved
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    said = next((i for i, dname in enumerate(days) if dname in (d.get("stated_as") or "").lower()), None)
    if said is not None and local.weekday() != said:
        return None
    last = max((m.get("time") or "" for m in thread), default="")
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if at < last_dt - timedelta(days=2) or at > last_dt + timedelta(days=120):
            return None  # nonsense resolution
    except ValueError:
        pass
    return {"at": at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "text": (d.get("stated_as") or "")[:80], "quote": (d.get("quote") or "")[:200], "source": "thread"}
