"""A non-Carrara client service (TENANT=vant): own key, own branding, own campaigns. Run on its own:
TENANT=vant python tests/test_tenant.py
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TENANT"] = "vant"
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "v.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
os.environ.pop("DASHBOARD_KEY_SHA256", None)
if "weasyprint" not in sys.modules:
    stub = types.ModuleType("weasyprint")

    class _HTML:
        def __init__(self, string=""):
            self.s = string

        def write_pdf(self):
            return b"%PDF-1.4 stub " + self.s.encode()[:2000]
    stub.HTML = _HTML
    sys.modules["weasyprint"] = stub

import main  # noqa: E402
import store  # noqa: E402
import tenant  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

INGEST = "ingest-test"
os.environ["INGEST_KEY_SHA256"] = hashlib.sha256(INGEST.encode()).hexdigest()
main.dashboard.INGEST_KEY_SHA256 = os.environ["INGEST_KEY_SHA256"]
MARCO_KEY_HASH = "7f274d0b11225490c338020f316135d59b70619fed2fe9f60989a93d0d2f6a99"
VANT_KEY = "vant-view-key"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def test_unconfigured_client_is_locked_even_to_marcos_key():
    c = TestClient(main.app)
    assert main.dashboard._view_hash() == ""
    assert c.get("/briefs?key=anything").status_code == 401
    assert c.get("/static/carrara-logo.png").status_code == 404  # Carrara's assets are not served here


def test_configured_client_brand_key_and_prompt():
    c = TestClient(main.app)
    h = {"x-ingest-key": INGEST}
    r = c.post("/api/briefs/ingest", headers=h, json={"settings": {
        "tenant_config": {"firm": "The Vant Group", "firm_short": "Vant Group", "sender_label": "Vant", "caller": "Michael",
                          "caller_context": "Managing Director of The Vant Group, a Texas M&A advisory firm",
                          "confirm_focus": "ownership, revenue and EBITDA, timeline", "accent": "#0b3d2e",
                          "campaign_prefixes": ["Vant"], "mailbox_hint": "vant"},
        "tenant_logo_b64": PNG, "view_key_sha256": hashlib.sha256(VANT_KEY.encode()).hexdigest()}})
    assert r.status_code == 200 and set(r.json()["settings"]) >= {"tenant_config", "tenant_logo_b64", "view_key_sha256"}
    assert c.get(f"/briefs?key={VANT_KEY}", follow_redirects=False).status_code == 303
    c.cookies.set("mb_key", VANT_KEY)
    page = c.get("/briefs").text
    assert "The Vant Group" in page and "Carrara" not in page and "Marco" not in page and "#0b3d2e" in page
    assert c.get("/static/logo.png").content.startswith(b"\x89PNG")
    # the brief prompt is written for Michael at Vant
    seen = {}

    class Msg:
        def create(self, **kw):
            seen["prompt"] = kw["messages"][0]["content"]
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps({"walking_in": ["x"]}))])
    main.claude_client = types.SimpleNamespace(messages=Msg())
    req = main.BriefingRequest(lead_name="Rod Smith", company_name="Air Pro Elite")
    main.generate_assessment(req, "verified_research")
    assert "for Michael, Managing Director of The Vant Group" in seen["prompt"] and "Marco" not in seen["prompt"]
    assert "ownership, revenue and EBITDA, timeline" in seen["prompt"]
    assert tenant.calendar_owner() == "Michael's"


def test_booked_event_for_this_client_only():
    calls = []
    main._handle_booked = lambda bk, dry: calls.append(bk["email"])
    c = TestClient(main.app)
    ev = {"event_type": "LEAD_CATEGORY_UPDATED", "event_id": "v1", "campaign_name": "Vant - Buyer Interest 3-Step",
          "campaign_id": 3749377, "lead_category": {"new_id": 96272, "new_name": "Booked"},
          "lead_data": {"email": "rod@airproelite.com", "first_name": "Rod", "last_name": "Smith"}}
    assert c.post("/hooks/smartlead-booked", json=ev).json().get("queued")
    other = {**ev, "event_id": "v2", "campaign_name": "CRR - Manufacturing Oct", "lead_data": {"email": "x@y.com"}}
    assert c.post("/hooks/smartlead-booked", json=other).json().get("ignored")
    time.sleep(0.3)
    assert calls == ["rod@airproelite.com"]


def test_own_account_client_takes_every_campaign():
    calls = []
    main._handle_booked = lambda bk, dry: calls.append(bk["email"])
    store.set_setting("tenant_config", json.dumps({**json.loads(store.get_setting("tenant_config")), "all_campaigns": True}))
    c = TestClient(main.app)
    ev = {"event_type": "LEAD_CATEGORY_UPDATED", "event_id": "a1", "campaign_name": "Wave 5 Sell-Side",
          "lead_category": {"new_id": 96272, "new_name": "Booked"}, "lead_data": {"email": "drew@trigonins.com"}}
    assert c.post("/hooks/smartlead-booked", json=ev).json().get("queued")
    time.sleep(0.3)
    assert calls == ["drew@trigonins.com"]


def test_new_client_never_posts_to_slack_until_switched_on():
    sent = []
    main.requests.post = lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError("posted"))
    main.overview_from_site = lambda *a, **k: "Overview."
    main.company_research = lambda *a, **k: {}
    main.resolve_owner_profile = lambda client, req: ("• Owner.", "verified_research", "t")
    main.generate_assessment = lambda req, status, words=None, facts=None: {"walking_in": ["x"], "key_strengths": []}
    req = main.BriefingRequest(lead_name="Rod Smith", email="rod@airproelite.com", company_name="Air Pro Elite")
    out = main.run_brief(req, [], source="smartlead-booked", store_brief=True)
    assert out["status"] == "success" and sent == []
    assert store.get_booking(store.bid_for("rod@airproelite.com"))["brief"]["posted_to_slack"] == 0
    c = TestClient(main.app)
    r = c.post("/api/briefs/notify", headers={"x-ingest-key": INGEST}, json={"email": "rod@airproelite.com"})
    assert r.status_code == 409 and sent == []


def test_calendly_booking_creates_call_and_cancel_hides_it():
    import hmac as _h
    import hashlib as _hl
    import calendly
    store.set_setting("calendly_signing_key", "sign-test")
    got = []
    main._handle_calendly = lambda bk: (got.append(bk), main.dashboard.ingest_booking(
        {k: v for k, v in bk.items() if k not in ("qa", "invitee_uri", "rescheduled")}, extract=False))
    c = TestClient(main.app)

    def send(event, extra=None):
        body = json.dumps({"event": event, "payload": {**{
            "email": "drew@trigonins.com", "name": "Drew Taylor", "first_name": "Drew", "created_at": "2026-10-02T15:00:00.000000Z",
            "uri": "https://api.calendly.com/scheduled_events/E1/invitees/I1", "rescheduled": False,
            "questions_and_answers": [{"question": "Agency name", "answer": "Trigon Insurance"},
                                      {"question": "What would you like to discuss?", "answer": "Valuing my book"},
                                      {"question": "Purpose?", "answer": "Selling my Agency\nRoutine check-up"}],
            "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/E1", "name": "Free Valuation Inquiry",
                                "start_time": "2026-10-06T15:00:00.000000Z"}}, **(extra or {})}}).encode()
        t = str(int(time.time()))
        sig = _h.new(b"sign-test", f"{t}.".encode() + body, _hl.sha256).hexdigest()
        return c.post("/hooks/calendly", content=body, headers={"content-type": "application/json",
                                                                "calendly-webhook-signature": f"t={t},v1={sig}"})
    assert c.post("/hooks/calendly", content=b"{}", headers={"calendly-webhook-signature": "t=1,v1=bad"}).status_code == 401
    assert send("invitee.created").json().get("queued")
    time.sleep(0.3)
    b = store.get_booking(store.bid_for("drew@trigonins.com"))
    assert b["meeting_at"] == "2026-10-06T15:00:00Z" and b["meeting_source"] == "calendar" and b["company"] == "Trigon Insurance"
    assert got[0]["qa"][1] == ("What would you like to discuss?", "Valuing my book")
    assert calendly.qa_text(got[0]["qa"]).startswith("Agency name: Trigon Insurance")
    assert b["form_answers"].endswith("Valuing my book\nPurpose?: Selling my Agency; Routine check-up")
    page = c.get(f"/b/{b['share_token']}").text
    assert "Booking form answers" in page and "Valuing my book" in page
    other = {"uri": "https://api.calendly.com/scheduled_events/E0/invitees/I0",
             "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/E0", "name": "Free Valuation Inquiry",
                                 "start_time": "2026-10-03T15:00:00.000000Z"}}
    assert send("invitee.canceled", other).json().get("ignored")      # an older call, not the one on the site
    assert store.get_booking(store.bid_for("drew@trigonins.com"))["hidden"] == 0
    assert send("invitee.canceled", {"uri": "https://api.calendly.com/scheduled_events/E1/invitees/I2"}).json().get("hidden")
    assert store.get_booking(store.bid_for("drew@trigonins.com"))["hidden"] == 1


def test_home_timezone_leads():
    import dashboard
    store.set_setting("tenant_config", json.dumps({"firm": "X", "home_tz": "ET"}))
    assert dashboard._fmt_call("2026-10-01T18:00:00Z") == ("Thu 1 Oct", "2:00 PM ET · 1:00 PM CT")
    store.set_setting("tenant_config", json.dumps({"firm": "X"}))
    assert dashboard._fmt_call("2026-10-01T18:00:00Z") == ("Thu 1 Oct", "1:00 PM CT · 2:00 PM ET")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
