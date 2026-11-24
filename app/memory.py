"""What the system remembers: past memos (precedent) and partner corrections (calibration).

Task 1: only the few most relevant memo passages are sent to the model, never the whole set.
Kushal's ask: a correction made once should count next time on a similar company.
"""
import json
import re

from . import config, db, rubric

STOP = set("the a an and or of to in for with on at by is are was were be it this that we our from as has have not no".split())


def _tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in STOP and len(w) > 2}


def load_memos():
    path = config.DATA_DIR / "memos.json"
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8")
    sig = f"{path}:{len(raw)}"
    if db.get_state("memos_source") == sig and db.q1("SELECT COUNT(*) AS n FROM memos")["n"]:
        return
    data = json.loads(raw)["memos"]
    with db.tx() as c:
        c.execute("DELETE FROM memo_passages")
        c.execute("DELETE FROM memos")
        for m in data:
            text = f"PROFILE: {m['profile']}\nDECISION: {m['decision']}\nREASONING: {m['reasoning']}"
            c.execute("INSERT INTO memos(id,company,sector,stage,decision,date,text) VALUES(?,?,?,?,?,?,?)",
                      (m["id"], m["company"], m["sector"], m["stage"], m["decision"], m["date"], text))
            for i, part in enumerate([m["profile"], f"Decision: {m['decision']}. {m['reasoning']}"]):
                c.execute("INSERT INTO memo_passages(memo_id,idx,text) VALUES(?,?,?)", (m["id"], i, part))
    db.set_state("memos_source", sig)


def relevant_memo_passages(sector: str, claims_text: str, k: int = 3, exclude_memo: str | None = None):
    want = _tokens(claims_text) | _tokens(sector)
    rows = db.q("""SELECT p.memo_id, p.idx, p.text, m.company, m.sector, m.decision
                   FROM memo_passages p JOIN memos m ON m.id=p.memo_id WHERE p.idx=1""")
    scored = []
    for r in rows:
        if exclude_memo and r["memo_id"] == exclude_memo:
            continue  # backtest: never let the system see the answer for the company it is judging
        overlap = len(want & _tokens(r["text"]))
        bonus = 5 if sector and r["sector"] == sector.lower() else 0
        scored.append((overlap + bonus, r))
    scored.sort(key=lambda x: -x[0])
    return [r for s, r in scored[:k] if s > 0]


def relevant_corrections(sector: str, claims_text: str, k_per_criterion: int = 3, exclude_app: int | None = None):
    """Corrections from similar companies: same sector, or strong keyword overlap."""
    want = _tokens(claims_text)
    rows = db.q("SELECT * FROM corrections ORDER BY id DESC")
    out = []
    for c in rubric.CRITERIA:
        cand = []
        for r in rows:
            if r["criterion"] != c["key"] or r["app_id"] == exclude_app:
                continue
            kw = set(json.loads(r["keywords"] or "[]"))
            sim = (3 if sector and r["sector"] == sector.lower() else 0) + len(want & kw) / 5
            if sim >= 3:
                cand.append((sim, r))
        cand.sort(key=lambda x: -x[0])
        out += [r for _, r in cand[:k_per_criterion]]
    return out


def record_correction(app_id: int, criterion: str, original, corrected: float, reason: str, by: str):
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    claims = " ".join(r["text"] for r in db.q("SELECT text FROM claims WHERE app_id=?", (app_id,)))
    kws = sorted(_tokens(claims))[:80]
    cid = db.run("""INSERT INTO corrections(app_id,criterion,sector,keywords,original,corrected,reason,by_user,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                 (app_id, criterion, (app["sector"] or "").lower(), json.dumps(kws),
                  original if original is not None else corrected, corrected, reason, by, db.now_iso()))
    db.run("UPDATE scores SET score=?, corrected_by=? WHERE app_id=? AND criterion=?", (corrected, by, app_id, criterion))
    return cid


def calibration_text(corrs) -> str:
    if not corrs:
        return "(none yet)"
    return "\n".join(
        f"- [K{c['id']}] {c['criterion']}: partners moved a similar {c['sector']} company from "
        f"{c['original']} to {c['corrected']} because: {c['reason']}" for c in corrs)


def style_examples(k: int = 3):
    """Riya's own edits to past drafts - used to steer new drafts."""
    return db.q("""SELECT kind, ai_draft, body FROM emails WHERE direction='out' AND status='sent'
                   AND ai_draft IS NOT NULL AND ai_draft != body ORDER BY id DESC LIMIT ?""", (k,))
