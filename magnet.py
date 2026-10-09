"""Email 3 reply assets, built automatically for each lead who asks: a rough valuation or a sector report.

Email 3 offers one of two things: a rough valuation range for the company, or a short report on who is buying in
its sector and at what multiples. When a lead says yes, the positive-reply sync on gamic-ops calls
POST /api/magnet/generate, which returns a private link at once (/m/<token>, plus /m/<token>/pdf) and builds the asset in a
background thread; GET /api/magnet/<token> reports building / ready / failed.

How the numbers are made:
- Sector benchmarks come from Claude with web search, as JSON with a source for every figure. They are cached per sector and
  size band for 30 days, so repeat leads in one sector reuse the same research. Benchmarks that fail a sanity check are refused.
- The valuation arithmetic is done here in code, never by the model: staff x sales per employee, x EBITDA margin, x multiple.
- The sector report is cached per sector for 30 days and personalised with the lead's name and company.
Nothing is sent to the prospect from here. The link goes onto the #positive-replies card and the GHL note, for Gene to send.
"""
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import store
import tenant

router = APIRouter()
MODEL = os.environ.get("MAGNET_MODEL", "claude-sonnet-4-6")
CACHE_DAYS = 30
WATCHDOG_SECS = 900
# the 2026 search tool filters results with code execution and ran 20+ minutes on benchmark prompts; the basic tool is enough here
SEARCH_TOOL = os.environ.get("MAGNET_SEARCH_TOOL", "web_search_20250305")
BOOKING = "link.advocateadvisorsllc.com/widget/bookings/advocate-advisors-consultation"
_lock = threading.Lock()
_client = None


def init(client) -> None:
    global _client
    _client = client
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS magnets (token TEXT PRIMARY KEY, kind TEXT, email TEXT, company TEXT,
                     html TEXT, data TEXT, created_at TEXT)""")
        if "status" not in [r[1] for r in c.execute("PRAGMA table_info(magnets)")]:
            c.execute("ALTER TABLE magnets ADD COLUMN status TEXT DEFAULT 'ready'")
        # a restart kills the build threads, so anything still "building" at startup will never finish
        c.execute("""UPDATE magnets SET status='failed', data='{"error": "interrupted by a restart"}' WHERE status='building'""")


def _ingest(request: Request) -> None:
    import dashboard
    dashboard._ingest(request)


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def money(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}k"


def _claude_json(prompt: str, uses: int = 8, deadline: int = 720) -> dict:
    """Research with web search; gives up after `deadline` seconds in total so a build can never hang."""
    messages = [{"role": "user", "content": prompt}]
    resp, t0, turns = None, time.time(), 0
    while True:
        left = deadline - (time.time() - t0)
        if left < 30:
            raise ValueError(f"research ran past {deadline}s after {turns} turns")
        resp = _client.messages.create(model=MODEL, max_tokens=4000, messages=messages, timeout=left,
                                       tools=[{"type": SEARCH_TOOL, "name": "web_search", "max_uses": uses}])
        turns += 1
        print(json.dumps({"event": "magnet_research_turn", "turn": turns, "stop": resp.stop_reason,
                          "secs": round(time.time() - t0)}), flush=True)
        if resp.stop_reason != "pause_turn" or turns >= 4:
            break
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": resp.content}]
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON from research (stop={resp.stop_reason}, turns={turns})")
    return json.loads(m.group(0))


def _cached(key: str, build):
    raw = store.get_setting(key)
    if raw:
        j = json.loads(raw)
        if time.time() - j.get("_at", 0) < CACHE_DAYS * 86400:
            return j
    j = build()
    j["_at"] = time.time()
    store.set_setting(key, json.dumps(j))
    return j


def _clean(s) -> str:
    return re.sub(r"\s*[—–]\s*", " - ", str(s or "")).strip()


def _tighten(lo: float, hi: float, ratio: float) -> tuple:
    """Shrink a range around its geometric middle so high/low is at most `ratio`."""
    if lo <= 0 or hi / lo <= ratio:
        return lo, hi
    mid = (lo * hi) ** 0.5
    return mid / ratio ** 0.5, mid * ratio ** 0.5


BANNED_SOURCES = re.compile(r"ct acquisitions|dealstream|bizbuysell|iconic|business broker|sunbelt|transworld|axial|true north|axia growth", re.I)


def size_band(staff: int) -> str:
    return "100 to 199 staff" if staff < 200 else "200 to 499 staff" if staff < 500 else "500 to 1,000 staff"


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
def bench(sector: str, staff: int) -> dict:
    band = size_band(staff)

    def build():
        p = f"""You are an M&A analyst. Find public benchmarks to value a privately held US company in this sector: "{sector}", with {band}.
Search for current figures (2024 to 2026 preferred). Every number must come from a source you found; never invent one.
Return ONLY JSON:
{{"sector_label": "the sector in plain words, plural, e.g. specialty distributors",
 "sales_per_employee_low": integer US dollars, "sales_per_employee_high": integer US dollars,
 "sales_basis": "one sentence: what the range is based on, naming the benchmark figure",
 "ebitda_margin_low": percent number, "ebitda_margin_high": percent number,
 "margin_basis": "one sentence naming the benchmark",
 "multiple_low": number, "multiple_high": number,
 "multiple_basis": "one sentence: EV/EBITDA multiples for PRIVATE deals of this size in this sector, naming the benchmark",
 "value_drivers": [{{"title": "3 to 6 words", "text": "one or two sentences on what moves the price for this sector"}}] (5 or 6 items),
 "buyer_stats": [{{"figure": "short figure like 85% or 7.2x", "label": "what it means, under 18 words"}}] (3 items, each from a source),
 "buyer_source": "short source line for the buyer stats",
 "sources": ["Publisher, Title (Year)" for every source used]}}
Keep ranges tight around the benchmark: sales per employee within about 20% either side of the benchmark average, EBITDA
margin no more than 5 points wide, multiple no more than 2.5 turns wide. Multiples must suit a company of {band}, not public
companies. Each *_basis sentence under 30 words. No em dashes.
Source rules: prefer industry associations, government data (Census, BLS), deal-data providers (GF Data, PitchBook, Capital IQ),
large accounting and research firms (KPMG, Deloitte, PwC, BDO, IBISWorld) and trade press. Never cite a business broker, a
boutique M&A advisor or a sell-side firm's marketing page (for example CT Acquisitions, DealStream, BizBuySell, Iconic): those are
competitors of the firm sending this. If a figure is only on such a page, find the original source it cites and use that instead. When a figure comes from GF Data,
cite GF Data, not the advisor who reposted it."""
        j = _claude_json(p, uses=4)
        sl, sh = float(j["sales_per_employee_low"]), float(j["sales_per_employee_high"])
        ml, mh = float(j["ebitda_margin_low"]), float(j["ebitda_margin_high"])
        xl, xh = float(j["multiple_low"]), float(j["multiple_high"])
        sl, sh = _tighten(sl, sh, 1.6)
        xl, xh = (xl, xh) if xh - xl <= 3 else ((xl + xh) / 2 - 1.5, (xl + xh) / 2 + 1.5)
        ml, mh = (ml, mh) if mh - ml <= 6 else ((ml + mh) / 2 - 3, (ml + mh) / 2 + 3)
        j.update(sales_per_employee_low=sl, sales_per_employee_high=sh, ebitda_margin_low=ml, ebitda_margin_high=mh,
                 multiple_low=xl, multiple_high=xh)
        if not (40_000 <= sl <= sh <= 3_000_000 and 1 <= ml <= mh <= 45 and 2 <= xl <= xh <= 20) or not j.get("sources"):
            raise ValueError(f"benchmarks failed sanity check: {sl},{sh},{ml},{mh},{xl},{xh}")
        return j

    return _cached(f"magnet_bench:v2:{sector.lower()}|{band}", build)


def valuation_html(lead: dict, b: dict) -> tuple:
    staff = int(lead["employees"])
    sl, sh = float(b["sales_per_employee_low"]), float(b["sales_per_employee_high"])
    ml, mh = float(b["ebitda_margin_low"]) / 100, float(b["ebitda_margin_high"]) / 100
    xl, xh = float(b["multiple_low"]), float(b["multiple_high"])
    rl, rh = staff * sl, staff * sh
    el, eh = rl * ml, rh * mh
    vl, vh = el * xl, eh * xh
    vc = staff * (sl + sh) / 2 * (ml + mh) / 2 * (xl + xh) / 2
    co, first = esc(lead["company"]), esc(lead.get("first_name"))
    who = esc(f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip().upper())
    month = datetime.now(timezone.utc).strftime("%B %Y")
    drivers = "".join(f"<li><b>{esc(_clean(d.get('title')))}.</b> {esc(_clean(d.get('text')))}</li>" for d in b.get("value_drivers", [])[:6])
    stats = "".join(f'<div class="stat"><b>{esc(s.get("figure"))}</b><span>{esc(_clean(s.get("label")))}</span></div>' for s in b.get("buyer_stats", [])[:3])
    srcs = "; ".join(esc(_clean(s)) for s in b.get("sources", []) if not BANNED_SOURCES.search(str(s)))
    body = f"""
<div class="cover" style="padding-top:.2in">
<p class="brand">ADVOCATE ADVISORS</p><p class="sub">INVESTMENT BANKING</p>
<p class="kicker">ROUGH VALUATION · PREPARED FOR {who}</p>
<h1>What {co} might be worth today</h1>
<p class="meta">A first look from public information only · {month}</p>
<div class="hl"><p>ROUGH ENTERPRISE VALUE RANGE</p><p class="big">{money(vl)} to {money(vh)}</p><p>Central estimate around {money(vc)}</p></div>
<p class="src">Built from headcount, typical margins for {esc(lead.get('sector') or (b.get('sector_label') or '').lower())} and recent deal multiples. Your own numbers will narrow this range considerably.</p>
</div>
<section><p class="kicker">SECTION 01</p><h2>How we got there</h2>
<p class="lead">Three steps, each from public data. Where we had to estimate, we used a range rather than a single number.</p>
<table class="calc"><tr><th>STEP</th><th>WHAT WE USED</th><th style="text-align:right">LOW</th><th style="text-align:right">HIGH</th></tr>
<tr><td>1. Revenue</td><td>About {staff:,} staff, at {money(sl)} to {money(sh)} of sales per employee. {esc(_clean(b.get('sales_basis')))}</td><td class="n">{money(rl)}</td><td class="n">{money(rh)}</td></tr>
<tr><td>2. EBITDA</td><td>{ml*100:.0f}% to {mh*100:.0f}% of sales. {esc(_clean(b.get('margin_basis')))}</td><td class="n">{money(el)}</td><td class="n">{money(eh)}</td></tr>
<tr><td>3. Multiple</td><td>{xl:.1f}x to {xh:.1f}x EBITDA. {esc(_clean(b.get('multiple_basis')))}</td><td class="n">{xl:.1f}x</td><td class="n">{xh:.1f}x</td></tr>
<tr><td><b>Enterprise value</b></td><td><b>EBITDA × multiple</b></td><td class="n"><b>{money(vl)}</b></td><td class="n"><b>{money(vh)}</b></td></tr></table>
<p class="src">Sources: {srcs}. Headcount from public company profiles.</p></section>
<section><p class="kicker">SECTION 02</p><h2>What could put {co} at the top of the range</h2>
<p class="lead">Buyers pay most for earnings they're confident will repeat. These are the things that usually decide where in the range a business lands.</p>
<ul>{drivers}</ul></section>
<section><p class="kicker">SECTION 03</p><h2>Who buys businesses like this</h2>
<div class="stats">{stats}</div><p class="src">Source: {esc(_clean(b.get('buyer_source')))}</p>
{_proof()}</section>
{_cta("With three numbers from you (last year's sales, EBITDA and your largest customer's share), Gene can turn this into a much tighter range and show which buyers are most active in your sector right now.", "04")}"""
    data = {"staff": staff, "ev_low": vl, "ev_high": vh, "ev_central": vc, "bench": {k: v for k, v in b.items() if k != "_at"}}
    return _page(f"Rough valuation - {lead['company']}", body), data


# ---------------------------------------------------------------------------
# Sector report
# ---------------------------------------------------------------------------
def report_data(sector: str) -> dict:
    def build():
        p = f"""You are an M&A analyst writing a short briefing for owners of privately held US {sector}. Search for current
information (2025 and 2026). Every figure must come from a source you found; never invent a number, deal or quote.
Return ONLY JSON:
{{"sector_label": "plain plural words for the sector",
 "short_version": ["5 bullets, each one or two sentences: deal activity, who is buying, multiples, demand drivers, what it means for owners"],
 "stats": [{{"figure": "short figure", "label": "what it is, under 15 words"}}] (3 items),
 "stats_source": "short source line",
 "dynamics": [{{"heading": "2 to 4 words", "text": "two or three sentences"}}] (3 or 4 items),
 "deals": [{{"date": "Mon YYYY", "target": "company acquired", "acquirer": "buyer", "note": "under 15 words, e.g. strategic, PE add-on"}}] (up to 6 real deals from 2025 or 2026 in this sector),
 "multiples": [{{"segment": "who or what size", "multiple": "e.g. 6.0x to 7.5x EBITDA", "source": "publisher"}}] (2 to 4 rows; private lower middle market where possible),
 "owner_takeaways": ["4 items, one or two sentences each: what this means for an owner thinking about the next few years"],
 "sources": ["Publisher, Title (Year)" for every source used]}}
Plain English, no hype, no em dashes. If you cannot find real deals, return an empty deals list.
Source rules: prefer industry associations, government data (Census, BLS), deal-data providers (GF Data, PitchBook, Capital IQ),
large accounting and research firms (KPMG, Deloitte, PwC, BDO, IBISWorld) and trade press. Never cite a business broker, a
boutique M&A advisor or a sell-side firm's marketing page (for example CT Acquisitions, DealStream, BizBuySell, Iconic): those are
competitors of the firm sending this. If a figure is only on such a page, find the original source it cites and use that instead. When a figure comes from GF Data,
cite GF Data, not the advisor who reposted it."""
        j = _claude_json(p, uses=10)
        if not j.get("short_version") or not j.get("sources"):
            raise ValueError("sector report research came back empty")
        return j

    return _cached(f"magnet_report:v2:{sector.lower()}", build)


def report_html(lead: dict, r: dict) -> str:
    label = esc(lead.get("sector") or (r.get("sector_label") or "").lower())
    who = esc(f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip().upper())
    month = datetime.now(timezone.utc).strftime("%B %Y")
    short = "".join(f"<li>{esc(_clean(x))}</li>" for x in r.get("short_version", [])[:5])
    stats = "".join(f'<div class="stat"><b>{esc(s.get("figure"))}</b><span>{esc(_clean(s.get("label")))}</span></div>' for s in r.get("stats", [])[:3])
    dyn = "".join(f"<h3>{esc(_clean(d.get('heading')))}</h3><p>{esc(_clean(d.get('text')))}</p>" for d in r.get("dynamics", [])[:4])
    deals = r.get("deals") or []
    deal_tbl = ("<table><tr><th>DATE</th><th>TARGET</th><th>BUYER</th><th>NOTE</th></tr>" + "".join(
        f"<tr><td>{esc(d.get('date'))}</td><td>{esc(_clean(d.get('target')))}</td><td>{esc(_clean(d.get('acquirer')))}</td><td>{esc(_clean(d.get('note')))}</td></tr>"
        for d in deals[:6]) + "</table>") if deals else ""
    mult = "".join(f"<tr><td>{esc(_clean(m.get('segment')))}</td><td>{esc(_clean(m.get('multiple')))}</td><td>{esc(_clean(m.get('source')))}</td></tr>" for m in r.get("multiples", [])[:4])
    take = "".join(f"<li>{esc(_clean(x))}</li>" for x in r.get("owner_takeaways", [])[:4])
    srcs = "".join(f"<p>{esc(_clean(s))}</p>" for s in r.get("sources", []) if not BANNED_SOURCES.search(str(s)))
    body = f"""
<div class="cover" style="padding-top:.2in">
<p class="brand">ADVOCATE ADVISORS</p><p class="sub">INVESTMENT BANKING</p>
<p class="kicker">SECTOR REPORT · PREPARED FOR {who}</p>
<h1>Who is buying {label}, and at what price</h1>
<p class="meta">Prepared for {esc(lead['company'])} · {month}</p>
<div class="box"><p class="kicker">THE SHORT VERSION</p><ul>{short}</ul></div></div>
<section><p class="kicker">SECTION 01</p><h2>Where the market is</h2><div class="stats">{stats}</div>
<p class="src">Source: {esc(_clean(r.get('stats_source')))}</p>{dyn}</section>
<section><p class="kicker">SECTION 02</p><h2>Recent deals and multiples</h2>{deal_tbl}
<table><tr><th>SEGMENT</th><th>MULTIPLE</th><th>SOURCE</th></tr>{mult}</table></section>
<section><p class="kicker">SECTION 03</p><h2>What it means for owners</h2><ul>{take}</ul>{_proof()}</section>
{_cta("If you'd like to know where " + esc(lead['company']) + " would sit in these ranges, Gene can walk you through it with your own numbers.", "04")}
<section class="sources"><p class="kicker">SOURCES</p>{srcs}</section>"""
    return _page(f"Sector report - {r.get('sector_label') or lead.get('sector')}", body)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
def _proof() -> str:
    return """<div class="box"><p class="serif" style="font-weight:700;margin:0 0 4px">How a real process changes the number</p>
<p>Advocate Advisors is an award-winning investment bank that works only for owners. Two recent results:</p>
<ul><li>Sold a $120M revenue electrical contractor for 50% more than other investment banks said it was worth.</li>
<li>Sold a chemical R&amp;D company at 25 times EBITDA.</li></ul>
<p>In both cases the difference came from putting the business in front of several buyers and letting them compete.</p></div>"""


def _cta(text: str, n: str) -> str:
    return f"""<section><p class="kicker">SECTION {n}</p><h2>Next step</h2><p>{text}</p>
<div class="cta"><p class="serif">Book 15 minutes with Gene</p><p class="link"><a href="https://{BOOKING}">{BOOKING}</a></p>
<p class="note">Or reply to the email this came with. Everything you share stays confidential.</p></div>
<p class="foot">A first look from public information only, not a formal valuation or an offer. Figures are estimates from published benchmarks.</p></section>"""


def _page(title: str, body: str) -> str:
    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>'


def _validate(kind: str, lead: dict) -> dict:
    if kind not in ("valuation", "report"):
        raise ValueError("kind must be valuation or report")
    for k in ("company", "sector"):
        if not lead.get(k):
            raise ValueError(f"missing {k}")
    if kind == "valuation":
        staff = int(float(lead.get("employees") or 0))
        if staff < 10:
            raise ValueError("no usable headcount")
        lead["employees"] = staff
    return lead


def build(kind: str, lead: dict) -> tuple:
    if kind == "valuation":
        return valuation_html(lead, bench(lead["sector"], lead["employees"]))
    return report_html(lead, report_data(lead["sector"])), {"sector": lead["sector"]}


def _run(tok: str, kind: str, lead: dict) -> None:
    """Background build: research can take minutes and must never block the app's event loop."""
    try:
        page, data = build(kind, lead)
        status = "ready"
    except Exception as e:
        page, data, status = "", {"error": str(e)[:300]}, "failed"
        print(json.dumps({"event": "magnet_failed", "token": tok, "kind": kind, "company": lead.get("company"), "error": str(e)[:300]}), flush=True)
    with store._conn() as c:  # never overwrite a watchdog "failed" with a late result
        c.execute("UPDATE magnets SET html=?, data=?, status=? WHERE token=? AND status='building'", (page, json.dumps(data), status, tok))


def _expire(tok: str) -> None:
    """A research call that never returns must not leave a lead's asset 'building' forever."""
    with store._conn() as c:
        c.execute("""UPDATE magnets SET status='failed', data=? WHERE token=? AND status='building'""",
                  (json.dumps({"error": f"no result after {WATCHDOG_SECS}s"}), tok))


def start(kind: str, lead: dict) -> dict:
    lead = _validate(kind, dict(lead))
    tok = secrets.token_urlsafe(12)
    with store._conn() as c:
        c.execute("INSERT INTO magnets (token, kind, email, company, html, data, created_at, status) VALUES (?,?,?,?,?,?,?,?)",
                  (tok, kind, (lead.get("email") or "").lower(), lead["company"], "", "{}", store.now_iso(), "building"))
    threading.Thread(target=_run, args=(tok, kind, lead), daemon=True).start()
    threading.Timer(WATCHDOG_SECS, _expire, args=(tok,)).start()
    return {"token": tok, "kind": kind, "status": "building", "path": f"/m/{tok}", "pdf": f"/m/{tok}/pdf"}


def status(tok: str) -> dict:
    m = _get(tok)
    out = {"token": tok, "kind": m["kind"], "status": m.get("status") or "ready", "path": f"/m/{tok}", "pdf": f"/m/{tok}/pdf"}
    d = json.loads(m.get("data") or "{}")
    out.update({k: v for k, v in d.items() if k.startswith("ev_") or k == "error"})
    return out


def _get(tok: str) -> dict:
    with store._conn() as c:
        r = c.execute("SELECT * FROM magnets WHERE token=?", (tok,)).fetchone()
    if not r:
        raise HTTPException(status_code=404)
    return dict(r)


@router.post("/api/magnet/generate")
async def api_generate(request: Request):
    _ingest(request)
    if tenant.TENANT != "advocate":  # the assets carry Advocate's branding and proof
        raise HTTPException(status_code=404)
    d = await request.json()
    try:
        return start(d.get("kind"), d.get("lead") or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/api/magnet/{tok}")
def api_status(tok: str, request: Request):
    _ingest(request)
    return status(tok)


def _ready(tok: str) -> dict:
    m = _get(tok)
    if (m.get("status") or "ready") != "ready":
        raise HTTPException(status_code=404 if m.get("status") == "failed" else 425, detail=m.get("status"))
    return m


@router.get("/m/{tok}", response_class=HTMLResponse)
def view(tok: str):
    return HTMLResponse(_ready(tok)["html"], headers={"X-Robots-Tag": "noindex"})


@router.get("/m/{tok}/pdf")
def pdf(tok: str):
    from weasyprint import HTML
    m = _ready(tok)
    name = re.sub(r"[^A-Za-z0-9]+", "_", m["company"]).strip("_")
    label = "Rough_Valuation" if m["kind"] == "valuation" else "Sector_Report"
    return Response(HTML(string=m["html"]).write_pdf(), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Advocate_Advisors_{label}_{name}.pdf"'})


CSS = """
@page{size:Letter;margin:0.9in 0.95in 0.85in}
:root{--ink:#1A1A1A;--head:#17181A;--slate:#6B675F;--gold:#A89372;--goldt:#857052;--box:#F5F3EF;--rule:#DCD6CC;--alt:#FAF8F5}
body{font-family:Calibri,"Helvetica Neue",Arial,sans-serif;color:var(--ink);font-size:11pt;line-height:1.45;margin:0}
.serif,h1,h2,h3{font-family:Georgia,serif;color:var(--head)}
.brand{font-family:Georgia,serif;font-weight:700;font-size:15pt;letter-spacing:.3em;margin:0}
.sub{font-size:7.5pt;letter-spacing:.45em;color:var(--goldt);margin:2px 0 0}
.kicker{font-size:8pt;font-weight:700;letter-spacing:.2em;color:var(--goldt);margin:0 0 3px}
.cover{padding-top:.4in}
.cover .kicker{color:var(--slate);font-size:10pt;margin-top:.55in}
h1{font-size:23pt;line-height:1.2;margin:6px 0 14px;padding-bottom:14px;border-bottom:2px solid var(--gold)}
.meta{color:var(--slate);margin:0 0 2px}
h2{font-size:17pt;margin:0 0 10px;padding-bottom:6px;border-bottom:1.5px solid var(--head)}
h3{font-size:12.5pt;margin:14px 0 4px}
p{margin:0 0 8px}
.lead{font-size:11.5pt}
.src{font-size:8pt;font-style:italic;color:var(--slate);margin:5px 0 12px}
section{margin-top:22px}
.pb{break-before:page}
.stats{display:flex;gap:8px;margin:10px 0 4px}
.stat{flex:1;background:var(--box);padding:10px 12px}
.stat b{display:block;font-family:Georgia,serif;font-size:20pt;color:var(--head)}
.stat span{font-size:9pt;color:var(--slate)}
table{width:100%;border-collapse:collapse;font-size:9.5pt;margin:6px 0 2px;break-inside:avoid}
th{background:var(--head);color:#fff;text-align:left;font-size:7.5pt;letter-spacing:.12em;padding:6px 8px}
td{border-bottom:1px solid var(--rule);padding:5px 8px}
tr:nth-child(even) td{background:var(--alt)}
td:first-child{font-weight:700;white-space:nowrap}
td.n{text-align:right;white-space:nowrap}
ul{list-style:none;padding:0;margin:6px 0}
li{position:relative;padding-left:18px;margin:0 0 6px}
li:before{content:"\25AA";color:var(--gold);position:absolute;left:0}
ol{list-style:none;padding:0;margin:6px 0;counter-reset:q}
ol li{padding-left:30px}
ol li:before{counter-increment:q;content:counter(q,decimal-leading-zero);color:var(--goldt);font-weight:700}
.box{background:var(--box);border-left:4px solid var(--gold);padding:12px 16px;margin:8px 0;break-inside:avoid}
.cta{margin-top:12px}
.cta .serif{font-size:13pt;font-weight:700;margin:0 0 4px}
.cta .link{color:var(--goldt);font-size:10pt;word-break:break-all}
.sources p{font-size:8.5pt;color:var(--slate);margin:0 0 3px}
.foot{font-size:8pt;color:var(--slate);font-style:italic;margin-top:10px}
.big{font-family:Georgia,serif;font-size:34pt;font-weight:700;color:var(--head);margin:4px 0 0}
.calc td:first-child{width:34%}.calc td.n{font-family:Georgia,serif;font-size:12pt}
.note{font-size:9pt;color:var(--slate)}.hl{background:var(--head);color:#fff;padding:14px 18px;margin:10px 0 6px}
.hl .big{color:#fff}.hl p{margin:0;color:#DCD6CC}"""
