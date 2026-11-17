"""Approved follow-ups sent in the prospect's original email thread (Ben 17 Nov 2026).

A Gamic job (gamic-ops) finds a positive prospect who went quiet after our reply, drafts the follow-up with their
sector report, and stores it here with POST /api/followups. Slack gets a link to /f/<token>, where someone reads and
edits the draft and presses Send. Send replies inside the original Smartlead thread from the same inbox; nothing
goes out without that click. The Smartlead key is the site setting smartlead_api_key, never shown on a page.
"""
import html
import json
import os
import secrets

import requests
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

import store

router = APIRouter()
SL = "https://server.smartlead.ai/api/v1"
# Gene's public booking calendar (link.advocateadvisorsllc.com/widget/bookings/advocate-advisors-consultation)
DEFAULT_CALENDAR = {"advocate": "SBQYcB6jD8gI7TFsIkmF"}
LEGACY_TIMES = "Wednesday at 1pm ET or Thursday at 3:30pm ET"  # the placeholder pair in the first 12 drafts (17 Nov)


def _calendar_id() -> str:
    import tenant
    return tenant.get("booking_calendar_id") or DEFAULT_CALENDAR.get(tenant.TENANT, "")


def live_slots() -> tuple:
    """Two open times on the caller's booking calendar, on different days, at least 16 hours out. ('', '') if none."""
    from datetime import datetime, timedelta, timezone
    cal = _calendar_id()
    if not cal:
        return "", ""
    now = datetime.now(timezone.utc)
    try:
        r = requests.get(f"https://backend.leadconnectorhq.com/calendars/{cal}/free-slots", timeout=20,
                         params={"startDate": int(now.timestamp() * 1000), "endDate": int((now + timedelta(days=14)).timestamp() * 1000),
                                 "timezone": "America/New_York"}, headers={"User-Agent": "Mozilla/5.0"})
        days = {k: v for k, v in r.json().items() if isinstance(v, dict)}
    except Exception:
        return "", ""
    picks, seen = [], set()
    for day in sorted(days):
        for t in days[day].get("slots") or []:
            d = datetime.fromisoformat(t)
            if d < now + timedelta(hours=16) or day in seen or d.hour < 9 or d.hour > 16:
                continue
            seen.add(day)
            picks.append(d)
            break
        if len(picks) == 2:
            break
    fmt = lambda d: d.strftime("%A ") + str(d.day) + d.strftime(" %b at ") + d.strftime("%I:%M%p").lstrip("0").replace(":00", "").lower() + " ET"
    return (fmt(picks[0]), fmt(picks[1])) if len(picks) == 2 else ("", "")


def _fill_times(body: str) -> str:
    if "{{slot1}}" not in body and LEGACY_TIMES not in body:
        return body
    t1, t2 = live_slots()
    if not t1:
        return body.replace("{{slot1}}", "[time 1]").replace("{{slot2}}", "[time 2]")
    return body.replace(LEGACY_TIMES, f"{t1} or {t2}").replace("{{slot1}}", t1).replace("{{slot2}}", t2)


def init() -> None:
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS followups (token TEXT PRIMARY KEY, email TEXT, company TEXT,
                     first_name TEXT, campaign_id TEXT, stats_id TEXT, message_id TEXT, reply_time TEXT, body TEXT,
                     they_said TEXT, report_url TEXT, status TEXT, created_at TEXT, done_at TEXT, result TEXT)""")


def _ingest(request: Request) -> None:
    import dashboard
    dashboard._ingest(request)


def _key() -> str:
    return store.get_setting("smartlead_api_key") or os.environ.get("SMARTLEAD_API_KEY", "")


def _get(tok: str) -> dict:
    with store._conn() as c:
        r = c.execute("SELECT * FROM followups WHERE token=?", (tok,)).fetchone()
    if not r:
        raise HTTPException(status_code=404)
    return dict(r)


def _set(tok: str, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    with store._conn() as c:
        c.execute(f"UPDATE followups SET {cols} WHERE token=?", (*kw.values(), tok))


@router.post("/api/followups")
async def api_create(request: Request):
    _ingest(request)
    d = await request.json()
    for k in ("email", "campaign_id", "stats_id", "body"):
        if not d.get(k):
            raise HTTPException(status_code=422, detail=f"missing {k}")
    tok = secrets.token_urlsafe(16)
    with store._conn() as c:
        c.execute("""INSERT INTO followups (token, email, company, first_name, campaign_id, stats_id, message_id, reply_time,
                     body, they_said, report_url, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (tok, d["email"].lower(), d.get("company") or "", d.get("first_name") or "", str(d["campaign_id"]),
                   str(d["stats_id"]), d.get("message_id") or "", d.get("reply_time") or "", d["body"],
                   d.get("they_said") or "", d.get("report_url") or "", "pending", store.now_iso()))
    return {"token": tok, "path": f"/f/{tok}"}


@router.get("/api/followups/{tok}")
def api_status(tok: str, request: Request):
    _ingest(request)
    f = _get(tok)
    return {k: f[k] for k in ("email", "company", "status", "done_at", "result")}


CSS = """:root{--bg:#f6f7f9;--card:#fff;--fg:#17202b;--muted:#5b6675;--line:#dfe3ea;--go:#1d7a4f;--stop:#a33a3a}
@media (prefers-color-scheme: dark){:root{--bg:#12161c;--card:#1a2029;--fg:#e7ebf0;--muted:#9aa5b3;--line:#2b3340;--go:#6fcf9c;--stop:#ef8f8f}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,sans-serif;margin:0}
.w{max-width:720px;margin:0 auto;padding:28px max(16px,env(safe-area-inset-left))}
.c{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-top:16px}
.m{color:var(--muted);font-size:14px}textarea{width:100%;box-sizing:border-box;min-height:260px;font:inherit;padding:12px;
border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
.b{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}button{font:inherit;font-weight:600;padding:10px 18px;border-radius:8px;
border:0;cursor:pointer}.s{background:var(--go);color:#fff}.k{background:transparent;color:var(--stop);border:1px solid var(--line)}"""


def _page(title: str, inner: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{html.escape(title)}</title><style>{CSS}</style><div class=w>{inner}</div>",
                        headers={"X-Robots-Tag": "noindex"})


@router.get("/f/{tok}", response_class=HTMLResponse)
def view(tok: str):
    f = _get(tok)
    head = f"<h1 style='margin:0;font-size:22px'>Follow-up to {html.escape(f['company'] or f['email'])}</h1>" \
           f"<p class=m>{html.escape(f['first_name'])} · {html.escape(f['email'])}</p>"
    if f["status"] != "pending":
        return _page("Follow-up", head + f"<div class=c><b>{html.escape(f['status'].capitalize())}</b>"
                                           f"<p class=m>{html.escape(f['done_at'] or '')} {html.escape(f['result'] or '')}</p></div>")
    said = f"<div class=c><div class=m>Their last reply</div><p>{html.escape(f['they_said'])}</p></div>" if f["they_said"] else ""
    form = (f"<form class=c method=post action='/f/{tok}/send'><div class=m>Edit if needed, then send. It goes in the same "
            f"email thread, from the same inbox, with the usual signature.</div><p><textarea id=body name=body>"
            f"{html.escape(_fill_times(f['body']))}</textarea></p><div class=b><button class=s type=submit>Send</button>"
            f"<button class=k type=submit formaction='/f/{tok}/skip'>Don't send</button></div></form>")
    return _page("Follow-up", head + said + form)


def _to_html(text: str) -> str:
    paras = [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


@router.post("/f/{tok}/send", response_class=HTMLResponse)
def send(tok: str, body: str = Form(...)):
    f = _get(tok)
    if f["status"] != "pending":
        return view(tok)
    key = _key()
    if not key:
        raise HTTPException(status_code=503, detail="Smartlead key not set on this site")
    payload = {"email_stats_id": f["stats_id"], "email_body": _to_html(body), "add_signature": True}
    if f["message_id"]:
        payload["reply_message_id"] = f["message_id"]
    if f["reply_time"]:
        payload["reply_email_time"] = f["reply_time"]
    _set(tok, status="sending")
    try:
        r = requests.post(f"{SL}/campaigns/{f['campaign_id']}/reply-email-thread", params={"api_key": key},
                          json=payload, timeout=30)
        r.raise_for_status()
    except Exception as e:
        _set(tok, status="pending", result=f"send failed: {str(e)[:150]}")
        print(json.dumps({"event": "followup_send_failed", "email": f["email"], "error": str(e)[:200]}), flush=True)
        raise HTTPException(status_code=502, detail="Smartlead did not accept the reply. Nothing was sent; try again.")
    _set(tok, status="sent", body=body, done_at=store.now_iso(), result="sent in thread")
    print(json.dumps({"event": "followup_sent", "email": f["email"], "company": f["company"]}), flush=True)
    return view(tok)


@router.post("/f/{tok}/skip", response_class=HTMLResponse)
def skip(tok: str):
    f = _get(tok)
    if f["status"] == "pending":
        _set(tok, status="skipped", done_at=store.now_iso(), result="not sent")
    return view(tok)
