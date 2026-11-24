"""Open-web search (whiteboard: "free sources"): press, directories and founder profiles beyond the
applicant's own site, via Gemini's built-in Google Search tool.

Trust rules:
* Only results Google actually returned count: a URL must match a search citation from the same answer, or
  be opened successfully by the scraper. URLs the model can't back up are dropped.
* Same-name companies are the main risk (most early startups share a name with someone). The model must say
  why a result is the same company (website, city, product, founder); "unsure" results are not used and the
  near-miss is reported as a gap instead.
* Each kept result is opened by the scraper (one page) so claims rest on the page itself; if it can't be
  opened (paywall, LinkedIn), the search summary is used and labelled as such.
* Founder names go to this search step only (to find profiles). Everything passed on to extraction and scoring
  is scrubbed back to the founder ID as usual.
"""
import logging
from urllib.parse import urlparse

import httpx

from . import config, llm, privacy, scraper

log = logging.getLogger("search")
KIND_TO_SOURCE = {"news": "news", "founder_profile": "founder_profile"}


def _domain(u: str) -> str:
    h = (urlparse(u if "://" in u else "https://" + u).hostname or "").lower()
    return h.removeprefix("www.")


def _cited(citations) -> tuple[set, set]:
    """Domains and exact URLs Google returned. Grounding URLs are often redirect links whose title is the
    source's domain, so titles count as domains too."""
    domains, urls = set(), set()
    for c in citations or []:
        url, title = (c.get("url") or ""), (c.get("title") or "")
        if url:
            urls.add(scraper.norm(url))
            d = _domain(url)
            if "vertexaisearch" not in d and "google" not in d:
                domains.add(d)
        if title and "." in title and " " not in title.strip():
            domains.add(_domain(title.strip()))
    return domains, urls


def _backed(url: str, domains: set, urls: set) -> bool:
    d = _domain(url)
    return scraper.norm(url) in urls or any(d == x or d.endswith("." + x) or x.endswith("." + d) for x in domains)


REDIRECT_HOST = "vertexaisearch.cloud.google.com"


def _resolve(url: str) -> tuple[str | None, bool]:
    """Gemini reports search results as Google's grounding redirect links. Those links only exist for pages
    Google actually returned, so one hop to read the real address both resolves and verifies the result.
    Returns (real_url, came_from_google); (None, False) if a Google link doesn't resolve."""
    u = urlparse(url)
    if (u.hostname or "").lower() != REDIRECT_HOST or not u.path.startswith("/grounding-api-redirect/"):
        return url, False
    try:
        r = httpx.get(url, follow_redirects=False, timeout=8, headers={"User-Agent": scraper.USER_AGENT})
        loc = r.headers.get("location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc.startswith(("http://", "https://")):
            return loc, True
    except httpx.HTTPError as e:
        log.info("could not resolve grounding link: %s", e)
    return None, False


def run(app: dict, founder: dict, *, deck_hint: str = "") -> dict:
    """Returns {'sources': [(source_type, url, text)], 'notes': [str]}. Never raises."""
    if config.LLM_MODE != "live":
        return {"sources": [], "notes": ["Open-web search needs Gemini (LLM_MODE=live); only the company website was read."]}
    names = [n for n in [(founder.get("name") or "").strip()] if n and n.lower() != "unknown"] if config.SEARCH_FOUNDERS else []
    own = _domain(app.get("website") or "") if app.get("website") else ""
    hint = privacy.scrub(deck_hint or "", app.get("founder_id", ""), founder.get("name", ""))[:400]
    try:
        out = llm.call("web_search", {
            "company": app["company"], "website": own or "(none given)", "sector": app.get("sector") or "unknown",
            "city": app.get("city") or "unknown", "founders": ", ".join(names) or "(not searched)", "hint": hint,
        }, app_id=app["id"], step="search:google")
    except Exception as e:
        log.info("search failed for %s: %s", app["company"], e)
        return {"sources": [], "notes": [f"Web search failed ({type(e).__name__}); only the company website was read."]}

    domains, urls = _cited(out.get("_citations"))
    sources, notes, unsure = [], [], []
    for r in out.get("results", [])[:12]:
        url, via_google = _resolve((r.get("url") or "").strip())
        if url is None:
            notes.append(f"A search result link from Google could not be resolved ({r.get('title') or 'untitled'}); not used.")
            continue
        if not url or (own and _domain(url) == own):
            continue  # the company's own site is read by the scraper already
        if r.get("same_entity") not in ("yes", "likely"):
            unsure.append(f"{r.get('title') or _domain(url)} ({_domain(url)})")
            continue
        page = None
        try:
            scraper.check_url(url)
            res = scraper.crawl(url, app_id=app["id"], max_pages=1, budget_s=config.SEARCH_PAGE_BUDGET_S)
            page = res["pages"][0] if res["pages"] else None
        except scraper.Blocked:
            pass  # e.g. LinkedIn: never opened; the search summary is used if Google backs it
        if not page and not (via_google or _backed(url, domains, urls)):
            notes.append(f"Search suggested {url[:120]}, but Google returned no matching source and the page could not "
                         "be opened; not used.")
            continue
        stype = KIND_TO_SOURCE.get(r.get("kind"), "web")
        why = f"Same company because: {r.get('match_reason', '')[:200]}"
        if page:
            text = f"{page['title']} ({r.get('publisher') or _domain(url)}, {r.get('date') or 'undated'}; read " \
                   f"{page['fetched_at'][:10]}). {why}\n{page['text']}"
        else:
            facts = "\n".join(f"- {f}" for f in (r.get("facts") or [])[:8])
            text = f"{r.get('title') or url} ({r.get('publisher') or _domain(url)}, {r.get('date') or 'undated'}; " \
                   f"search summary - page not opened). {why}\n{facts}"
        sources.append((stype, url, text))
        if len(sources) >= config.SEARCH_MAX_RESULTS:
            break
    if unsure:
        notes.append("Found results for similarly named organisations that may not be this company, not used: "
                     + "; ".join(unsure[:3]) + ".")
    if not sources:
        notes.append(f"Web search found no independent source about {app['company']}.")
    return {"sources": sources, "notes": notes}
