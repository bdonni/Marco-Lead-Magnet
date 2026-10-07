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


def _plural(n: int, one: str, many: str) -> str:
    return f"{_n(n)} {one if int(n) == 1 else many}"


def _names(items: list, limit: int) -> str:
    seen, out = set(), []
    for x in items:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    if len(out) <= limit:
        return ", ".join(out[:-1]) + (" and " + out[-1] if len(out) > 1 else (out[0] if out else ""))
    return ", ".join(out[:limit]) + f" and {len(out) - limit} more"


def build(now: Optional[datetime] = None, link: Optional[str] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        stats = json.loads(store.get_setting("campaign_stats") or "")
    except ValueError:
        stats = None
    if not isinstance(stats, dict):
        raise HTTPException(status_code=404, detail="no campaign numbers on this site yet")
    today = now.astimezone(ET).date()
    monday = week_start(now)
    last_monday = monday - timedelta(days=7)
    in_week = lambda d: monday.isoformat() <= str(d)[:10] <= today.isoformat()
    in_last = lambda d: last_monday.isoformat() <= str(d)[:10] < monday.isoformat()
    days = stats.get("days") or []
    week = [d for d in days if in_week(d.get("day"))]
    emails = sum(int(d.get("e1") or 0) + int(d.get("followups") or 0) for d in week)
    first = sum(int(d.get("e1") or 0) for d in week)
    positives = sum(int(d.get("positives") or 0) for d in week)
    pos_last = sum(int(d.get("positives") or 0) for d in days if in_last(d.get("day")))
    best = max(week, key=lambda d: int(d.get("positives") or 0), default=None)
    start_utc = datetime(monday.year, monday.month, monday.day, tzinfo=ET).astimezone(timezone.utc)
    booked = sorted((b for b in store.list_bookings()
                     if (_dt(b.get("booked_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= start_utc),
                    key=lambda b: b.get("meeting_at") or b.get("booked_at") or "")
    camps = stats.get("campaigns") or []
    prog = stats.get("program") or {}
    caller = tenant.get("caller") or "there"
    url = link or (os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/campaigns")
    wd = lambda iso: date.fromisoformat(str(iso)[:10]).strftime("%A")
    short = lambda iso: date.fromisoformat(str(iso)[:10]).strftime("%a %-d %b")
    label = lambda c: " · ".join(x for x in (c.get("wave") or c.get("area"), c.get("segment")) if x)

    # opening note, built only from this week's numbers
    launched = [c for c in camps if c.get("first_send") and in_week(c["first_send"])]
    if positives == 0:
        intro = "A quieter week for replies."
    elif pos_last and positives > pos_last:
        intro = f"Big week, {caller}. {_plural(positives, 'owner', 'owners')} asked to talk, up from {_n(pos_last)} last week."
    else:
        intro = f"Good week, {caller}. {_plural(positives, 'owner', 'owners')} asked to talk."
    if booked:
        intro += f" {_plural(len(booked), 'call was', 'calls were')} booked for you."
    if best and int(best.get("positives") or 0) >= 3:
        lead = f" {wd(best['day'])} was the standout, with {_n(best['positives'])} positive replies in one day"
        same = [c for c in launched if c.get("first_send") == best["day"]]
        intro += lead + (f", the day {same[0].get('wave') or 'a new list'} went out for the first time." if same else ".")
    elif launched:
        intro += f" {launched[0].get('wave') or 'A new list'} went out for the first time on {wd(launched[0]['first_send'])}."

    lines = [f"Hey {caller}, here's how your week went.", "", intro, "",
             "*This week in numbers*",
             f"• {_n(emails)} emails sent, {_n(first)} of them first emails to new owners",
             f"• {_plural(positives, 'positive reply', 'positive replies')}",
             f"• {_plural(len(booked), 'call booked', 'calls booked')}"]
    said_yes = [p.get("company") for p in stats.get("positives_recent") or [] if in_week(p.get("day"))]
    if said_yes:
        lines += ["", "*Who said yes*", _names(said_yes, 20)]
    if booked:
        lines += ["", "*Calls booked this week*"]
        for b in booked[:10]:
            who = b.get("company") or b.get("lead_name") or "Unknown company"
            person = b.get("lead_name") if b.get("lead_name") and b.get("lead_name") != who else ""
            when = f"call {short(_dt(b['meeting_at']).astimezone(ET).date().isoformat())}" if _dt(b.get("meeting_at")) else ""
            lines.append("• " + " · ".join(x for x in (who, person, when) if x))
        if len(booked) > 10:
            lines.append(f"• and {len(booked) - 10} more on your dashboard")
    went = []
    for c in camps:
        e1 = sum(n for d, n in (c.get("e1_days") or {}).items() if in_week(d))
        tot = sum(n for d, n in (c.get("emails_days") or {}).items() if in_week(d))
        if tot:
            parts = [f"{_n(e1)} first emails"] if e1 else []
            if tot - e1:
                parts.append(f"{_n(tot - e1)} follow-ups")
            went.append(f"• {label(c)}: {', '.join(parts)}")
    if went:
        lines += ["", "*What went out*"] + went
    nxt, horizon = [], (today + timedelta(days=7)).isoformat()
    j = (stats.get("inboxes") or {}).get("joining") or {}
    if j.get("count") and today.isoformat() < str(j.get("date")) <= horizon:
        nxt.append(f"• {short(j['date'])}: {_n(j['count'])} new inboxes go live")
    for c in camps:
        if c.get("state") == "starting" and c.get("starts") and today.isoformat() < c["starts"] <= horizon:
            nxt.append(f"• {short(c['starts'])}: {label(c)} starts for {_n(c.get('queued'))} owners")
    for c in camps:
        if c.get("state") == "sending" and int(c.get("queued") or 0):
            nxt.append(f"• {label(c)}: {_n(c['queued'])} owners still to get a first email")
    if nxt:
        lines += ["", "*Next week*"] + nxt
    lines.append("")
    if prog.get("since") and prog.get("positives"):
        since = date.fromisoformat(prog["since"]).strftime("%-d %B")
        lines.append(f"Since {since}: {_n(prog['positives'])} positive replies from {_n(prog.get('owners_emailed'))} owners emailed.")
    lines += [f"Every campaign and every booked call, live: <{url}|open your dashboard>", "",
              "Have a great weekend,", "Ben and the Gamic team"]
    text = "\n".join(lines)
    firm = tenant.get("firm_short") or tenant.get("firm") or ""
    payload = {"text": f"{firm} weekly summary: {_n(emails)} emails, {_n(positives)} positive replies, {_n(len(booked))} calls booked",
               "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": part[:2900]}}
                          for part in text.split("\n\n") if part.strip()], "unfurl_links": False}
    return {"week": monday.isoformat(), "emails": emails, "first_emails": first, "positives": positives, "positives_last_week": pos_last,
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
