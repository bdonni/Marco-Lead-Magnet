"""Call times from Marco's own calendar (Google Calendar, read through its private iCal address).

Ben, 2026-10-02: "call times need to be pulled from his calendar, it can't rely on the email thread".
Marco's calendar holds both kinds of booking: Calendly ("Carrara Strategy Meeting") and the invites Rayhaan sends
by hand. Every few minutes the feed is read, each event is matched to a booking (attendee email, then the
attendee's company domain, then the contact's full name in the title), and the matched start time is stored with
source "calendar", which outranks a hand-set time and the email thread.

The iCal address is a secret. It is stored in the site's database (settings: calendar_ics_url) through the
Gamic-only ingest API, never in this public repo.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import store

GENERIC = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com",
           "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "me.com", "protonmail.com"}
WINDOWS_TZ = {"Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
              "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
              "US Mountain Standard Time": "America/Phoenix", "UTC": "UTC"}
LINE = re.compile(r'^([A-Z][A-Z0-9-]*)((?:;[A-Z0-9-]+=(?:"[^"]*"|[^:;]*))*):(.*)$', re.S)
SYNC_EVERY = 300

_state = {"last_ok": None, "last_error": None, "events": 0, "matched": 0}


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _to_utc(value: str, params: dict) -> Optional[datetime]:
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return None  # all-day entries are not calls
    try:
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        local = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tzid = (params.get("TZID") or "America/Chicago").strip('"')
        tz = ZoneInfo(WINDOWS_TZ.get(tzid, tzid))
        return local.replace(tzinfo=tz).astimezone(timezone.utc)
    except Exception:
        return None


def parse_ics(text: str) -> list:
    """Minimal VEVENT reader: start, end, title, description, attendees, status, uid. Recurring series skipped."""
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    events, cur = [], None
    for raw in text.split("\n"):
        if raw == "BEGIN:VEVENT":
            cur = {"attendees": [], "params": {}}
            continue
        if raw == "END:VEVENT":
            if cur and cur.get("start") and not cur.get("rrule"):
                events.append({k: v for k, v in cur.items() if k != "params"})
            cur = None
            continue
        if cur is None:
            continue
        m = LINE.match(raw)
        if not m:
            continue
        name, params_raw, value = m.group(1), m.group(2), m.group(3)
        params = dict(re.findall(r';([A-Z0-9-]+)=("[^"]*"|[^:;]*)', params_raw))
        if name == "DTSTART":
            cur["start"] = _to_utc(value, params)
        elif name == "DTEND":
            cur["end"] = _to_utc(value, params)
        elif name == "SUMMARY":
            cur["title"] = _unescape(value)
        elif name == "DESCRIPTION":
            cur["description"] = _unescape(value)[:4000]
        elif name == "UID":
            cur["uid"] = value.strip()
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
        elif name == "RRULE":
            cur["rrule"] = True
        elif name in ("ATTENDEE", "ORGANIZER"):
            em = re.sub(r"^mailto:", "", value.strip(), flags=re.I).lower()
            if "@" in em:
                cur["attendees" if name == "ATTENDEE" else "organizer"] = (
                    cur["attendees"] + [em] if name == "ATTENDEE" else em)
    return events


def _dom(email: str) -> str:
    return (email or "").split("@")[-1].lower()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def match(booking: dict, events: list, now: datetime) -> Optional[dict]:
    """The calendar event that is this booking's call, or None."""
    email = (booking.get("email") or "").lower()
    dom = _dom(email)
    dom = "" if dom in GENERIC else dom
    name = (booking.get("lead_name") or "").strip()
    co = _norm(store.norm_company(booking.get("company")) if booking.get("company") else "")
    try:
        booked = datetime.fromisoformat((booking.get("booked_at") or "").replace("Z", "+00:00"))
    except ValueError:
        booked = now - timedelta(days=30)
    scored = []
    for ev in events:
        if ev.get("status") == "CANCELLED" or not ev.get("start"):
            continue
        if ev["start"] < booked - timedelta(days=2):
            continue  # an older meeting with the same company is not this call
        text = f"{ev.get('title', '')}\n{ev.get('description', '')}"
        att = ev.get("attendees") or []
        if email and email in att:
            score = 3
        elif dom and any(_dom(a) == dom for a in att):
            score = 2
        elif len(name.split()) >= 2 and name.lower() in text.lower():
            score = 2
        elif len(co) >= 5 and co in _norm(text):
            score = 1
        else:
            continue
        scored.append((score, ev))
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [ev for s, ev in scored if s == best]
    upcoming = sorted((ev for ev in top if ev["start"] >= now - timedelta(hours=2)), key=lambda e: e["start"])
    return upcoming[0] if upcoming else max(top, key=lambda e: e["start"])


def _own(email: str) -> bool:
    """The client's own people (and Gamic) are not prospects."""
    import tenant
    hint = (tenant.get("mailbox_hint") or "").lower()
    own_domains = [d.lower() for d in (tenant.get("own_domains") or [])]
    e = (email or "").lower()
    return bool((hint and hint in e) or any(e.endswith("@" + d) for d in own_domains) or e.endswith("@gamicmedia.com"))


def sync_once(url: Optional[str] = None) -> dict:
    url = url or store.get_setting("calendar_ics_url")
    if not url:
        return {"ok": False, "reason": "no calendar connected"}
    now = datetime.now(timezone.utc)
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (Gamic brief bot)"})
        r.raise_for_status()
        events = [e for e in parse_ics(r.text)
                  if e["start"] >= now - timedelta(days=10) and e["start"] <= now + timedelta(days=90)]
    except Exception as e:
        _state["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        store.set_setting("calendar_last_error", _state["last_error"])
        print(json.dumps({"event": "calendar_sync_failed", "error": _state["last_error"]}), flush=True)
        return {"ok": False, "reason": _state["last_error"]}
    matched, used = 0, set()
    for b in store.all_bookings_raw():
        ev = match(b, events, now)
        if not ev:
            continue
        used.add(ev.get("uid"))
        at = ev["start"].replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if b.get("meeting_source") == "calendar" and b.get("meeting_at") == at and b.get("meeting_event_uid") == ev.get("uid"):
            matched += 1
            continue
        store.upsert_booking({"email": b["email"], "meeting": {"at": at, "text": (ev.get("title") or "")[:120],
                                                               "quote": None, "source": "calendar",
                                                               "uid": ev.get("uid")}})
        matched += 1
    _state.update(last_ok=now.isoformat(), last_error=None, events=len(events), matched=matched)
    store.set_setting("calendar_last_ok", now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    store.set_setting("calendar_last_error", None)
    unmatched = [{"uid": e.get("uid"), "start": e["start"].isoformat(),
                  "attendees": [a for a in (e.get("attendees") or []) if not _own(a)]}
                 for e in events if e.get("uid") not in used and e["start"] >= now - timedelta(hours=2)
                 and e.get("status") != "CANCELLED" and (e.get("attendees") or [])]
    store.set_setting("calendar_unmatched", json.dumps(unmatched)[:200000])
    return {"ok": True, "events": len(events), "matched": matched, "unmatched_upcoming": len(unmatched)}


def start_background_sync():
    def loop():
        time.sleep(15)
        while True:
            try:
                sync_once()
            except Exception as e:  # never let the loop die
                print(json.dumps({"event": "calendar_loop_error", "error": str(e)[:200]}), flush=True)
            time.sleep(SYNC_EVERY)
    threading.Thread(target=loop, daemon=True, name="calendar-sync").start()
