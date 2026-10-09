"""Lead pool: positive replies from a client's chosen campaigns, claimed first-come by its Managing Directors.

Built for The Vant Group (four MDs share the sell-side replies). Off for every client unless its tenant config has
"lead_pool_enabled": true; then a background loop reads Smartlead every 10 minutes, new positive replies land in
pool_leads with a brief queued for each (never posted to Slack), and the /briefs dashboard shows them at the top
("Leads to claim", then "Claimed") for the MDs to claim. A pool lead's brief page carries its claim status too.

Tenant config keys: lead_pool_enabled, lead_pool_campaign_patterns (case-insensitive substrings of the campaign
name; parent campaigns only), lead_pool_since (ISO time; older replies are ignored), md_roster (display names).
The Smartlead key is the site setting smartlead_api_key (set through the Gamic-only ingest API), falling back to
env SMARTLEAD_API_KEY. It is never logged, echoed or shown.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote

import requests
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import briefview
import dashboard
import store
import tenant
from bookings import normalize_thread
from dashboard import e

BASE = "https://server.smartlead.ai/api/v1"
HEADERS = {"User-Agent": "curl/8.4.0"}  # Cloudflare in front of Smartlead blocks python's default UA
SYNC_EVERY = 600
MIN_GAP = 0.6  # seconds between Smartlead calls from this loop (account rate limit)
PAGE = 100
EXCERPT_CHARS = 600
POSITIVE = {"interested", "meeting request", "information request", "booked", "positive response but no reply"}

http_get = requests.get  # tests swap this out; nothing here touches the network in tests
_sleep = time.sleep
on_new_lead: Optional[Callable] = None  # set by main: build the brief for a new pool lead, Slack off

_throttle = {"last": 0.0}
_throttle_lock = threading.Lock()
_start_lock = threading.Lock()
_started = {"thread": None}

router = APIRouter()


class SmartleadError(Exception):
    pass


# ---------------------------------------------------------------------------
# Smartlead reads
# ---------------------------------------------------------------------------

def api_key() -> str:
    return store.get_setting("smartlead_api_key") or os.environ.get("SMARTLEAD_API_KEY", "")


def scrub(text: str) -> str:
    """Error text with any API key taken out, so nothing logged or stored carries it."""
    s = re.sub(r"api_key=[^&\s'\"]+", "api_key=***", str(text or ""))
    k = api_key()
    return s.replace(k, "***") if k else s


def _get(path: str, params: Optional[dict] = None):
    key = api_key()
    if not key:
        raise SmartleadError("no Smartlead key")
    q = {"api_key": key, **(params or {})}
    for attempt in range(5):
        with _throttle_lock:
            wait = _throttle["last"] + MIN_GAP - time.monotonic()
            if wait > 0:
                _sleep(wait)
            _throttle["last"] = time.monotonic()
        try:
            r = http_get(BASE + path, params=q, headers=HEADERS, timeout=30)
        except Exception as ex:  # the exception text can hold the full URL, key included: keep only its type
            raise SmartleadError(f"{type(ex).__name__} on {path}") from None
        if r.status_code == 429:
            _sleep(min(60, 5 * 2 ** attempt))
            continue
        if r.status_code != 200:
            raise SmartleadError(f"HTTP {r.status_code} on {path}")
        try:
            return r.json()
        except ValueError:
            raise SmartleadError(f"bad JSON on {path}") from None
    raise SmartleadError(f"rate limited on {path}")


def matching_campaigns(rows, patterns: list) -> list:
    """Parent campaigns whose name holds any pattern (case-insensitive). Subsequences are skipped."""
    pats = [p.lower() for p in patterns or [] if p]
    out = []
    for c in rows or []:
        if not isinstance(c, dict) or c.get("parent_campaign_id") or not c.get("id"):
            continue
        name = (c.get("name") or "").lower()
        if pats and any(p in name for p in pats):
            out.append(c)
    return out


def is_positive(category: Optional[str]) -> bool:
    return (category or "").strip().lower() in POSITIVE


def _when(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if d else None


def replied_rows(campaign_id) -> list:
    """Every replied row of one campaign, all pages. Any failed page raises, so a half-read campaign stores nothing."""
    rows, offset = [], 0
    while True:
        data = _get(f"/campaigns/{campaign_id}/statistics",
                    {"offset": offset, "limit": PAGE, "email_status": "replied"})
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise SmartleadError(f"unexpected statistics reply for campaign {campaign_id}")
        got = data["data"]
        rows += [r for r in got if isinstance(r, dict)]
        offset += PAGE
        try:
            total = int(data.get("total_stats") or 0)
        except (TypeError, ValueError):
            total = 0
        if len(got) < PAGE or (total and offset >= total) or offset >= 100000:
            return rows


def latest_by_email(rows: list, since: Optional[datetime] = None) -> dict:
    """email -> that lead's newest replied row, ignoring replies before `since`."""
    out = {}
    for r in rows:
        email = (r.get("lead_email") or "").strip().lower()
        t = _when(r.get("reply_time"))
        if "@" not in email or t is None or (since and t < since):
            continue
        cur = out.get(email)
        if cur is None or t > _when(cur.get("reply_time")):
            out[email] = r
    return out


def _cf(cf: dict, *names) -> str:
    low = {str(k).lower(): v for k, v in (cf or {}).items()}
    for n in names:
        v = low.get(n)
        if v not in (None, "") and str(v).strip():
            return str(v).strip()
    return ""


def excerpt(thread: list, limit: int = EXCERPT_CHARS) -> str:
    """The prospect's first reply as text, cut at a word near `limit`."""
    text = next((m.get("text") or "" for m in thread if m.get("type") == "REPLY"), "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return cut + "…"


def pool_record(email: str, row: dict, camp: dict, rec: dict, thread: list) -> dict:
    cf = rec.get("custom_fields") or {}
    first = (rec.get("first_name") or "").strip()
    last = (rec.get("last_name") or "").strip()
    if not first and not last and row.get("lead_name"):
        first, _, last = str(row["lead_name"]).strip().partition(" ")
    loc = (rec.get("location") or "").strip() or ", ".join(
        x for x in (_cf(cf, "city"), _cf(cf, "state")) if x)
    return {
        "email": email, "first_name": first, "last_name": last.strip(),
        "company": _cf(cf, "clean_company") or (rec.get("company_name") or "").strip(),
        "title": _cf(cf, "title", "job_title", "jobtitle", "position") or (rec.get("title") or "").strip(),
        "website": (rec.get("website") or rec.get("company_url") or _cf(cf, "website") or "").strip(),
        "location": loc, "campaign_id": int(camp["id"]), "campaign_name": camp.get("name"),
        "lead_id": str(rec.get("id") or ""), "category": row.get("lead_category"),
        "reply_time": _iso(_when(row.get("reply_time"))), "reply_excerpt": excerpt(thread),
        "thread_json": json.dumps(thread, ensure_ascii=False),
    }


def _thread(campaign_id, lead_id) -> list:
    data = _get(f"/campaigns/{campaign_id}/leads/{lead_id}/message-history")
    hist = data.get("history") if isinstance(data, dict) else data
    if not isinstance(hist, list):
        raise SmartleadError(f"unexpected message history for campaign {campaign_id}")
    return normalize_thread(hist)


def _queue_brief(d: dict, thread: list) -> None:
    """Hand a new pool lead to the brief machinery. The booking row only carries the brief; it stays off the
    Booked calls list until the lead books (store.list_bookings)."""
    email = d["email"]
    if store.get_booking(store.bid_for(email)) is None:  # the card links to /briefs/<bid>, so the row always exists
        store.upsert_booking({"email": email, "lead_name": f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip(),
                              "first_name": d.get("first_name"), "title": d.get("title"), "company": d.get("company"),
                              "website": d.get("website"), "location": d.get("location"),
                              "campaign_id": d.get("campaign_id"), "campaign_name": d.get("campaign_name"),
                              "lead_id": d.get("lead_id"), "thread": thread})
    if on_new_lead and store.needs_brief(email):
        on_new_lead(email)


def _add_new(email: str, row: dict, camp: dict) -> bool:
    rec = _get("/leads/", {"email": email})
    if not isinstance(rec, dict) or not rec.get("id"):
        raise SmartleadError("no lead record")
    thread = _thread(camp["id"], rec["id"])
    d = pool_record(email, row, camp, rec, thread)
    created, _ = store.pool_upsert(d)
    if created:
        _queue_brief(d, thread)
    return created


def _update_known(known: dict, row: dict, camp: dict) -> bool:
    """A lead already in the pool: refresh its category, and its thread when there is a newer reply.
    Its claim is never touched."""
    if int(known.get("campaign_id") or 0) != int(camp["id"]):
        return False
    upd = {"email": known["email"]}
    cat = row.get("lead_category")
    if cat and cat != known.get("category"):
        upd["category"] = cat
    t_new, t_old = _when(row.get("reply_time")), _when(known.get("reply_time"))
    if t_new and (t_old is None or t_new > t_old) and known.get("lead_id"):
        thread = _thread(camp["id"], known["lead_id"])
        upd.update(reply_time=_iso(t_new), thread_json=json.dumps(thread, ensure_ascii=False),
                   reply_excerpt=excerpt(thread))
        if store.get_booking(store.bid_for(known["email"])):
            store.upsert_booking({"email": known["email"], "thread": thread})
    if len(upd) == 1:
        return False
    store.pool_upsert(upd)
    return True


def _log(event: str, **kw) -> None:
    print(json.dumps({"event": event, **{k: (scrub(v) if isinstance(v, str) else v) for k, v in kw.items()}}),
          flush=True)


def ingest_once() -> dict:
    if not tenant.lead_pool_enabled():
        return {"ok": False, "reason": "lead pool is off"}
    if not api_key():
        return {"ok": False, "reason": "no Smartlead key"}
    since = tenant.lead_pool_since()
    out = {"ok": True, "campaigns": 0, "new": 0, "updated": 0, "failed_campaigns": 0, "skipped_leads": 0}
    try:
        camps = matching_campaigns(_get("/campaigns"), tenant.lead_pool_patterns())
    except SmartleadError as ex:
        store.set_setting("lead_pool_last_error", scrub(str(ex))[:200])
        _log("lead_pool_failed", error=str(ex))
        return {"ok": False, "reason": scrub(str(ex))}
    for camp in camps:
        try:
            rows = replied_rows(camp["id"])
        except SmartleadError as ex:
            out["failed_campaigns"] += 1
            _log("lead_pool_campaign_failed", campaign_id=camp.get("id"), error=str(ex))
            continue
        out["campaigns"] += 1
        for email, row in latest_by_email(rows, since).items():
            try:
                known = store.pool_by_email(email)
                if known:
                    out["updated"] += 1 if _update_known(known, row, camp) else 0
                elif is_positive(row.get("lead_category")) and _add_new(email, row, camp):
                    out["new"] += 1
            except SmartleadError as ex:  # not stored, so it is tried again next round
                out["skipped_leads"] += 1
                _log("lead_pool_lead_failed", campaign_id=camp.get("id"), error=str(ex))
    store.set_setting("lead_pool_last_ok", store.now_iso())
    store.set_setting("lead_pool_last_error", None)
    if out["new"] or out["failed_campaigns"] or out["skipped_leads"]:
        _log("lead_pool_synced", **{k: v for k, v in out.items() if k != "ok"})
    return out


def _catch_up() -> None:
    """A restart can kill a brief mid-build: requeue pool leads from the last 12 hours that still have none."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    for d in store.pool_list():
        made = _when(d.get("created_at"))
        if not d["has_brief"] and made and made >= cutoff and on_new_lead:
            on_new_lead(d["email"])


def ensure_started() -> bool:
    """Start the 10-minute loop once the client's config switches the pool on (at boot, or when Gamic saves
    settings). Never for a client without the pool, and never under DISABLE_CALENDAR_SYNC (tests)."""
    if os.environ.get("DISABLE_CALENDAR_SYNC") == "1" or not tenant.lead_pool_enabled():
        return False
    with _start_lock:
        if _started["thread"] is not None:
            return False

        def loop():
            _sleep(20)
            caught_up = False
            while True:
                try:
                    if tenant.lead_pool_enabled():
                        if not caught_up:
                            _catch_up()
                            caught_up = True
                        ingest_once()
                except Exception as ex:  # never let the loop die
                    _log("lead_pool_loop_error", error=str(ex)[:200])
                _sleep(SYNC_EVERY)
        _started["thread"] = threading.Thread(target=loop, daemon=True, name="lead-pool")
        _started["thread"].start()
    return True


# ---------------------------------------------------------------------------
# Dashboard sections: the pool lives on the /briefs page (dashboard.briefs_index) and on each lead's brief page
# (dashboard.render_detail). Both call in here only when the pool is on, so other clients' pages are unchanged.
# ---------------------------------------------------------------------------

CSS = """<style>
.poolsec{margin-bottom:18px}.poolsec .card+.card{margin-top:14px}
.poolsec .briefhead{flex-wrap:wrap}
.poolsec ul.quotes{list-style:none;margin:8px 0 0;padding:0}
.poolsec ul.quotes li{border-left:3px solid var(--accent);padding:2px 0 2px 12px;font-style:italic;font-size:13.5px;
white-space:pre-wrap;word-wrap:break-word}
form.pick{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
form.pick select{font:inherit;font-size:13px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;
background:var(--panel);color:var(--ink)}
.btn.small{font-size:12px;padding:3px 9px}
.flash{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-weight:600}
.poolcard{margin-top:14px;padding:16px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
</style>"""

PICK_JS = """<script>(function(){var k='leadpool_md',v=null;try{v=localStorage.getItem(k)}catch(e){}
document.querySelectorAll('select.md-pick').forEach(function(s){
if(v&&[].some.call(s.options,function(o){return o.value===v}))s.value=v;});
document.querySelectorAll('form.pick').forEach(function(f){f.addEventListener('submit',function(){
var s=f.querySelector('select.md-pick');try{if(s&&s.value)localStorage.setItem(k,s.value)}catch(e){}});});})();</script>"""


def _gate() -> None:
    if not tenant.lead_pool_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _month_start() -> datetime:
    hz = dashboard._zones()[1]
    return datetime.now(hz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _ago(iso: Optional[str]) -> str:
    d = _when(iso)
    if not d:
        return ""
    now = datetime.now(timezone.utc)
    secs = (now - d).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    hz = dashboard._zones()[1]
    days = (now.astimezone(hz).date() - d.astimezone(hz).date()).days
    if days <= 0:
        return f"{int(secs // 3600)}h ago"
    return "yesterday" if days == 1 else f"{days} days ago"


def _md_select(roster: list) -> str:
    opts = "".join(f"<option value='{e(m)}'>{e(m)}</option>" for m in roster)
    return f"<select name='md' class='md-pick' required><option value='' disabled selected>Your name</option>{opts}</select>"


def _claimed_line(d: dict) -> str:
    return f"Claimed by {d['claimed_by']} - {dashboard._fmt_stamp(d.get('claimed_at'))}"


def claim_form(d: dict, roster: list, nxt: str) -> str:
    """Claim form when nobody holds the lead; otherwise who holds it, plus a Release form (the server only takes
    a release from the holder's name)."""
    tok, stop = e(d["token"]), " onclick='event.stopPropagation()'"
    hidden = f"<input type='hidden' name='next' value='{e(nxt)}'>"
    if d.get("claimed_by"):
        return (f"<span class='badge ok'>{e(_claimed_line(d))}</span>"
                f"<form method='post' action='/leads/{tok}/release' class='pick' style='margin-top:8px'{stop}>"
                f"{_md_select(roster)}{hidden}<button class='btn ghost small' type='submit'>Release</button></form>")
    return (f"<form method='post' action='/leads/{tok}/claim' class='pick'{stop}>{_md_select(roster)}{hidden}"
            f"<button class='btn' type='submit'>Claim</button></form>")


def _row(d: dict, roster: list) -> str:
    bid = e(store.bid_for(d["email"]))
    name = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
    site = (d.get("website") or "").replace("https://", "").replace("http://", "").strip("/")
    t = dashboard._dt(d.get("reply_time"))
    when = (f"<b>{e(dashboard._fmt_day(d.get('reply_time')))}</b><span class='rel'>replied {e(_ago(d.get('reply_time')))}</span>"
            if t else "<span class='badge neutral'>Reply time unknown</span>")
    quote_html = f"<ul class='quotes'><li>{e(d.get('reply_excerpt'))}</li></ul>" if d.get("reply_excerpt") else ""
    cat = f"<span class='badge neutral'>{e(d.get('category'))}</span>" if d.get("category") else ""
    brief = ("<span class='badge ok'>Brief ready</span>" if d.get("has_brief")
             else "<span class='badge neutral'>Brief on its way</span>")
    return f"""<tr onclick="location.href='/briefs/{bid}'">
<td class="when">{when}</td>
<td><div class="co"><a href="/briefs/{bid}">{e(d.get('company') or name or '(company unknown)')}</a></div><small class="muted">{e(site)}</small>{quote_html}</td>
<td>{e(name)}{('<br><small class="muted">' + e(d.get('title')) + '</small>') if d.get('title') else ''}<br><small class="muted">{e(briefview.clean_place(d.get('location')) or '')}</small></td>
<td><small>{e(d.get('campaign_name') or '')}</small><div class="chips">{cat}{brief}</div></td>
<td>{claim_form(d, roster, '/briefs')}</td></tr>"""


def _table(rows: list, roster: list, last_col: str, empty: str) -> str:
    if not rows:
        return f"<div class='empty'>{e(empty)}</div>"
    return (f"<table><thead><tr><th>Replied</th><th>Company</th><th>Contact</th><th>Campaign</th><th>{e(last_col)}</th>"
            f"</tr></thead><tbody>{''.join(_row(d, roster) for d in rows)}</tbody></table>")


def section(msg: Optional[str] = None) -> str:
    """'Leads to claim' and 'Claimed', for the top of the /briefs page."""
    rows = store.pool_list()
    roster = tenant.md_roster()
    open_rows = [d for d in rows if not d.get("claimed_by")]
    claimed = sorted((d for d in rows if d.get("claimed_by")), key=lambda d: d.get("claimed_at") or "", reverse=True)
    month = _month_start()
    counts = store.claim_counts(_iso(month))
    chips = "".join(f"<span class='chip'>{e(m)} · <b>{int(counts.get(m, 0))}</b></span>" for m in roster)
    flash = f"<div class='flash'>{e(msg)}</div>" if msg else ""
    return f"""{CSS}<section class="poolsec">{flash}
<div class="card"><div class="briefhead"><div><b>Leads to claim</b> <small class="muted">({len(open_rows)})</small><br>
<small class="muted">Positive replies, first to claim takes the lead</small></div>
<div><small class="muted">Claims in {e(month.strftime('%B'))}</small><div class="chips" style="margin-top:4px">{chips}</div></div></div>
{_table(open_rows, roster, 'Claim', 'No leads waiting to be claimed.')}</div>
<div class="card"><div class="briefhead"><div><b>Claimed</b> <small class="muted">({len(claimed)})</small></div></div>
{_table(claimed, roster, 'Claimed by', 'Nothing claimed yet.')}</div></section>{PICK_JS}"""


def detail_block(email: str, bid: str, editable: bool, msg: Optional[str] = None) -> str:
    """Claim status (and the claim or release form for a signed-in viewer) on a pool lead's brief page."""
    d = store.pool_by_email(email)
    if not d:
        return ""
    if not editable:
        status = (f"<span class='badge ok'>{e(_claimed_line(d))}</span>" if d.get("claimed_by")
                  else "<span class='badge neutral'>Not claimed yet</span>")
    else:
        status = claim_form(d, tenant.md_roster(), f"/briefs/{bid}")
    meta = " · ".join(e(x) for x in (d.get("campaign_name"), d.get("category"),
                                     f"replied {_ago(d.get('reply_time'))}" if d.get("reply_time") else "") if x)
    flash = f"<div class='flash' style='margin-top:14px'>{e(msg)}</div>" if msg and editable else ""
    return f"""{CSS}{flash}<div class="card poolcard"><div><b>Lead pool</b><br><small class="muted">{meta}</small></div>
<div>{status}</div></div>{PICK_JS if editable else ''}"""


def _back(nxt: str, msg: str) -> RedirectResponse:
    nxt = nxt if re.fullmatch(r"/briefs(/[A-Za-z0-9]+)?", nxt or "") else "/briefs"
    return RedirectResponse(f"{nxt}?msg={quote(msg)}", status_code=303)


@router.post("/leads/{token}/claim")
def claim_lead(request: Request, token: str, md: str = Form(""), next: str = Form("/briefs")):
    _gate()
    if not dashboard._viewer(request):
        return dashboard.locked()
    d = store.pool_get(token)
    if not d:
        return _back(next, "That lead is no longer in the pool.")
    md = (md or "").strip()
    if md not in tenant.md_roster():
        return _back(next, "Pick your name from the list, then claim.")
    ok, holder = store.claim(token, md)
    label = d.get("company") or d.get("first_name") or "the lead"
    if ok:
        _log("lead_claimed", md=md, company=d.get("company"))
        return _back(next, f"{md} claimed {label}.")
    return _back(next, f"Already claimed by {holder}." if holder else "Couldn't claim that lead.")


@router.post("/leads/{token}/release")
def release_lead(request: Request, token: str, md: str = Form(""), next: str = Form("/briefs")):
    _gate()
    if not dashboard._viewer(request):
        return dashboard.locked()
    d = store.pool_get(token)
    if not d:
        return _back(next, "That lead is no longer in the pool.")
    label = d.get("company") or d.get("first_name") or "The lead"
    if store.release(token, (md or "").strip()):
        _log("lead_released", md=md, company=d.get("company"))
        return _back(next, f"{label} is back in the pool.")
    if not d.get("claimed_by"):
        return _back(next, f"{label} isn't claimed.")
    return _back(next, f"Only {d['claimed_by']} can release {label}.")


@router.post("/api/leads/sync")
def api_sync(request: Request):
    """Gamic-only (ingest key): read Smartlead now instead of waiting for the loop. Returns counts, never the key."""
    _gate()
    dashboard._ingest(request)
    return ingest_once()
