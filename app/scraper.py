"""Website scraper for the research step (whiteboard: "scrape").

Reads an applicant's site the way an analyst would: the homepage, then the few pages that hold facts
(about, team, pricing, customers, case studies, press, blog), within a page and time budget.

Safety and manners:
* Only http/https to public internet addresses. Every hop (including redirects) is resolved and checked, so a
  founder can't point us at internal addresses (SSRF).
* robots.txt is respected; the bot names itself; one request at a time per site with a short pause.
* Size, type and time limits per page; nothing is executed.
* Pages are cached in the database with the date they were read, so re-screening doesn't re-hit the site and
  every claim can be traced to the page and date it came from.
* Sites that build their text with JavaScript come back nearly empty; for those, Gemini's URL-reading tool
  fetches the page on Google's side (live mode only).
* LinkedIn and other sites that forbid automated access are never scraped.
"""
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from . import config, db

log = logging.getLogger("scraper")

USER_AGENT = "AllInScreeningBot/1.0 (first-pass research on companies that applied to the fund)"
NO_SCRAPE_HOSTS = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "crunchbase.com")
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|mp4|mov|zip|docx?|xlsx?|pptx?|css|js|ico|woff2?)$", re.I)
# Pages that usually hold the facts a screener needs, best first.
WANTED = [("about", 9), ("team", 9), ("founder", 9), ("people", 7), ("customer", 8), ("case-stud", 8),
          ("case stud", 8), ("clients", 7), ("pricing", 7), ("plans", 5), ("product", 5), ("how-it-works", 5),
          ("press", 6), ("news", 5), ("media", 4), ("blog", 3), ("careers", 3), ("jobs", 3), ("investors", 4)]
THIN_TEXT = 300  # fewer characters than this = probably a JavaScript-built page


class Blocked(Exception):
    pass


# ---------------- URL safety ----------------
def _public_host(host: str) -> bool:
    if config.SCRAPER_ALLOW_PRIVATE:  # tests only
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise Blocked(f"{host} does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def check_url(url: str) -> str:
    """Normalise and vet a URL; raises Blocked with a reason."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Blocked(f"not a web address: {url[:80]}")
    host = u.hostname.lower()
    if any(host == h or host.endswith("." + h) for h in NO_SCRAPE_HOSTS):
        raise Blocked(f"{host} does not allow automated access")
    if host.endswith((".example", ".invalid", ".test", ".localhost")) or host == "localhost":
        if not config.SCRAPER_ALLOW_PRIVATE:
            raise Blocked(f"{host} is a placeholder or local address")
    if u.port not in (None, 80, 443) and not config.SCRAPER_ALLOW_PRIVATE:
        raise Blocked(f"unusual port {u.port}")
    if not _public_host(host):
        raise Blocked(f"{host} points to a private or internal address")
    return urldefrag(url)[0]


def norm(url: str) -> str:
    """One spelling per page: lower-case host, no default port, no fragment, no trailing slash."""
    u = urlparse(urldefrag(url)[0])
    host = (u.hostname or "").lower()
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return f"{u.scheme}://{host}{port}{u.path.rstrip('/')}" + (f"?{u.query}" if u.query else "")


# ---------------- HTTP ----------------
class _Client:
    """One polite session for one crawl: safe redirects, size cap, per-host pause, robots cache."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.http = httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last_hit: dict[str, float] = {}

    def close(self):
        self.http.close()

    def _wait(self, host):
        gap = config.SCRAPER_DELAY_S - (time.time() - self.last_hit.get(host, 0))
        if gap > 0:
            time.sleep(gap)
        self.last_hit[host] = time.time()

    def get(self, url: str, *, html_only=True) -> tuple[str, str]:
        """Returns (final_url, body). Follows up to 5 redirects, vetting each hop."""
        for _ in range(6):
            if time.time() > self.deadline:
                raise Blocked("time budget used up")
            url = check_url(url)
            host = urlparse(url).hostname
            self._wait(host)
            with self.http.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise Blocked(f"HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "")
                if html_only and "html" not in ctype and "xml" not in ctype:
                    raise Blocked(f"not a web page ({ctype.split(';')[0] or 'unknown type'})")
                body, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > config.SCRAPER_MAX_BYTES:
                        break
                    body.append(chunk)
                return url, b"".join(body).decode(r.encoding or "utf-8", errors="replace")
        raise Blocked("too many redirects")

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}"
        if base not in self.robots:
            rp = None
            try:
                _, txt = self.get(base + "/robots.txt", html_only=False)
                rp = RobotFileParser()
                rp.parse(txt.splitlines())
            except Exception:
                rp = None  # no robots.txt (or unreadable) = no restrictions stated
            self.robots[base] = rp
        rp = self.robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)


# ---------------- HTML parsing ----------------
class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer", "form"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "br", "tr", "td", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.links, self.jsonld = "", {}, [], []
        self.headings: list[str] = []
        self.paragraphs = 0
        self.app_root = False
        self._text: list[str] = []
        self._skip = 0
        self._in = None
        self._buf = ""
        self._link = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k in ("description", "og:description", "og:title", "og:site_name", "keywords") and a.get("content"):
                self.meta[k] = a["content"].strip()[:500]
        elif tag == "p":
            self.paragraphs += 1
        elif tag == "div" and a.get("id") in ("root", "__next", "app", "__nuxt", "___gatsby"):
            self.app_root = True
        if tag == "a" and a.get("href"):
            self._link = [a["href"], ""]
        elif tag == "script" and "ld+json" in a.get("type", ""):
            self._in, self._buf = "jsonld", ""
            return
        elif tag == "title":
            self._in, self._buf = "title", ""
            return
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self._text.append("\n")
        if tag in ("h1", "h2", "h3"):
            self._text.append("## ")

    def handle_endtag(self, tag):
        if self._in == "jsonld" and tag == "script":
            self.jsonld.append(self._buf)
            self._in = None
            return
        if self._in == "title" and tag == "title":
            self.title = re.sub(r"\s+", " ", self._buf).strip()[:200]
            self._in = None
            return
        if tag == "a" and self._link:
            self.links.append((self._link[0], re.sub(r"\s+", " ", self._link[1]).strip()[:80]))
            self._link = None
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self._text.append("\n")

    def handle_data(self, data):
        if self._in:
            self._buf += data
            return
        if self._link is not None:
            self._link[1] += data
        if not self._skip:
            self._text.append(data)

    def text(self) -> str:
        t = "".join(self._text)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        lines = [ln.strip() for ln in t.split("\n")]
        out, seen = [], set()
        for ln in lines:
            if len(ln) < 3 or ln in seen:  # drop crumbs and repeated menu/cookie lines
                continue
            seen.add(ln)
            out.append(ln)
        return "\n".join(out)


def _org_facts(jsonld_blobs: list[str]) -> list[str]:
    """Pull company facts the site publishes for search engines (schema.org JSON-LD)."""
    facts = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        if "@graph" in o:
            walk(o["@graph"])
        t = o.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x in ("Organization", "Corporation", "LocalBusiness", "SoftwareApplication", "Product") for x in types):
            for k, label in (("name", "Name"), ("legalName", "Legal name"), ("description", "Description"),
                             ("foundingDate", "Founded"), ("numberOfEmployees", "Employees"),
                             ("address", "Address"), ("areaServed", "Area served"), ("founder", "Founder"),
                             ("offers", "Offer"), ("aggregateRating", "Rating")):
                v = o.get(k)
                if v:
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v, ensure_ascii=False)
                    facts.append(f"{label}: {str(v)[:300]}")

    for b in jsonld_blobs:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return facts[:12]


def parse(url: str, html: str) -> dict:
    p = _Page()
    try:
        p.feed(html)
    except Exception as e:  # malformed HTML: keep what we have
        log.info("parse issue %s: %s", url, e)
    head = []
    if p.meta.get("description") or p.meta.get("og:description"):
        head.append("Site description: " + (p.meta.get("description") or p.meta.get("og:description")))
    head += _org_facts(p.jsonld)
    body = p.text()
    return {"url": url, "title": p.title or p.meta.get("og:title") or url, "text": "\n".join(head + [body]),
            "body_chars": len(body), "links": p.links,
            # JavaScript-built: almost no text, and either no paragraphs at all or an empty app container
            "js_built": len(body) < THIN_TEXT and (p.paragraphs == 0 or p.app_root)}


def pick_links(base_url: str, links, limit: int) -> list[str]:
    """Same-site links most likely to hold facts, best first."""
    host = urlparse(base_url).hostname
    scored = {}
    for href, label in links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        u = norm(urljoin(base_url, href))
        pu = urlparse(u)
        if pu.scheme not in ("http", "https") or (pu.hostname or "").removeprefix("www.") != (host or "").removeprefix("www."):
            continue
        if SKIP_EXT.search(pu.path) or u == norm(base_url) or pu.query:
            continue
        hay = (pu.path + " " + label).lower()
        s = max((w for k, w in WANTED if k in hay), default=0)
        if s and (u not in scored or scored[u] < s):
            # prefer short paths: /about over /blog/2021/some-post-about-us
            scored[u] = s - min(3, pu.path.count("/") - 1)
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]


# ---------------- cache ----------------
def _cached(url: str):
    r = db.q1("SELECT * FROM web_pages WHERE url=?", (url,))
    if not r:
        return None
    try:
        age = (db.now() - datetime.fromisoformat(r["fetched_at"])).days
    except Exception:
        return None
    return r if age < config.SCRAPER_CACHE_DAYS else None


def _store(page: dict, method: str):
    db.run("""INSERT INTO web_pages(url,final_url,title,text,method,fetched_at) VALUES(?,?,?,?,?,?)
              ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url, title=excluded.title, text=excluded.text,
              method=excluded.method, fetched_at=excluded.fetched_at""",
           (page["requested"], page["url"], page["title"], page["text"], method, page["fetched_at"]))


# ---------------- Gemini fallback for JavaScript-built pages ----------------
def read_with_gemini(url: str, app_id=None) -> dict | None:
    if config.LLM_MODE != "live":
        return None
    from . import llm  # local import: llm imports nothing from here
    try:
        out = llm.call("read_url", {"url": url}, app_id=app_id, step="scrape:gemini_url_context")
    except Exception as e:
        log.info("gemini url read failed %s: %s", url, e)
        return None
    text = (out.get("text") or "").strip()
    if out.get("status") != "ok" or len(text) < 50:
        return None
    return {"url": url, "title": out.get("title") or url, "text": text}


# ---------------- crawl ----------------
def crawl(start_url: str, *, app_id=None, max_pages=None, budget_s=None) -> dict:
    """Returns {'pages': [{url,title,text,fetched_at,method}], 'notes': [str]}. Never raises."""
    max_pages = max_pages or config.SCRAPER_MAX_PAGES
    deadline = time.time() + (budget_s or config.SCRAPER_BUDGET_S)
    pages, notes = [], []
    try:
        start = check_url(start_url)
    except Blocked as e:
        return {"pages": [], "notes": [f"Did not read {start_url}: {e}."]}

    cli = _Client(deadline)
    try:
        queue, seen = [start], set()
        first = True
        while queue and len(pages) < max_pages and time.time() < deadline:
            url = queue.pop(0)
            key = norm(url)
            if key in seen:
                continue
            seen.add(key)
            hit = _cached(key)
            if hit:
                pages.append({"url": hit["final_url"] or key, "title": hit["title"], "text": hit["text"],
                              "fetched_at": hit["fetched_at"], "method": hit["method"] + " (cached)"})
                if first:  # we still need the homepage's links to find the other pages
                    links = json.loads(db.get_state(f"links:{key}", "[]") or "[]")
                    queue += pick_links(key, links, max_pages - 1)
                first = False
                continue
            try:
                if not cli.allowed(url):
                    notes.append(f"{url}: the site's robots.txt asks bots not to read this page, so it was skipped.")
                    first = False
                    continue
                final, html = cli.get(url)
            except Blocked as e:
                notes.append(f"Could not read {url}: {e}.")
                first = False
                continue
            except httpx.HTTPError as e:
                notes.append(f"Could not read {url}: {type(e).__name__}.")
                first = False
                continue
            if norm(final) != key and norm(final) in seen:
                first = False
                continue  # redirected to a page we already have
            seen.add(norm(final))
            pg = parse(final, html)
            method = "http"
            if pg["js_built"]:
                g = read_with_gemini(final, app_id)
                if g:
                    pg["text"], method = g["text"], "gemini_url_context"
                elif first:
                    notes.append(f"{final} has almost no readable text without running JavaScript; "
                                 "set LLM_MODE=live to let Gemini read it.")
            page = {"requested": key, "url": final, "title": pg["title"], "text": pg["text"][:config.SCRAPER_PAGE_CHARS],
                    "fetched_at": db.now_iso(), "method": method}
            _store(page, method)
            if len(page["text"]) >= 80:
                pages.append(page)
            if first:
                db.set_state(f"links:{key}", json.dumps(pg["links"][:300]))
                queue += pick_links(final, pg["links"], max_pages - 1)
            first = False
    finally:
        cli.close()
    if time.time() > deadline and queue:
        notes.append(f"Stopped reading {urlparse(start).hostname} after {len(pages)} pages (time budget).")
    return {"pages": pages, "notes": notes}
