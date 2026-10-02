"""Brief site + direct Smartlead "Booked" hook. No network, no Slack, no Claude.

Run: python tests/test_dashboard.py
"""
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
VIEW_KEY, INGEST_KEY = "view-key-for-tests", "ingest-key-for-tests"
import hashlib
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
REAL_RUN_BRIEF = main.run_brief
import store  # noqa: E402
from bookings import booking_from_payload, normalize_thread, is_booked_event, is_marco_campaign  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PAYLOAD = {
    "event_type": "LEAD_CATEGORY_UPDATED", "event_id": "ev-imex-1",
    "event_timestamp": "2026-09-30T21:20:13.000Z",
    "campaign_name": "CRR - Manufacturing Oct W2 (Google + gateway)", "campaign_id": 4028770,
    "lead_category": {"new_id": 96272, "old_id": 5, "new_name": "Booked", "old_name": "Information Request"},
    "lead_data": {"first_name": "Adam", "last_name": "Zilberbaum", "email": "adam@imexdopplers.com",
                  "company_name": "Imex", "website": "imexdopplers.com",
                  "custom_fields": {"state": "Maryland", "clean_company": "Imex"}},
    "history": [
        {"type": "SENT", "time": "2026-09-28T15:00:00Z", "from": "marco.b@x.com", "to": "adam@imexdopplers.com",
         "subject": "Imex", "email_body": "<p>Hi Adam,</p><p>Quick question about Imex.</p>"},
        {"type": "REPLY", "time": "2026-09-30T21:13:41+00:00", "from": "igol@gmail.com", "to": "marco.b@x.com",
         "email_body": "<div>Hi Marco,<br>Friday at 10am ET works for us.</div><div>On Wed, Sep 30, 2026 at 5:11 PM Marco wrote:</div><blockquote>old</blockquote>"},
    ],
}


def fresh_client():
    c = TestClient(main.app)
    return c


def test_payload_parsing():
    assert is_booked_event(PAYLOAD) and is_marco_campaign(PAYLOAD["campaign_name"])
    assert not is_marco_campaign("GMC - IB PLAYBOOK BROAD 22/09") and is_marco_campaign("Marco - Chicago 3")
    assert not is_booked_event({**PAYLOAD, "lead_category": {"new_id": 5, "new_name": "Information Request"}})
    from bookings import is_marco_event
    pos = {**PAYLOAD, "campaign_name": "POS REPLY", "campaign_id": 4042008}
    assert not is_marco_event(pos)
    assert is_marco_event(pos, {4042008})
    assert is_marco_event({**pos, "sl_senders_mailbox": "marco.b@joincarrarastrategy.com"})
    assert not is_marco_event({**pos, "sl_senders_mailbox": "kory@ggc-outreach.com"})
    bk = booking_from_payload(PAYLOAD)
    assert bk["email"] == "adam@imexdopplers.com" and bk["lead_name"] == "Adam Zilberbaum"
    assert bk["company"] == "Imex" and bk["location"] == "Maryland" and len(bk["thread"]) == 2
    reply = bk["thread"][1]
    assert reply["type"] == "REPLY" and "Friday at 10am ET" in reply["text"] and "wrote:" not in reply["text"]


def test_site_needs_key_and_shows_booking():
    calls = []
    main.run_brief = lambda req, notes, **kw: calls.append((req.lead_name, req.company_name, kw.get("source"), kw.get("dry_run")))
    main.dashboard._hooks["extract_meeting"] = lambda thread, state: {
        "at": "2026-10-02T14:00:00Z", "text": "Friday at 10am ET", "quote": "Friday at 10am ET works for us.",
        "source": "thread"}
    c = fresh_client()

    # ignored events
    assert c.post("/hooks/smartlead-booked", json={**PAYLOAD, "campaign_name": "GGC - TAM2"}).json()["ignored"]
    assert c.post("/hooks/smartlead-booked", json={**PAYLOAD, "lead_category": {"new_id": 1, "new_name": "Interested"}}).json()["ignored"]
    # the real one
    r = c.post("/hooks/smartlead-booked", json=PAYLOAD)
    assert r.json().get("queued"), r.text
    assert c.post("/hooks/smartlead-booked", json=PAYLOAD).json().get("duplicate")
    time.sleep(0.5)
    assert calls and calls[0][:3] == ("Adam Zilberbaum", "Imex", "smartlead-booked"), calls

    # locked without the key
    assert c.get("/briefs").status_code == 401
    assert c.get("/briefs?key=wrong").status_code == 401
    r = c.get(f"/briefs?key={VIEW_KEY}", follow_redirects=False)
    assert r.status_code == 303 and "mb_key" in r.headers.get("set-cookie", "")
    c.cookies.set("mb_key", VIEW_KEY)
    page = c.get("/briefs?view=all").text
    assert "Imex" in page and "Adam Zilberbaum" in page
    bid = store.bid_for("adam@imexdopplers.com")
    b = store.get_booking(bid)
    assert b["meeting_at"] == "2026-10-02T14:00:00Z" and b["meeting_source"] == "thread"
    assert b["booked_at"] == "2026-09-30T21:20:13Z" and b["thread_count"] == 2
    detail = c.get(f"/briefs/{bid}").text
    assert "Friday at 10am ET works for us." in detail and "9:00 AM CT" in detail and "10:00 AM ET" in detail
    assert "not been generated yet" in detail


def test_manual_time_wins_and_brief_shows():
    c = fresh_client()
    c.cookies.set("mb_key", VIEW_KEY)
    bid = store.bid_for("adam@imexdopplers.com")
    r = c.post(f"/briefs/{bid}/meeting", data={"date": "2026-10-02", "time": "11:30", "tz": "ET"}, follow_redirects=False)
    assert r.status_code == 303
    store.upsert_booking({"email": "adam@imexdopplers.com", "meeting": {"at": "2026-10-09T14:00:00Z", "source": "thread"}})
    b = store.get_booking(bid)
    assert b["meeting_at"] == "2026-10-02T15:30:00Z" and b["meeting_source"] == "manual"

    store.save_brief("adam@imexdopplers.com", "Imex", "Adam Zilberbaum", "smartlead-booked", "verified_research",
                     {"business_summary": "Imex makes handheld Dopplers.", "owner_summary": "• Adam is President.",
                      "website": "imexdopplers.com", "founded_year": "1976"},
                     {"motivation_hypothesis": "Testing the waters.", "key_strengths": ["Brand", "Warranty"],
                      "marco_briefing_note": "Igol joins; lead with valuation."}, posted=True)
    detail = c.get(f"/briefs/{bid}").text
    for s in ("Imex makes handheld Dopplers.", "Testing the waters.", "Walking in", "Owner verified", "Download PDF"):
        assert s in detail, s
    pdf = c.get(f"/briefs/{bid}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert store.posted_recently("adam@imexdopplers.com")
    # a Clay call for the same lead after the post is skipped
    r = c.post("/generate-briefing", json={"lead_name": "Adam Said", "email": "adam@imexdopplers.com", "company_name": "Imex"})
    assert r.json()["status"] == "duplicate_skipped"


def test_ingest_requires_key_and_upserts():
    c = fresh_client()
    assert c.post("/api/briefs/ingest", json={"bookings": []}).status_code == 401
    r = c.post("/api/briefs/ingest", headers={"x-ingest-key": INGEST_KEY}, json={"extract": False, "bookings": [
        {"email": "Paul@IkorIndustries.com", "lead_name": "Paul Lesniak", "company": "Ikor Industries",
         "booked_at": "2026-10-01T20:20:00Z", "history": PAYLOAD["history"]}],
        "briefs": [{"email": "plesniak@ikorindustries.com", "company": "Ikor Industries", "source": "import",
                    "request": {"business_summary": "Contract manufacturer."}, "assessment": {}}]})
    assert r.json() == {"bookings": 1, "briefs": 1}
    st = c.get("/api/briefs/state", headers={"x-ingest-key": INGEST_KEY}).json()["bookings"]
    assert any(x["email"] == "paul@ikorindustries.com" and x["thread_count"] == 2 for x in st)
    r = c.post("/api/briefs/ingest", headers={"x-ingest-key": INGEST_KEY}, json={"purge": ["paul@ikorindustries.com"]})
    assert r.json()["purged"] == 1
    st = c.get("/api/briefs/state", headers={"x-ingest-key": INGEST_KEY}).json()["bookings"]
    assert not any(x["email"] == "paul@ikorindustries.com" for x in st)


def test_booked_at_rules():
    store.upsert_booking({"email": "x@y.com", "booked_at": "2026-09-29T10:00:00Z", "booked_at_soft": True})
    store.upsert_booking({"email": "x@y.com", "booked_at": "2026-09-30T10:00:00Z"})  # event beats soft
    assert store.get_booking(store.bid_for("x@y.com"))["booked_at"] == "2026-09-30T10:00:00Z"
    store.upsert_booking({"email": "x@y.com", "booked_at": "2026-09-28T10:00:00Z", "booked_at_soft": True})
    assert store.get_booking(store.bid_for("x@y.com"))["booked_at"] == "2026-09-30T10:00:00Z"


def test_meeting_extractor_guards():
    from bookings import extract_meeting

    class Fake:
        def __init__(self, out):
            self.out = out
            self.messages = self

        def create(self, **kw):
            self.prompt = kw["messages"][0]["content"]
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(self.out))])

    thread = [{"type": "SENT", "time": "2026-09-29T19:59:11Z", "text": "thursday at 1pm or friday at 10am pacific?"},
              {"type": "REPLY", "time": "2026-09-29T20:03:36Z", "text": "I can do Friday."}]
    # Boulder Dog Food: the model said Friday but resolved a Saturday -> rejected
    bad = Fake({"agreed": True, "local_datetime": "2026-10-03T10:00", "timezone": "America/Los_Angeles",
                "stated_as": "Friday at 10am pacific", "quote": "I can do Friday."})
    assert extract_meeting(bad, "m", thread, "Colorado") is None
    assert "Fri 2026-10-02" in bad.prompt and "(Tuesday)" in bad.prompt
    good = Fake({"agreed": True, "local_datetime": "2026-10-02T10:00", "timezone": "America/Los_Angeles",
                 "stated_as": "Friday at 10am pacific", "quote": "I can do Friday."})
    m = extract_meeting(good, "m", thread, "Colorado")
    assert m["at"] == "2026-10-02T17:00:00Z" and m["source"] == "thread"
    assert extract_meeting(Fake({"agreed": False}), "m", thread, "Colorado") is None


def test_relay_fires_marco_brief_and_still_forwards():
    calls, fwd = [], []
    main.run_brief = lambda req, notes, **kw: calls.append((req.lead_name, kw.get("source")))
    main.forward_to_clay = lambda to, payload: fwd.append(payload.get("event_id"))
    c = fresh_client()
    clay = "https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-df38f22f-fd6a-47ba-a8de-058e7af56462"
    p1 = {**PAYLOAD, "event_id": "ev-relay-1", "lead_data": {**PAYLOAD["lead_data"], "email": "new@lead.com"}}
    r = c.post("/hooks/smartlead-slim", params={"to": clay}, json=p1)
    assert r.json()["marco"] is True
    p2 = {**PAYLOAD, "event_id": "ev-relay-2", "campaign_name": "GMC - PE NAMED 25/09"}
    r2 = c.post("/hooks/smartlead-slim", params={"to": clay}, json=p2)
    assert r2.json()["marco"] is False
    time.sleep(0.5)
    assert fwd == ["ev-relay-1", "ev-relay-2"]
    assert calls and calls[0] == ("Adam Zilberbaum", "smartlead-booked")


def test_booked_brief_posts_short_notice_with_link():
    sent = []

    class R:
        status_code = 200

        def raise_for_status(self):
            pass
    main.requests.post = lambda url, json=None, timeout=None, **kw: (sent.append((url, json)), R())[1]
    main.SLACK_WEBHOOK_URL = "https://hooks.slack.test/x"
    main.overview_from_site = lambda *a, **k: "Makes handheld Dopplers."
    main.company_research = lambda *a, **k: {"founded_year": "1976", "hq": "Kingsville, MD", "employees": "11-50 (LinkedIn)",
                                             "ownership": "acquired from Natus Medical in 2023",
                                             "recent_developments": ["Jan 2023: acquired from Natus Medical"]}
    main.resolve_owner_profile = lambda client, req: ("• Adam is President.", "verified_research", "test")
    main.generate_assessment = lambda req, status, words=None, facts=None: {
        "walking_in": ["Igol, Adam's partner, runs the emails; ask him to join."],
        "they_said": ["Igol (partner): we certainly do not need to sell"],
        "confirm_on_call": ["Who owns what share of Imex?"], "why_now": ["Testing the waters on value."],
        "key_strengths": ["Fifty-year brand"], "business_points": ["Makes handheld Dopplers."],
        "owner_points": ["Adam is President since 2023."], "motivation_hypothesis": "m", "marco_briefing_note": "n"}

    class _H:
        def __init__(self, string=""):
            pass

        def write_pdf(self):
            return b"%PDF-1.4 real"
    main.WeasyHTML = _H
    email = "notice@test.com"
    store.upsert_booking({"email": email, "company": "Notice Co", "lead_name": "Nina Test", "location": "Texas",
                         "meeting": {"at": "2026-10-09T15:00:00Z", "source": "thread", "text": "Fri 10am CT"}})
    req = main.BriefingRequest(lead_name="Nina Test", email=email, company_name="Notice Co", website="notice.com")
    out = REAL_RUN_BRIEF(req, [], source="smartlead-booked", store_brief=True)
    assert out["status"] == "success"
    assert len(sent) == 1, sent
    url, payload = sent[0]
    b = store.get_booking(store.bid_for(email))
    txt = json.dumps(payload)
    assert url == "https://hooks.slack.test/x"
    assert "Call booked: Notice Co" in txt and "/b/" + b["share_token"] in txt and "10:00 AM CT" in txt
    assert "Pre-Call Brief" not in txt and "Deal" not in txt  # the long card is gone
    assert store.posted_recently(email)
    # the shared link opens that booking without the site key, read-only, and the PDF comes from the cache
    c = TestClient(main.app)
    page = c.get(f"/b/{b['share_token']}").text
    assert "Notice Co" in page and "Makes handheld Dopplers." in page and "Save time" not in page and "All booked calls" not in page
    for want in ("Walking in", "In their words", "Confirm on the call", "Why they might talk now", "The business",
                 "The owner", "Deal strengths", "Recent developments", "11-50 (LinkedIn)", "Kingsville, MD",
                 "1976 (", "acquired from Natus Medical in 2023", "<ol><li>Who owns what share of Imex?</li></ol>"):
        assert want in page, want
    calls = []
    main.dashboard._hooks["render_pdf"] = lambda *a: (calls.append(1), b"%PDF-1.4 rendered")[1]
    pdf = c.get(f"/b/{b['share_token']}/pdf")
    assert pdf.status_code == 200 and pdf.content == b"%PDF-1.4 real" and calls == []  # pre-warmed at build time
    assert c.get("/b/not-a-token").status_code == 401


def test_briefview_turns_paragraphs_into_bullets_and_pdf_matches():
    import briefview
    legacy_owner = ("Profile of Brian Packard\n\u2022 Role: Brian is President and CEO since 1996.\n"
                    "\u2022 Age Range: likely 50-60 years.")
    assert briefview.points(legacy_owner) == ["Role: Brian is President and CEO since 1996.", "Age Range: likely 50-60 years."]
    para = ("Kroll Furniture specializes in custom furniture for hospitality. Established in 1954, it serves designers. "
            "Ok. While detailed figures are not available, the company is mature.")
    assert briefview.points(para) == ["Kroll Furniture specializes in custom furniture for hospitality.",
                                      "Established in 1954, it serves designers.",
                                      "While detailed figures are not available, the company is mature."]
    v = briefview.view({"business_summary": para, "recent_news": "No significant news found."},
                       {"motivation_hypothesis": "He took over in 2023. Two years in is a natural window.",
                        "key_strengths": ["70-year history"]})
    assert v["why_now"] == ["He took over in 2023.", "Two years in is a natural window."] and v["recent"] == []
    req = main.BriefingRequest(company_name="Kroll Furniture", lead_name="Kevin Ebrahimi", founded_year="1954",
                               location="San Francisco, CA", business_summary=para, employees="11-50")
    html = main.build_pdf_html(req, {"walking_in": ["Lead with the 1954 history."], "key_strengths": ["70-year history"]},
                               "verified_upstream")
    assert "Walking in" in html and "<li>Lead with the 1954 history.</li>" in html and "The business" in html
    assert "<li>Established in 1954, it serves designers.</li>" in html and "11-50" in html and "1954 (" in html
    assert "Exit Motivation Hypothesis" not in html and "Advisor Note" not in html


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
