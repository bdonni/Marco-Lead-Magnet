"""Regression tests for the 2026-10-02 'briefs not firing' fixes. No network, no Slack.

Run: python -m pytest tests/test_intake.py  (or python tests/test_intake.py)
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intake import flatten, normalize, slim_payload, name_from_email, CLAY_WEBHOOK_RE, MAX_BYTES


class Req(types.SimpleNamespace):
    pass


def make_req(**kw):
    base = {f: None for f in ("lead_name", "first_name", "email", "company_name", "website", "company_linkedin",
                              "location", "company_type", "founded_year", "business_summary", "recent_news",
                              "owner_summary", "title")}
    base.update(kw)
    return Req(**base)


def test_json_shaped_owner_summary_is_flattened():
    # Pyrexar, 2026-10-01: the Claygent answered with JSON text and broke Clay's hand-built body.
    raw = json.dumps({"founder_or_owner": "Mark Falkowski is the founder and CEO.",
                      "age_estimate": "", "background": ["BSEE", "medical devices"]})
    out = flatten(raw)
    assert out.startswith("• Founder or owner: Mark Falkowski is the founder and CEO.")
    assert "Age estimate" not in out
    assert "• Background: BSEE; medical devices" in out
    assert "{" not in out


def test_clay_wrappers_numbers_and_blanks():
    assert flatten({"response": "Plain answer"}) == "Plain answer"
    assert flatten(1954) == "1954"
    assert flatten("  ") is None
    assert flatten("null") is None
    assert flatten("[object Object]") is None


def test_blank_name_and_company_get_fallbacks():
    # Gunslinger Custom Paint, 2026-09-30: enrichment found no person name, so Clay never called us.
    r = make_req(first_name="Jeff", email="jeff@gcpaint.com", website="", company_name="")
    notes = normalize(r)
    assert r.lead_name == "Jeff"
    assert r.website == "gcpaint.com"
    assert r.company_name == "Gcpaint"
    assert notes


def test_email_name_must_agree_with_first_name():
    r = make_req(first_name="Paul", email="plesniak@ikorindustries.com", company_name="Ikor Industries")
    normalize(r)
    assert r.lead_name == "Paul"          # "Plesniak" alone is not trusted as a full name
    assert name_from_email("mark.falkowski@pyrexar.com") == "Mark Falkowski"
    assert name_from_email("info@alliedplumbingservice.com") is None
    r2 = make_req(email="info@alliedplumbingservice.com", company_name="Allied Plumbing")
    normalize(r2)
    assert r2.lead_name is None


def test_founded_year_is_cleaned():
    r = make_req(company_name="Kroll", founded_year="Founded in 1954.")
    normalize(r)
    assert r.founded_year == "1954"
    r2 = make_req(company_name="X", founded_year="unknown")
    normalize(r2)
    assert r2.founded_year is None


def test_slim_payload_drops_history_and_keeps_formula_keys():
    big = "<p>" + ("long thread text " * 4000) + "</p>"
    payload = {
        "event_type": "LEAD_CATEGORY_UPDATED", "event_id": "e1", "to": "plesniak@ikorindustries.com",
        "description": "Lead - plesniak@ikorindustries.com category updated to Booked",
        "campaign_name": "CRR - Manufacturing Oct W2 (Google + gateway)", "app_url": "https://app.smartlead.ai/x",
        "last_reply": {"email_body": big}, "history": [{"email_body": big}] * 6,
        "leadCorrespondence": {"history": [big] * 4},
        "lead_data": {"first_name": "Paul", "last_name": "Lesniak", "website": "ikorindustries.com",
                      "linkedin_profile": None, "custom_fields": {"state": "Arizona"}},
    }
    assert len(json.dumps(payload)) > 400_000
    out = slim_payload(payload)
    assert "history" not in out and "leadCorrespondence" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= MAX_BYTES
    for k in ("to", "description", "campaign_name", "app_url", "event_type"):
        assert out[k] == payload[k]
    assert out["lead_data"]["first_name"] == "Paul"
    assert out["last_reply"]["email_body"].startswith("<p>long thread text")


def test_relay_only_forwards_to_clay_webhooks():
    ok = "https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-df38f22f-fd6a-47ba-a8de-058e7af56462"
    assert CLAY_WEBHOOK_RE.match(ok)
    for bad in ("https://evil.example.com/hook", ok + "/x", ok.replace("https", "http"),
                "https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-zz"):
        assert not CLAY_WEBHOOK_RE.match(bad)


def test_endpoints_with_stubs():
    """Exercise /generate-briefing and /hooks/smartlead-slim without Claude, WeasyPrint, Slack or Clay."""
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    if "weasyprint" not in sys.modules:
        stub = types.ModuleType("weasyprint")
        stub.HTML = object
        sys.modules["weasyprint"] = stub
    import main
    from fastapi.testclient import TestClient

    calls = {"run": [], "fwd": []}
    main._run_brief_logged = lambda req, notes: calls["run"].append((req.lead_name, req.company_name, req.founded_year))
    main.forward_to_clay = lambda to, payload: calls["fwd"].append((to, payload.get("event_id")))
    c = TestClient(main.app)

    body = {"lead_name": "", "first_name": "Adam", "email": "adam@imexdopplers.com", "company_name": "Imex",
            "founded_year": 1987, "location": "", "owner_summary": json.dumps({"founder_or_owner": "x"})}
    r = c.post("/generate-briefing", json=body)
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    r2 = c.post("/generate-briefing", json=body)
    assert r2.json()["status"] == "duplicate_skipped"
    r3 = c.post("/generate-briefing?force=true", json=body)
    assert r3.json()["status"] == "queued"
    import time; time.sleep(0.2)
    assert calls["run"][0] == ("Adam", "Imex", "1987")

    target = "https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-df38f22f-fd6a-47ba-a8de-058e7af56462"
    assert c.post("/hooks/smartlead-slim?to=https://evil.example.com", json={"a": 1}).status_code == 400
    ok = c.post("/hooks/smartlead-slim", params={"to": target}, json={"event_id": "ev-9", "to": "a@b.com"})
    assert ok.status_code == 200 and ok.json().get("queued")
    dup = c.post("/hooks/smartlead-slim", params={"to": target}, json={"event_id": "ev-9", "to": "a@b.com"})
    assert dup.json().get("duplicate")
    time.sleep(0.2)
    assert calls["fwd"] == [(target, "ev-9")]


def test_smartlead_record_wins_on_name_and_company():
    # Imex, 2026-09-30: Clay's enrichment named the contact "Adam Said"; our lead is Adam Zilberbaum.
    from intake import apply_lead_record
    r = make_req(lead_name="Adam Said", first_name="Adam", email="adam@imexdopplers.com", company_name="Imex",
                 website="imexdopplers.com")
    notes = normalize(r)
    apply_lead_record(r, {"email": "adam@imexdopplers.com", "first_name": "Adam", "last_name": "Zilberbaum",
                          "company_name": "Imex", "custom_fields": {"state": "Maryland"}}, notes)
    assert r.lead_name == "Adam Zilberbaum" and r.location == "Maryland" and r.company_name == "Imex"
    # a domain-derived company name gives way to the record
    r2 = make_req(first_name="Jeff", email="jeff@gcpaint.com")
    n2 = normalize(r2)
    apply_lead_record(r2, {"email": "jeff@gcpaint.com", "first_name": "Jeff", "last_name": "Theisen",
                           "company_name": "Gunslinger Custom Paint", "website": "gcpaint.com"}, n2)
    assert (r2.lead_name, r2.company_name) == ("Jeff Theisen", "Gunslinger Custom Paint")
    # no record: nothing changes
    r3 = make_req(lead_name="Mark Falkowski", email="mark.falkowski@pyrexar.com", company_name="Pyrexar Medical")
    apply_lead_record(r3, {}, [])
    assert r3.lead_name == "Mark Falkowski"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
