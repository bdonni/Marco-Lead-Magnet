"""Live campaign page: numbers arrive through the ingest API and show behind the same private link.

Run: python tests/test_campaigns.py
"""
import hashlib
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
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
from fastapi.testclient import TestClient  # noqa: E402

STATS = {
    "generated_at": "2026-10-07T20:00:00+00:00",
    "program": {"since": "2026-09-10", "owners_emailed": 4321, "emails_sent": 9876, "positives": 35,
                "rate_one_in": 123, "positives_this_week": 9, "positives_today": 4, "queued": 2468},
    "days": [{"day": "2026-10-06", "e1": 150, "followups": 1060, "positives": 5},
             {"day": "2026-10-07", "e1": 1338, "followups": 76, "positives": 8}],
    "campaigns": [
        {"id": 1, "area": "Manufacturing", "wave": "October wave 3", "segment": "Google Workspace", "state": "sending",
         "owners_emailed": 684, "queued": 406, "in_progress": 652, "steps": [684, 0, 0], "positives": 6,
         "rate_one_in": 114, "first_send": "2026-10-07", "last_send": "2026-10-07", "days_left": 1},
        {"id": 2, "area": "Manufacturing", "wave": "October wave 3", "segment": "Microsoft 365 + other",
         "state": "starting", "starts": "2026-10-12", "owners_emailed": 0, "queued": 3503, "steps": [0, 0, 0]},
        {"id": 3, "area": "Manufacturing", "wave": "", "segment": "A", "group": "September tests", "state": "done",
         "owners_emailed": 700, "positives": 5, "first_send": "2026-09-10", "last_send": "2026-09-23"},
        {"id": 4, "area": "Manufacturing", "wave": "", "segment": "B", "group": "September tests", "state": "done",
         "owners_emailed": 533, "positives": 0, "first_send": "2026-09-14", "last_send": "2026-09-30"},
    ],
    "recent_positives": [{"company": "Example Fabrication <b>", "first_name": "Pat", "day": "2026-10-07", "step": 1,
                          "campaign": "Manufacturing · October wave 2"}],
    "inboxes": {"count": 120, "healthy": 118, "avg_health": 99.0, "daily_capacity": 1800,
                "joining": {"count": 60, "date": "2099-10-12"}},
}


def run():
    c = TestClient(main.app)
    # nothing to show and no tab before the first push
    assert c.get("/campaigns", follow_redirects=False).status_code == 401
    r = c.get(f"/briefs?key={VIEW_KEY}", follow_redirects=False)
    assert r.status_code == 303
    c.cookies.set("mb_key", VIEW_KEY)
    assert "<nav class=\"sitenav\">" not in c.get("/briefs").text
    assert "appear here within 10 minutes" in c.get("/campaigns").text

    # a wrong ingest key is refused; the right one stores the numbers
    assert c.post("/api/briefs/ingest", json={"settings": {"campaign_stats": STATS}},
                  headers={"x-ingest-key": "nope"}).status_code == 401
    r = c.post("/api/briefs/ingest", json={"settings": {"campaign_stats": STATS}}, headers={"x-ingest-key": INGEST_KEY})
    assert r.json().get("settings") == ["campaign_stats"], r.text

    page = c.get("/campaigns").text
    for want in ("Positive replies", "4,321", "1 in 123", "2,468", "October wave 3", "684 of 1,090",
                 "about 1 sending day left", "Starts Mon 12 Oct", "3,503", "September tests", "2 campaigns",
                 "1,233", "1 in 247", "+60 inboxes", "Example Fabrication &lt;b&gt;", "http-equiv=\"refresh\""):
        assert want in page, want
    assert "Example Fabrication <b>" not in page
    assert "call times" not in page.split('class="foot"')[1]
    assert "<nav class=\"sitenav\">" in c.get("/briefs").text

    # a fresh client without the cookie still gets the locked page, and ?key= sets the cookie
    c2 = TestClient(main.app)
    assert c2.get("/campaigns", follow_redirects=False).status_code == 401
    assert c2.get("/campaigns?key=wrong", follow_redirects=False).status_code == 401
    r = c2.get(f"/campaigns?key={VIEW_KEY}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/campaigns"
    print("campaign page tests: ok")


if __name__ == "__main__":
    run()
