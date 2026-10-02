"""Company research for briefs that arrive without Clay's enrichment (the direct Smartlead "Booked" path).

Clay used to supply founded year, city, company type and a "recent developments" Claygent. The direct trigger
skips Clay, so this fills the same facts from the web, keeping only what sources say about this company.
"""
import json
import re
from typing import Optional

from briefview import clean_place


def company_research(client, model: str, company: Optional[str], site: Optional[str], state: Optional[str]) -> dict:
    if not client or not (company or site):
        return {}
    ask = f"""Research the company "{company or site}" (website {site or 'unknown'}{', based in ' + state if state else ''}) for an M&A advisor's pre-call brief.
Use only sources that are clearly about this company: its own website, its LinkedIn page, business directories, local news, press releases. Ignore companies with similar names.
Return ONLY JSON:
{{"founded_year": "YYYY or empty",
  "hq": "City, ST or empty",
  "employees": "headcount or range and where it comes from, e.g. '11-50 (LinkedIn)', or empty",
  "ownership": "ownership as the sources state it, e.g. 'founder-owned', 'family-owned, second generation', 'acquired by X in 2023', 'PE-backed (Y)', or empty",
  "revenue": "only if a source states it, with the source, else empty",
  "recent_developments": ["up to 4 items from the last 24 months, each 'Mon YYYY: what happened', under 20 words"],
  "facts": ["up to 5 other facts useful for a sale conversation (products, customers, certifications, facilities), each under 20 words"]}}
Leave a field empty rather than guess. No em dashes."""
    messages = [{"role": "user", "content": ask}]
    try:
        for _ in range(3):  # resume server-side pause_turn at most twice
            resp = client.messages.create(model=model, max_tokens=2500, messages=messages,
                                          tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}])
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": ask}, {"role": "assistant", "content": resp.content}]
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(json.dumps({"event": "company_research_failed", "company": company, "error": str(e)[:200]}), flush=True)
        return {}
    out = {}
    for k in ("founded_year", "hq", "employees", "ownership", "revenue"):
        v = str(data.get(k) or "").strip().replace("—", " - ").replace("–", " - ")
        if v and v.lower() not in ("unknown", "n/a", "none", "empty"):
            out[k] = v
    if out.get("hq"):
        hq = clean_place(out["hq"])
        if hq:
            out["hq"] = hq
        else:
            out.pop("hq")
    for k in ("recent_developments", "facts"):
        items = [str(x).strip().replace("—", " - ") for x in (data.get(k) or []) if str(x).strip()]
        if items:
            out[k] = items[:5]
    return out
