"""Weekly review: a page on the client's site for one week (Monday to Friday) and the Slack message that mirrors it.

Numbers come from the site's own data: "campaign_stats" (pushed every 10 minutes by Gamic's sync) for emails, positive
replies and next week's plan, and the bookings table for calls booked. /weekly shows the current week live; when the
Friday message posts, that week is saved and stays viewable as it was sent.

Gamic's scheduler calls POST /api/briefs/weekly-summary once a week. It is a dry run unless dry_run is false, posts
through the same Slack destination as the 'Call booked' notes, at most once per week, and never when the client's
Slack is switched off.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import store
import tenant
from campaigns_view import CSS as CAMPAIGN_CSS, _chart, _n
from dashboard import COOKIE, ET, _dt, _ingest, _logo_tile, _ok, _view_hash, _viewer, e, locked, nav, page

router = APIRouter()
POSTED_KEY = "weekly_summary_posted_week"
INDEX_KEY = "weekly_review_weeks"


def week_start(now: datetime) -> date:
    d = now.astimezone(ET).date()
    return d - timedelta(days=d.weekday())


def _plural(n, one: str, many: str) -> str:
    return f"{_n(n)} {one if int(n or 0) == 1 else many}"


def _short(iso) -> str:
    return date.fromisoformat(str(iso)[:10]).strftime("%a %-d %b")


def _weekday(iso) -> str:
    return date.fromisoformat(str(iso)[:10]).strftime("%A")


def _label(c: dict) -> str:
    return " · ".join(x for x in (c.get("wave") or c.get("area"), c.get("segment")) if x)


def _stats() -> dict:
    try:
        stats = json.loads(store.get_setting("campaign_stats") or "")
    except ValueError:
        stats = None
    if not isinstance(stats, dict):
        raise HTTPException(status_code=404, detail="no campaign numbers on this site yet")
    return stats


def review(now: Optional[datetime] = None, monday: Optional[date] = None) -> dict:
    """Everything one week's review shows, from the site's current data."""
    now = now or datetime.now(timezone.utc)
    stats = _stats()
    today = now.astimezone(ET).date()
    monday = monday or week_start(now)
    end = min(monday + timedelta(days=6), today)
    in_week = lambda d: monday.isoformat() <= str(d)[:10] <= end.isoformat()
    in_last = lambda d: (monday - timedelta(days=7)).isoformat() <= str(d)[:10] < monday.isoformat()
    by_day = {str(d.get("day")): d for d in stats.get("days") or []}
    days = []
    for i in range(5):  # Monday to Friday, zeros for days with no sending
        d = (monday + timedelta(days=i)).isoformat()
        if d <= end.isoformat():
            row = by_day.get(d) or {}
            days.append({"day": d, "e1": int(row.get("e1") or 0), "followups": int(row.get("followups") or 0),
                         "positives": int(row.get("positives") or 0)})
    emails = sum(d["e1"] + d["followups"] for d in days)
    first = sum(d["e1"] for d in days)
    positives = sum(d["positives"] for d in days)
    pos_last = sum(int(d.get("positives") or 0) for d in stats.get("days") or [] if in_last(d.get("day")))
    best = max(days, key=lambda d: d["positives"], default=None)
    start_utc = datetime(monday.year, monday.month, monday.day, tzinfo=ET).astimezone(timezone.utc)
    stop_utc = start_utc + timedelta(days=7)
    booked = []
    for b in store.list_bookings():
        at = _dt(b.get("booked_at"))
        if at and start_utc <= at < stop_utc:
            call = _dt(b.get("meeting_at"))
            company = b.get("company") or b.get("lead_name") or "Unknown company"
            booked.append({"company": company, "person": b.get("lead_name") if b.get("lead_name") != company else "",
                           "call": call.astimezone(ET).date().isoformat() if call else None, "bid": b.get("bid")})
    booked.sort(key=lambda b: b["call"] or "9999")
    seen, said_yes = set(), []
    for p in stats.get("positives_recent") or []:
        if in_week(p.get("day")) and (p.get("company") or "").lower() not in seen:
            seen.add((p.get("company") or "").lower())
            said_yes.append({k: p.get(k) for k in ("company", "first_name", "day", "campaign", "step")})
    camps = stats.get("campaigns") or []
    went = []
    for c in camps:
        e1 = sum(n for d, n in (c.get("e1_days") or {}).items() if in_week(d))
        tot = sum(n for d, n in (c.get("emails_days") or {}).items() if in_week(d))
        if tot:
            went.append({"label": _label(c), "first": e1, "followups": tot - e1})
    launched = [c for c in camps if c.get("first_send") and in_week(c["first_send"])]
    nxt = []
    horizon_lo, horizon_hi = end.isoformat(), (end + timedelta(days=7)).isoformat()
    j = (stats.get("inboxes") or {}).get("joining") or {}
    if j.get("count") and horizon_lo < str(j.get("date")) <= horizon_hi:
        nxt.append({"when": _short(j["date"]), "text": f"{_n(j['count'])} new inboxes go live, more sending capacity"})
    for c in camps:
        if c.get("state") == "starting" and c.get("starts") and horizon_lo < c["starts"] <= horizon_hi:
            nxt.append({"when": _short(c["starts"]), "text": f"{c.get('wave') or c.get('area')} starts: {_n(c.get('queued'))} more owners"
                        + (f" on {c['segment']}" if c.get("segment") else "")})
    for c in camps:
        if c.get("state") == "sending" and int(c.get("queued") or 0):
            nxt.append({"when": "All week", "text": f"{_label(c)}: the last {_n(c['queued'])} first emails go out"})
    caller = tenant.get("caller") or "there"
    if positives == 0:
        note = f"A quieter week for replies, {caller}."
    elif pos_last and positives > pos_last:
        note = f"Big week, {caller}. {_plural(positives, 'owner', 'owners')} asked to talk, up from {_n(pos_last)} last week"
    else:
        note = f"Good week, {caller}. {_plural(positives, 'owner', 'owners')} asked to talk"
    if positives:
        note += f", and {_plural(len(booked), 'call was', 'calls were')} booked for you." if booked else "."
    elif booked:
        note += f" {_plural(len(booked), 'call was', 'calls were')} booked for you."
    if best and best["positives"] >= 3:
        same = [c for c in launched if c.get("first_send") == best["day"]]
        note += (f" {_weekday(best['day'])} was the standout: {_n(best['positives'])} owners said yes in one day"
                 + (f", the day {same[0].get('wave') or 'a new list'} went out for the first time." if same else "."))
    elif launched:
        note += f" {launched[0].get('wave') or 'A new list'} went out for the first time on {_weekday(launched[0]['first_send'])}."
    return {"week": monday.isoformat(), "week_label": f"Week of {monday.strftime('%-d %B')}", "through": end.isoformat(),
            "generated_at": now.isoformat(timespec="seconds"), "note": note, "days": days, "emails": emails, "first_emails": first,
            "positives": positives, "positives_last_week": pos_last, "booked": booked, "said_yes": said_yes,
            "went_out": went, "next_week": nxt}


def _names(items: list, limit: int) -> str:
    if len(items) <= limit:
        return ", ".join(items[:-1]) + (" and " + items[-1] if len(items) > 1 else (items[0] if items else ""))
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


def slack_payload(r: dict, link: str) -> dict:
    """The Slack message: same note, numbers and next week as the page, with a link to the full review."""
    last = f"  ({_n(r['positives_last_week'])} last week)" if r.get("positives_last_week") else ""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Weekly review · {r['week_label'].replace('Week of', 'week of')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": r["note"]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Positive replies*\n{_n(r['positives'])}{last}"},
            {"type": "mrkdwn", "text": f"*Calls booked*\n{_n(len(r['booked']))}"},
            {"type": "mrkdwn", "text": f"*Emails sent*\n{_n(r['emails'])}"},
            {"type": "mrkdwn", "text": f"*New owners reached*\n{_n(r['first_emails'])}"}]},
        {"type": "divider"},
    ]
    if r["said_yes"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": "*Who said yes*\n" + _names([p["company"] for p in r["said_yes"]], 12)}})
    if r["booked"]:
        rows = [" · ".join(x for x in (b["company"], b["person"], _short(b["call"]) if b["call"] else "") if x) for b in r["booked"][:8]]
        if len(r["booked"]) > 8:
            rows.append(f"and {len(r['booked']) - 8} more")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Calls booked*\n" + "\n".join(rows)}})
    if r["next_week"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Next week*\n" + "\n".join(
            f"• *{n['when']}*  {n['text']}" for n in r["next_week"])}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<{link}|Open the full weekly review>"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Have a great weekend. Ben and the Gamic team"}]})
    for b in blocks:
        if b.get("text", {}).get("type") == "mrkdwn":
            b["text"]["text"] = b["text"]["text"][:2900]
    firm = tenant.get("firm_short") or tenant.get("firm") or ""
    return {"text": f"{firm} weekly review: {_n(r['positives'])} positive replies, {_n(len(r['booked']))} calls booked, "
                    f"{_n(r['emails'])} emails sent", "blocks": blocks, "unfurl_links": False}


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


def _week_link(base: str, week: str) -> str:
    return f"{base}{'&' if '?' in base else '?'}week={quote(week)}"


def _saved_weeks() -> list:
    try:
        return [w for w in json.loads(store.get_setting(INDEX_KEY) or "[]") if isinstance(w, str)]
    except ValueError:
        return []


def _save(r: dict) -> None:
    store.set_setting(f"weekly_review:{r['week']}", json.dumps(r))
    weeks = sorted(set(_saved_weeks()) | {r["week"]}, reverse=True)[:26]
    store.set_setting(INDEX_KEY, json.dumps(weeks))


@router.post("/api/briefs/weekly-summary")
async def weekly_summary(request: Request):
    """Gamic-only (ingest key). Body: {"link": review page URL, "dry_run": bool, "force": bool}."""
    _ingest(request)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    r = review()
    base = data.get("link") or (os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/weekly")
    payload = slack_payload(r, _week_link(base, r["week"]))
    out = {"week": r["week"], "positives": r["positives"], "booked": len(r["booked"]), "emails": r["emails"],
           "first_emails": r["first_emails"], "payload": payload}
    if data.get("dry_run", True):
        return {"posted": False, "dry_run": True, **out}
    if not tenant.get("slack_enabled"):
        raise HTTPException(status_code=409, detail="Slack is switched off for this client")
    if store.get_setting(POSTED_KEY) == r["week"] and not data.get("force"):
        return {"posted": False, "skipped": "already posted this week", **out}
    _save(r)
    res = _post(payload)
    store.set_setting(POSTED_KEY, r["week"])
    print(json.dumps({"event": "weekly_review_posted", "week": r["week"], "positives": r["positives"],
                      "booked": len(r["booked"]), "emails": r["emails"]}), flush=True)
    return {"posted": True, **res, **{k: v for k, v in out.items() if k != "payload"}}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE_CSS = """
.weekpills{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}
.notecard{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px 22px;box-shadow:var(--shadow);margin-bottom:16px}
.notecard p{font-size:18px;line-height:1.55;margin:0 0 10px}
.notecard .sig{font-size:13px;color:var(--muted)}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.rows{list-style:none;margin:0;padding:0}
.rows li{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line)}
.rows li:last-child{border-bottom:0}.rows .who{font-weight:600}.rows small{color:var(--muted)}
.rows .r{text-align:right;white-space:nowrap}
.mini{height:8px;border-radius:999px;background:var(--soft);overflow:hidden;display:flex;width:160px;flex:none}
.mini span{display:block;height:100%}.mini .a{background:var(--accent)}.mini .b{background:var(--accent);opacity:.32}
.when-chip{display:inline-block;min-width:96px;font-size:12.5px;font-weight:700;color:var(--accent);background:var(--chip);
padding:4px 10px;border-radius:999px;text-align:center;flex:none}
@media (max-width:900px){.two{grid-template-columns:1fr}.mini{width:100px}}
"""


def _render(r: dict, weeks: list, live: bool) -> str:
    last = f"{_n(r['positives_last_week'])} last week" if r.get("positives_last_week") else "this week"
    kpis = (f'<div class="kpis">'
            f'<div class="kpi lead"><div class="num">{_n(r["positives"])}</div><div class="lbl">Positive replies</div><div class="hint">{e(last)}</div></div>'
            f'<div class="kpi"><div class="num">{_n(len(r["booked"]))}</div><div class="lbl">Calls booked</div><div class="hint">on your calendar this week</div></div>'
            f'<div class="kpi"><div class="num">{_n(r["emails"])}</div><div class="lbl">Emails sent</div><div class="hint">first emails and follow-ups</div></div>'
            f'<div class="kpi"><div class="num">{_n(r["first_emails"])}</div><div class="lbl">New owners reached</div><div class="hint">first email this week</div></div></div>')
    yes = "".join(f'<li><div><div class="who">{e(p["company"])}</div><small>{e(p.get("first_name") or "")}'
                  f'{" · " if p.get("first_name") else ""}{e(p.get("campaign") or "")}</small></div>'
                  f'<div class="r"><small>{e(_short(p["day"]))}</small></div></li>' for p in r["said_yes"]) or "<li><small>None yet this week.</small></li>"
    call_rows = []
    for b in r["booked"]:
        name = f"<a href='/briefs/{e(b['bid'])}'>{e(b['company'])}</a>" if b.get("bid") else e(b["company"])
        when = ("call " + _short(b["call"])) if b.get("call") else "time to confirm"
        call_rows.append(f'<li><div><div class="who">{name}</div><small>{e(b.get("person") or "")}</small></div>'
                         f'<div class="r"><small>{e(when)}</small></div></li>')
    calls = "".join(call_rows) or "<li><small>None yet this week.</small></li>"
    top = max([w["first"] + w["followups"] for w in r["went_out"]] or [1])
    went = "".join(f'<li><div><div class="who">{e(w["label"])}</div><small>{_n(w["first"])} first emails · {_n(w["followups"])} follow-ups</small></div>'
                   f'<div class="mini"><span class="a" style="width:{100 * w["first"] / top:.0f}%"></span>'
                   f'<span class="b" style="width:{100 * w["followups"] / top:.0f}%"></span></div></li>' for w in r["went_out"])
    nxt = "".join(f'<li><div style="display:flex;gap:14px;align-items:center"><span class="when-chip">{e(n["when"])}</span>'
                  f'<span>{e(n["text"])}</span></div></li>' for n in r["next_week"])
    pills = "".join(f"<a class='tab{' on' if w == r['week'] else ''}' href='/weekly?week={e(w)}'>"
                    f"{'This week' if i == 0 and w == weeks[0] else 'Week of ' + date.fromisoformat(w).strftime('%-d %b')}</a>"
                    for i, w in enumerate(weeks))
    status = (f"Live, updating through the week · as of {e(_short(r['through']))}" if live
              else f"As sent on {e(_short(r['through']))}")
    parts = [
        f'{nav("weekly")}<header class="top"><div class="brand">{_logo_tile()}<div><h1>Weekly review</h1>'
        f'<div class="sub">{e(tenant.get("firm"))} · {e(r["week_label"])}</div></div></div></header>',
        f'<div class="weekpills">{pills}</div>' if len(weeks) > 1 else "",
        f'<div class="notecard"><p>{e(r["note"])}</p><div class="sig">Ben and the Gamic team · {status}</div></div>',
        kpis,
        '<div class="sect"><h2>Day by day</h2></div>', _chart(r["days"]),
        '<div class="two"><div><div class="sect"><h2>Who said yes</h2>'
        f'<small>{_plural(len(r["said_yes"]), "owner", "owners")}</small></div><div class="card"><ul class="rows">{yes}</ul></div></div>'
        '<div><div class="sect"><h2>Calls booked</h2>'
        f'<small>{_plural(len(r["booked"]), "call", "calls")}</small></div><div class="card"><ul class="rows">{calls}</ul></div></div></div>',
    ]
    if went:
        parts.append(f'<div class="sect"><h2>What went out</h2></div><div class="card"><ul class="rows">{went}</ul></div>')
    if nxt:
        parts.append(f'<div class="sect"><h2>Next week</h2></div><div class="card"><ul class="rows">{nxt}</ul></div>')
    return f"<style>{CAMPAIGN_CSS}{PAGE_CSS}</style>" + "".join(parts)


@router.get("/weekly", response_class=HTMLResponse)
def weekly_page(request: Request, key: Optional[str] = None, week: Optional[str] = None):
    if key is not None:
        if not _ok(key, _view_hash()):
            return locked()
        r = RedirectResponse("/weekly" + (f"?week={quote(week)}" if week else ""), status_code=303)
        r.set_cookie(COOKIE, key, max_age=180 * 86400, httponly=True, secure=True, samesite="lax")
        return r
    if not _viewer(request):
        return locked()
    if not store.get_setting("campaign_stats"):
        return page(f"Weekly review · {tenant.get('firm_short')}",
                    f'{nav("weekly")}<div class="card empty">The weekly review appears here once campaign numbers arrive.</div>')
    now = datetime.now(timezone.utc)
    current = week_start(now).isoformat()
    weeks = [current] + [w for w in _saved_weeks() if w != current]
    r = None
    if week and week != current:
        try:
            r = json.loads(store.get_setting(f"weekly_review:{week}") or "")
        except ValueError:
            r = None
    live = r is None
    if live:
        r = review(now)
    return page(f"Weekly review · {tenant.get('firm_short')}", _render(r, weeks, live), refresh=600 if live else 0,
                foot=f"Prepared for {tenant.get('firm')} by Gamic")
