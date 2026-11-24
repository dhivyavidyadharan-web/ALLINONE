# All In Capital — inbound screening system

Screens inbound applications against the fund's thesis, criterion by criterion, with a source behind every
claim and every gap named. Riya approves or rejects; the system drafts the email and, once she sends it,
books the call. Case 03 (the system) + Case 04 (prototype to production).

## Core stack

| | Role | Where in code |
|---|---|---|
| **Neon** (Postgres) | All state: applications, evidence, scores, queue, emails, calendar, costs, traces | `app/db.py` (pg8000, pooled URL, TLS, auto-reconnect after Neon idles) |
| **Gemini** | Every model call | `app/llm.py` (google-genai Interactions API, JSON-schema output, `store=False`) |
| **Vercel** | Hosting (FastAPI on Vercel Functions) + daily cron | `pyproject.toml` `[tool.vercel]`, `vercel.json` |
| **GitHub** | Code, CI on SQLite + Postgres 16, 5-minute queue heartbeat, auto-deploy to Vercel | `.github/workflows/` |

**Add-ons** (on top of the core, each switched on by config): web scraper (`app/scraper.py`, `LIVE_WEB=1`),
model fallback chain (`FALLBACK_*`), checks (citation guard, conflict detection, PII audit, injection
detection), feedback loop (`app/memory.py`), Portkey gateway, Langfuse tracing, alert webhook.

## How the queue works on serverless

Vercel functions stop when the response is sent, so nothing runs in the background. Applications are written
to Neon as `queued` immediately; `POST /api/tick` claims new work for up to 150 s, three at a time (Vercel allows 300 s per function, so the last application always finishes). Ticks come from:
the open dashboard (while anything is queued), the GitHub heartbeat every 5 min, and Vercel Cron daily as a
safety net. Postgres row locks (`FOR UPDATE SKIP LOCKED`) make overlapping ticks safe, and a run killed by a
timeout is re-queued after 6 minutes. The backtest, follow-ups and Langfuse export all run inside ticks too.

## Run locally

```powershell
pip install -r requirements-dev.txt
```
```powershell
copy .env.example .env
```
```powershell
python -m uvicorn app.main:app --port 8000
```
With `DATABASE_URL` empty it uses a local SQLite file; with `LLM_MODE=mock` it needs no keys. Locally a
background worker also drains the queue.

Tests (never touch Neon; set `TEST_DATABASE_URL` to a throwaway Postgres to run them on Postgres):
```powershell
python -m pytest -q
```
Check real keys (read-only on Neon, one tiny Gemini call):
```powershell
python scripts/smoke_live.py
```

## Deploy

1. **Neon**: create a project, copy the *pooled* connection string.
2. **GitHub**: push this repo (`.env` is git-ignored).
3. **Vercel**: Add New Project → import the GitHub repo. Set environment variables: `DATABASE_URL`,
   `GEMINI_API_KEY`, `LLM_MODE=live`, `APP_PASSWORD`, `SESSION_SECRET`, `CRON_SECRET` (optional: `PORTKEY_API_KEY`,
   `LANGFUSE_*`, `INTAKE_TOKEN`, `LIVE_WEB=1`). Deploy. Tables are created on first request.
4. **GitHub secrets** for the heartbeat: `APP_URL` (your Vercel URL) and `CRON_SECRET` (same value).

## The scraper

`app/scraper.py`, on with `LIVE_WEB=1`. For each applicant website it reads the homepage plus the four pages most
likely to hold facts (about, team, customers, case studies, pricing, press), within 20 seconds per site.

* **What it keeps**: visible text (scripts, menus and footers dropped), the site's meta description, and the company
  data sites publish for search engines (schema.org: founding date, employee count, address). Max 6,000 characters
  per page. Every page becomes its own source, so each claim cites the exact page and the date it was read.
* **JavaScript-built sites** (almost no text without running scripts): Gemini's URL-reading tool fetches the page on
  Google's side (`read_url` prompt, live mode only). Otherwise the gap is named.
* **Manners**: obeys robots.txt, names itself (`AllInScreeningBot/1.0`), one request at a time per site with a pause,
  size and type limits, pages cached in Neon (`web_pages`) for 7 days.
* **Safety**: only public internet addresses, checked again on every redirect, so a founder can't point it at an
  internal address. LinkedIn and social networks are never scraped (their terms forbid it); a given LinkedIn URL is
  recorded as a gap. Scraped text goes through the same injection detection as decks.
* **Not covered**: pages behind logins or paywalls.

## Open-web search

`app/search.py`, on with `LIVE_WEB=1` and `LLM_MODE=live`. Gemini's built-in Google Search looks for press,
directories and founder profiles beyond the applicant's own site (company + city + sector; company + funding/news;
each founder + company).

* **Only real results count.** Gemini reports each result as a Google grounding redirect link, which exists only for
  pages Google actually returned. One request resolves it to the real address, which is what gets stored and cited.
  Links that don't resolve, and plain URLs the scraper can't open, are dropped and named as a gap.
* **Same-name companies.** Each result must say why it is the same company (website, city, product, founder).
  "Unsure" results are not used; the near-miss is shown to Riya as a gap.
* **Claims rest on the page.** Each kept result is opened by the scraper (one page), so claims cite the page itself.
  When it can't be opened (paywall, LinkedIn), Google's summary is used and labelled "search summary - page not opened".
* **Independent vs founder material.** The company's own website counts as founder material. The conflict check
  compares deck, form and own site against third-party sources, e.g. "deck says 1,200 farmers, press says 1,100".
* **Privacy.** Founder names go to the search step only, to find their profiles (`SEARCH_FOUNDERS=0` turns this off).
  Everything passed on to extraction and scoring is scrubbed to the founder ID; `test_no_pii_reaches_the_model`
  checks every other step.
* **Cost.** Google charges per search request on top of tokens. Set `SEARCH_CALL_USD` from Gemini's current price
  list so `/costs` and the daily limit include it.

## The whiteboard, mapped to code

| Whiteboard box | Code |
|---|---|
| Define rubrics | `app/rubric.py`, `/thesis` — gates + 6 weighted criteria, each with its source (ASSUMPTION where unverified) |
| Collect info ← founder fit, deck, free sources, scrape | `app/research.py` + `app/scraper.py` + `app/search.py` — sources → short claims with ids; cross-source conflict check |
| Score the startup | `app/pipeline.py::score` — uncited scores discarded → "no evidence", never averaged in |
| Recommendation | fixed rules in `rubric.verdict`; the model only writes the case for/against |
| Human → Final decision | `/app/{id}` approve / reject; agreement with AI recorded |
| Feedback loop | `app/memory.py` — corrections reused on similar companies; email edits steer drafts |
| Draft email → HITL → Send → Meeting booking agent | `app/outreach.py` — accept books both calendars, counter books or re-offers, decline closes, silence → follow-up |

## Case 04: five questions

| Question | Where |
|---|---|
| Does it work? | `/backtest` (memos screened blind), `tests/` |
| Is it safe? | `/audit` (exact payload per call + PII scan), founder ID instead of contact details, `store=False` at Gemini, login on the deployed app, secrets only in env |
| Real-world volume? | Neon queue + ticks + stale recovery; retries → fallback models; `test_volume_20_none_disappear` |
| Can you operate it? | `/traces` per run (+ Langfuse if set); content-hashed prompt versions |
| Gateway down? | Portkey is optional: if it is unreachable or rejects its key, the call goes straight to Gemini (logged as `GatewayDown`). Search/page-reading steps never use Portkey because they need Google's built-in tools |
| Economics? | `/costs` per call/app/day/week/month; daily limit pauses and alerts |

Mail and calendar are built-in simulations behind `outreach.send_mail / is_free / book` (swap for Gmail and
Google Calendar). See `docs/TRUST.md` and `docs/THESIS.md`.

## Data

* `data/seed/` — **real Case 03 seed data** from the course Drive folder: 10 applications (deck + form, raw text in
  `raw/`), 12 deal memos, partner meeting notes, and the known portfolio. The app loads this by default.
* `data/samples/` — fictional data used only by the automated tests (`DATA_DIR` switches between them).
* The rubric (`app/rubric.py`) cites the memo or meeting behind every criterion; `docs/THESIS.md` lists what is
  still assumed. Consider keeping the GitHub repo **private**: `data/seed` is course material.
