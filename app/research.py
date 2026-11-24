"""Collect info (whiteboard box 2): deck + form + founder profile + free sources + scrape.

Each source is reduced to short extracted claims, tagged with its source. Only those
claims go forward to scoring - never whole web pages (Task 1).
"""
import json
import logging
import re

import httpx

from . import config, db, llm, privacy, tracing

log = logging.getLogger("research")
_fixtures = None


def fixtures():
    global _fixtures
    if _fixtures is None:
        p = config.DATA_DIR / "web_fixtures.json"
        _fixtures = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"pages": {}, "search_index": {}}
    return _fixtures


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", txt).strip()


def fetch(url: str) -> tuple[str, str] | None:
    """Return (title, text) or None. Offline fixtures unless LIVE_WEB=1."""
    if not url:
        return None
    if not config.LIVE_WEB:
        p = fixtures()["pages"].get(url.rstrip("/"))
        return (p["title"], p["text"]) if p else None
    try:
        r = httpx.get(url, timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (screening research bot)"})
        if r.status_code >= 400:
            return None
        title = re.search(r"(?is)<title>(.*?)</title>", r.text)
        return ((title.group(1).strip() if title else url), _strip_html(r.text)[:6000])
    except Exception as e:
        log.info("fetch failed %s: %s", url, e)
        return None


def search(company: str) -> list[str]:
    """Offline demo index of extra URLs per company. Live open-web search is app/search.py."""
    if not config.LIVE_WEB:
        return fixtures()["search_index"].get(company, [])
    return []


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower().removeprefix("www.")


def _source_type(url: str) -> str:
    if "linkedin" in url:
        return "founder_profile"
    if "news" in url:
        return "news"
    return "web"


def collect(app: dict) -> dict:
    """Returns {'claims': [...], 'gaps': [...], 'flags': [...]} and stores claims."""
    app_id, fid = app["id"], app["founder_id"]
    founder = db.q1("SELECT * FROM founders WHERE id=?", (fid,)) or {}
    fname = founder.get("name", "")
    form = db.jload(app["form_json"], {}) or {}
    db.run("DELETE FROM claims WHERE app_id=? AND source_type!='reviewer'", (app_id,))

    sources = []
    if app["deck_text"]:
        sources.append(("deck", "deck", app["deck_text"]))
    form_txt = "\n".join(f"{k}: {v}" for k, v in form.items() if str(v).strip())
    if form_txt:
        sources.append(("form", "form", form_txt))

    gaps, flags = [], []
    if config.LIVE_WEB:
        _scrape_live(app, founder, sources, gaps)
    else:
        _offline_fixtures(app, founder, sources, gaps)
    if not app.get("website"):
        gaps.append({"criterion": "research", "missing": "No company website given.",
                     "cost": "Product and traction claims rest on the deck alone."})
    if not any(t in ("founder_profile",) for t, _, _ in sources):
        gaps.append({"criterion": "founder_quality", "missing": "No independent founder profile found.",
                     "cost": "Founder background is self-reported only."})

    n = 0
    for stype, ref, text in sources:
        clean = privacy.scrub(text, fid, fname)[:5000]
        with tracing.span(f"extract_claims:{stype}", input={"source": ref, "chars": len(clean)}) as s:
            out = llm.call("extract_claims", {"source_id": ref, "source_type": stype, "text": clean},
                           app_id=app_id, step=f"extract:{stype}", mock_ctx={"text": clean})
            s["output"] = {"claims": len(out.get("claims", [])), "injection": out.get("injection_detected")}
        if out.get("injection_detected"):
            flags.append(f"Prompt-injection attempt in {ref}: \"{out.get('injection_text','')[:120]}\" - ignored.")
        for ctext in out.get("claims", [])[:15]:
            n += 1
            db.run("INSERT INTO claims(app_id,cid,text,source_type,source_ref,created_at) VALUES(?,?,?,?,?,?)",
                   (app_id, f"C{n}", ctext.strip(), stype, ref, db.now_iso()))
    # reviewer-added claims keep their ids but are renumbered after the automatic ones
    for r in db.q("SELECT id FROM claims WHERE app_id=? AND source_type='reviewer' ORDER BY id", (app_id,)):
        n += 1
        db.run("UPDATE claims SET cid=? WHERE id=?", (f"C{n}", r["id"]))
    claims = db.q("SELECT cid, text, source_type, source_ref FROM claims WHERE app_id=? ORDER BY id", (app_id,))
    # Cross-check the full source texts (not just extracted claims, which are capped per source).
    # The company's own website is founder material too; only third-party pages count as independent.
    own = _host(app.get("website") or "")
    sentences = [{"cid": stype if stype in ("deck", "form") else ref, "text": s.strip(),
                  "source_type": "own_site" if stype == "web" and own and _host(ref) == own else stype}
                 for stype, ref, text in sources for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    gaps += conflicts(sentences)
    return {"claims": claims, "gaps": gaps, "flags": flags}


def _offline_fixtures(app, founder, sources, gaps):
    urls = []
    for u in [app.get("website"), founder.get("linkedin")] + search(app["company"]):
        if u and u.rstrip("/") not in urls:
            urls.append(u.rstrip("/"))
    with tracing.span("search_and_fetch", input={"company": app["company"], "urls": urls, "mode": "offline"}) as s:
        fetched = 0
        for u in urls:
            page = fetch(u)
            if page:
                fetched += 1
                sources.append((_source_type(u), u, f"{page[0]}. {page[1]}"))
            else:
                gaps.append({"criterion": "research", "missing": f"Could not read {u}",
                             "cost": "Claims from this source are unverified."})
        s["output"] = {"fetched": fetched, "of": len(urls)}


def _scrape_live(app, founder, sources, gaps):
    """Crawl the company site (several pages), then search the open web for press and founder profiles."""
    from . import scraper
    from . import search as websearch
    starts = [u for u in [app.get("website")] if u]
    with tracing.span("scrape", input={"company": app["company"], "start_urls": starts}) as s:
        n_pages, notes = 0, []
        for u in dict.fromkeys(starts):
            res = scraper.crawl(u, app_id=app["id"])
            notes += res["notes"]
            for pg in res["pages"][:config.SCRAPER_MAX_PAGES]:
                n_pages += 1
                sources.append(("web", pg["url"], f"{pg['title']} (read {pg['fetched_at'][:10]}).\n{pg['text']}"))
        s["output"] = {"pages": n_pages, "notes": notes}
    with tracing.span("web_search", input={"company": app["company"]}) as s:
        found = websearch.run(app, founder, deck_hint=app.get("deck_text") or "")
        sources += found["sources"]
        notes += found["notes"]
        s["output"] = {"sources": [u for _, u, _ in found["sources"]], "notes": found["notes"]}
    for note in notes[:5]:
        gaps.append({"criterion": "research", "missing": note, "cost": "Claims that page would cover rest on the founder's material."})
    if app.get("website") and n_pages == 0:
        gaps.append({"criterion": "research", "missing": "Nothing could be read from the company website.",
                     "cost": "Product and traction claims rest on the deck alone."})
    if founder.get("linkedin"):
        gaps.append({"criterion": "founder_quality", "missing": "LinkedIn profile given but not read (LinkedIn forbids automated access).",
                     "cost": "Check the founder's background by hand or via the call."})


NOUNS = (r"(distributors?|distributor partners|customers|clients|users|merchants|retailers|pilots|firms|practices|"
         r"students|subscribers|travellers|workers|teams|downloads|farmers|stores|outlets|shops|kiranas|sellers|buyers|"
         r"brands|restaurants|schools|clinics|hospitals|patients|members|households|installs|partners)")
NUM_NOUN = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(\+)?\s*((?:[a-z-]+\s){0,2}?)" + NOUNS + r"\b", re.I)
TEAM_SIZE = re.compile(r"\bteam\s*(?:of|size)\s*:?\s*(\d+)(\+)?", re.I)          # "Team of 12", "Team size 5"
FRACTION = re.compile(r"\b\d[\d,]*\s+of\s+\d[\d,]*\b", re.I)                      # "9 of 16 teams" is a breakdown
FUTURE = re.compile(r"\b(asking|to reach|plan|will|target|goal|tam|sam|som|in \d+ (months|years)|next \d+ (months|years))\b", re.I)
QUALIFIERS = {"paying": "paying", "paid": "paying", "active": "active", "live": "live", "pilot": "pilot",
              "registered": "registered", "signed": "signed", "enterprise": "enterprise"}
CONNECTORS = {"per", "of", "x", "from", "in", "across", "for"}


def _key(between: str, noun: str) -> str | None:
    words = between.lower().split()
    if CONNECTORS & set(words):
        return None  # "48,000 per team", "60 x sensors": not a count of things
    q = next((QUALIFIERS[w] for w in words if w in QUALIFIERS), "")
    base = noun.lower().split()[0].rstrip("s")
    return f"{q} {base}".strip()


def conflicts(claims) -> list[dict]:
    """Deterministic cross-check: the same headline count stated differently in two places - deck vs form, or
    the founder's material vs an independent web source. Only flags when each side states ONE value for the
    same metric (e.g. 'paying customers'), so breakdowns, projections and different metrics don't trip it."""
    def nums(src_types):
        out = {}
        for c in claims:
            if c["source_type"] not in src_types or FUTURE.search(c["text"]):
                continue
            text = FRACTION.sub(" ", c["text"])
            for n, plus, between, noun in NUM_NOUN.findall(text):
                k = _key(between, noun)
                if k:
                    out.setdefault(k, set()).add((float(n.replace(",", "")), bool(plus)))
            for n, plus in TEAM_SIZE.findall(text):
                out.setdefault("team member", set()).add((float(n), bool(plus)))
        # Headline figure per metric = the largest count stated (sub-counts like 'pilot with 12' are smaller).
        return {k: {max(v)} for k, v in out.items()}

    def compare(a, b, la, lb):
        res = []
        for k, vals in a.items():
            (v, _), = list(vals)[:1]
            for w, plus in b.get(k, []):
                lo, hi = min(v, w), max(v, w)
                if hi and (hi - lo) / hi > 0.05 and not (plus and w <= v <= w * 1.5):
                    res.append({"criterion": "conflict",
                                "missing": f"{la} and {lb} disagree on {k}s: {la} says {v:g}, {lb} says {w:g}{'+' if plus else ''}.",
                                "cost": "Ask the founder which is current before relying on it."})
        return res

    deck, form = nums({"deck"}), nums({"form"})
    own_site = nums({"own_site"})
    web = nums({"web", "news", "founder_profile"})
    founder_side = {**own_site, **form, **deck}  # deck wins where they overlap; deck vs form is checked separately
    found = compare(deck, form, "deck", "form") + compare(founder_side, web, "founder material", "independent sources")
    seen, out = set(), []
    for f in found:
        if f["missing"] not in seen:
            seen.add(f["missing"]); out.append(f)
    return out[:4]
