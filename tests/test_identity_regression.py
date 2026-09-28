"""Regression test for the wrong-person owner profile (Digital Dental Leaders, 2026-09-25).
Needs ANTHROPIC_API_KEY and SERPER_API_KEY. Run: python3 tests/test_identity_regression.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from anthropic import Anthropic
import identity

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def req(**kw):
    base = dict(lead_name=None, first_name=None, email=None, company_name=None, website=None, company_linkedin=None,
                location=None, company_type=None, founded_year=None, business_summary=None, recent_news=None,
                owner_summary=None, title=None)
    base.update(kw)
    return types.SimpleNamespace(**base)

WRONG = ("Profile Overview of Eric True\n• Role: Eric True is currently a Project Engineer at TREKK Design Group, LLC since "
         "September 2025. There is no indication in his LinkedIn profile or on the company website that he is the founder "
         "or owner of Digital Dental Leaders.\n• Age Range: Eric's LinkedIn suggests he graduated around 2019.")
RIGHT = ("• Eric True is the CEO and co-founder of Digital Dental Leaders, a digital dental laboratory in Southern California "
         "he started with partner Ben Bixby.\n• He has led the business since it was founded.")
fails = 0
def check(label, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + label + (f" | {detail}" if detail else ""))
    fails += 0 if cond else 1

# 1. the exact wrong profile from 25 Sep must never reach the brief
r1 = req(lead_name="Eric True", email="eric@digitaldentalleaders.com", company_name="Digital Dental Leaders",
         website="https://digitaldentalleaders.com", owner_summary=WRONG)
text, status, why = identity.resolve_owner_profile(client, r1)
check("wrong-person profile rejected", status != "verified_upstream", status)
check("brief never mentions TREKK", "TREKK" not in text.upper())
if os.environ.get("SERPER_API_KEY"):
    check("rebuilt from name+company sources", status == "verified_research", why)
    check("rebuilt profile names the right company", "Digital Dental Leaders" in text, text[:160].replace("\n", " "))
print("   owner text:", text.replace("\n", " ")[:300])

# 1b. same wrong person, stated confidently with no giveaway phrase - the model check must still catch it
QUIET = "• Eric True is a Project Engineer at TREKK Design Group, LLC, which he joined in September 2025 after graduating in 2019."
v, why_q = identity.check_profile(client, identity.anchors(r1), QUIET)
check("quiet wrong-person profile caught by model check", v != "MATCH", f"{v}: {why_q}")

# 2. a profile that does place him at the company passes untouched
text2, status2, why2 = identity.resolve_owner_profile(client, req(lead_name="Eric True", email="eric@digitaldentalleaders.com",
    company_name="Digital Dental Leaders", website="digitaldentalleaders.com", owner_summary=RIGHT))
check("correct profile accepted", status2 == "verified_upstream" and text2 == RIGHT, why2)

# 3. nobody verifiable -> plain 'unverified', no invented facts
text3, status3, why3 = identity.resolve_owner_profile(client, req(lead_name="Zorblat Quenner", email="zq@nonexistent-fab-shop-4471.com",
    company_name="Quenner Precision Fabrication 4471", website="nonexistent-fab-shop-4471.com", owner_summary="", title="Owner"))
check("unverifiable person marked unverified", status3 == "unverified", why3)
check("unverified text keeps our record title", "Owner per our lead record" in text3)

print("RESULT:", "ALL PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
