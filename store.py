"""SQLite store behind Marco's pre-call brief site.

Lives on the Railway volume (/data) so bookings, threads and briefs survive deploys.
One row per booked lead (keyed by lead email) and one row per generated brief.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional


def _db_path() -> str:
    p = os.environ.get("BRIEFS_DB")
    if p:
        return p
    if os.path.isdir("/data"):
        return "/data/briefs.db"
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "briefs.db")


DB_PATH = _db_path()
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  email            TEXT PRIMARY KEY,
  bid              TEXT UNIQUE,
  lead_name        TEXT,
  first_name       TEXT,
  title            TEXT,
  company          TEXT,
  website          TEXT,
  location         TEXT,
  campaign_id      INTEGER,
  campaign_name    TEXT,
  lead_id          TEXT,
  booked_at        TEXT,
  booked_src       TEXT,
  meeting_at       TEXT,
  meeting_text     TEXT,
  meeting_source   TEXT,
  meeting_quote    TEXT,
  thread_json      TEXT,
  thread_count     INTEGER DEFAULT 0,
  thread_hash      TEXT,
  thread_updated_at TEXT,
  hidden           INTEGER DEFAULT 0,
  created_at       TEXT,
  updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS briefs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT,
  company         TEXT,
  lead_name       TEXT,
  created_at      TEXT,
  source          TEXT,
  owner_status    TEXT,
  request_json    TEXT,
  assessment_json TEXT,
  posted_to_slack INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (
  id         INTEGER PRIMARY KEY,
  name       TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS briefs_email ON briefs(email);
CREATE INDEX IF NOT EXISTS briefs_company ON briefs(company);
"""

MEETING_RANK = {"thread": 1, "manual": 2, "calendar": 3}

BOOKING_FIELDS = ("lead_name", "first_name", "title", "company", "website", "location", "campaign_id",
                  "campaign_name", "lead_id", "form_answers")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bid_for(email: str) -> str:
    return hashlib.sha1(("mb:" + email.strip().lower()).encode()).hexdigest()[:12]


def norm_company(name: Optional[str]) -> str:
    s = (name or "").lower()
    s = re.sub(r"\b(inc|llc|ltd|co|corp|corporation|company|the)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")  # page reads never wait behind a brief being written
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(bookings)")}
        if "share_token" not in cols:
            c.execute("ALTER TABLE bookings ADD COLUMN share_token TEXT")
        if "meeting_event_uid" not in cols:
            c.execute("ALTER TABLE bookings ADD COLUMN meeting_event_uid TEXT")
        if "meeting_day_only" not in cols:
            c.execute("ALTER TABLE bookings ADD COLUMN meeting_day_only INTEGER DEFAULT 0")
        if "form_answers" not in cols:  # answers from a booking form (Calendly), kept for brief rebuilds
            c.execute("ALTER TABLE bookings ADD COLUMN form_answers TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS bookings_share ON bookings(share_token)")
        c.execute("CREATE INDEX IF NOT EXISTS bookings_bid ON bookings(bid)")
        for r in c.execute("SELECT email FROM bookings WHERE share_token IS NULL").fetchall():
            c.execute("UPDATE bookings SET share_token=? WHERE email=?", (secrets.token_urlsafe(12), r["email"]))


def upsert_booking(d: dict) -> Optional[str]:
    """Insert or update one booking. Blank incoming values never wipe stored ones.
    A manually set meeting time is never replaced by an automatic one."""
    email = (d.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    ts = now_iso()
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM bookings WHERE email=?", (email,)).fetchone()
        if row is None:
            c.execute("INSERT INTO bookings(email, bid, share_token, created_at, updated_at) VALUES (?,?,?,?,?)",
                      (email, bid_for(email), secrets.token_urlsafe(12), ts, ts))
            row = c.execute("SELECT * FROM bookings WHERE email=?", (email,)).fetchone()
        sets, vals = [], []
        for f in BOOKING_FIELDS:
            v = d.get(f)
            if v in (None, ""):
                continue
            if f in ("lead_name", "company") and row[f] and d.get("names_soft"):
                continue  # a sync guess never overwrites a name the brief already settled
            sets.append(f"{f}=?"); vals.append(v)
        # booked_at: 'event' times (webhook, brief request, Clay row) beat 'soft' guesses; earliest event wins
        bat, src = d.get("booked_at"), ("soft" if d.get("booked_at_soft") else "event")
        if bat:
            cur, cur_src = row["booked_at"], row["booked_src"]
            if (not cur) or (src == "event" and (cur_src != "event" or str(bat) < str(cur))):
                sets += ["booked_at=?", "booked_src=?"]; vals += [bat, src]
        if "thread" in d and isinstance(d["thread"], list):
            th = json.dumps(d["thread"], ensure_ascii=False)
            h = hashlib.sha1(th.encode()).hexdigest()
            if h != row["thread_hash"] and (len(d["thread"]) >= (row["thread_count"] or 0)):
                sets += ["thread_json=?", "thread_count=?", "thread_hash=?", "thread_updated_at=?"]
                vals += [th, len(d["thread"]), h, ts]
        m = d.get("meeting")
        # Marco's calendar beats a time set by hand, which beats one read from the email thread.
        if isinstance(m, dict) and MEETING_RANK.get(m.get("source"), 0) >= MEETING_RANK.get(row["meeting_source"], 0):
            sets += ["meeting_at=?", "meeting_text=?", "meeting_source=?", "meeting_quote=?", "meeting_event_uid=?",
                     "meeting_day_only=?"]
            vals += [m.get("at"), m.get("text"), m.get("source"), m.get("quote"), m.get("uid"),
                     1 if m.get("day_only") else 0]
        if "hidden" in d:
            sets.append("hidden=?"); vals.append(1 if d["hidden"] else 0)
        if sets:
            sets.append("updated_at=?"); vals.append(ts)
            c.execute(f"UPDATE bookings SET {', '.join(sets)} WHERE email=?", (*vals, email))
    return bid_for(email)


def set_meeting_uid(email: str, uid: Optional[str]) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE bookings SET meeting_event_uid=? WHERE email=?", (uid, (email or "").lower()))


def save_brief(email: Optional[str], company: Optional[str], lead_name: Optional[str], source: str,
               owner_status: Optional[str], request: dict, assessment: dict, posted: bool = False,
               created_at: Optional[str] = None) -> int:
    email = (email or "").strip().lower() or None
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO briefs(email, company, lead_name, created_at, source, owner_status, request_json,"
            " assessment_json, posted_to_slack) VALUES (?,?,?,?,?,?,?,?,?)",
            (email, company, lead_name, created_at or now_iso(), source, owner_status,
             json.dumps(request, ensure_ascii=False), json.dumps(assessment, ensure_ascii=False), 1 if posted else 0))
        return cur.lastrowid


def needs_brief(email: str) -> bool:
    with _lock, _conn() as c:
        return c.execute("SELECT 1 FROM briefs WHERE email=? LIMIT 1", (email.lower(),)).fetchone() is None


def _brief_dict(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["request"] = json.loads(r["request_json"] or "{}")
    out["assessment"] = json.loads(r["assessment_json"] or "{}")
    return out


def _latest_briefs() -> tuple:
    """Latest brief per lead and per company, light columns only (the list page never needs the text)."""
    by_email, by_company = {}, {}
    with _lock, _conn() as c:
        for r in c.execute("SELECT id, email, company, created_at, owner_status, posted_to_slack FROM briefs "
                           "ORDER BY created_at ASC, id ASC"):
            d = dict(r)
            if r["email"]:
                by_email[r["email"]] = d
            k = norm_company(r["company"])
            if k:
                by_company[k] = d
    return by_email, by_company


def list_bookings(include_hidden: bool = False) -> list:
    by_email, by_company = _latest_briefs()
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM bookings")]
    out = []
    for b in rows:
        if b.get("hidden") and not include_hidden:
            continue
        b["brief"] = by_email.get(b["email"]) or by_company.get(norm_company(b.get("company")))
        b.pop("thread_json", None)
        out.append(b)
    return out


def get_booking_by_token(token: str) -> Optional[dict]:
    with _lock, _conn() as c:
        r = c.execute("SELECT bid FROM bookings WHERE share_token=?", (token,)).fetchone()
    return get_booking(r["bid"]) if r else None


def mark_posted(brief_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE briefs SET posted_to_slack=1 WHERE id=?", (brief_id,))


def get_booking(bid: str) -> Optional[dict]:
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM bookings WHERE bid=?", (bid,)).fetchone()
        if not r:
            return None
        b = dict(r)
        briefs = [_brief_dict(x) for x in c.execute(
            "SELECT * FROM briefs WHERE email=? ORDER BY created_at DESC, id DESC", (b["email"],))]
        if not briefs and b.get("company"):
            key = norm_company(b["company"])
            briefs = [_brief_dict(x) for x in c.execute("SELECT * FROM briefs ORDER BY created_at DESC, id DESC")
                      if norm_company(x["company"]) == key]
    b["thread"] = json.loads(b.get("thread_json") or "[]")
    b["briefs"] = briefs
    b["brief"] = briefs[0] if briefs else None
    return b


def state() -> list:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT email, bid, share_token, company, lead_id, campaign_id, thread_count, thread_hash, thread_updated_at, "
            "meeting_at, meeting_source, booked_at, "
            "(SELECT COUNT(*) FROM briefs WHERE briefs.email=bookings.email) AS briefs FROM bookings")]

def posted_recently(email: str, hours: int = 24) -> bool:
    """True when a brief for this lead was posted to Slack inside the window."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    with _lock, _conn() as c:
        for r in c.execute("SELECT created_at FROM briefs WHERE email=? AND posted_to_slack=1", (email.lower(),)):
            try:
                if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp() >= cutoff:
                    return True
            except (ValueError, AttributeError):
                continue
    return False


def set_campaigns(rows: list) -> int:
    ts = now_iso()
    with _lock, _conn() as c:
        for r in rows:
            c.execute("INSERT INTO campaigns(id, name, updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                      (int(r["id"]), r.get("name"), ts))
    return len(rows)


def campaign_ids() -> set:
    with _lock, _conn() as c:
        return {r["id"] for r in c.execute("SELECT id FROM campaigns")}


def purge(emails: list) -> int:
    """Remove bookings and their briefs (Gamic-only, via the ingest key)."""
    em = [e.strip().lower() for e in emails if e and "@" in e]
    with _lock, _conn() as c:
        n = 0
        for e in em:
            n += c.execute("DELETE FROM bookings WHERE email=?", (e,)).rowcount
            c.execute("DELETE FROM briefs WHERE email=?", (e,))
    return n


def get_setting(key: str) -> Optional[str]:
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key: str, value: Optional[str]) -> None:
    with _lock, _conn() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))


def all_bookings_raw() -> list:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT email, bid, lead_name, company, booked_at, meeting_at, meeting_source, meeting_event_uid, hidden, "
            "campaign_name FROM bookings")]
