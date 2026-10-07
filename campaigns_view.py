"""Live campaign page: every campaign sending for the client, its progress and its results.

The numbers arrive as one settings value, "campaign_stats", pushed every 10 minutes by Gamic's sync job through
the ingest API. This service never calls the sending platform itself and holds none of its keys.
Same private link and cookie as the brief pages.
"""
import json
import math
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import store
import tenant
from dashboard import COOKIE, _ok, _view_hash, _viewer, e, locked, page, _logo_tile, nav

router = APIRouter()

CSS = """
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:6px 0 18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
.kpi .num{font-size:34px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.kpi .lbl{font-size:13px;font-weight:600;margin-top:4px}
.kpi .hint{font-size:12.5px;color:var(--muted);margin-top:2px}
.kpi.lead{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.kpi.lead .hint{color:inherit;opacity:.82}
.sect{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:26px 0 10px}
.sect h2{margin:0}.sect small{color:var(--muted)}
.chart{padding:16px 18px 10px}
.chart svg{width:100%;height:auto;display:block}
.chart .b1{fill:var(--accent)}.chart .b2{fill:var(--accent);opacity:.32}
.chart .pos{fill:var(--ok)}.chart .posn{fill:#fff;font-weight:700;font-size:11px}
.chart .ax{fill:var(--muted);font-size:11px}.chart .grid{stroke:var(--line)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.cgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.camp{padding:18px 20px;display:flex;flex-direction:column;gap:12px}
.camp .head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.camp .area{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.camp .title{font-size:19px;font-weight:700;line-height:1.25;margin-top:2px}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--ok);background:var(--ok-bg);
padding:3px 10px;border-radius:999px;white-space:nowrap}
.live:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 1.6s ease-in-out infinite}
.soon{display:inline-flex;font-size:12px;font-weight:700;color:var(--accent);background:var(--chip);padding:3px 10px;border-radius:999px;white-space:nowrap}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.prog .row{display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:6px}
.prog .row b{font-size:15px}
.track{height:10px;border-radius:999px;background:var(--soft);overflow:hidden}
.track span{display:block;height:100%;border-radius:999px;background:var(--accent)}
.note{font-size:13px;color:var(--muted)}
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.step{background:var(--soft);border-radius:10px;padding:8px 10px}
.step span{display:block;font-size:11.5px;color:var(--muted)}.step b{font-size:16px}
.res{display:flex;gap:18px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:12px}
.res div span{display:block;font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.res div b{font-size:18px}.res .good b{color:var(--ok)}
.feed{list-style:none;margin:0;padding:0}
.feed li{display:flex;justify-content:space-between;gap:12px;padding:11px 18px;border-bottom:1px solid var(--line)}
.feed li:last-child{border-bottom:0}.feed .who{font-weight:600}.feed small{color:var(--muted)}
.feed .when{white-space:nowrap;text-align:right;min-width:auto}
.infra{padding:18px 20px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.infra div span{display:block;font-size:12px;color:var(--muted)}.infra div b{font-size:24px;letter-spacing:-.01em}
.updated{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.updated:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok)}
td.num,th.num{text-align:right;white-space:nowrap}
tbody.static tr{cursor:default}tbody.static tr:hover{background:none}
@media (max-width:900px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.cgrid{grid-template-columns:1fr}}
@media (max-width:700px){.kpi .num{font-size:28px}.steps{grid-template-columns:repeat(3,minmax(0,1fr))}
.fin thead{display:none}.fin tr{display:grid!important;grid-template-columns:1fr 1fr;gap:2px 10px}
.fin td.first{grid-column:1/-1;font-weight:600}td.num{text-align:left}}
"""


def _n(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


def _day(iso: Optional[str], weekday: bool = True) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    return d.strftime("%a %-d %b" if weekday else "%-d %b")


def _rel_day(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    t = datetime.now(timezone.utc).astimezone(dashboard_ct()).date()
    if d == t:
        return "today"
    if (t - d).days == 1:
        return "yesterday"
    return _day(iso)


def dashboard_ct():
    from dashboard import CT
    return CT


def _ago(iso: Optional[str]) -> str:
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "not yet"
    mins = int((datetime.now(timezone.utc) - d).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60}h ago"


def stats() -> Optional[dict]:
    try:
        d = json.loads(store.get_setting("campaign_stats") or "")
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def _chart(days: list) -> str:
    if not days:
        return ""
    W, H, top, bottom, left, right = 760, 230, 34, 28, 46, 6
    n = len(days)
    slot = (W - left - right) / n
    bw = min(46, slot * 0.58)
    peak = max((d.get("e1", 0) + d.get("followups", 0)) for d in days) or 1
    step = 10 ** max(0, int(math.log10(peak)))
    nice = math.ceil(peak / step) * step
    usable = H - top - bottom
    parts = []
    for i in range(1, 4):
        y = top + usable - usable * i / 3
        parts.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}" stroke-dasharray="3 4"/>')
        parts.append(f'<text class="ax" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{_n(nice * i / 3)}</text>')
    for i, d in enumerate(days):
        x = left + slot * i + (slot - bw) / 2
        e1, fu = d.get("e1", 0), d.get("followups", 0)
        h1 = usable * e1 / nice
        h2 = usable * fu / nice
        y1 = top + usable - h1
        y2 = y1 - h2
        tip = f"{_day(d['day'])}: {_n(e1)} first emails, {_n(fu)} follow-ups, {d.get('positives', 0)} positive replies"
        parts.append(f'<g><title>{e(tip)}</title>'
                     f'<rect class="b2" x="{x:.1f}" y="{y2:.1f}" width="{bw:.1f}" height="{max(h2, 0):.1f}" rx="3"/>'
                     f'<rect class="b1" x="{x:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{max(h1, 0):.1f}" rx="3"/></g>')
        p = d.get("positives", 0)
        if p:
            cy = max(y2 - 14, 12)
            parts.append(f'<circle class="pos" cx="{x + bw / 2:.1f}" cy="{cy:.1f}" r="11"/>'
                         f'<text class="posn" x="{x + bw / 2:.1f}" y="{cy + 4:.1f}" text-anchor="middle">{p}</text>')
        lab = date.fromisoformat(d["day"]).strftime("%a %-d")
        parts.append(f'<text class="ax" x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle">{e(lab)}</text>')
    svg = f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Emails sent and positive replies per day">{"".join(parts)}</svg>'
    legend = ('<div class="legend"><span><i style="background:var(--accent)"></i>First emails</span>'
              '<span><i style="background:var(--accent);opacity:.32"></i>Follow-ups</span>'
              '<span><i style="background:var(--ok);border-radius:50%"></i>Positive replies that day</span></div>')
    return f'<div class="card chart">{svg}{legend}</div>'


def _steps(c: dict) -> str:
    st = list(c.get("steps") or [])[:3] + [0] * max(0, 3 - len(c.get("steps") or []))
    return '<div class="steps">' + "".join(
        f'<div class="step"><span>Email {i + 1} sent</span><b>{_n(v)}</b></div>' for i, v in enumerate(st)) + "</div>"


def _card(c: dict) -> str:
    emailed, queued = int(c.get("owners_emailed") or 0), int(c.get("queued") or 0)
    planned = emailed + queued
    pct = round(100 * emailed / planned) if planned else 100
    if c["state"] == "starting":
        badge = f'<span class="soon">Starts {e(_day(c.get("starts")))}</span>' if c.get("starts") else '<span class="soon">Ready</span>'
        prog = (f'<div class="prog"><div class="row"><span>Owners ready for a first email</span><b>{_n(queued)}</b></div></div>'
                f'<div class="note">Built, checked and loaded. First emails go out from {e(_day(c.get("starts")) or "launch")}.</div>')
        res = ""
    else:
        badge = '<span class="live">Sending</span>'
        if queued:
            left = int(c.get("days_left") or 0)
            when = (f"about {left} sending day{'s' if left != 1 else ''} left" if left else "")
            note = f"{_n(queued)} owners still to get a first email" + (f" · {when}" if when else "")
        else:
            note = f"All first emails sent · follow-ups going out to {_n(c.get('in_progress'))} owners"
        prog = (f'<div class="prog"><div class="row"><span>First emails sent</span><b>{_n(emailed)} of {_n(planned)}</b></div>'
                f'<div class="track"><span style="width:{pct}%"></span></div></div><div class="note">{e(note)}</div>{_steps(c)}')
        rate = f"1 in {_n(c['rate_one_in'])}" if c.get("rate_one_in") else "-"
        res = (f'<div class="res"><div class="good"><span>Positive replies</span><b>{_n(c.get("positives"))}</b></div>'
               f'<div><span>Owners per positive</span><b>{rate}</b></div>'
               f'<div><span>Started</span><b>{e(_day(c.get("first_send"), weekday=False))}</b></div></div>')
    seg = f'<div class="chips"><span class="chip">{e(c.get("segment"))}</span></div>' if c.get("segment") else ""
    return (f'<div class="card camp"><div class="head"><div><div class="area">{e(c.get("area"))}</div>'
            f'<div class="title">{e(c.get("wave") or c.get("area"))}</div>{seg}</div>{badge}</div>{prog}{res}</div>')


def _finished(camps: list) -> str:
    rows, groups = [], {}
    for c in camps:
        if c.get("group"):
            g = groups.setdefault(c["group"], {"name": c["group"], "n": 0, "owners": 0, "pos": 0, "first": None, "last": None})
            g["n"] += 1
            g["owners"] += int(c.get("owners_emailed") or 0)
            g["pos"] += int(c.get("positives") or 0)
            g["first"] = min(x for x in (g["first"], c.get("first_send")) if x) if (g["first"] or c.get("first_send")) else None
            g["last"] = max(x for x in (g["last"], c.get("last_send")) if x) if (g["last"] or c.get("last_send")) else None
        else:
            rows.append({"name": " · ".join(x for x in (c.get("area"), c.get("wave")) if x),
                         "sub": c.get("segment"), "owners": c.get("owners_emailed"), "pos": c.get("positives"),
                         "first": c.get("first_send"), "last": c.get("last_send")})
    for g in groups.values():
        rows.append({"name": g["name"], "sub": f"{g['n']} campaigns", "owners": g["owners"], "pos": g["pos"],
                     "first": g["first"], "last": g["last"]})
    if not rows:
        return ""
    rows.sort(key=lambda r: r.get("last") or "", reverse=True)
    trs = "".join(
        f'<tr><td class="first">{e(r["name"])}<br><small class="muted">{e(r.get("sub") or "")}</small></td>'
        f'<td class="num">{_n(r["owners"])}<br><small class="muted">owners emailed</small></td>'
        f'<td class="num">{_n(r["pos"])}<br><small class="muted">positive replies</small></td>'
        f'<td class="num">{("1 in " + _n(round(int(r["owners"]) / int(r["pos"])))) if r["pos"] else "-"}<br><small class="muted">owners per positive</small></td>'
        f'<td class="num">{e(_day(r.get("first"), False))} - {e(_day(r.get("last"), False))}<br><small class="muted">sending dates</small></td></tr>'
        for r in rows)
    return (f'<div class="sect"><h2>Finished</h2></div><div class="card fin"><table><tbody class="static">{trs}</tbody></table></div>')


def _joining(inbox: dict) -> str:
    """'+60 new inboxes join Mon 12 Oct', while that date is still ahead."""
    j = inbox.get("joining") or {}
    try:
        ahead = date.fromisoformat(str(j.get("date"))[:10]) >= datetime.now(timezone.utc).astimezone(dashboard_ct()).date()
    except ValueError:
        return ""
    if not j.get("count") or not ahead:
        return ""
    return (f'<div style="grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px"><span>Joining next</span>'
            f'<b>+{_n(j["count"])} inboxes</b> <small class="muted">from {e(_day(j.get("date")))}</small></div>')


def _foot() -> str:
    return f"Prepared for {tenant.get('firm')} by Gamic"


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, key: Optional[str] = None):
    if key is not None:
        if not _ok(key, _view_hash()):
            return locked()
        r = RedirectResponse("/campaigns", status_code=303)
        r.set_cookie(COOKIE, key, max_age=180 * 86400, httponly=True, secure=True, samesite="lax")
        return r
    if not _viewer(request):
        return locked()
    d = stats()
    head = (f'{nav("campaigns")}<header class="top"><div class="brand">{_logo_tile()}<div><h1>Campaigns</h1>'
            f'<div class="sub">{e(tenant.get("firm"))} · every campaign sending for you, live</div></div></div></header>')
    if not d:
        body = head + '<div class="card empty">Campaign numbers appear here within 10 minutes.</div>'
        return page(f"Campaigns · {tenant.get('firm_short')}", body, refresh=300, foot=_foot())
    p = d.get("program") or {}
    camps = d.get("campaigns") or []
    since = _day(p.get("since"), weekday=False)
    kpis = (
        f'<div class="kpis">'
        f'<div class="kpi lead"><div class="num">{_n(p.get("positives"))}</div><div class="lbl">Positive replies</div>'
        f'<div class="hint">{_n(p.get("positives_today"))} today · {_n(p.get("positives_this_week"))} this week</div></div>'
        f'<div class="kpi"><div class="num">{_n(p.get("owners_emailed"))}</div><div class="lbl">Owners emailed</div>'
        f'<div class="hint">since {e(since)}</div></div>'
        f'<div class="kpi"><div class="num">1 in {_n(p.get("rate_one_in"))}</div><div class="lbl">Owners per positive reply</div>'
        f'<div class="hint">across every campaign</div></div>'
        f'<div class="kpi"><div class="num">{_n(p.get("queued"))}</div><div class="lbl">Owners queued</div>'
        f'<div class="hint">checked and waiting for a first email</div></div></div>')
    # campaigns still sending first emails lead, newest first; follow-up-only campaigns after them
    sending = sorted((c for c in camps if c.get("state") == "sending"), key=lambda c: c.get("first_send") or "", reverse=True)
    sending.sort(key=lambda c: int(c.get("queued") or 0) == 0)
    starting = [c for c in camps if c.get("state") == "starting"]
    done = [c for c in camps if c.get("state") in ("done", "paused")]
    upd = f'<span class="updated">Updated {e(_ago(d.get("generated_at")))}</span>'
    parts = [head, f'<div class="sect" style="margin-top:4px"><h2>Since {e(since)}</h2>{upd}</div>', kpis,
             '<div class="sect"><h2>Last 10 sending days</h2></div>', _chart(d.get("days") or [])]
    if sending:
        parts.append(f'<div class="sect"><h2>Sending now</h2><small>{len(sending)} campaign{"s" if len(sending) != 1 else ""}</small></div>')
        parts.append('<div class="cgrid">' + "".join(_card(c) for c in sending) + "</div>")
    if starting:
        parts.append('<div class="sect"><h2>Starting next</h2></div>')
        parts.append('<div class="cgrid">' + "".join(_card(c) for c in starting) + "</div>")
    feed = d.get("recent_positives") or []
    inbox = d.get("inboxes") or {}
    lis = "".join(
        f'<li><div><div class="who">{e(f.get("company"))}</div><small>{e(f.get("first_name") or "")}'
        f'{" · " if f.get("first_name") else ""}replied to email {e(f.get("step"))}</small></div>'
        f'<div class="when"><small>{e(_rel_day(f.get("day")))}</small><br><small>{e(f.get("campaign") or "")}</small></div></li>'
        for f in feed[:10])
    infra = ""
    if inbox.get("count"):
        infra = (f'<div class="card infra"><div><span>Inboxes sending</span><b>{_n(inbox.get("count"))}</b></div>'
                 f'<div><span>Average inbox health</span><b>{inbox.get("avg_health") or "-"}%</b></div>'
                 f'<div><span>Healthy inboxes</span><b>{_n(inbox.get("healthy"))}</b></div>'
                 f'<div><span>Emails a day, full capacity</span><b>{_n(inbox.get("daily_capacity"))}</b></div>{_joining(inbox)}</div>')
    parts.append('<div class="cols"><div><div class="sect"><h2>Latest positive replies</h2></div>'
                 f'<div class="card"><ul class="feed">{lis or "<li>None yet.</li>"}</ul></div></div>'
                 f'<div><div class="sect"><h2>Sending infrastructure</h2></div>{infra}</div></div>')
    parts.append(_finished(done))
    parts.append('<p class="note" style="margin-top:18px">A positive reply is an owner who asked for a call or for more '
                 'information. Numbers refresh every 10 minutes.</p>')
    body = f"<style>{CSS}</style>" + "".join(parts)
    return page(f"Campaigns · {tenant.get('firm_short')}", body, refresh=300, foot=_foot())
