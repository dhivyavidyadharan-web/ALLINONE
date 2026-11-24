"""Task 5 - cost of every model call, totals, and the daily limit that pauses the system."""
import logging
from datetime import datetime, timedelta

import httpx

from . import config, db

log = logging.getLogger("cost")


class BudgetExceeded(Exception):
    pass


def price(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = config.PRICES.get(model) or config.PRICES[config.MOCK_PRICE_AS]
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


def daily_limit() -> float:
    return float(db.get_state("daily_limit_usd", config.DAILY_COST_LIMIT_USD))


def today_key() -> str:
    return db.now().date().isoformat()


def spent_today() -> float:
    r = db.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM llm_calls WHERE substr(created_at,1,10)=?", (today_key(),))
    return round(r["s"], 4)


def is_paused() -> bool:
    return db.get_state("paused", "0") == "1"


def pause(reason: str):
    if not is_paused():
        db.set_state("paused", "1")
        db.set_state("paused_reason", reason)
        alert("critical", f"System paused: {reason}")


def resume():
    db.set_state("paused", "0")
    db.set_state("paused_reason", "")
    db.set_state("override_day", today_key())  # a human resumed today: allow spending past the limit until midnight
    alert("info", "System resumed by a human.")


def guard():
    """Called before every model call. Raises BudgetExceeded and pauses when the limit is hit."""
    if is_paused():
        raise BudgetExceeded(db.get_state("paused_reason", "paused"))
    if db.get_state("override_day") == today_key():
        return
    spent, lim = spent_today(), daily_limit()
    if spent >= lim:
        pause(f"daily cost limit reached (${spent:.2f} of ${lim:.2f})")
        raise BudgetExceeded("daily cost limit reached")


def alert(level: str, message: str):
    db.run("INSERT INTO alerts(level,message,created_at) VALUES(?,?,?)", (level, message, db.now_iso()))
    log.warning("[ALERT %s] %s", level, message)
    if config.ALERT_WEBHOOK_URL:
        try:
            httpx.post(config.ALERT_WEBHOOK_URL, json={"text": f"All In Capital screener - {level}: {message}"}, timeout=10)
        except Exception as e:
            log.warning("alert webhook failed: %s", e)


def summary() -> dict:
    now = db.now()
    week_start = (now - timedelta(days=now.weekday())).date().isoformat()
    month_start = now.date().replace(day=1).isoformat()

    def total(where="1=1", args=()):
        r = db.q1(f"SELECT COALESCE(SUM(cost_usd),0) s, COUNT(*) n FROM llm_calls WHERE {where}", args)
        return round(r["s"], 4), r["n"]

    by_app = db.q("""SELECT a.id, a.ref, a.company, COUNT(c.id) AS calls, COALESCE(SUM(c.cost_usd),0) AS cost,
                     SUM(c.input_tokens) AS tin, SUM(c.output_tokens) AS tout
                     FROM applications a LEFT JOIN llm_calls c ON c.app_id=a.id
                     GROUP BY a.id, a.ref, a.company ORDER BY cost DESC""")
    by_day = db.q("""SELECT substr(created_at,1,10) AS d, COUNT(*) AS calls, SUM(cost_usd) AS cost
                     FROM llm_calls GROUP BY substr(created_at,1,10) ORDER BY d DESC LIMIT 400""")
    for r in by_day:
        r["day"] = r["d"]
    by_week, by_month = {}, {}
    for r in by_day:  # roll days up in Python: identical on Postgres and SQLite
        y, w, _ = datetime.fromisoformat(r["d"]).isocalendar()
        for bucket, key in ((by_week, f"{y}-W{w:02d}"), (by_month, r["d"][:7])):
            b = bucket.setdefault(key, {"calls": 0, "cost": 0.0})
            b["calls"] += r["calls"]; b["cost"] += r["cost"] or 0
    by_week = [{"wk": k, **v} for k, v in sorted(by_week.items(), reverse=True)][:12]
    by_month = [{"month": k, **v} for k, v in sorted(by_month.items(), reverse=True)][:12]
    by_day = by_day[:31]
    by_step = db.q("""SELECT step, COUNT(*) AS calls, SUM(cost_usd) AS cost, AVG(cost_usd) AS avg_cost
                      FROM llm_calls GROUP BY step ORDER BY cost DESC""")
    scored = db.q1("SELECT COUNT(*) n FROM applications WHERE status IN ('scored','approved','rejected')")["n"]
    all_cost = total()[0]
    return {
        "today": total("substr(created_at,1,10)=?", (now.date().isoformat(),)),
        "week": total("substr(created_at,1,10)>=?", (week_start,)),
        "month": total("substr(created_at,1,10)>=?", (month_start,)),
        "all": total(),
        "per_scored_app": round(all_cost / scored, 4) if scored else None,
        "by_app": by_app, "by_day": by_day, "by_week": by_week, "by_month": by_month, "by_step": by_step,
        "limit": daily_limit(), "paused": is_paused(), "paused_reason": db.get_state("paused_reason", ""),
    }
