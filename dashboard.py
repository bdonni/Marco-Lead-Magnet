"""A client's pre-call brief site: every booked call, when it is, the email thread and the brief.

Access is a private link (?key=...). Only the SHA-256 of each key is in this public repo; the keys
themselves live with Gamic. DASHBOARD_KEY_SHA256 / INGEST_KEY_SHA256 env vars override the defaults.
"""
import hashlib
import hmac
import html
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

import store
import tenant
import briefview
import calendar_sync
from bookings import normalize_thread

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_CARRARA_VIEW_KEY_SHA256 = "7f274d0b11225490c338020f316135d59b70619fed2fe9f60989a93d0d2f6a99"
VIEW_KEY_SHA256 = os.environ.get("DASHBOARD_KEY_SHA256", _CARRARA_VIEW_KEY_SHA256 if tenant.TENANT == "carrara" else "")


def _view_hash() -> str:
    return store.get_setting("view_key_sha256") or VIEW_KEY_SHA256
INGEST_KEY_SHA256 = os.environ.get("INGEST_KEY_SHA256",
                                   "81236eb1d08cfa7fa5affa84909706f9ba478294ee527669462c5f23a3092602")
COOKIE = "mb_key"
CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
TZ_CHOICES = {"ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "PT": "America/Los_Angeles"}

router = APIRouter()
_hooks = {"render_pdf": None, "extract_meeting": None}


def configure(render_pdf: Callable = None, extract_meeting: Callable = None):
    if render_pdf:
        _hooks["render_pdf"] = render_pdf
    if extract_meeting:
        _hooks["extract_meeting"] = extract_meeting


def _ok(key: Optional[str], want: str) -> bool:
    if not key or not want:
        return False
    return hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), want)


def _viewer(request: Request) -> bool:
    return _ok(request.cookies.get(COOKIE), _view_hash())


def _ingest(request: Request) -> None:
    if not _ok(request.headers.get("x-ingest-key"), INGEST_KEY_SHA256):
        raise HTTPException(status_code=401, detail="bad ingest key")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_call(iso: Optional[str]) -> tuple:
    d = _dt(iso)
    if not d:
        return ("", "")
    ct, et = d.astimezone(CT), d.astimezone(ET)
    day = ct.strftime("%a %-d %b")
    return (day, f"{ct.strftime('%-I:%M %p')} CT · {et.strftime('%-I:%M %p')} ET")


def _fmt_day(iso: Optional[str]) -> str:
    d = _dt(iso)
    return d.astimezone(CT).strftime("%-d %b %Y") if d else ""


def _fmt_stamp(iso: Optional[str]) -> str:
    d = _dt(iso)
    return d.astimezone(CT).strftime("%a %-d %b, %-I:%M %p CT") if d else ""


def _rel(iso: Optional[str]) -> str:
    """'in 40 min', 'today, in 3h', 'tomorrow', 'in 5 days', 'now', 'earlier today', 'yesterday', '3 days ago'."""
    d = _dt(iso)
    if not d:
        return ""
    now = datetime.now(timezone.utc)
    secs = (d - now).total_seconds()
    days = (d.astimezone(CT).date() - now.astimezone(CT).date()).days
    if -5400 < secs < 0:
        return "now"
    if secs >= 0:
        if secs < 3600:
            return f"in {max(1, int(secs // 60))} min"
        if days == 0:
            return f"today, in {int(secs // 3600)}h"
        if days == 1:
            return "tomorrow"
        return f"in {days} days"
    if days == 0:
        return "earlier today"
    if days == -1:
        return "yesterday"
    return f"{-days} days ago"


def e(s) -> str:
    return html.escape("" if s is None else str(s))


def _para(text: Optional[str]) -> str:
    if not text:
        return ""
    blocks = [b.strip() for b in str(text).replace("\r", "").split("\n\n") if b.strip()]
    out = []
    for b in blocks:
        lines = [l.strip() for l in b.split("\n") if l.strip()]
        if lines and all(l.startswith(("•", "-", "*")) for l in lines):
            out.append("<ul>" + "".join(f"<li>{e(l.lstrip('•-* ').strip())}</li>" for l in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(e(l) for l in lines) + "</p>")
    return "".join(out)


OWNER_BADGE = {
    "verified_upstream": ("Owner verified", "ok"),
    "verified_research": ("Owner verified", "ok"),
    "unverified": ("Owner unverified", "warn"),
}


def _bucket(b: dict) -> str:
    d = _dt(b.get("meeting_at"))
    if not d:
        return "unset"
    return "upcoming" if d > datetime.now(timezone.utc) - timedelta(hours=2) else "past"


def _ordered(rows: list) -> list:
    """Upcoming calls soonest first, then bookings without a time (newest booking first), then past calls."""
    up = sorted((b for b in rows if _bucket(b) == "upcoming"), key=lambda b: b.get("meeting_at") or "")
    unset = sorted((b for b in rows if _bucket(b) == "unset"), key=lambda b: b.get("booked_at") or "", reverse=True)
    past = sorted((b for b in rows if _bucket(b) == "past"), key=lambda b: b.get("meeting_at") or "", reverse=True)
    return up + unset + past


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--ink:#14171c;--muted:#5d6672;--line:#e3e6ea;--soft:#f0f2f5;--accent:#144e83;
--accent-ink:#fff;--ok:#127a46;--ok-bg:#e6f5ec;--warn:#9a5b00;--warn-bg:#fff3dc;--bad:#b42318;--bad-bg:#fdecea;
--chip:#eef1f6;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--panel:#171a21;--ink:#e8eaee;
--muted:#9aa3ae;--line:#2a2f39;--soft:#1d212a;--accent:#8fb3e0;--accent-ink:#0b0d12;--ok:#4ade80;--ok-bg:#12261b;
--warn:#fbbf24;--warn-bg:#2a2112;--bad:#f87171;--bad-bg:#2c1515;--chip:#232834;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
header.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
h1{font-size:26px;line-height:1.2;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px;margin-top:4px}
.stats{display:flex;gap:10px;flex-wrap:wrap}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:96px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:20px;line-height:1.2}.stat span{color:var(--muted);font-size:12px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0 14px}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink);font-size:13px}
.tab.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}.tab:hover{text-decoration:none}
.search{flex:1;min-width:200px;max-width:340px;margin-left:auto;padding:8px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--ink);font:inherit}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
padding:12px 14px;border-bottom:1px solid var(--line)}
td{padding:14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}tbody tr{cursor:pointer}tbody tr:hover{background:var(--soft)}
.when{min-width:118px;white-space:nowrap}.when b{display:block}.when small,.muted{color:var(--muted)}small{font-size:12.5px}
.co{font-weight:600}.warnline{color:var(--warn);font-weight:600}.rel{display:inline-block;margin-top:2px;font-size:12px;color:var(--accent);font-weight:600}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
.badge.ok{color:var(--ok);background:var(--ok-bg)}.badge.warn{color:var(--warn);background:var(--warn-bg)}
.badge.bad{color:var(--bad);background:var(--bad-bg)}.badge.neutral{color:var(--muted);background:var(--chip)}
.empty{padding:40px;text-align:center;color:var(--muted)}
.back{display:inline-block;margin-bottom:14px;font-size:14px}
.hero{padding:22px;display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.facts-card{margin-top:14px;padding:16px 22px}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px 22px}
.fact span{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.fact b{font-weight:600;font-size:14px;overflow-wrap:anywhere}
.chip{background:var(--chip);border-radius:6px;padding:3px 9px;font-size:13px;color:var(--ink)}
.calltime{min-width:260px;background:var(--soft);border-radius:10px;padding:14px 16px}
.calltime .big{font-size:20px;font-weight:700;line-height:1.25}
.calltime form{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.calltime input,.calltime select{font:inherit;font-size:13px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;
background:var(--panel);color:var(--ink)}
.btn{white-space:nowrap;font:inherit;font-size:13px;font-weight:600;padding:6px 12px;border-radius:8px;border:1px solid var(--accent);
background:var(--accent);color:var(--accent-ink);cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent)}
.cols{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px;margin-top:18px}
.sec{padding:18px 20px}.sec+.sec{border-top:1px solid var(--line)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 8px}
.sec p{margin:0 0 10px}.sec ul,.sec ol{margin:0;padding-left:20px}.sec li{margin-bottom:7px;line-height:1.5}
.sec ul.quotes{list-style:none;padding-left:0}.sec ul.quotes li{border-left:3px solid var(--accent);padding:2px 0 2px 12px;font-style:italic}
.briefhead{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line)}
.thread{max-height:none}
.msg{padding:14px 18px;border-bottom:1px solid var(--line)}.msg:last-child{border-bottom:0}
.msg .meta{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--muted);margin-bottom:6px}
.msg .who{font-weight:700;color:var(--ink)}.msg.reply{background:var(--soft)}
.msg .body{white-space:pre-wrap;word-wrap:break-word;font-size:14px}
.foot{margin-top:26px;color:var(--muted);font-size:12.5px;text-align:center}
.brand{display:flex;gap:16px;align-items:center}
.logo-tile{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 9px;display:inline-flex;box-shadow:var(--shadow);flex:none}
.logo-tile img{height:58px;width:auto;display:block}
.brandbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
.brandbar .logo-tile img{height:40px}.brandbar .back{margin:0}
@media (max-width:860px){.cols{grid-template-columns:1fr}}
@media (max-width:700px){.wrap{padding:18px 16px 40px}thead{display:none}table,tbody,tr,td{display:block;width:100%}
tbody tr{padding:12px 14px;border-bottom:1px solid var(--line)}td{border:0;padding:3px 0}
td.col-thread{display:none}.search{max-width:none;margin-left:0}}
"""


def _calendar_status() -> str:
    ok = _dt(store.get_setting("calendar_last_ok"))
    if not store.get_setting("calendar_ics_url"):
        return f"call times not linked to {tenant.calendar_owner()} calendar yet"
    if not ok:
        return f"connecting to {tenant.calendar_owner()} calendar"
    mins = int((datetime.now(timezone.utc) - ok).total_seconds() // 60)
    return f"call times from {tenant.calendar_owner()} calendar, checked {'just now' if mins < 1 else f'{mins} min ago'}"


def _logo_tile() -> str:
    if not tenant.logo_bytes():
        return ""
    return f'<span class="logo-tile"><img src="/static/logo.png" alt="{e(tenant.get("firm"))}" height="58"></span>'


def page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<link rel="icon" type="image/png" href="/static/logo.png">
<title>{e(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<style>{CSS.replace("--accent:#144e83;", f"--accent:{tenant.get('accent')};").replace("--accent:#8fb3e0;", f"--accent:{tenant.get('accent_dark')};")}</style></head><body><div class="wrap">{body}
<div class="foot">Prepared for {e(tenant.get('firm'))} by Gamic · {e(_calendar_status())}</div></div></body></html>"""
    return HTMLResponse(doc, headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"})


def locked() -> HTMLResponse:
    body = """<div class="card empty" style="margin-top:80px">""" + _logo_tile() + """<h1 style="font-size:20px;margin:14px 0 6px">Pre-Call Briefs</h1>
<p>This page needs your private access link. Ask Gamic to resend it.</p></div>"""
    r = page("Pre-Call Briefs", body)
    r.status_code = 401
    return r


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/briefs", response_class=HTMLResponse)
def briefs_index(request: Request, key: Optional[str] = None, view: str = "upcoming"):
    if key is not None:
        if not _ok(key, _view_hash()):
            return locked()
        r = RedirectResponse("/briefs", status_code=303)
        r.set_cookie(COOKIE, key, max_age=180 * 86400, httponly=True, secure=True, samesite="lax")
        return r
    if not _viewer(request):
        return locked()

    rows = _ordered(store.list_bookings())
    counts = {k: sum(1 for b in rows if _bucket(b) == k) for k in ("upcoming", "unset", "past")}
    now = datetime.now(timezone.utc)
    week = sum(1 for b in rows if _bucket(b) == "upcoming" and _dt(b["meeting_at"]) < now + timedelta(days=7))
    view = view if view in ("upcoming", "unset", "past", "all") else "upcoming"
    shown = rows if view == "all" else [b for b in rows if _bucket(b) == view]

    trs = []
    for b in shown:
        day, hours = _fmt_call(b.get("meeting_at"))
        ct_part, _, et_part = hours.partition(" · ")
        if b.get("meeting_day_only"):
            ct_part, et_part = "time not confirmed", (b.get("meeting_text") or "")[:40]
        unconf = ("" if b.get("meeting_source") == "calendar"
                  else "<br><small class='warnline'>not confirmed</small>")
        when = (f"<b>{e(day)}</b><small>{e(ct_part)}<br>{e(et_part)}</small><br><span class='rel'>{e(_rel(b.get('meeting_at')))}</span>{unconf}"
                if day else "<span class='badge neutral'>Time not set</span>")
        br = b.get("brief")
        if br:
            label, cls = OWNER_BADGE.get(br.get("owner_status") or "", ("Brief ready", "ok"))
            brief = f"<span class='badge {cls}'>{e('Brief ready' if cls == 'ok' else label)}</span>"
        else:
            brief = "<span class='badge bad'>No brief yet</span>"
        site = (b.get("website") or "").replace("https://", "").replace("http://", "").strip("/")
        search = " ".join(str(b.get(k) or "") for k in ("company", "lead_name", "email", "website", "location")).lower()
        trs.append(f"""<tr data-s="{e(search)}" onclick="location.href='/briefs/{e(b['bid'])}'">
<td class="when">{when}</td>
<td><div class="co"><a href="/briefs/{e(b['bid'])}">{e(b.get('company') or '(company unknown)')}</a></div><small class="muted">{e(site)}</small></td>
<td>{e(b.get('lead_name') or b.get('first_name') or '')}<br><small class="muted">{e(briefview.clean_place(b.get('location')) or '')}</small></td>
<td>{e(_fmt_day(b.get('booked_at')))}</td>
<td>{brief}</td>
<td class="col-thread"><small class="muted">{int(b.get('thread_count') or 0)} emails</small></td></tr>""")

    tabs = "".join(
        f"<a class='tab{' on' if view == k else ''}' href='/briefs?view={k}'>{lbl}</a>"
        for k, lbl in (("upcoming", f"Upcoming ({counts['upcoming']})"), ("unset", f"Time not set ({counts['unset']})"),
                       ("past", f"Past ({counts['past']})"), ("all", f"All ({len(rows)})")))
    table = (f"""<div class="card"><table><thead><tr><th>Call</th><th>Company</th><th>Contact</th><th>Booked</th>
<th>Brief</th><th class="col-thread">Thread</th></tr></thead><tbody id="rows">{''.join(trs)}</tbody></table></div>"""
             if trs else "<div class='card empty'>Nothing here yet.</div>")
    body = f"""<header class="top"><div class="brand">{_logo_tile()}<div><h1>Pre-Call Briefs</h1><div class="sub">{e(tenant.get('firm'))} · every booked call, its email thread and the brief</div></div></div>
<div class="stats"><div class="stat"><b>{week}</b><span>calls next 7 days</span></div>
<div class="stat"><b>{counts['upcoming']}</b><span>upcoming</span></div><div class="stat"><b>{len(rows)}</b><span>booked in total</span></div></div></header>
<div class="bar"><div class="tabs">{tabs}</div><input class="search" id="q" placeholder="Search company, contact, state" autocomplete="off"></div>
{table}
<script>const q=document.getElementById('q');q&&q.addEventListener('input',()=>{{const v=q.value.trim().toLowerCase();
document.querySelectorAll('#rows tr').forEach(r=>{{r.style.display=!v||r.dataset.s.includes(v)?'':'none'}})}});</script>"""
    return page(f"Pre-Call Briefs · {tenant.get('firm_short')}", body)


PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL")
                   or (("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]) if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
                   or "https://marco-lead-magnet-production.up.railway.app").rstrip("/")


def share_link(b: Optional[dict]) -> str:
    """Per-booking link for Slack: opens that one booking without the site key."""
    return f"{PUBLIC_BASE_URL}/b/{b['share_token']}" if b and b.get("share_token") else f"{PUBLIC_BASE_URL}/briefs"


def render_detail(b: dict, editable: bool, pdf_url: str, back: bool) -> HTMLResponse:
    br = b.get("brief") or {}
    req = br.get("request") or {}
    a = br.get("assessment") or {}

    day, hours = _fmt_call(b.get("meeting_at"))
    if day and b.get("meeting_day_only"):
        hours = "Time not confirmed"
    if day:
        src = b.get("meeting_source")
        if src == "calendar":
            note = f"On {e(tenant.calendar_owner())} calendar: {e(b.get('meeting_text') or 'event')}"
        elif src == "manual":
            note = "Set by hand"
        elif b.get("meeting_day_only"):
            note = f"Day from the email thread, time not confirmed: “{e(b.get('meeting_quote') or b.get('meeting_text'))}”"
        else:
            note = f"From the email thread: “{e(b.get('meeting_quote') or b.get('meeting_text'))}”"
        if src != "calendar" and store.get_setting("calendar_ics_url"):
            note += f" · not on {e(tenant.calendar_owner())} calendar yet"
        call = f"<div class='big'>{e(day)}</div><div>{e(hours)}</div><small class='muted'>{e(_rel(b.get('meeting_at')))} · {note}</small>"
    else:
        call = f"<div class='big'>Time not set</div><small class='muted'>Not on {e(tenant.calendar_owner())} calendar yet. It appears here as soon as it is.</small>"
    d0 = _dt(b.get("meeting_at"))
    d_ct = d0.astimezone(CT) if d0 else None
    bid = b["bid"]
    form = "" if not editable else f"""<form method="post" action="/briefs/{e(bid)}/meeting">
<input type="date" name="date" value="{d_ct.strftime('%Y-%m-%d') if d_ct else ''}" required>
<input type="time" name="time" value="{d_ct.strftime('%H:%M') if d_ct else ''}" required>
<select name="tz">{''.join(f"<option{' selected' if k == 'CT' else ''}>{k}</option>" for k in TZ_CHOICES)}</select>
<button class="btn" type="submit">Save time</button></form>"""

    v = briefview.view(req, a, b) if br else {"facts": briefview.facts(req, b)}
    site = b.get("website") or req.get("website")
    facts_html = ""
    for k, val in v["facts"]:
        if k == "Website" and site:
            url = site if site.startswith("http") else "https://" + site
            val_html = f"<a href='{e(url)}' target='_blank' rel='noopener'>{e(val)} ↗</a>"
        else:
            val_html = e(val)
        facts_html += f"<div class='fact'><span>{e(k)}</span><b>{val_html}</b></div>"

    if br:
        label, cls = OWNER_BADGE.get(br.get("owner_status") or "", ("Owner profile", "neutral"))
        secs = []
        for key, title in briefview.SECTIONS:
            items = v.get(key) or []
            if not items:
                continue
            badge = f" <span class='badge {cls}' style='margin-left:6px'>{e(label)}</span>" if key == "owner" else ""
            tag = "ol" if key == "confirm" else "ul"
            klass = " class='quotes'" if key == "they_said" else ""
            lis = "".join(f"<li>{e(x)}</li>" for x in items)
            secs.append(f"<div class='sec'><h2>{e(title)}{badge}</h2><{tag}{klass}>{lis}</{tag}></div>")
        pdf = (f"<a class='btn' href='{e(pdf_url)}'>Download PDF</a>" if _hooks["render_pdf"] else "")
        if editable:
            pdf = (f"<span style='display:flex;gap:6px'><button class='btn ghost' type='button' "
                   f"onclick=\"navigator.clipboard.writeText('{e(share_link(b))}');this.textContent='Link copied'\">"
                   f"Copy link</button>{pdf}</span>")
        posted = " · posted to Slack" if br.get("posted_to_slack") else ""
        brief_html = f"""<div class="card"><div class="briefhead"><div><b>Pre-call brief</b><br>
<small class="muted">Generated {e(_fmt_stamp(br.get('created_at')))}{posted}</small></div>{pdf}</div>{''.join(secs)}</div>"""
    else:
        brief_html = "<div class='card empty'>The brief for this booking has not been generated yet.</div>"

    msgs = []
    for m in b.get("thread") or []:
        reply = m.get("type") == "REPLY"
        who = "Prospect" if reply else (tenant.get("sender_label") or "Us")
        msgs.append(f"""<div class="msg{' reply' if reply else ''}"><div class="meta"><span><span class="who">{who}</span> · {e(m.get('from'))}</span>
<span>{e(_fmt_stamp(m.get('time')))}</span></div>{('<div class="muted" style="font-size:13px;margin-bottom:4px">' + e(m.get('subject')) + '</div>') if m.get('subject') else ''}
<div class="body">{e(m.get('text'))}</div></div>""")
    thread_html = (f"<div class='card thread'><div class='briefhead'><b>Email thread</b><small class='muted'>{len(msgs)} emails</small></div>{''.join(msgs)}</div>"
                   if msgs else "<div class='card empty'>No emails stored for this booking yet.</div>")

    back_link = '<a class="back" href="/briefs">← All booked calls</a>' if back else "<span></span>"
    body = f"""<div class="brandbar">{back_link}{_logo_tile()}</div>
<div class="card hero"><div><h1>{e(b.get('company') or '(company unknown)')}</h1>
<div class="sub">{e(b.get('lead_name') or '')}{(' · ' + e(b.get('title'))) if b.get('title') else ''} · booked {e(_fmt_day(b.get('booked_at')))}</div>
</div>
<div class="calltime"><small class="muted">CALL</small>{call}{form}</div></div>
<div class="card facts-card"><div class="facts">{facts_html}</div></div>
<div class="cols"><div>{brief_html}</div><div>{thread_html}</div></div>"""
    return page(f"{b.get('company') or 'Booking'} · Pre-Call Brief", body)


@router.get("/briefs/{bid}", response_class=HTMLResponse)
def brief_detail(request: Request, bid: str, key: Optional[str] = None):
    if key is not None:
        if not _ok(key, _view_hash()):
            return locked()
        r = RedirectResponse(f"/briefs/{bid}", status_code=303)
        r.set_cookie(COOKIE, key, max_age=180 * 86400, httponly=True, secure=True, samesite="lax")
        return r
    if not _viewer(request):
        return locked()
    b = store.get_booking(bid)
    if not b:
        raise HTTPException(status_code=404, detail="not found")
    return render_detail(b, True, f"/briefs/{bid}/pdf", True)


@router.get("/b/{token}", response_class=HTMLResponse)
def shared_detail(request: Request, token: str):
    b = store.get_booking_by_token(token)
    if not b:
        return locked()
    viewer = _viewer(request)
    return render_detail(b, viewer, f"/b/{token}/pdf", viewer)


@router.post("/briefs/{bid}/meeting")
def set_meeting(request: Request, bid: str, date: str = Form(...), time: str = Form(...), tz: str = Form("CT")):
    if not _viewer(request):
        return locked()
    b = store.get_booking(bid)
    if not b:
        raise HTTPException(status_code=404, detail="not found")
    try:
        local = datetime.fromisoformat(f"{date}T{time}")
        at = local.replace(tzinfo=ZoneInfo(TZ_CHOICES.get(tz, "America/Chicago"))).astimezone(timezone.utc)
    except Exception:
        raise HTTPException(status_code=400, detail="bad date or time")
    store.upsert_booking({"email": b["email"], "meeting": {
        "at": at.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "text": f"{date} {time} {tz}",
        "quote": None, "source": "manual"}})
    return RedirectResponse(f"/briefs/{bid}", status_code=303)


PDF_DIR = os.path.join(os.path.dirname(store.DB_PATH), "pdf")
PDF_VERSION = "v3"  # bump when the PDF layout changes so cached copies are rebuilt


def pdf_path(brief_id) -> str:
    return os.path.join(PDF_DIR, f"{brief_id}-{PDF_VERSION}.pdf")


def _pdf_response(b: Optional[dict]) -> Response:
    """PDFs are rendered once per brief and cached on the volume, so repeat opens are instant."""
    if not b or not b.get("brief") or not _hooks["render_pdf"]:
        raise HTTPException(status_code=404, detail="no brief")
    br = b["brief"]
    path = pdf_path(br["id"])
    try:
        with open(path, "rb") as fh:
            pdf = fh.read()
    except OSError:
        pdf = _hooks["render_pdf"](br.get("request") or {}, br.get("assessment") or {}, br.get("owner_status") or "")
        try:
            os.makedirs(PDF_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as fh:
                fh.write(pdf)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    name = "".join(ch for ch in (b.get("company") or "brief") if ch.isalnum() or ch in " -_").strip().replace(" ", "_")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Pre-Call_Brief_{name}.pdf"',
                             "Cache-Control": "private, max-age=3600"})


@router.get("/briefs/{bid}/pdf")
def brief_pdf(request: Request, bid: str):
    if not _viewer(request):
        return locked()
    return _pdf_response(store.get_booking(bid))


@router.get("/b/{token}/pdf")
def shared_pdf(token: str):
    return _pdf_response(store.get_booking_by_token(token))


# ---------------------------------------------------------------------------
# Ingest (Gamic's sync and backfill)
# ---------------------------------------------------------------------------

def _maybe_extract(email: str):
    fn = _hooks["extract_meeting"]
    if not fn:
        return
    b = store.get_booking(store.bid_for(email))
    if not b or b.get("meeting_source") == "manual" or not b.get("thread"):
        return
    m = fn(b["thread"], b.get("location"))
    if m:
        store.upsert_booking({"email": email, "meeting": m})


def ingest_booking(d: dict, extract: bool = True) -> Optional[str]:
    if isinstance(d.get("history"), list) and not isinstance(d.get("thread"), list):
        d = {**d, "thread": normalize_thread(d["history"])}
    before = store.get_booking(store.bid_for(d.get("email") or "")) if d.get("email") else None
    bid = store.upsert_booking(d)
    after = store.get_booking(bid) if bid else None
    if extract and after and after.get("thread") and (
            not before or before.get("thread_hash") != after.get("thread_hash") or not after.get("meeting_at")):
        threading.Thread(target=_maybe_extract, args=(after["email"],), daemon=True).start()
    return bid


@router.post("/api/briefs/ingest")
async def api_ingest(request: Request):
    _ingest(request)
    data = await request.json()
    out = {"bookings": 0, "briefs": 0}
    if isinstance(data.get("campaigns"), list):
        out["campaigns"] = store.set_campaigns(data["campaigns"])
    if isinstance(data.get("purge"), list):
        out["purged"] = store.purge(data["purge"])
    if isinstance(data.get("settings"), dict):
        for k, v in data["settings"].items():
            if k in ("calendar_ics_url", "tenant_config", "tenant_logo_b64", "view_key_sha256", "slack_webhook_url",
                     "slack_channel_id", "tenant_routes"):
                store.set_setting(k, (json.dumps(v) if isinstance(v, (dict, list)) else v) or None)
                out.setdefault("settings", []).append(k)
    if data.get("calendar_sync"):
        out["calendar"] = calendar_sync.sync_once()
    for d in data.get("bookings") or []:
        if ingest_booking(d, extract=bool(data.get("extract", True))):
            out["bookings"] += 1
    for br in data.get("briefs") or []:
        store.save_brief(br.get("email"), br.get("company"), br.get("lead_name"), br.get("source") or "import",
                         br.get("owner_status"), br.get("request") or {}, br.get("assessment") or {},
                         bool(br.get("posted")), br.get("created_at"))
        out["briefs"] += 1
    return out


@router.get("/api/briefs/state")
def api_state(request: Request):
    _ingest(request)
    return {"bookings": store.state()}


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {"carrara-logo.png", "carrara-icon.png"}


@router.get("/static/logo.png")
def tenant_logo():
    b = tenant.logo_bytes()
    if not b:
        raise HTTPException(status_code=404, detail="no logo")
    return Response(b, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/static/{name}")
def static_file(name: str):
    if name not in STATIC_FILES or tenant.TENANT != "carrara":
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(os.path.join(STATIC_DIR, name), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/api/briefs/calendar")
def api_calendar(request: Request):
    """Gamic-only: upcoming calendar events that match no booking yet (attendee emails only)."""
    _ingest(request)
    try:
        unmatched = json.loads(store.get_setting("calendar_unmatched") or "[]")
    except ValueError:
        unmatched = []
    return {"connected": bool(store.get_setting("calendar_ics_url")), "last_ok": store.get_setting("calendar_last_ok"),
            "last_error": store.get_setting("calendar_last_error"), "unmatched": unmatched}
