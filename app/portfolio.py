"""Portfolio conflict check (Partner notes, Meeting 4).

Riya: "Any new application should be checked against the full list of portfolio companies, not just ones
I remember." Kushal: "I would rather get a flag on a false alarm than miss a real conflict."
So this is deliberately sensitive: any keyword overlap raises a HOLD for Kushal; it never rejects.
"""
import json
import re

from . import config

_cache = None


def load() -> dict:
    global _cache
    if _cache is None:
        p = config.DATA_DIR / "portfolio.json"
        _cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"complete": False, "companies": []}
    return _cache


def companies() -> list[dict]:
    return load().get("companies", [])


def is_complete() -> bool:
    return bool(load().get("complete"))


def summary_text() -> str:
    cs = companies()
    if not cs:
        return "(no portfolio list loaded)"
    head = "" if is_complete() else "(PARTIAL list - the full portfolio was not provided)\n"
    return head + "\n".join(f"- {c['name']} ({c['sector']}): {c['what']}" for c in cs)


def overlaps(company: str, texts: list[str]) -> list[dict]:
    """Return [{company, matched, evidence}] for portfolio companies whose keywords appear in the applicant's text."""
    blob = " ".join(texts).lower()
    out = []
    for c in companies():
        if c["name"].lower() == (company or "").lower():
            continue
        hits = [k for k in c.get("keywords", []) if re.search(rf"\b{re.escape(k.lower())}s?\b", blob)]
        if hits:
            out.append({"company": c["name"], "matched": hits[:4],
                        "evidence": f"Applicant text mentions {', '.join(hits[:4])}; {c['name']} is: {c['what']}"})
    return out
