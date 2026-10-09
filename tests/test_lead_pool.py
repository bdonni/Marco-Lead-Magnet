"""MD lead pool (leadpool.py): off by default, first-come claims, holder-only release, month counts, Smartlead ingest.
No network, no Slack, no Claude.

Run: python tests/test_lead_pool.py
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("TENANT", None)  # the default tenant (Carrara)
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "lp.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
os.environ.pop("SMARTLEAD_API_KEY", None)
VIEW_KEY, INGEST_KEY = "view-key-for-tests", "ingest-key-for-tests"
os.environ["DASHBOARD_KEY_SHA256"] = hashlib.sha256(VIEW_KEY.encode()).hexdigest()
os.environ["INGEST_KEY_SHA256"] = hashlib.sha256(INGEST_KEY.encode()).hexdigest()
if "weasyprint" not in sys.modules:
    stub = types.ModuleType("weasyprint")

    class _HTML:
        def __init__(self, string=""):
            self.s = string

        def write_pdf(self):
            return b"%PDF-1.4 stub"
    stub.HTML = _HTML
    sys.modules["weasyprint"] = stub

import main  # noqa: E402
import store  # noqa: E402
import tenant  # noqa: E402
import leadpool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MAIN_HOOK = leadpool.on_new_lead  # main's wiring: brief queued with Slack off

SL_KEY = "sl-secret-key-123"
ROSTER = ["Michael Horn", "Dirk Armbrust", "Dwayne Evans", "Ian Biggs"]
VANT_POOL = {"lead_pool_enabled": True, "lead_pool_campaign_patterns": ["SellSide", "Buyer Interest"],
             "lead_pool_since": "2026-10-01T00:00:00Z", "md_roster": ROSTER}
H = {"x-ingest-key": INGEST_KEY}


def client() -> TestClient:
    c = TestClient(main.app)
    c.cookies.set("mb_key", VIEW_KEY)
    return c


def set_config(cfg: dict) -> None:
    r = TestClient(main.app).post("/api/briefs/ingest", headers=H, json={"settings": {"tenant_config": cfg}})
    assert r.status_code == 200


def test_default_tenant_has_no_pool():
    assert tenant.TENANT == "carrara" and not tenant.lead_pool_enabled()
    c = client()
    assert c.get("/leads").status_code == 404
    assert c.get("/leads?view=all").status_code == 404
    assert c.post("/leads/x/claim", data={"md": "Michael Horn"}).status_code == 404
    assert c.post("/leads/x/release", data={"md": "Michael Horn"}).status_code == 404
    assert c.post("/api/leads/sync", headers=H).status_code == 404
    assert leadpool.ensure_started() is False and leadpool._started["thread"] is None
    # nav is exactly what it was before the pool existed: nothing without campaign stats, three tabs with them
    assert main.dashboard.nav("briefs") == ""
    store.set_setting("campaign_stats", "{}")
    want = ('<nav class="sitenav">' + "<a class='tab on' href='/briefs'>Booked calls</a>"
            "<a class='tab' href='/campaigns'>Campaigns</a><a class='tab' href='/weekly'>Weekly review</a></nav>")
    assert main.dashboard.nav("briefs") == want
    assert "/leads" not in c.get("/briefs").text
    store.set_setting("campaign_stats", None)
    store.upsert_booking({"email": "x@y.com", "company": "Y Co", "booked_at": "2026-10-01T00:00:00Z"})
    html = c.get("/briefs").text
    assert "Leads to claim" not in html and "leadpool_md" not in html and "Y Co" in html
    assert "Lead pool" not in c.get(f"/briefs/{store.bid_for('x@y.com')}").text
    # the briefs page still works and the empty pool table hides nothing
    assert c.get("/briefs").status_code == 200


def test_smartlead_key_set_through_ingest_is_never_echoed():
    r = TestClient(main.app).post("/api/briefs/ingest", headers=H, json={"settings": {"smartlead_api_key": SL_KEY}})
    assert r.status_code == 200 and r.json()["settings"] == ["smartlead_api_key"]
    assert SL_KEY not in r.text
    assert store.get_setting("smartlead_api_key") == SL_KEY and leadpool.api_key() == SL_KEY
    assert TestClient(main.app).post("/api/briefs/ingest", json={"settings": {"smartlead_api_key": "x"}}).status_code == 401
    assert SL_KEY not in TestClient(main.app).get("/api/briefs/state", headers=H).text


def test_tenant_helpers():
    set_config(VANT_POOL)
    assert tenant.lead_pool_enabled() and tenant.md_roster() == ROSTER
    assert tenant.lead_pool_patterns() == ["SellSide", "Buyer Interest"]
    assert tenant.lead_pool_since().isoformat() == "2026-10-01T00:00:00+00:00"
    assert leadpool.matching_campaigns([
        {"id": 1, "name": "Vant - SELLSIDE TX W1"}, {"id": 2, "name": "Vant sellside POS", "parent_campaign_id": 1},
        {"id": 3, "name": "Vant - Buyer Interest"}, {"id": 4, "name": "Vant - Other"}], tenant.lead_pool_patterns()) \
        == [{"id": 1, "name": "Vant - SELLSIDE TX W1"}, {"id": 3, "name": "Vant - Buyer Interest"}]
    assert leadpool.matching_campaigns([{"id": 1, "name": "x"}], []) == []
    assert leadpool.is_positive("positive response but no reply") and leadpool.is_positive("Meeting Request")
    assert not leadpool.is_positive("Not Interested") and not leadpool.is_positive(None)


# ---------------------------------------------------------------------------
# Fake Smartlead
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def row(email, cat, t, name="Pat Doe"):
    return {"lead_email": email, "lead_name": name, "lead_category": cat, "reply_time": t, "sequence_number": 1}


class FakeSL:
    def __init__(self):
        self.calls, self.fail_429_once, self.fail_c3_page2 = [], True, True
        self.stats = {
            1: [row("ann@acme.com", "Interested", "2026-10-05T15:00:00.000Z", "Ann Lee"),
                row("bob@nope.com", "Not Interested", "2026-10-05T16:00:00.000Z"),
                row("old@early.com", "Meeting Request", "2026-09-20T10:00:00.000Z"),
                row("dee@delta.com", "information request", "2026-10-06T09:00:00.000Z", "Dee Delta")],
            3: [row("eve@buyer.com", "Booked", "2026-10-07T12:00:00.000Z"),
                row("fay@buyer.com", "Interested", "2026-10-07T13:00:00.000Z")],
            4: [row("zed@other.com", "Interested", "2026-10-07T13:00:00.000Z")],
        }
        self.leads = {
            "ann@acme.com": {"id": 101, "first_name": "Ann", "last_name": "Lee", "email": "ann@acme.com",
                             "company_name": "Acme Holdings LLC", "website": "acme.com", "location": "",
                             "custom_fields": {"clean_company": "Acme", "Title": "Owner", "City": "Austin", "State": "TX"}},
            "dee@delta.com": {"id": 104, "first_name": "Dee", "last_name": "Delta", "email": "dee@delta.com",
                              "company_name": "Delta Pumps", "website": "https://deltapumps.com", "location": "Dallas, TX",
                              "custom_fields": {}},
            "eve@buyer.com": {"id": 105, "first_name": "Eve", "last_name": "B", "email": "eve@buyer.com",
                              "company_name": "Eve Co", "custom_fields": {}},
            "fay@buyer.com": {"id": 106, "first_name": "Fay", "last_name": "B", "email": "fay@buyer.com",
                              "company_name": "Fay Co", "custom_fields": {}},
        }
        self.history = {
            101: [{"type": "SENT", "time": "2026-10-04T15:00:00Z", "email_body": "<p>Hi Ann, a question about Acme.</p>"},
                  {"type": "REPLY", "time": "2026-10-05T15:00:00Z",
                   "email_body": "<div>Yes, we'd consider selling. " + "Tell me more. " * 80 + "</div>"}],
            104: [{"type": "REPLY", "time": "2026-10-06T09:00:00Z", "email_body": "<p>Send me info please.</p>"}],
            105: [{"type": "REPLY", "time": "2026-10-07T12:00:00Z", "email_body": "<p>Booked.</p>"}],
            106: [{"type": "REPLY", "time": "2026-10-07T13:00:00Z", "email_body": "<p>Interested.</p>"}],
        }

    def __call__(self, url, params=None, headers=None, timeout=None):
        assert headers == {"User-Agent": "curl/8.4.0"} and params["api_key"] == SL_KEY
        path = url.replace(leadpool.BASE, "")
        self.calls.append((path, {k: v for k, v in params.items() if k != "api_key"}))
        if path == "/campaigns":
            if self.fail_429_once:
                self.fail_429_once = False
                return Resp(429, {})
            return Resp(200, [{"id": 1, "name": "Vant - SellSide TX W1", "parent_campaign_id": None},
                              {"id": 2, "name": "Vant SellSide POS REPLY", "parent_campaign_id": 1},
                              {"id": 3, "name": "Vant - Buyer Interest Oct", "parent_campaign_id": None},
                              {"id": 4, "name": "Vant - Other", "parent_campaign_id": None}])
        if path.endswith("/statistics"):
            cid = int(path.split("/")[2])
            assert params["email_status"] == "replied"
            off, lim = params["offset"], params["limit"]
            if cid == 3 and off > 0 and self.fail_c3_page2:
                return Resp(500, {})
            rows = self.stats[cid]
            return Resp(200, {"total_stats": str(len(rows)), "data": rows[off:off + lim]})
        if path == "/leads/":
            rec = self.leads.get(params["email"])
            return Resp(200, rec) if rec else Resp(404, {})
        if path.endswith("/message-history"):
            return Resp(200, {"history": self.history[int(path.split("/")[4])]})
        raise AssertionError("unexpected call " + path)


def install_fake() -> FakeSL:
    fake = FakeSL()
    leadpool.http_get = fake
    leadpool.MIN_GAP = 0
    leadpool.PAGE = 1  # page through every row
    slept = []
    leadpool._sleep = slept.append
    fake.slept = slept
    queued = []
    leadpool.on_new_lead = queued.append
    fake.queued = queued
    return fake


def test_ingest_since_and_category_filters_and_failed_page():
    set_config(VANT_POOL)
    fake = install_fake()
    out = leadpool.ingest_once()
    assert out["ok"] and out["new"] == 2 and out["failed_campaigns"] == 1 and out["campaigns"] == 1, out
    assert fake.slept and fake.slept[0] == 5  # backed off on the 429
    pool = {d["email"]: d for d in store.pool_list()}
    # positive and after lead_pool_since only; campaign 3 failed on page 2 so nothing from it was stored
    assert set(pool) == {"ann@acme.com", "dee@delta.com"}
    assert not any("/campaigns/2/" in p or "/campaigns/4/" in p for p, _ in fake.calls)  # subsequence + no match
    ann = store.pool_by_email("ann@acme.com")
    assert ann["company"] == "Acme" and ann["title"] == "Owner" and ann["location"] == "Austin, TX"
    assert ann["first_name"] == "Ann" and ann["campaign_id"] == 1 and ann["campaign_name"] == "Vant - SellSide TX W1"
    assert ann["category"] == "Interested" and ann["reply_time"] == "2026-10-05T15:00:00Z" and ann["lead_id"] == "101"
    assert ann["reply_excerpt"].startswith("Yes, we'd consider selling.") and len(ann["reply_excerpt"]) <= 601
    assert ann["reply_excerpt"].endswith("…") and len(ann["thread"]) == 2 and ann["claimed_by"] is None
    assert len(ann["token"]) >= 16
    assert fake.queued == ["ann@acme.com", "dee@delta.com"]
    # each pool lead has a booking row to carry its brief, but it is not on the Booked calls list
    b = store.get_booking(store.bid_for("ann@acme.com"))
    assert b and b["company"] == "Acme" and len(b["thread"]) == 2 and not b["booked_at"]
    assert "ann@acme.com" not in {x["email"] for x in store.list_bookings()}
    # campaign 3 reads cleanly next time: its leads arrive then
    fake.fail_c3_page2 = False
    out2 = leadpool.ingest_once()
    assert out2["new"] == 2 and out2["failed_campaigns"] == 0, out2
    assert {"eve@buyer.com", "fay@buyer.com"} <= {d["email"] for d in store.pool_list()}
    assert fake.queued[-2:] == ["eve@buyer.com", "fay@buyer.com"]


def test_claim_is_first_come_and_release_only_by_holder():
    tok = store.pool_by_email("dee@delta.com")["token"]
    assert store.claim(tok, "Not An MD") == (False, None)
    assert store.claim("no-such-token", "Ian Biggs") == (False, None)
    assert store.claim(tok, "Dirk Armbrust") == (True, "Dirk Armbrust")
    assert store.claim(tok, "Ian Biggs") == (False, "Dirk Armbrust")
    assert store.release(tok, "Ian Biggs") is False
    assert store.pool_get(tok)["claimed_by"] == "Dirk Armbrust"
    assert store.release(tok, "Dirk Armbrust") is True
    d = store.pool_get(tok)
    assert d["claimed_by"] is None and d["claimed_at"] is None
    assert len(d["released_history"]) == 1 and d["released_history"][0]["md"] == "Dirk Armbrust"
    assert d["released_history"][0]["claimed_at"] and d["released_history"][0]["released_at"]
    assert store.release(tok, "Dirk Armbrust") is False  # nothing to release now
    assert store.claim(tok, "Ian Biggs") == (True, "Ian Biggs")
    store.release(tok, "Ian Biggs")


def test_month_counts():
    toks = {d["email"]: d["token"] for d in store.pool_list()}
    assert store.claim(toks["ann@acme.com"], "Michael Horn")[0]
    assert store.claim(toks["eve@buyer.com"], "Michael Horn")[0]
    assert store.claim(toks["fay@buyer.com"], "Dirk Armbrust")[0]
    with sqlite3.connect(store.DB_PATH) as c:  # one of Michael's claims was made last month
        c.execute("UPDATE pool_leads SET claimed_at='2026-01-15T12:00:00Z' WHERE email='eve@buyer.com'")
    assert store.claim_counts() == {"Michael Horn": 1, "Dirk Armbrust": 1}
    assert store.claim_counts("2026-01-01T00:00:00Z") == {"Michael Horn": 2, "Dirk Armbrust": 1}
    store.release(toks["fay@buyer.com"], "Dirk Armbrust")
    assert store.claim_counts() == {"Michael Horn": 1}


def test_existing_claim_survives_reingest():
    ann = store.pool_by_email("ann@acme.com")
    assert ann["claimed_by"] == "Michael Horn"
    fake = install_fake()
    fake.fail_429_once = fake.fail_c3_page2 = False
    fake.stats[1][0] = row("ann@acme.com", "Meeting Request", "2026-10-08T10:00:00.000Z", "Ann Lee")
    fake.history[101].append({"type": "REPLY", "time": "2026-10-08T10:00:00Z", "email_body": "<p>Tuesday works.</p>"})
    out = leadpool.ingest_once()
    assert out["new"] == 0 and out["updated"] == 1, out
    after = store.pool_by_email("ann@acme.com")
    assert after["claimed_by"] == "Michael Horn" and after["claimed_at"] == ann["claimed_at"]
    assert after["token"] == ann["token"] and after["category"] == "Meeting Request"
    assert after["reply_time"] == "2026-10-08T10:00:00Z" and len(after["thread"]) == 3
    assert fake.queued == []  # no second brief
    assert sum(1 for p, _ in fake.calls if p == "/leads/") == 0  # known leads need no lead lookup
    # nothing changed: no thread refetch
    fake.calls.clear()
    assert leadpool.ingest_once()["updated"] == 0
    assert not any(p.endswith("/message-history") for p, _ in fake.calls)


def test_key_never_logged_on_errors():
    def boom(url, params=None, headers=None, timeout=None):
        raise ConnectionError(f"Max retries exceeded with url: {url}?api_key={params['api_key']}")
    leadpool.http_get = boom
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = leadpool.ingest_once()
    assert out["ok"] is False and SL_KEY not in json.dumps(out) and SL_KEY not in buf.getvalue()
    assert SL_KEY not in (store.get_setting("lead_pool_last_error") or "")
    assert SL_KEY not in leadpool.scrub(f"x?api_key={SL_KEY}&y=1")


def test_dashboard_sections_and_routes():
    c = client()
    assert c.get("/leads").status_code == 404  # no standalone page: the pool lives on /briefs
    html = c.get("/briefs").text
    top, booked = html.split('<div class="bar">', 1)  # the pool sits above the booked-calls list
    assert "Leads to claim" in top and "Claimed" in top and "Leads to claim" not in booked
    to_claim, claimed = top.split("<b>Claimed</b>", 1)
    assert "Delta Pumps" in to_claim and "/claim" in to_claim and "Acme" not in to_claim
    assert "Acme" in claimed and "Claimed by Michael Horn - " in claimed and "/release" in claimed
    assert "Ann Lee" in claimed and "Owner" in claimed and "Austin, TX" in claimed and "Vant - SellSide TX W1" in claimed
    assert "Yes, we&#x27;d consider selling." in claimed
    assert "Michael Horn · <b>1</b>" in top and "Ian Biggs · <b>0</b>" in top and "Claims in " in top
    assert "leadpool_md" in html and "/leads'" not in html and "href='/leads" not in html  # no Leads tab
    assert "ann@acme.com" not in booked and "Delta Pumps" not in booked  # pool replies are not booked calls
    assert SL_KEY not in html
    # every card links to the same brief page the rest of the dashboard uses; it shows the claim status too
    ann_bid, dee_bid = store.bid_for("ann@acme.com"), store.bid_for("dee@delta.com")
    assert f"/briefs/{ann_bid}" in claimed and f"/briefs/{dee_bid}" in to_claim
    detail = c.get(f"/briefs/{ann_bid}").text
    assert "Lead pool" in detail and "Claimed by Michael Horn - " in detail and f"value='/briefs/{ann_bid}'" in detail
    assert "Brief ready" not in to_claim
    store.save_brief("dee@delta.com", "Delta Pumps", "Dee Delta", "lead-pool", "unverified", {}, {})
    assert "Brief ready" in c.get("/briefs").text.split("<b>Claimed</b>")[0]
    # claim from the list: first come wins, the second MD sees who has it; redirects come back to /briefs
    tok = store.pool_by_email("dee@delta.com")["token"]
    r = c.post(f"/leads/{tok}/claim", data={"md": "Dwayne Evans", "next": "/briefs"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/briefs?msg=Dwayne%20Evans%20claimed")
    r = c.post(f"/leads/{tok}/claim", data={"md": "Ian Biggs"}, follow_redirects=False)
    assert r.headers["location"] == "/briefs?msg=Already%20claimed%20by%20Dwayne%20Evans."
    assert "Already claimed by Dwayne Evans." in c.get(r.headers["location"]).text
    r = c.post(f"/leads/{tok}/claim", data={"md": "Somebody"}, follow_redirects=False)
    assert "Pick%20your%20name" in r.headers["location"]
    # release from the brief page: refused for anyone but the holder, then back to that page
    r = c.post(f"/leads/{tok}/release", data={"md": "Ian Biggs", "next": f"/briefs/{dee_bid}"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/briefs/{dee_bid}?msg=Only%20Dwayne%20Evans")
    assert "Only Dwayne Evans can release" in c.get(r.headers["location"]).text
    assert store.pool_get(tok)["claimed_by"] == "Dwayne Evans"
    r = c.post(f"/leads/{tok}/release", data={"md": "Dwayne Evans", "next": "https://evil.test/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/briefs?msg=") and "back%20in%20the%20pool" in r.headers["location"]
    assert store.pool_get(tok)["claimed_by"] is None
    assert TestClient(main.app).post(f"/leads/{tok}/claim", data={"md": "Ian Biggs"}).status_code == 401
    assert store.pool_get(tok)["claimed_by"] is None
    assert "<script>x</script>" not in c.get("/briefs?msg=<script>x</script>").text
    # the shared (no key) brief link shows the status but no forms
    share = store.get_booking(ann_bid)["share_token"]
    shared = TestClient(main.app).get(f"/b/{share}").text
    assert "Claimed by Michael Horn - " in shared and "/release" not in shared
    # Gamic-only manual sync needs the ingest key and returns counts only
    assert TestClient(main.app).post("/api/leads/sync").status_code == 401
    install_fake().fail_429_once = False
    r = TestClient(main.app).post("/api/leads/sync", headers=H)
    assert r.status_code == 200 and r.json()["ok"] and SL_KEY not in r.text


def test_new_lead_brief_never_posts_to_slack():
    assert tenant.get("slack_enabled") is True  # Carrara's default: Slack on, yet the pool's brief must not post
    seen = []
    real = main.run_brief
    main.run_brief = lambda req, notes, **kw: seen.append((req.email, kw))
    try:
        store.upsert_booking({"email": "new@lead.com", "company": "New Lead Co"})
        MAIN_HOOK("new@lead.com")
        for _ in range(50):
            if seen:
                break
            time.sleep(0.05)
    finally:
        main.run_brief = real
    assert seen and seen[0][0] == "new@lead.com"
    assert seen[0][1]["post"] is False and seen[0][1]["source"] == "lead-pool" and seen[0][1]["store_brief"] is True


def test_turning_it_off_hides_everything_again():
    set_config({**VANT_POOL, "lead_pool_enabled": False})
    c = client()
    html = c.get("/briefs").text
    assert "Leads to claim" not in html and "/leads/" not in html and "leadpool_md" not in html
    assert "Lead pool" not in c.get(f"/briefs/{store.bid_for('ann@acme.com')}").text
    assert c.post("/leads/x/claim", data={"md": "Michael Horn"}).status_code == 404
    assert leadpool.ingest_once() == {"ok": False, "reason": "lead pool is off"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
