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
         "days": [{"day": "2026-09-29", "e1": 300, "followups": 700, "positives": 3},   # last week
                  {"day": "2026-10-02", "e1": 100, "followups": 900, "positives": 2},   # last week
                  {"day": "2026-10-05", "e1": 150, "followups": 1000, "positives": 0},
                  {"day": "2026-10-07", "e1": 1200, "followups": 50, "positives": 9}],
         "positives_recent": [{"company": "Older Co", "day": "2026-10-01"}, {"company": "Example Fab", "day": "2026-10-07"},
                              {"company": "Sample Mills", "day": "2026-10-07"}],
         "campaigns": [{"area": "Manufacturing", "wave": "Wave 3", "segment": "Google Workspace", "state": "sending",
                        "first_send": "2026-10-07", "queued": 400, "e1_days": {"2026-10-07": 800},
                        "emails_days": {"2026-10-07": 800}},
                       {"area": "Manufacturing", "wave": "Wave 2", "segment": "Microsoft 365", "state": "sending",
                        "first_send": "2026-09-28", "queued": 1700, "e1_days": {"2026-10-05": 150, "2026-10-07": 400},
                        "emails_days": {"2026-10-05": 1150, "2026-10-07": 450, "2026-10-02": 99}},
                       {"area": "Manufacturing", "wave": "Wave 4", "segment": "Microsoft 365 + other", "state": "starting",
                        "starts": "2026-10-12", "queued": 3500}],
         "inboxes": {"joining": {"count": 60, "date": "2026-10-12"}}}


def run():
    c = TestClient(main.app)
    assert c.post("/api/briefs/weekly-summary", json={}, headers={"x-ingest-key": "nope"}).status_code == 401
    assert c.post("/api/briefs/weekly-summary", json={}, headers=H).status_code == 404  # no numbers yet
    store.set_setting("campaign_stats", json.dumps(STATS))
    store.upsert_booking({"email": "a@example-fab.com", "company": "Example Fab", "booked_at": "2026-10-06T15:00:00Z"})
    store.upsert_booking({"email": "b@example-mill.com", "company": "Example Mill", "booked_at": "2026-10-02T15:00:00Z"})

    s = weekly.build(now=datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc), link="https://example.test/campaigns?key=k")
    assert s["week"] == "2026-10-05", s["week"]
    assert (s["emails"], s["first_emails"], s["positives"], s["positives_last_week"], s["booked"]) == (2400, 1350, 9, 5, 1), s
    for want in ("here's how your week went", "Big week", "9 owners asked to talk, up from 5 last week and 1 call was booked for you.",
                 "Wednesday was the standout, with 9 positive replies in one day, the day Wave 3 went out for the first time",
                 "*This week in numbers*", "2,400 emails sent, 1,350 of them first emails", "9 positive replies", "1 call booked",
                 "*Who said yes*", "Example Fab and Sample Mills", "*Calls booked this week*", "Example Fab",
                 "*What went out*", "Wave 3 · Google Workspace: 800 first emails", "Wave 2 · Microsoft 365: 550 first emails, 1,050 follow-ups",
                 "*Next week*", "Mon 12 Oct: 60 new inboxes go live", "Mon 12 Oct: Wave 4 · Microsoft 365 + other starts for 3,500 owners",
                 "Wave 2 · Microsoft 365: 1,700 owners still to get a first email",
                 "Since 10 September: 35 positive replies from 4,321", "<https://example.test/campaigns?key=k|open your dashboard>",
                 "Have a great weekend", "Ben and the Gamic team"):
        assert want in s["text"], want
    assert "Older Co" not in s["text"] and "Example Mill" not in s["text"]
    assert "\u2014" not in s["text"]
    assert all(len(b["text"]["text"]) <= 3000 for b in s["payload"]["blocks"])

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
