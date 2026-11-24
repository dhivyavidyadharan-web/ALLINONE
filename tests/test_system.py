"""Run: python -m pytest -q
Uses a throwaway database and the offline mock model - no keys, no network."""
import json
import os
import tempfile
import threading
from pathlib import Path

# Environment (throwaway DB, mock model, fictional data) is set in tests/conftest.py.
import pytest  # noqa: E402

from app import config, cost, db, intake, memory, outreach, pipeline, privacy, rubric, worker  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    db.init(force=True)
    with db.tx() as c:
        for t in ["applications", "founders", "claims", "gaps", "scores", "recommendations", "decisions", "corrections",
                  "emails", "outreach", "calendar_events", "llm_calls", "spans", "traces", "alerts", "state", "backtest", "web_pages"]:
            c.execute(f"DELETE FROM {t}")
    db.invalidate_clock()
    memory.load_memos()
    outreach.seed_calendar()
    config.MOCK_FAIL_RATE = 0.0
    yield


def drain(concurrency=3, max_rounds=20):
    """Run the real worker claim loop with N threads until the queue is empty."""
    for _ in range(max_rounds):
        def w():
            while not cost.is_paused():
                i = worker.claim_next()
                if i is None:
                    return
                pipeline.process(i)
        ts = [threading.Thread(target=w) for _ in range(concurrency)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        if cost.is_paused() or not db.q1("SELECT 1 FROM applications WHERE status='queued'"):
            return


def test_volume_20_none_disappear():
    tag, ids = intake.volume_test(20)
    drain()
    rows = db.q("SELECT status, status_reason FROM applications WHERE batch=?", (tag,))
    assert len(rows) == 20
    assert all(r["status"] in ("scored", "needs_attention") for r in rows), rows
    assert all(r["status_reason"] for r in rows)
    acct = worker.accounting()
    assert acct["ok"] and acct["total"] == 20
    # the empty application is flagged, with a reason, and costs nothing
    ghost = db.q1("SELECT * FROM applications WHERE company='Ghost Co' AND batch=?", (tag,))
    assert ghost["status"] == "needs_attention" and "Empty" in ghost["status_reason"]
    assert db.q1("SELECT COUNT(*) n FROM llm_calls WHERE app_id=?", (ghost["id"],))["n"] == 0


def test_transient_failures_retry_and_never_lose_work():
    config.MOCK_FAIL_RATE = 0.5  # half of all attempts fail (after Portkey-style inner retries)
    tag, ids = intake.volume_test(20)
    drain(max_rounds=10)
    rows = db.q("SELECT status, status_reason FROM applications WHERE batch=?", (tag,))
    assert len(rows) == 20
    assert all(r["status"] in ("scored", "needs_attention", "queued") for r in rows)
    assert all(r["status_reason"] for r in rows)


def test_daily_limit_pauses_and_keeps_queue():
    db.set_state("daily_limit_usd", 0.02)
    tag, ids = intake.volume_test(20)
    drain()
    assert cost.is_paused()
    assert db.q1("SELECT COUNT(*) n FROM alerts WHERE level='critical'")["n"] >= 1
    rows = db.q("SELECT status, status_reason FROM applications WHERE batch=?", (tag,))
    assert len(rows) == 20
    waiting = [r for r in rows if r["status"] == "queued"]
    assert waiting, "some applications should be waiting for the budget"
    assert all(r["status_reason"] for r in rows)
    cost.resume()
    drain()
    assert not db.q1("SELECT 1 FROM applications WHERE status IN ('queued','processing')")


def test_no_pii_reaches_the_model():
    intake.load_batch("inbound-week-1")
    drain()
    # Founder names may go to the web-search step only (to find their profiles); never to extraction or scoring.
    payloads = [r["sent_payload"] for r in db.q("SELECT sent_payload FROM llm_calls WHERE prompt_name!='web_search'")]
    assert payloads
    leaks = [p for p in payloads if privacy.pii_findings(p)]
    assert not leaks, leaks[:1]
    names = [f["name"] for f in db.q("SELECT name FROM founders") if f["name"] not in ("", "Unknown")]
    for p in payloads:
        for n in names:
            assert n not in p, f"founder name {n} sent to model"


def test_every_score_has_a_source():
    intake.load_batch("inbound-week-1")
    drain()
    for s in db.q("SELECT * FROM scores WHERE score IS NOT NULL"):
        cids = json.loads(s["claim_ids"])
        assert cids, s
        for c in cids:
            assert db.q1("SELECT 1 FROM claims WHERE app_id=? AND cid=?", (s["app_id"], c))


def test_uncited_score_is_discarded():
    crit, gates = pipeline._validate_scores({"criteria": [{"key": "evidence_of_pull", "score": 5, "claim_ids": ["C99"],
                                                           "memo_refs": [], "rationale": "trust me", "missing": ""}],
                                            "gates": []}, {"C1"})
    t = next(c for c in crit if c["criterion"] == "evidence_of_pull")
    assert t["score"] is None and "discarded" in t["rationale"]


def test_injection_is_flagged_and_ignored():
    intake.load_batch("inbound-week-1")
    drain()
    a = db.q1("SELECT * FROM applications WHERE company='AlphaGrid'")
    rec = db.q1("SELECT * FROM recommendations WHERE app_id=?", (a["id"],))
    assert rec["verdict"] != "take_call"
    assert db.q1("SELECT 1 FROM gaps WHERE app_id=? AND criterion='safety'", (a["id"],))


def test_gates_and_expected_verdicts():
    intake.load_batch("inbound-week-1")
    drain()
    v = {r["company"]: r["verdict"] for r in db.q(
        "SELECT a.company, r.verdict FROM applications a JOIN recommendations r ON r.app_id=a.id")}
    for bad in ("Loop Studio", "RupeeRush", "Nexa Ledger", "GreenKart Foods"):
        assert v[bad] == "pass", (bad, v[bad])
    assert v["Paisa Setu"] == "take_call"
    assert v["Vayu Drones"] == "riya_review"  # too little evidence: abstain, don't guess


def test_correction_carries_to_similar_company():
    intake.load_batch("inbound-week-1")
    drain()
    haldi = db.q1("SELECT id FROM applications WHERE company='Haldi House'")["id"]
    before = db.q1("SELECT score FROM scores WHERE app_id=? AND criterion='defensibility'", (haldi,))["score"]
    memory.record_correction(haldi, "defensibility", before, 1.0, "Ayurvedic skincare is crowded; claim is not contrarian", "Kushal")
    # a similar consumer-brands company arrives later
    intake.load_batch("inbound-week-2")
    drain()
    mitti = db.q1("SELECT id FROM applications WHERE company='Mitti Pets'")["id"]
    s = db.q1("SELECT * FROM scores WHERE app_id=? AND criterion='defensibility'", (mitti,))
    assert json.loads(s["corrections_used"]), "correction should be applied to a similar company"
    assert db.q1("SELECT times_applied FROM corrections")["times_applied"] >= 1


def test_booking_flow_accept_counter_followup():
    intake.load_batch("inbound-week-1")
    drain()
    pid = db.q1("SELECT id FROM applications WHERE company='Paisa Setu'")["id"]
    eid = outreach.decide(pid, "approve", "strong founder", "Riya")
    e = db.q1("SELECT * FROM emails WHERE id=?", (eid,))
    assert "Neha" in e["body"] and "1." in e["body"]
    outreach.send_draft(eid, e["subject"], e["body"].replace("Best,", "Warmly,"))
    r = outreach.receive_reply(pid, "Option 2 works great, thanks!")
    assert db.q1("SELECT stage FROM outreach WHERE app_id=?", (pid,))["stage"] == "booked"
    owners = {r["owner"] for r in db.q("SELECT owner FROM calendar_events WHERE app_id=?", (pid,))}
    assert owners == {"riya", "kushal", db.q1("SELECT founder_id FROM applications WHERE id=?", (pid,))["founder_id"]}
    # PN-5: calls only on Tuesday or Thursday afternoons, 30 minutes
    for s in json.loads(db.q1("SELECT offered_slots FROM outreach WHERE app_id=?", (pid,))["offered_slots"]):
        d = outreach._dt(s).astimezone(outreach.IST)
        assert d.weekday() in (1, 3) and 14 <= d.hour < 17 or (d.hour, d.minute) == (17, 0)

    hid = db.q1("SELECT id FROM applications WHERE company='Haldi House'")["id"]
    eid = outreach.decide(hid, "approve", "", "Riya")
    e = db.q1("SELECT * FROM emails WHERE id=?", (eid,))
    outreach.send_draft(eid, e["subject"], e["body"])
    db.set_state("clock_offset_days", 4)
    outreach.follow_up_tick()
    assert db.q1("SELECT follow_ups FROM outreach WHERE app_id=?", (hid,))["follow_ups"] == 1
    outreach.receive_reply(hid, "Could we do Friday 3pm instead?")
    assert db.q1("SELECT stage FROM outreach WHERE app_id=?", (hid,))["stage"] in ("booked", "slots_offered")


def test_serverless_tick_drains_queue_and_recovers_stale():
    tag, ids = intake.volume_test(6)
    # simulate a Vercel function killed mid-run: one app stuck in 'processing' for an hour
    db.run("UPDATE applications SET status='processing', started_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ids[0],))
    out = worker.tick(budget_s=60)
    assert out["recovered"] == 1
    assert out["remaining"] == 0
    rows = db.q("SELECT status FROM applications WHERE batch=?", (tag,))
    assert all(r["status"] in ("scored", "needs_attention") for r in rows)


def test_backtest_runs_through_tick():
    from app import backtest
    run_id = backtest.run()
    assert run_id and backtest.run() is None  # one run at a time
    worker.tick(budget_s=60)
    r = backtest.results(run_id)
    assert r["running"] == 0 and r["n"] == 8


def test_fallback_model_used_when_primary_keeps_failing(monkeypatch):
    from app import llm
    calls = []

    def fake(name, model, system, user):
        calls.append(model)
        if model == config.MODEL_EXTRACT:
            raise llm.TransientModelError("503 overloaded")
        return {"claims": ["x"], "injection_detected": False, "injection_text": ""}, 10, 5
    monkeypatch.setattr(config, "LLM_MODE", "live")
    monkeypatch.setattr(llm, "_gemini", fake)
    monkeypatch.setattr(config, "RETRIES_PER_MODEL", 2)
    out = llm.call("extract_claims", {"source_id": "s", "source_type": "deck", "text": "t"}, step="t")
    assert out["claims"] == ["x"]
    assert calls[:2] == [config.MODEL_EXTRACT] * 2 and calls[2] == config.FALLBACK_MODELS["MODEL_EXTRACT"][0]
    steps = [r["step"] for r in db.q("SELECT step FROM llm_calls ORDER BY id")]
    assert any("fallback" in s for s in steps)  # every attempt is recorded, including the failures


def test_deployed_app_is_locked(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "APP_PASSWORD", "pw-123")
    monkeypatch.setattr(config, "SESSION_SECRET", "s3cret")
    monkeypatch.setattr(config, "CRON_SECRET", "cron-xyz")
    c = TestClient(main.app)
    assert c.get("/", follow_redirects=False).status_code == 303          # dashboard needs login
    assert c.get("/api/health").status_code == 401
    assert c.post("/api/tick").status_code == 401                           # no secret
    assert c.post("/api/tick", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/api/tick", headers={"Authorization": "Bearer cron-xyz"}).status_code == 200
    assert c.post("/api/applications", json={}).status_code == 401
    assert c.post("/login", data={"password": "nope"}, follow_redirects=False).headers["location"] == "/login?bad=1"
    r = c.post("/login", data={"password": "pw-123"}, follow_redirects=False)
    assert r.status_code == 303 and "aic_session" in r.cookies
    assert c.get("/").status_code == 200


def test_real_case03_batch(monkeypatch):
    """Regression on the real Case 03 seed data (data/seed): gates, portfolio hold and deck-vs-form conflicts."""
    from app import portfolio, research
    seed = config.ROOT / "data" / "seed"
    if not (seed / "applications.json").exists():
        pytest.skip("real seed data not present")
    monkeypatch.setattr(config, "DATA_DIR", seed)
    monkeypatch.setattr(portfolio, "_cache", None)
    monkeypatch.setattr(research, "_fixtures", None)
    memory.load_memos()
    assert db.q1("SELECT COUNT(*) AS n FROM memos")["n"] == 12
    intake.load_batch("case03-inbound")
    drain()
    rows = {r["company"]: r for r in db.q(
        "SELECT a.id, a.company, a.status, r.verdict FROM applications a JOIN recommendations r ON r.app_id=a.id")}
    assert len(rows) == 10
    assert rows["Talkbook"]["verdict"] == "hold"      # overlaps MeetRecord (partner notes, meeting 4)
    assert rows["Settlr"]["verdict"] == "pass"        # Series A: stage gate
    assert rows["CareCorner"]["verdict"] == "pass"    # healthcare: sector gate
    gaps = lambda c: " ".join(g["missing"] for g in db.q("SELECT missing FROM gaps WHERE app_id=?", (rows[c]["id"],)))
    assert "deck says 12, form says 5" in gaps("EarnWise")
    assert "deck says 38, form says 42" in gaps("StockSathi")
    assert "injection" not in gaps("CAPilot").lower()  # 'AI assistant' product is not an injection
    assert not any("Giva" in gaps(c) for c in rows)    # 'Goldman' must not match 'gold'


def test_deploy_missing_settings_shows_setup_page(monkeypatch):
    """On Vercel with settings missing: a clear setup page, never a crash or a SQLite fallback."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "ON_VERCEL", True)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "LLM_MODE", "live")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    c = TestClient(main.app)
    r = c.get("/")
    assert r.status_code == 503 and "DATABASE_URL" in r.text and "GEMINI_API_KEY" in r.text and "APP_PASSWORD" in r.text
    assert c.get("/api/ping").json()["missing_settings"] == ["DATABASE_URL", "APP_PASSWORD", "GEMINI_API_KEY"]
