"""Kushal: 'Run it against the decisions in our files and tell me how often it disagrees with us.
When it does, tell me whether we were wrong or it was.'

Each past memo's profile (what the fund knew at decision time) is screened like a new
application. The memo itself is excluded from retrieval so the system cannot see the answer.
Serverless-friendly: `run()` queues one row per memo; `step()` (called by worker.tick) does one.
"""
import uuid

from . import cost, db, llm, pipeline, tracing


def _expected(decision: str) -> str:
    return "take_call" if decision == "invest" else "pass"


def run() -> str | None:
    if db.q1("SELECT 1 FROM backtest WHERE predicted IN ('pending','running')"):
        return None  # one run at a time
    run_id = "bt-" + uuid.uuid4().hex[:6]
    for m in db.q("SELECT id, company, decision FROM memos ORDER BY id"):
        db.run("INSERT INTO backtest(run_id,memo_id,company,actual,predicted,created_at) VALUES(?,?,?,?,?,?)",
               (run_id, m["id"], m["company"], m["decision"], "pending", db.now_iso()))
    return run_id


def step() -> bool:
    """Process one pending memo. Returns False when there is nothing to do."""
    lock = " FOR UPDATE SKIP LOCKED" if db.is_pg() else ""
    with db.tx() as c:
        row = c.execute(f"SELECT * FROM backtest WHERE predicted='pending' ORDER BY id LIMIT 1{lock}").fetchone()
        if not row:
            return False
        c.execute("UPDATE backtest SET predicted='running' WHERE id=?", (row["id"],))
    m = db.q1("SELECT * FROM memos WHERE id=?", (row["memo_id"],))
    try:
        pred, total, agreed, analysis = _score_memo(row["run_id"], m)
        db.run("UPDATE backtest SET predicted=?, total=?, agreed=?, analysis=?, created_at=? WHERE id=?",
               (pred, total, agreed, analysis, db.now_iso(), row["id"]))
    except cost.BudgetExceeded:
        db.run("UPDATE backtest SET predicted='pending' WHERE id=?", (row["id"],))
        return False
    except Exception as e:
        db.run("UPDATE backtest SET predicted='error', analysis=? WHERE id=?", (str(e)[:300], row["id"]))
    return True


def _score_memo(run_id: str, m: dict):
    profile = m["text"].split("\nDECISION:")[0].replace("PROFILE: ", "")
    reasoning = m["text"].split("REASONING: ", 1)[-1]
    app = {"id": None, "company": m["company"], "sector": m["sector"], "stage": m["stage"],
           "founder_id": "F-MEMO", "source": "past memo"}
    with tracing.trace(f"backtest:{m['id']}", None, metadata={"run": run_id, "memo": m["id"]}, tags=["backtest"]):
        out = llm.call("extract_claims", {"source_id": m["id"], "source_type": "memo_profile", "text": profile},
                       step="backtest:extract", mock_ctx={"text": profile})
        claims = [{"cid": f"C{i+1}", "text": t, "source_type": "memo_profile", "source_ref": m["id"]}
                  for i, t in enumerate(out.get("claims", []))]
        r = pipeline.score(app, {"claims": claims, "gaps": [], "flags": []}, exclude_memo=m["id"], persist=False, check_portfolio=False)
        pred = r["verdict"]
        agreed = None if pred in ("riya_review", "hold") else int(pred == _expected(m["decision"]))
        analysis = ""
        if agreed == 0 or pred in ("riya_review", "hold"):
            ex = llm.call("backtest_explain", {
                "company": m["company"], "actual": m["decision"], "predicted": pred, "total": r["total"],
                "scores": "\n".join(f"{c['criterion']}: {c['score']} - {c['rationale']}" for c in r["criteria"]),
                "reasoning": reasoning}, step="backtest:explain", mock_ctx={"reasoning": reasoning})
            analysis = f"[likely right: {ex['likely_right']}] {ex['explanation']}"
    return pred, r["total"], agreed, analysis


def results(run_id: str | None = None) -> dict:
    if not run_id:
        last = db.q1("SELECT run_id FROM backtest ORDER BY id DESC LIMIT 1")
        run_id = last["run_id"] if last else None
    rows = db.q("SELECT * FROM backtest WHERE run_id=? ORDER BY memo_id", (run_id,)) if run_id else []
    done = [r for r in rows if r["predicted"] not in ("pending", "running")]
    decided = [r for r in done if r["agreed"] is not None]
    agree = sum(r["agreed"] for r in decided)
    cm = {}
    for r in done:
        cm[(r["actual"], r["predicted"])] = cm.get((r["actual"], r["predicted"]), 0) + 1
    for a in ("invest", "pass"):
        for p in ("take_call", "riya_review", "pass"):
            cm.setdefault((a, p), 0)
    live = db.q1("SELECT COUNT(*) AS n, COALESCE(SUM(agreed),0) AS a FROM decisions WHERE agreed IS NOT NULL")
    return {"run_id": run_id, "rows": rows, "n": len(done), "decided": len(decided), "agree": agree,
            "agreement": round(agree / len(decided), 2) if decided else None,
            "abstain": len([r for r in done if r["predicted"] in ("riya_review", "hold")]),
            "false_pass": sum(1 for r in done if r["actual"] == "invest" and r["predicted"] == "pass"),
            "cm": cm, "live_n": live["n"], "live_agree": live["a"],
            "overrides": db.q1("SELECT COUNT(*) AS n FROM corrections")["n"],
            "running": len(rows) - len(done)}
