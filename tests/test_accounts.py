"""Accounts: invite -> set password -> sign in -> pages open; wrong password, removal and expiry lock again.
Run: python tests/test_accounts.py
"""
import hashlib, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
INGEST = "ingest-key-for-tests"
os.environ["INGEST_KEY_SHA256"] = hashlib.sha256(INGEST.encode()).hexdigest()
os.environ["DASHBOARD_KEY_SHA256"] = hashlib.sha256(b"view-key").hexdigest()
import types
if "weasyprint" not in sys.modules:
    stub = types.ModuleType("weasyprint")
    class _HTML:
        def __init__(self, string=""):
            self.s = string
        def write_pdf(self):
            return b"%PDF-1.4 stub"
    stub.HTML = _HTML
    sys.modules["weasyprint"] = stub
from fastapi.testclient import TestClient
import main  # noqa: E402
c = TestClient(main.app, base_url="https://testserver")

def test_flow():
    r = c.get("/briefs", follow_redirects=False)
    assert r.status_code == 401 and "private access link" in r.text  # no accounts yet: old message
    inv = c.post("/api/briefs/accounts", headers={"x-ingest-key": INGEST}, json={"action": "invite", "email": "Gene@Example.com"}).json()
    tok = inv["link"].rsplit("/", 1)[1]
    assert "gene@example.com" in c.get(f"/invite/{tok}").text
    r = c.post(f"/invite/{tok}", content="password=short&confirm=short", headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert "at least 10" in r.text
    r = c.post(f"/invite/{tok}", content="password=correct-horse-1&confirm=correct-horse-1", headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/briefs").status_code == 200  # signed in by the invite
    assert "expired" in c.get(f"/invite/{tok}").text  # one-time link
    c.get("/logout")
    r = c.get("/briefs", follow_redirects=False)
    assert r.status_code == 401 and 'action="/login"' in r.text  # accounts exist: sign-in form
    r = c.post("/login", content="email=gene@example.com&password=wrong-password&next=/briefs", headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 401
    r = c.post("/login", content="email=gene@example.com&password=correct-horse-1&next=//evil.com", headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/briefs"  # no open redirect
    assert c.get("/briefs").status_code == 200
    c.post("/api/briefs/accounts", headers={"x-ingest-key": INGEST}, json={"action": "remove", "email": "gene@example.com"})
    assert c.get("/briefs", follow_redirects=False).status_code == 401  # removal ends the session
    assert c.post("/api/briefs/accounts", json={"action": "list"}).status_code == 401  # admin needs the ingest key

def test_pt_times():
    import dashboard, tenant
    orig = tenant.get
    tenant.get = lambda k, d=None: "PT" if k == "home_tz" else orig(k, d)
    try:
        day, hours = dashboard._fmt_call("2026-10-13T17:00:00Z")
        assert hours == "10:00 AM PT · 12:00 PM CT · 1:00 PM ET", hours
    finally:
        tenant.get = orig

if __name__ == "__main__":
    test_flow(); test_pt_times(); print("accounts tests: ok")
