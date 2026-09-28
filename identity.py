"""Owner identity gate for the pre-call brief.

Why this exists: on 2026-09-25 the brief for Digital Dental Leaders profiled the wrong Eric True
(a project engineer at TREKK Design Group) instead of Digital Dental Leaders' co-founder and CEO.
The upstream owner lookup had searched by name only. Marco caught it in Slack.

Rule enforced here: an owner profile is only used when it places THIS person at THIS company
(company name, website domain or email domain). Anything else is discarded, the service then
does its own name + company research, and when nothing can be verified the brief says so plainly
instead of describing a stranger.
"""
import os
import re
import json
import html as htmllib
from typing import Optional, Tuple

import requests

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
IDENTITY_MODEL = os.environ.get("IDENTITY_MODEL", "claude-sonnet-4-6")

# Phrases an upstream profile uses when it has found someone else with the same name.
RED_FLAGS = re.compile(
    r"no indication|not (?:the |a )?(?:founder|owner|co-?founder)|does not appear|doesn't appear|"
    r"could not (?:confirm|verify|find)|couldn't (?:confirm|verify|find)|unable to (?:confirm|verify|find)|"
    r"no (?:clear |direct )?(?:connection|link|affiliation)|different (?:person|individual|company)|"
    r"may not be the same|not associated with",
    re.I,
)
GENERIC_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
                         "live.com", "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net"}
TEAM_PATHS = ["", "/about", "/about-us", "/our-story", "/team", "/our-team", "/leadership", "/company",
              "/who-we-are", "/meet-the-team"]


def _domain(url_or_email: Optional[str]) -> str:
    s = (url_or_email or "").strip().lower()
    if "@" in s:
        s = s.split("@", 1)[1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s


def anchors(req) -> dict:
    email_dom = _domain(req.email)
    return {
        "name": (req.lead_name or "").strip(),
        "company": (req.company_name or "").strip(),
        "site": _domain(req.website),
        "email_domain": "" if email_dom in GENERIC_EMAIL_DOMAINS else email_dom,
        "title": (getattr(req, "title", None) or "").strip(),
    }


def _claude_json(client, prompt: str, max_tokens: int = 600) -> dict:
    msg = client.messages.create(model=IDENTITY_MODEL, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}


def check_profile(client, a: dict, summary: str) -> Tuple[str, str]:
    """-> (MATCH | MISMATCH | UNCLEAR, reason). Only MATCH may be used in a brief."""
    if not summary or not summary.strip():
        return "UNCLEAR", "no profile supplied"
    if RED_FLAGS.search(summary):
        return "MISMATCH", "profile text itself says it could not tie this person to the company"
    prompt = f"""You verify identity before a profile goes into an M&A pre-call brief.

Person we are meeting: {a['name'] or 'unknown'}
Their company: {a['company'] or 'unknown'} (website {a['site'] or 'unknown'}, email domain {a['email_domain'] or 'unknown'})
Title on our record: {a['title'] or 'not recorded'}

Candidate profile text:
\"\"\"{summary[:4000]}\"\"\"

Does this profile describe THIS person at THIS company, rather than someone else with the same name?
- MATCH only if the profile explicitly places the person at {a['company'] or 'the company'} (by name or domain) as a current employee, founder or owner.
- MISMATCH if the profile's current role is at a different employer, or it says there is no link to the company.
- UNCLEAR otherwise.
Return ONLY JSON: {{"verdict": "MATCH|MISMATCH|UNCLEAR", "employer_named_in_profile": "...", "reason": "one sentence"}}"""
    try:
        out = _claude_json(client, prompt, 300)
    except Exception as e:  # a failed check never lets a profile through
        return "UNCLEAR", f"identity check failed: {e}"
    v = str(out.get("verdict", "UNCLEAR")).upper()
    return (v if v in ("MATCH", "MISMATCH", "UNCLEAR") else "UNCLEAR"), str(out.get("reason", ""))


def _serper(q: str) -> list:
    if not SERPER_API_KEY:
        return []
    try:
        r = requests.post("https://google.serper.dev/search", timeout=12,
                          headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                          json={"q": q, "num": 8})
        return r.json().get("organic", []) or []
    except Exception:
        return []


def _site_mentions(site: str, name: str) -> list:
    """Sentences on the company's own site that mention the person's surname."""
    if not site or not name:
        return []
    last = name.split()[-1]
    found, seen = [], set()
    for path in TEAM_PATHS:
        try:
            r = requests.get(f"https://{site}{path}", timeout=8, headers={"User-Agent": "Mozilla/5.0 (Gamic brief bot)"})
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
        except Exception:
            continue
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text)
        for s in re.split(r"(?<=[.!?])\s+", text):
            if re.search(rf"\b{re.escape(last)}\b", s, re.I) and len(s) < 400 and s not in seen:
                seen.add(s)
                found.append(f"{site}{path or '/'}: {s.strip()}")
        if len(found) >= 8:
            break
    return found[:8]


def _claude_web_evidence(client, a: dict) -> list:
    """Keyless fallback: Claude's server-side web search, facts kept only when a source names both person and company."""
    if not client or not a["name"] or not (a["company"] or a["site"]):
        return []
    ask = f"""Search the web for {a['name']} of {a['company'] or a['site']} (website {a['site'] or 'unknown'}).
Report ONLY facts from sources that name BOTH {a['name']} and {a['company'] or a['site']}. Ignore anyone else with the same name.
Return ONLY JSON: {{"evidence": [{{"url": "...", "fact": "one sentence stating what the source says about {a['name']} at {a['company'] or a['site']}"}}]}}
Return {{"evidence": []}} if no source names both."""
    messages = [{"role": "user", "content": ask}]
    try:
        for _ in range(3):  # resume server-side pause_turn at most twice
            resp = client.messages.create(model=IDENTITY_MODEL, max_tokens=4000, messages=messages,
                                          tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}])
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": ask}, {"role": "assistant", "content": resp.content}]
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\{.*\}", text, re.S)
        items = (json.loads(m.group(0)).get("evidence") if m else None) or []
    except Exception as e:
        print(json.dumps({"event": "owner_identity_web_search_failed", "error": str(e)[:200]}), flush=True)
        return []
    last = a["name"].split()[-1].lower()
    out = []
    for it in items[:8]:
        fact, url = str(it.get("fact", "")).strip(), str(it.get("url", "")).strip()
        if fact and last in fact.lower():
            out.append(f"{fact} | {url}")
    return out


def gather_evidence(a: dict, client=None) -> list:
    """Search results and site sentences that name BOTH the person and the company."""
    name, company, site = a["name"], a["company"], a["site"]
    if not name or not (company or site):
        return []
    last = name.split()[-1].lower()
    comp_tokens = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if len(t) > 2 and t not in
                   {"inc", "llc", "ltd", "corp", "company", "the", "and", "group", "co"}]
    queries = [f'"{name}" "{company}"'] if company else []
    if site:
        queries.append(f'"{name}" {site}')
    queries.append(f'site:linkedin.com/in "{name}" "{company or site}"')
    ev, seen = [], set()
    for q in queries:
        for r in _serper(q):
            blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
            link = r.get("link", "")
            mentions_person = last in blob
            mentions_company = (site and site in (blob + " " + link.lower())) or \
                (comp_tokens and sum(t in blob for t in comp_tokens) >= max(1, min(2, len(comp_tokens))))
            if mentions_person and mentions_company and link not in seen:
                seen.add(link)
                ev.append(f"{r.get('title', '')} | {link} | {r.get('snippet', '')}")
    if not ev:
        ev += _claude_web_evidence(client, a)
    ev += _site_mentions(site, name)
    return ev[:14]


def write_verified_profile(client, a: dict, evidence: list) -> Optional[str]:
    prompt = f"""Write the Owner Profile section of an M&A pre-call brief for {a['name']} at {a['company']}.

Use ONLY the evidence below. Every line in the evidence mentions this person and this company.
Rules:
- State their role at {a['company']} and whether they founded or own it, only as far as the evidence says.
- Include background, tenure or age only if the evidence ties it to this person.
- Never mention any other employer unless the evidence shows it is this same person's history.
- If a fact is not in the evidence, leave it out. Do not guess ages.
- 2 to 4 short bullet points starting with "• ", plain US English, no em dashes, no headings.

Evidence:
{chr(10).join('- ' + e for e in evidence)}

Return ONLY JSON: {{"profile": "the bullet points", "confidence": "high|medium|low"}}"""
    try:
        out = _claude_json(client, prompt, 500)
    except Exception:
        return None
    prof = str(out.get("profile", "")).strip()
    return prof or None


def unverified_profile(a: dict) -> str:
    role = f"{a['title']} per our lead record" if a["title"] else "role not recorded on our lead record"
    where = f" (email at {a['email_domain']})" if a["email_domain"] else ""
    return (f"• {a['name'] or 'The contact'}: {role}{where}.\n"
            f"• We could not verify a public profile for {a['name'] or 'this person'} at {a['company'] or 'this company'}, "
            f"so their tenure, background and age are unconfirmed. Confirm their role and ownership early in the call.")


def resolve_owner_profile(client, req) -> Tuple[str, str, str]:
    """-> (owner_text, status, reason). status: verified_upstream | verified_research | unverified."""
    a = anchors(req)
    verdict, reason = check_profile(client, a, req.owner_summary or "")
    if verdict == "MATCH":
        return req.owner_summary, "verified_upstream", reason
    evidence = gather_evidence(a, client)
    if evidence:
        prof = write_verified_profile(client, a, evidence)
        if prof:
            v2, r2 = check_profile(client, a, prof)
            if v2 == "MATCH":
                return prof, "verified_research", f"upstream {verdict}: {reason} | rebuilt from {len(evidence)} name+company sources"
    return unverified_profile(a), "unverified", f"upstream {verdict}: {reason} | no verifiable name+company source"
