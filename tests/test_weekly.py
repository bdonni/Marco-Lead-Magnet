"""Weekly Slack summary: built from the site's own numbers, dry run by default, once per week, never with Slack off.

Run: python tests/test_weekly.py
"""
import hashlib
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
os.environ.pop("SLACK_WEBHOOK_URL", None)
INGEST_KEY = "ingest-key-for-tests"
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
import weekly  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

H = {"x-ingest-key": INGEST_KEY}
STATS = {"program": {"since": "2026-09-10", "owners_emailed": 4321, "positives": 35},
         "days": [{"day": "2026-10-02", "e1": 100, "followups": 900, "positives": 2},   # last week: not counted
                  {"day": "2026-10-05", "e1": 150, "followups": 1000, "positives": 0},
                  {"day": "2026-10-07", "e1": 1200, "followups": 50, "positives": 9}]}


def run():
    c = TestClient(main.app)
    assert c.post("/api/briefs/weekly-summary", json={}, headers={"x-ingest-key": "nope"}).status_code == 401
    assert c.post("/api/briefs/weekly-summary", json={}, headers=H).status_code == 404  # no numbers yet
    store.set_setting("campaign_stats", json.dumps(STATS))
    store.upsert_booking({"email": "a@example-fab.com", "company": "Example Fab", "booked_at": "2026-10-06T15:00:00Z"})
    store.upsert_booking({"email": "b@example-mill.com", "company": "Example Mill", "booked_at": "2026-10-02T15:00:00Z"})

    s = weekly.build(now=datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc), link="https://example.test/campaigns?key=k")
    assert s["week"] == "2026-10-05", s["week"]
    assert (s["emails"], s["first_emails"], s["positives"], s["booked"]) == (2400, 1350, 9, 1), s
    for want in ("weekly summary", "Week of 5 October", "*Emails sent:* 2,400", "1,350 owners emailed for the first time",
                 "*Positive replies:* 9", "*Calls booked:* 1", "Since 10 September: 35 positive replies from 4,321",
                 "<https://example.test/campaigns?key=k|open your dashboard>"):
        assert want in s["text"], want
    assert "—" not in s["text"]

    # the endpoint is a dry run unless told otherwise, and never posts without a Slack destination
    r = c.post("/api/briefs/weekly-summary", json={"link": "https://example.test/x"}, headers=H).json()
    assert r["dry_run"] is True and r["posted"] is False
    sent = []
    weekly._post = lambda payload: sent.append(payload) or {"via": "test"}
    r = c.post("/api/briefs/weekly-summary", json={"dry_run": False}, headers=H).json()
    assert r["posted"] is True and len(sent) == 1, r
    r = c.post("/api/briefs/weekly-summary", json={"dry_run": False}, headers=H).json()
    assert r["posted"] is False and "already posted" in r["skipped"] and len(sent) == 1
    r = c.post("/api/briefs/weekly-summary", json={"dry_run": False, "force": True}, headers=H).json()
    assert r["posted"] is True and len(sent) == 2
    print("weekly summary tests: ok")


if __name__ == "__main__":
    run()
