"""Dashboard + API.
Local:  python -m uvicorn app.main:app --port 8000
Vercel: entrypoint app.main:app (pyproject.toml [tool.vercel])."""
import hashlib
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (backtest, config, cost, db, intake, llm, memory, outreach, privacy, prompts, rubric,
               tracing, worker)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_ready = threading.Lock()
_booted = False


def boot():
    """Idempotent setup: schema, memos, demo calendar. Runs once per cold start."""
    global _booted
    if _booted:
        return
    with _ready:
        if not _booted:
            db.init()
            memory.load_memos()
            outreach.seed_calendar()
            _booted = True


def missing_settings() -> list[str]:
    """Settings a deployed app can't run without. Checked before touching the database, so a missing one
    shows a clear page instead of a crash (Vercel's filesystem is read-only, so there is no SQLite fallback)."""
    if not config.ON_VERCEL:
        return []
    need = {"DATABASE_URL": config.DATABASE_URL, "APP_PASSWORD": config.APP_PASSWORD}
    if config.LLM_MODE == "live":
        need["GEMINI_API_KEY"] = config.GEMINI_API_KEY
    return [k for k, v in need.items() if not v]


SETUP_PAGE = ("<h2>Setup needed</h2><p>These settings are missing in Vercel (Project &rarr; Settings &rarr; Environment "
              "Variables), using exactly these names:</p><ul>{items}</ul><p>Add them, then redeploy.</p>")


@asynccontextmanager
async def lifespan(_app):
    if not missing_settings():
        boot()
        worker.start()
    yield


app = FastAPI(title="All In Capital - Screening", lifespan=lifespan)
_STATIC = config.ROOT / "public" / "static"
if _STATIC.is_dir():  # on Vercel, public/ is served by the CDN at /static/... and is not inside the function
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ---------------- auth ----------------
COOKIE = "aic_session"
OPEN_PATHS = ("/login", "/static/", "/api/tick", "/api/applications", "/api/ping", "/favicon.ico")


def _sign(ts: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()


def _authed(req: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    v = req.cookies.get(COOKIE, "")
    ts, _, sig = v.partition(".")
    return bool(ts.isdigit() and hmac.compare_digest(sig, _sign(ts)) and time.time() - int(ts) < 14 * 86400)


def _bearer_ok(req: Request, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(req.headers.get("authorization", ""), f"Bearer {secret}")


@app.middleware("http")
async def guard(req: Request, call_next):
    p = req.url.path
    missing = missing_settings()
    if missing:
        if p.startswith("/api/"):
            return JSONResponse({"error": "setup needed", "missing_settings": missing}, 503)
        return HTMLResponse(SETUP_PAGE.format(items="".join(f"<li><code>{k}</code></li>" for k in missing)), 503)
    boot()
    if not p.startswith(OPEN_PATHS) and not _authed(req):
        if p.startswith("/api/"):
            return JSONResponse({"error": "login required"}, 401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(req)


@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request, bad: int = 0):
    return T.TemplateResponse(req, "login.html", {"request": req, "bad": bad})


@app.post("/login")
def login(password: str = Form(...)):
    if not config.APP_PASSWORD or not hmac.compare_digest(password, config.APP_PASSWORD):
        time.sleep(1)
        return RedirectResponse("/login?bad=1", status_code=303)
    ts = str(int(time.time()))
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", httponly=True, secure=config.ON_VERCEL, samesite="lax", max_age=14 * 86400)
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


# ---------------- queue trigger ----------------
@app.api_route("/api/tick", methods=["GET", "POST"])
def api_tick(req: Request):
    """Pull queued work. Called by Vercel Cron / GitHub Actions (Bearer CRON_SECRET) or the logged-in dashboard."""
    if not (_bearer_ok(req, config.CRON_SECRET) or (_authed(req) and (config.APP_PASSWORD or not config.ON_VERCEL))):
        return JSONResponse({"error": "unauthorized"}, 401)
    return worker.tick()


@app.get("/api/ping")
def ping():
    return {"ok": True}
T = Jinja2Templates(directory=str(config.ROOT / "app" / "templates"))
T.env.filters["fromjson"] = lambda s: db.jload(s, [])
T.env.filters["slot"] = lambda s: outreach.fmt(s) if s else ""
def _short(s):
    try:
        return datetime.fromisoformat(s).astimezone(outreach.IST).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return (s or "")[:16].replace("T", " ")


T.env.filters["short"] = _short
T.env.globals.update(rubric=rubric, config=config, VERDICT=rubric.VERDICT_LABEL)


def page(req: Request, name: str, **ctx):
    ctx.update(request=req, paused=cost.is_paused(), paused_reason=db.get_state("paused_reason", ""),
               route=llm.route(), langfuse=config.langfuse_enabled(), backend=db.backend_label(),
               working=db.q1("SELECT COUNT(*) AS n FROM applications WHERE status IN ('queued','processing')")["n"]
                       + db.q1("SELECT COUNT(*) AS n FROM backtest WHERE predicted IN ('pending','running')")["n"],
               logged_in=bool(config.APP_PASSWORD),
               alerts=db.q("SELECT * FROM alerts WHERE acknowledged=0 ORDER BY id DESC LIMIT 5"),
               clock_offset=float(db.get_state("clock_offset_days", "0") or 0))
    return T.TemplateResponse(req, name, ctx)


def back(url="/"):
    return RedirectResponse(url, status_code=303)


# ---------------- inbox ----------------
@app.get("/", response_class=HTMLResponse)
def inbox(req: Request, batch: str = "", status: str = ""):
    where, args = ["1=1"], []
    if batch:
        where.append("a.batch=?"); args.append(batch)
    if status:
        where.append("a.status=?"); args.append(status)
    rows = db.q(f"""SELECT a.*, r.total, r.coverage, r.verdict, r.two_liner, o.stage AS out_stage, o.meeting_start,
                    (SELECT COUNT(*) FROM gaps g WHERE g.app_id=a.id AND g.criterion='safety') AS flags,
                    (SELECT SUM(cost_usd) FROM llm_calls c WHERE c.app_id=a.id) AS cost
                    FROM applications a LEFT JOIN recommendations r ON r.app_id=a.id
                    LEFT JOIN outreach o ON o.app_id=a.id WHERE {' AND '.join(where)}
                    ORDER BY CASE a.status WHEN 'scored' THEN 0 WHEN 'needs_attention' THEN 1 WHEN 'processing' THEN 2
                             WHEN 'queued' THEN 3 ELSE 4 END,
                             CASE r.verdict WHEN 'hold' THEN 0 WHEN 'take_call' THEN 1 WHEN 'riya_review' THEN 2 ELSE 3 END,
                             r.total DESC NULLS LAST, a.id""", args)
    batches = [r["batch"] for r in db.q("SELECT DISTINCT batch FROM applications ORDER BY batch")]
    return page(req, "inbox.html", rows=rows, acct=worker.accounting(), batches=batches, batch=batch,
                status=status, sample_batches=sorted({a["batch"] for a in intake.sample_apps()}))


@app.post("/load/{batch}")
def load(batch: str):
    intake.load_batch(batch)
    return back(f"/?batch={batch}")


@app.post("/volume-test")
def volume(n: int = Form(20)):
    tag, _ = intake.volume_test(n)
    return back(f"/?batch={tag}")


@app.post("/pause")
def do_pause():
    cost.pause("paused manually")
    return back()


@app.post("/resume")
def do_resume():
    cost.resume()
    return back()


@app.post("/limit")
def set_limit(limit: float = Form(...)):
    db.set_state("daily_limit_usd", limit)
    db.set_state("override_day", "")
    return back("/costs")


@app.post("/alerts/ack")
def ack():
    db.run("UPDATE alerts SET acknowledged=1")
    return back()


@app.post("/clock/advance")
def advance(days: float = Form(1)):
    db.set_state("clock_offset_days", float(db.get_state("clock_offset_days", "0") or 0) + days)
    outreach.follow_up_tick()
    return back("/calendar")


@app.post("/clock/reset")
def reset_clock():
    db.set_state("clock_offset_days", 0)
    return back("/calendar")


# ---------------- one application ----------------
@app.get("/app/{app_id}", response_class=HTMLResponse)
def app_detail(req: Request, app_id: int):
    a = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    if not a:
        return HTMLResponse("Not found", 404)
    f = db.q1("SELECT id, name FROM founders WHERE id=?", (a["founder_id"],))
    claims = db.q("SELECT * FROM claims WHERE app_id=? ORDER BY id", (app_id,))
    cmap = {c["cid"]: c for c in claims}
    scores = db.q("SELECT * FROM scores WHERE app_id=?", (app_id,))
    smap = {s["criterion"]: s for s in scores}
    rec = db.q1("SELECT * FROM recommendations WHERE app_id=?", (app_id,))
    gates = db.jload(rec["gate_failures"], []) if rec else []
    gaps = db.q("SELECT * FROM gaps WHERE app_id=? ORDER BY criterion='safety' DESC, id", (app_id,))
    emails = db.q("SELECT * FROM emails WHERE app_id=? ORDER BY id", (app_id,))
    o = db.q1("SELECT * FROM outreach WHERE app_id=?", (app_id,))
    decisions = db.q("SELECT * FROM decisions WHERE app_id=? ORDER BY id DESC", (app_id,))
    used = sorted({k for s in scores for k in db.jload(s["corrections_used"], [])})
    corrs = db.q(f"SELECT * FROM corrections WHERE id IN ({','.join('?'*len(used))})", used) if used else []
    calls = db.q1("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE app_id=?", (app_id,))
    return page(req, "app.html", a=a, founder=f, claims=claims, cmap=cmap, smap=smap, rec=rec, gates=gates,
                gaps=gaps, emails=emails, o=o, decisions=decisions, corrs=corrs, calls=calls,
                form=db.jload(a["form_json"], {}), lf_url=tracing.langfuse_trace_url(a["trace_id"]) if a["trace_id"] else None,
                offered=db.jload(o["offered_slots"], []) if o else [])


@app.post("/app/{app_id}/correct")
def correct(app_id: int, criterion: str = Form(...), score: float = Form(...), reason: str = Form(...),
            by: str = Form(config.REVIEWER_NAME)):
    s = db.q1("SELECT score FROM scores WHERE app_id=? AND criterion=?", (app_id, criterion))
    memory.record_correction(app_id, criterion, s["score"] if s else None, max(0, min(5, score)), reason, by)
    # recompute total/verdict with the human score in place
    sc = {r["criterion"]: r["score"] for r in db.q("SELECT criterion, score FROM scores WHERE app_id=?", (app_id,))}
    rec = db.q1("SELECT * FROM recommendations WHERE app_id=?", (app_id,))
    if rec:
        total, cov = rubric.compute_total(sc)
        fails = [g for g in db.jload(rec["gate_failures"], []) if g["passed"] is False]
        db.run("UPDATE recommendations SET total=?, coverage=?, verdict=? WHERE app_id=?",
               (total, cov, rubric.verdict(total, cov, fails, hold=rec["verdict"] == "hold"), app_id))
    return back(f"/app/{app_id}#scores")


@app.post("/app/{app_id}/claim")
def add_claim(app_id: int, text: str = Form(...), source_ref: str = Form("")):
    n = db.q1("SELECT COUNT(*) n FROM claims WHERE app_id=?", (app_id,))["n"]
    db.run("INSERT INTO claims(app_id,cid,text,source_type,source_ref,created_at) VALUES(?,?,?,?,?,?)",
           (app_id, f"C{n+1}", privacy.scrub(text), "reviewer", source_ref or f"{config.REVIEWER_NAME}'s research", db.now_iso()))
    return back(f"/app/{app_id}#claims")


@app.post("/app/{app_id}/rescore")
def rescore(app_id: int):
    db.run("UPDATE applications SET status='queued', status_reason='Re-score requested.', attempts=0 WHERE id=?", (app_id,))
    return back(f"/app/{app_id}")


@app.post("/app/{app_id}/decide")
def decide(app_id: int, decision: str = Form(...), note: str = Form(""), by: str = Form(config.REVIEWER_NAME)):
    outreach.decide(app_id, decision, note, by)
    return back(f"/app/{app_id}#outreach")


@app.post("/email/{email_id}/send")
def send(email_id: int, subject: str = Form(...), body: str = Form(...)):
    e = db.q1("SELECT app_id FROM emails WHERE id=?", (email_id,))
    outreach.send_draft(email_id, subject, body)
    return back(f"/app/{e['app_id']}#outreach")


@app.post("/app/{app_id}/redraft")
def redraft(app_id: int, kind: str = Form("intro")):
    db.run("DELETE FROM emails WHERE app_id=? AND status='draft'", (app_id,))
    outreach.draft(app_id, kind)
    return back(f"/app/{app_id}#outreach")


@app.post("/app/{app_id}/reply")
def reply(app_id: int, text: str = Form(...)):
    outreach.receive_reply(app_id, text)
    return back(f"/app/{app_id}#outreach")


# ---------------- other pages ----------------
@app.get("/calendar", response_class=HTMLResponse)
def calendar(req: Request):
    start = (db.now() - timedelta(days=1)).isoformat()
    ev = db.q("""SELECT e.*, a.company, a.ref FROM calendar_events e LEFT JOIN applications a ON a.id=e.app_id
                 WHERE e.owner='riya' AND e.end_at>=? ORDER BY e.start_at LIMIT 80""", (start,))
    threads = db.q("""SELECT o.*, a.company, a.ref, a.id AS aid FROM outreach o JOIN applications a ON a.id=o.app_id
                      ORDER BY o.last_out_at DESC""")
    return page(req, "calendar.html", events=ev, threads=threads, now=db.now_iso())


@app.get("/costs", response_class=HTMLResponse)
def costs(req: Request):
    recent = db.q("""SELECT c.*, a.ref FROM llm_calls c LEFT JOIN applications a ON a.id=c.app_id
                     ORDER BY c.id DESC LIMIT 40""")
    return page(req, "costs.html", s=cost.summary(), recent=recent, today=cost.spent_today())


@app.get("/traces", response_class=HTMLResponse)
def traces(req: Request):
    rows = db.q("""SELECT t.*, (SELECT COUNT(*) FROM spans s WHERE s.trace_id=t.id) n_spans,
                   (SELECT COUNT(*) FROM llm_calls c WHERE c.trace_id=t.id) n_calls
                   FROM traces t ORDER BY t.start_at DESC LIMIT 150""")
    return page(req, "traces.html", rows=rows)


@app.get("/trace/{tid}", response_class=HTMLResponse)
def trace_detail(req: Request, tid: str):
    t = db.q1("SELECT * FROM traces WHERE id=?", (tid,))
    spans = db.q("SELECT * FROM spans WHERE trace_id=? ORDER BY start_at, end_at", (tid,))
    kids = {}
    for s in spans:
        kids.setdefault(s["parent_id"], []).append(s)
    ordered = []

    def walk(pid, depth):
        for s in kids.get(pid, []):
            ordered.append((depth, s))
            walk(s["id"], depth + 1)
    walk(None, 0)
    return page(req, "trace.html", t=t, spans=ordered, lf_url=tracing.langfuse_trace_url(tid),
                pv=db.jload(t["prompt_versions"], {}) if t else {})


PROMPT_SENDS = {
    "extract_claims": "One source at a time (deck, form, or one web page), scrubbed: founder name -> founder ID, emails/phones removed, HTML comments stripped, capped at 5,000 chars.",
    "score_application": "Rubric definitions; company name, founder ID, stated sector/stage; extracted claims only (no raw pages); top 3 relevant memo passages; matching partner corrections.",
    "recommend": "Computed scores, verdict, gaps. No claims text, no founder data.",
    "draft_email": "Company name, up to 2 scrubbed claims, offered slots, Riya's recent edits. Founder name is a placeholder filled in locally after the call.",
    "parse_reply": "Today's date, offered slots, the founder's reply with emails/phones removed.",
    "backtest_explain": "Memo reasoning and the system's scores for one past deal.",
}


@app.get("/audit", response_class=HTMLResponse)
def audit(req: Request):
    rows = []
    for name in prompts.PROMPTS:
        calls = db.q("SELECT sent_payload, input_tokens FROM llm_calls WHERE prompt_name=? ORDER BY id DESC LIMIT 500", (name,))
        pii = sum(1 for c in calls if privacy.pii_findings(c["sent_payload"]))
        last = json.loads(calls[0]["sent_payload"]) if calls else None
        rows.append({"name": name, "version": prompts.version(name), "model": prompts.model_for(name),
                     "sends": PROMPT_SENDS[name], "n": len(calls), "pii": pii,
                     "avg_in": int(sum(c["input_tokens"] for c in calls) / len(calls)) if calls else 0, "last": last})
    return page(req, "audit.html", rows=rows)


@app.get("/backtest", response_class=HTMLResponse)
def bt(req: Request, run: str = ""):
    return page(req, "backtest.html", r=backtest.results(run or None),
                runs=db.q("SELECT run_id, MIN(created_at) t, COUNT(*) n FROM backtest GROUP BY run_id ORDER BY t DESC"))


@app.post("/backtest/run")
def bt_run():
    backtest.run()
    return back("/backtest")


@app.post("/backtest/{row_id}/verdict")
def bt_verdict(row_id: int, verdict: str = Form(...)):
    db.run("UPDATE backtest SET human_verdict=? WHERE id=?", (verdict, row_id))
    return back("/backtest")


@app.get("/learning", response_class=HTMLResponse)
def learning(req: Request):
    corr = db.q("""SELECT k.*, a.company, a.ref FROM corrections k LEFT JOIN applications a ON a.id=k.app_id ORDER BY k.id DESC""")
    edits = db.q("""SELECT e.*, a.company FROM emails e JOIN applications a ON a.id=e.app_id WHERE e.direction='out'
                    AND e.status='sent' AND e.ai_draft IS NOT NULL ORDER BY e.id DESC LIMIT 30""")
    return page(req, "learning.html", corr=corr, edits=edits)


@app.get("/thesis", response_class=HTMLResponse)
def thesis(req: Request):
    return page(req, "thesis.html", rubric_text=rubric.rubric_text())


# ---------------- JSON API ----------------
@app.get("/api/health")
def health():
    return JSONResponse({"accounting": worker.accounting(), "paused": cost.is_paused(),
                         "spent_today": cost.spent_today(), "limit": cost.daily_limit(), "route": llm.route(),
                         "database": db.backend_label(), "models": {"score": config.MODEL_SCORE,
                         "extract": config.MODEL_EXTRACT, "write": config.MODEL_WRITE, "fallbacks": config.FALLBACK_MODELS},
                         "langfuse": config.langfuse_enabled(), "prompt_versions": prompts.all_versions()})


@app.post("/api/applications")
async def api_submit(req: Request):
    """Public form / webhook intake (Bearer INTAKE_TOKEN, or a logged-in user). One application or a list."""
    if not (_bearer_ok(req, config.INTAKE_TOKEN) or (_authed(req) and (config.APP_PASSWORD or not config.ON_VERCEL))):
        return JSONResponse({"error": "unauthorized"}, 401)
    body = await req.json()
    items = body if isinstance(body, list) else [body]
    return {"queued": [intake.submit(a) for a in items]}
