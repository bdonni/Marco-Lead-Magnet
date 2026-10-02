"""One readable view of a brief, shared by the site and the PDF so both always show the same thing.

Every section is a short list of bullets. Briefs written before 2026-10-02 stored paragraphs; those are split
into sentence bullets here instead of being shown as blocks of text.
"""
import re
from datetime import datetime
from typing import Optional

NO_NEWS = {"no significant news found", "no news found", "none", "n/a", ""}
_LEAD_INS = re.compile(r"^(profile( overview)? (of|for) [^:\n]+:?|summary:?|overall,?\s*)", re.I)


def points(text: Optional[str], limit: int = 6) -> list:
    """Paragraph or bullet text -> short bullet strings."""
    if not text:
        return []
    s = str(text).replace("\r", "").replace("—", " - ").replace("–", " - ")
    lines = [l.strip() for l in s.split("\n") if l.strip()]
    bullets = [re.sub(r"^[•\-*]\s*", "", l) for l in lines if re.match(r"^[•\-*]\s", l)]
    if not bullets:
        bullets = []
        for para in lines:
            para = _LEAD_INS.sub("", para).strip()
            for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", para):
                sent = sent.strip()
                if len(sent) >= 12:
                    bullets.append(sent)
    out = []
    for b in bullets:
        b = _LEAD_INS.sub("", b).strip()
        if b and b not in out:
            out.append(b)
    return out[:limit]


def clean_place(v: Optional[str]) -> Optional[str]:
    """'US correspondence address: 313 Stamford Dr, Newark, DE 19711 (Nextdoor); facility...' -> 'Newark, DE'."""
    if not v:
        return None
    s = re.sub(r"\([^)]*\)", "", str(v)).strip()
    if len(s) <= 40 and ";" not in s and ":" not in s and not re.search(r"\d", s):
        return s.strip(" ,.") or None
    m = re.search(r"([A-Z][A-Za-z.' -]{1,30}),\s*([A-Z]{2})\b", s)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"
    first = s.split(";")[0].split(":")[-1].strip(" ,.")
    return first if 0 < len(first) <= 40 else None


def clean_fact(v: Optional[str], limit: int = 110) -> Optional[str]:
    """Drop '(per site.com)' style source notes and cap the length."""
    if not v:
        return None
    s = re.sub(r"\s*\((per|source|via)\b[^)]*\)", "", str(v), flags=re.I).strip()
    return (s[:limit - 1].rstrip() + "…") if len(s) > limit else (s or None)


def _years(founded: Optional[str]) -> Optional[int]:
    try:
        y = int(str(founded).strip()[:4])
        return datetime.now().year - y if 1600 < y <= datetime.now().year else None
    except (TypeError, ValueError):
        return None


def facts(req: dict, booking: Optional[dict] = None) -> list:
    """At-a-glance facts as (label, value) pairs, blanks dropped."""
    b = booking or {}
    lead = req.get("lead_name") or b.get("lead_name")
    title = req.get("title") or b.get("title")
    founded = req.get("founded_year")
    yrs = _years(founded)
    loc = clean_place(req.get("location")) or clean_place(b.get("location"))
    site = (req.get("website") or b.get("website") or "").replace("https://", "").replace("http://", "").strip("/")
    rows = [
        ("Contact", f"{lead}{', ' + title if title else ''}" if lead else None),
        ("Location", loc),
        ("Founded", f"{founded} ({yrs} years)" if founded and yrs else founded),
        ("Size", clean_fact(req.get("employees"), 60)),
        ("Ownership", clean_fact(req.get("ownership") or req.get("company_type"))),
        ("Revenue", clean_fact(req.get("revenue"), 80)),
        ("Website", site or None),
        ("Email", req.get("email") or b.get("email")),
    ]
    return [(k, str(v)) for k, v in rows if v]


def view(req: dict, a: dict, booking: Optional[dict] = None) -> dict:
    news = req.get("recent_news") or ""
    recent = [] if news.strip().rstrip(".").lower() in NO_NEWS else points(news, 4)
    return {
        "facts": facts(req, booking),
        "walking_in": a.get("walking_in") or points(a.get("marco_briefing_note"), 5),
        "they_said": a.get("they_said") or [],
        "confirm": a.get("confirm_on_call") or [],
        "why_now": a.get("why_now") or points(a.get("motivation_hypothesis"), 3),
        "business": a.get("business_points") or points(req.get("business_summary"), 6),
        "owner": a.get("owner_points") or points(req.get("owner_summary"), 6),
        "strengths": [s for s in (a.get("key_strengths") or []) if s],
        "recent": recent,
    }


SECTIONS = [  # (key, title) in reading order for the call
    ("walking_in", "Walking in"),
    ("they_said", "In their words"),
    ("confirm", "Confirm on the call"),
    ("why_now", "Why they might talk now"),
    ("business", "The business"),
    ("owner", "The owner"),
    ("strengths", "Deal strengths"),
    ("recent", "Recent developments"),
]
