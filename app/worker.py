"""Case 04 Task 2 - the queue, built for serverless.

Applications wait in Neon with status 'queued'. Nothing runs in the background on Vercel, so
work is pulled by `tick()`, which is triggered by:
  * the dashboard (while anything is queued, the open inbox calls /api/tick every few seconds),
  * Vercel Cron (safety net; Hobby plans allow at most every 15 min),
  * a GitHub Actions schedule (every 5 min) - see .github/workflows/tick.yml.
Each tick works for at most TICK_BUDGET_S, WORKER_CONCURRENCY applications at a time. Two ticks
overlapping is safe: Postgres row locks (FOR UPDATE SKIP LOCKED) hand each application to one
worker only. A function killed mid-run leaves 'processing' rows; they are re-queued after
STALE_AFTER_MIN. Locally (uvicorn) the same loop also runs in background threads.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta

from . import backtest, config, cost, db, outreach, pipeline

log = logging.getLogger("worker")
_stop = threading.Event()


def claim_next() -> int | None:
    lock = " FOR UPDATE SKIP LOCKED" if db.is_pg() else ""
    with db.tx() as c:
        row = c.execute(f"SELECT id FROM applications WHERE status='queued' ORDER BY id LIMIT 1{lock}").fetchone()
        if not row:
            return None
        c.execute("""UPDATE applications SET status='processing', status_reason='Being researched and scored.',
                     attempts=attempts+1, started_at=?, updated_at=? WHERE id=?""",
                  (db.now_iso(), db.now_iso(), row["id"]))
        return row["id"]


def recover_stale() -> int:
    cutoff = (db.now() - timedelta(minutes=config.STALE_AFTER_MIN)).isoformat(timespec="seconds")
    rows = db.q("SELECT id FROM applications WHERE status='processing' AND (started_at IS NULL OR started_at < ?)", (cutoff,))
    for r in rows:
        db.run("""UPDATE applications SET status='queued', status_reason='Re-queued: previous run did not finish
                  (function timeout or restart).' WHERE id=? AND status='processing'""", (r["id"],))
    if rows:
        log.warning("re-queued %s stale applications", len(rows))
    return len(rows)


def _work_one(deadline: float) -> bool:
    if time.time() > deadline or cost.is_paused():
        return False
    app_id = claim_next()
    if app_id is None:
        return False
    pipeline.process(app_id)
    return True


def tick(budget_s: float | None = None) -> dict:
    """Do as much queued work as fits in the time budget. Safe to call often and concurrently."""
    t0 = time.time()
    deadline = t0 + (budget_s if budget_s is not None else config.TICK_BUDGET_S)
    recovered = recover_stale()
    done = 0
    with ThreadPoolExecutor(max_workers=config.WORKER_CONCURRENCY) as ex:
        def lane():
            n = 0
            while _work_one(deadline):
                n += 1
            return n
        futs = [ex.submit(lane) for _ in range(config.WORKER_CONCURRENCY)]
        wait(futs)
        done = sum(f.result() for f in futs)
    bt = 0
    while time.time() < deadline and not cost.is_paused() and backtest.step():
        bt += 1
    try:
        if not cost.is_paused():
            outreach.follow_up_tick()
    except Exception:
        log.exception("follow-up tick failed")
    return {"processed": done, "backtest_cases": bt, "recovered": recovered,
            "remaining": db.q1("SELECT COUNT(*) AS n FROM applications WHERE status IN ('queued','processing')")["n"],
            "paused": cost.is_paused(), "seconds": round(time.time() - t0, 1)}


# ---------------- local long-running mode (uvicorn on a laptop / VM) ----------------
def _loop():
    while not _stop.is_set():
        try:
            r = tick(budget_s=30)
            if not r["processed"] and not r["backtest_cases"]:
                _stop.wait(2)
        except Exception:
            log.exception("tick failed")
            _stop.wait(5)


def start():
    if config.RUN_BACKGROUND_WORKERS:
        threading.Thread(target=_loop, daemon=True, name="worker").start()


def accounting() -> dict:
    """Every application is in exactly one known state, and nothing is unexplained."""
    rows = db.q("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
    by = {r["status"]: r["n"] for r in rows}
    total = sum(by.values())
    unexplained = db.q("""SELECT ref, status FROM applications WHERE status='needs_attention'
                          AND (status_reason IS NULL OR status_reason='')""")
    known = {"queued", "processing", "scored", "needs_attention", "approved", "rejected"}
    unknown = {k: v for k, v in by.items() if k not in known}
    return {"total": total, "by_status": by, "unexplained": unexplained, "unknown_states": unknown,
            "ok": not unexplained and not unknown}
