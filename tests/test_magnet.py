"""Email 3 assets: valuation arithmetic is done in code, research is cached, bad benchmarks are refused, non-Advocate 404s.
Run: python tests/test_magnet.py
"""
import hashlib, json, os, sys, tempfile, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BRIEFS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DISABLE_CALENDAR_SYNC"] = "1"
os.environ["TENANT"] = "advocate"
INGEST = "ingest-key-for-tests"
os.environ["INGEST_KEY_SHA256"] = hashlib.sha256(INGEST.encode()).hexdigest()
if "weasyprint" not in sys.modules:
    stub = types.ModuleType("weasyprint")
    class _HTML:
        def __init__(self, string=""): self.s = string
        def write_pdf(self): return b"%PDF-1.4 stub"
    stub.HTML = _HTML
    sys.modules["weasyprint"] = stub
from fastapi.testclient import TestClient
import main, magnet  # noqa: E402
c = TestClient(main.app, base_url="https://testserver")
H = {"x-ingest-key": INGEST}
CALLS = []
BENCH = {"sector_label": "specialty distributors", "sales_per_employee_low": 350000, "sales_per_employee_high": 550000,
         "sales_basis": "Wholesale average about $443,000.", "ebitda_margin_low": 6, "ebitda_margin_high": 9,
         "margin_basis": "Wholesalers average about 7%.", "multiple_low": 6, "multiple_high": 8,
         "multiple_basis": "Private distribution deals averaged 6.0x to 7.2x.",
         "value_drivers": [{"title": "No customer too large", "text": "Concentration compresses multiples."}],
         "buyer_stats": [{"figure": "85%", "label": "of deals were strategic buyers"}], "buyer_source": "GF Data",
         "sources": ["CSIMarket, Wholesale efficiency (2026)"]}
REPORT = {"sector_label": "specialty distributors", "short_version": ["Deals are up."], "stats": [], "dynamics": [],
          "deals": [], "multiples": [{"segment": "$10-25M EV", "multiple": "6x", "source": "GF Data"}],
          "owner_takeaways": ["Timing matters."], "sources": ["GF Data (2026)"]}

def fake(prompt, uses=8):
    CALLS.append(prompt)
    return dict(REPORT if "briefing" in prompt else BENCH)

magnet._claude_json = fake
LEAD = {"first_name": "Greg", "last_name": "Stephens", "email": "g@amleonard.com", "company": "A.M. Leonard",
        "sector": "specialty distributors", "employees": 110}

def test_valuation_matches_hand_example():
    r = c.post("/api/magnet/generate", headers=H, json={"kind": "valuation", "lead": dict(LEAD)}).json()
    assert round(r["ev_low"] / 1e6, 1) == 13.9 and round(r["ev_high"] / 1e6, 1) == 43.6 and round(r["ev_central"] / 1e6, 1) == 26.0
    page = c.get(r["path"]).text
    assert "What A.M. Leonard might be worth today" in page and "$13.9M to $43.6M" in page and "PREPARED FOR GREG STEPHENS" in page
    assert c.get(r["pdf"]).content.startswith(b"%PDF")
    n = len(CALLS)
    c.post("/api/magnet/generate", headers=H, json={"kind": "valuation", "lead": dict(LEAD, company="Other Co")})
    assert len(CALLS) == n  # same sector + size band reuses the cached research

def test_report_and_guards():
    r = c.post("/api/magnet/generate", headers=H, json={"kind": "report", "lead": dict(LEAD)}).json()
    assert "Who is buying specialty distributors" in c.get(r["path"]).text
    assert c.post("/api/magnet/generate", json={"kind": "report", "lead": LEAD}).status_code == 401
    assert c.post("/api/magnet/generate", headers=H, json={"kind": "valuation", "lead": dict(LEAD, employees=0)}).status_code == 422
    global BENCH
    BENCH = dict(BENCH, multiple_high=60)
    assert c.post("/api/magnet/generate", headers=H, json={"kind": "valuation", "lead": dict(LEAD, sector="odd things")}).status_code == 422
    assert c.get("/m/nope").status_code == 404

if __name__ == "__main__":
    test_valuation_matches_hand_example(); test_report_and_guards(); print("magnet tests ok")
