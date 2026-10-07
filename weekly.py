"""Weekly summary for the client's Slack channel: emails sent, positive replies and calls booked this week,
with a link to the live dashboard.

Numbers come from the site's own data: "campaign_stats" (pushed every 10 minutes by Gamic's sync) for emails and
positive replies, and the bookings table for calls booked. Gamic's scheduler calls the endpoint once a week; it
posts through the same Slack destination as the 'Call booked' notes, once per week at most, and only for a client
whose Slack is switched on. dry_run returns the message without posting.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Request

import store
import tenant
from dashboard import ET, _dt, _ingest

router = APIRouter()
POSTED_KEY = "weekly_summary_posted_week"


def _n(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "0"


def week_start(now: datetime) -> date:
    d = now.astimezone(ET).date()
    return d - timedelta(days=d.weekday())


def build(now: Optional[datetime] = None, link: Optional[str] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        stats = json.loads(store.get_setting("campaign_stats") or "")
    except ValueError:
        stats = None
    if not isinstance(stats, dict):
        raise HTTPException(status_code=404, detail="no campaign numbers on this site yet")
    monday = week_start(now)
    days = [d for d in stats.get("days") or [] if monday.isoformat() <= str(d.get("day")) <= now.astimezone(ET).date().isoformat()]
    emails = sum(int(d.get("e1") or 0) + int(d.get("followups") or 0) for d in days)
    first = sum(int(d.get("e1") or 0) for d in days)
    positives = sum(int(d.get("positives") or 0) for d in days)
    start_utc = datetime(monday.year, monday.month, monday.day, tzinfo=ET).astimezone(timezone.utc)
    booked = [b for b in store.list_bookings() if (_dt(b.get("booked_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= start_utc]
    prog = stats.get("program") or {}
    caller = tenant.get("caller") or "there"
    firm = tenant.get("firm_short") or tenant.get("firm") or ""
    url = link or (os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/campaigns")
    since = ""
    if prog.get("since"):
        try:
            since = date.fromisoformat(prog["since"]).strftime("%-d %B")
        except ValueError:
            since = ""
    lines = [f"Hey {caller}, here's your {firm} weekly summary.", f"_Week of {monday.strftime('%-d %B')}_", "",
             f"*Emails sent:* {_n(emails)}  ({_n(first)} owners emailed for the first time)",
             f"*Positive replies:* {_n(positives)}",
             f"*Calls booked:* {_n(len(booked))}", ""]
    if since and prog.get("positives"):
        lines.append(f"Since {since}: {_n(prog.get('positives'))} positive replies from {_n(prog.get('owners_emailed'))} owners emailed.")
    lines.append(f"Every campaign and every booked call, live: <{url}|open your dashboard>")
    text = "\n".join(lines)
    payload = {"text": f"{firm} weekly summary: {_n(emails)} emails, {_n(positives)} positive replies, {_n(len(booked))} calls booked",
               "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], "unfurl_links": False}
    return {"week": monday.isoformat(), "emails": emails, "first_emails": first, "positives": positives,
            "booked": len(booked), "booked_companies": [b.get("company") for b in booked], "text": text, "payload": payload}


def _post(payload: dict) -> dict:
    hook = store.get_setting("slack_webhook_url") or os.environ.get("SLACK_WEBHOOK_URL", "")
    channel = store.get_setting("slack_channel_id") or os.environ.get("SLACK_CHANNEL_ID", "")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if hook:
        r = requests.post(hook, json=payload, timeout=10)
        r.raise_for_status()
        return {"via": "webhook", "status": r.status_code}
    if token and channel:
        r = requests.post("https://slack.com/api/chat.postMessage", timeout=10,
                          headers={"Authorization": f"Bearer {token}"}, json={"channel": channel, **payload})
        if not r.json().get("ok"):
            raise RuntimeError(f"slack chat.postMessage failed: {r.text[:200]}")
        return {"via": "bot"}
    raise RuntimeError("no Slack destination configured")


@router.post("/api/briefs/weekly-summary")
async def weekly_summary(request: Request):
    """Gamic-only (ingest key). Body: {"link": dashboard URL, "dry_run": bool, "force": bool}."""
    _ingest(request)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    s = build(link=(data.get("link") or None))
    out = {k: v for k, v in s.items() if k != "payload"}
    if data.get("dry_run", True):
        return {"posted": False, "dry_run": True, **out}
    if not tenant.get("slack_enabled"):
        raise HTTPException(status_code=409, detail="Slack is switched off for this client")
    if store.get_setting(POSTED_KEY) == s["week"] and not data.get("force"):
        return {"posted": False, "skipped": "already posted this week", **out}
    res = _post(s["payload"])
    store.set_setting(POSTED_KEY, s["week"])
    print(json.dumps({"event": "weekly_summary_posted", "week": s["week"], "emails": s["emails"],
                      "positives": s["positives"], "booked": s["booked"]}), flush=True)
    return {"posted": True, **res, **out}
