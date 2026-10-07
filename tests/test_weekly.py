"""Weekly review: the /weekly page and the Friday Slack message are built from the site's own numbers, the message is
a dry run by default, posts once per week, saves the week as sent, and never posts with Slack off.

Run: python tests/test_weekly.py
"""
import hashlib
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
os.environ.pop("SLACK_WEBHOOK_URL", None)
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
import weekly  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

H = {"x-ingest-key": INGEST_KEY}
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)  # Friday 6pm New York
STATS = {"program": {"since": "2026-09-10", "owners_emailed": 4321, "positives": 35},
         "days": [{"day": "2026-09-29", "e1": 300, "followups": 700, "positives": 3},   # last week
                  {"day": "2026-10-02", "e1": 100, "followups": 900, "positives": 2},   # last week
                  {"day": "2026-10-05", "e1": 150, "followups": 1000, "positives": 0},
                  {"day": "2026-10-07", "e1": 1200, "followups": 50, "positives": 9}],
         "positives_recent": [{"company": "Older Co", "day": "2026-10-01"}, {"company": "Example Fab", "first_name": "Pat", "day": "2026-10-07"},
                              {"company": "Sample Mills", "day": "2026-10-07"}],
         "campaigns": [{"area": "Manufacturing", "wave": "Wave 3", "segment": "Google Workspace", "state": "sending",
                        "first_send": "2026-10-07", "queued": 400, "e1_days": {"2026-10-07": 800}, "emails_days": {"2026-10-07": 800}},
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
    store.upsert_booking({"email": "a@example-fab.com", "company": "Example Fab", "lead_name": "Pat Doe",
                          "booked_at": "2026-10-06T15:00:00Z"})
    store.upsert_booking({"email": "b@example-mill.com", "company": "Example Mill", "booked_at": "2026-10-02T15:00:00Z"})

    r = weekly.review(now=NOW)
    assert r["week"] == "2026-10-05" and r["week_label"] == "Week of 5 October"
    assert (r["emails"], r["first_emails"], r["positives"], r["positives_last_week"], len(r["booked"])) == (2400, 1350, 9, 5, 1), r
    assert [d["day"] for d in r["days"]] == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    assert r["note"] == ("Big week, Marco. 9 owners asked to talk, up from 5 last week, and 1 call was booked for you. "
                         "Wednesday was the standout: 9 owners said yes in one day, the day Wave 3 went out for the first time."), r["note"]
    assert [p["company"] for p in r["said_yes"]] == ["Example Fab", "Sample Mills"]
    assert r["booked"][0]["company"] == "Example Fab" and r["booked"][0]["person"] == "Pat Doe"
    assert {"label": "Wave 2 · Microsoft 365", "first": 550, "followups": 1050} in r["went_out"]
    nxt = [n["when"] + " " + n["text"] for n in r["next_week"]]
    assert "Mon 12 Oct 60 new inboxes go live, more sending capacity" in nxt, nxt
    assert "Mon 12 Oct Wave 4 starts: 3,500 more owners on Microsoft 365 + other" in nxt, nxt
    assert "All week Wave 2 · Microsoft 365: the last 1,700 first emails go out" in nxt, nxt

    p = weekly.slack_payload(r, "https://example.test/weekly?key=k&week=2026-10-05")
    text = json.dumps(p, ensure_ascii=False)
    for want in ("Weekly review · week of 5 October", "Big week, Marco.", "*Positive replies*\\n9  (5 last week)",
                 "*Calls booked*\\n1", "*Emails sent*\\n2,400", "*New owners reached*\\n1,350", "*Who said yes*\\nExample Fab and Sample Mills",
                 "*Calls booked*\\nExample Fab · Pat Doe", "*Next week*", "Open the full weekly review",
                 "Have a great weekend. Ben and the Gamic team"):
        assert want in text, want
    assert "Since" not in text and "since 10" not in text and "—" not in text
    assert all(len(b["text"]["text"]) <= 3000 for b in p["blocks"] if "text" in b and b["text"].get("type") == "mrkdwn")

    # the endpoint is a dry run unless told otherwise; posting saves the week and happens once
    d = c.post("/api/briefs/weekly-summary", json={"link": "https://example.test/weekly?key=k"}, headers=H).json()
    assert d["dry_run"] is True and d["posted"] is False and "week=" in json.dumps(d["payload"])
    sent = []
    weekly._post = lambda payload: sent.append(payload) or {"via": "test"}
    d = c.post("/api/briefs/weekly-summary", json={"dry_run": False}, headers=H).json()
    assert d["posted"] is True and len(sent) == 1, d
    d = c.post("/api/briefs/weekly-summary", json={"dry_run": False}, headers=H).json()
    assert d["posted"] is False and "already posted" in d["skipped"] and len(sent) == 1
    assert weekly._saved_weeks() == [weekly.week_start(datetime.now(timezone.utc)).isoformat()]

    # the page: locked without the key, the key sets the cookie and keeps the week, then the review renders
    assert c.get("/weekly", follow_redirects=False).status_code == 401
    rr = c.get(f"/weekly?key={VIEW_KEY}&week=2026-10-05", follow_redirects=False)
    assert rr.status_code == 303 and rr.headers["location"] == "/weekly?week=2026-10-05"
    c.cookies.set("mb_key", VIEW_KEY)
    html = c.get("/weekly").text
    for want in ("Weekly review", "Day by day", "Who said yes", "Calls booked", "Positive replies", "Booked calls", "Campaigns"):
        assert want in html, want
    store.set_setting("weekly_review:2026-09-28", json.dumps({**r, "week": "2026-09-28", "week_label": "Week of 28 September",
                                                              "note": "Saved note for an earlier week."}))
    store.set_setting(weekly.INDEX_KEY, json.dumps(["2026-09-28"]))
    old = c.get("/weekly?week=2026-09-28").text
    assert "Saved note for an earlier week." in old and "As sent on" in old
    print("weekly review tests: ok")


if __name__ == "__main__":
    run()
