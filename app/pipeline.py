"""One application, end to end: collect info -> score -> recommendation (whiteboard boxes 2-4).

Status machine (every application is always in exactly one of these, with a reason):
  queued -> processing -> scored -> approved | rejected
                       -> needs_attention (reason says why; a human decides what next)
  (paused budget / transient errors put it back to queued with a reason; nothing is dropped)
"""
import json
import logging

from . import config, cost, db, llm, memory, portfolio, prompts, research, rubric, tracing

log = logging.getLogger("pipeline")


def set_status(app_id, status, reason=""):
    db.run("UPDATE applications SET status=?, status_reason=?, updated_at=? WHERE id=?",
           (status, reason, db.now_iso(), app_id))


def _validate_scores(raw: dict, claim_ids: set) -> tuple[list, list]:
    """Trust guard: a score with no valid citation is not a score."""
    crits = {c["key"]: c for c in raw.get("criteria", []) if c.get("key") in rubric.CRITERIA_BY_KEY}
    out = []
    for c in rubric.CRITERIA:
        r = crits.get(c["key"], {"score": None, "claim_ids": [], "memo_refs": [], "rationale": "", "missing": "Model returned nothing for this criterion."})
        cited = [x for x in r.get("claim_ids", []) if x in claim_ids]
        s = r.get("score")
        note = r.get("rationale", "")
        if s is not None:
            s = max(0.0, min(5.0, float(s)))
            if not cited:
                note = f"[Score {s} discarded: no valid source cited] " + note
                s = None
        out.append({"criterion": c["key"], "score": s, "claim_ids": cited, "memo_refs": r.get("memo_refs", []),
                    "rationale": note, "missing": r.get("missing", "") if s is None else ""})
    gates = []
    for g in rubric.GATES:
        r = next((x for x in raw.get("gates", []) if x.get("key") == g["key"]), None)
        if not r:
            gates.append({"key": g["key"], "passed": None, "claim_ids": [], "reason": "Not assessed."})
            continue
        cited = [x for x in r.get("claim_ids", []) if x in claim_ids]
        passed = r.get("passed")
        if passed is False and not cited:
            passed, r["reason"] = None, "[Fail discarded: no source cited] " + r.get("reason", "")
        gates.append({"key": g["key"], "passed": passed, "claim_ids": cited, "reason": r.get("reason", "")})
    return out, gates


def score(app: dict, found: dict, *, exclude_memo=None, persist=True, check_portfolio=True) -> dict:
    claims = found["claims"]
    claims_text = " ".join(c["text"] for c in claims)
    memos = memory.relevant_memo_passages(app.get("sector") or "", claims_text, exclude_memo=exclude_memo)
    corrs = memory.relevant_corrections(app.get("sector") or "", claims_text, exclude_app=app.get("id"))
    claims_block = "\n".join(f"{c['cid']} [{c['source_type']}: {c['source_ref']}] {c['text']}" for c in claims) or "(no evidence)"
    memo_block = "\n".join(f"[{m['memo_id']}] {m['company']} ({m['sector']}, {m['decision']}): {m['text']}" for m in memos) or "(none)"
    vars = {"rubric": rubric.rubric_text(app.get("sector") or ""), "company": app["company"],
            "founder_id": app.get("founder_id") or "F-?",
            "sector": app.get("sector") or "not stated", "stage": app.get("stage") or "not stated",
            "source": app.get("source") or "", "claims": claims_block, "memos": memo_block,
            "calibration": memory.calibration_text(corrs), "portfolio": portfolio.summary_text()}
    with tracing.span("score", input={"claims": len(claims), "memos": [m["memo_id"] for m in memos],
                                      "corrections": [c["id"] for c in corrs]}) as s:
        raw = llm.call("score_application", vars, app_id=app.get("id"), step="score",
                       mock_ctx={"claims": claims, "memos": memos, "corrections": corrs,
                                 "stage": app.get("stage") or "", "sector": app.get("sector") or ""})
        crit, gates = _validate_scores(raw, {c["cid"] for c in claims})
        s["output"] = {"scores": {c["criterion"]: c["score"] for c in crit}}
    # Portfolio conflict check: deterministic keyword match + the model's own read (PN-4). Either one -> HOLD.
    with tracing.span("portfolio_check") as s:
        overlaps = portfolio.overlaps(app["company"], [claims_text, app.get("deck_text") or ""]) if check_portfolio else []
        mo = raw.get("portfolio_overlap") if isinstance(raw, dict) else None
        if check_portfolio and mo and mo.get("company") and not any(o["company"] == mo["company"] for o in overlaps):
            overlaps.append({"company": mo["company"], "matched": [], "evidence": f"Model: {mo.get('reason', '')}"})
        s["output"] = overlaps
    used = [c["id"] for c in corrs]
    total, coverage = rubric.compute_total({c["criterion"]: c["score"] for c in crit})
    gate_fail = [f"{g['key']}: {g['reason']}" for g in gates if g["passed"] is False]
    verdict = rubric.verdict(total, coverage, gate_fail, hold=bool(overlaps))
    gaps = [{"criterion": "portfolio", "missing": f"Possible overlap with portfolio company {o['company']}. {o['evidence']}",
             "cost": "HOLD: Kushal to check with the portfolio founder before anything else happens (PN-4)."}
            for o in overlaps]
    if check_portfolio and not portfolio.is_complete():
        gaps.append({"criterion": "portfolio", "missing": "Conflict check ran against a PARTIAL portfolio list.",
                     "cost": "A conflict with an unlisted portfolio company would not be caught."})
    if (app.get("sector") or "").lower() == "deeptech":
        gaps.append({"criterion": "process", "missing": "Deeptech: Aditya runs the second call himself (PN-3).",
                     "cost": "Invite includes Aditya."})
    gaps += found["gaps"] + [{"criterion": c["criterion"], "missing": c["missing"],
                             "cost": f"{rubric.CRITERIA_BY_KEY[c['criterion']]['weight']} points unscored"}
                            for c in crit if c["score"] is None]
    gaps += [{"criterion": f"gate:{g['key']}", "missing": g["reason"], "cost": "Gate could not be checked"}
             for g in gates if g["passed"] is None]
    with tracing.span("recommend", input={"total": total, "coverage": coverage, "verdict": verdict}) as s:
        rec = llm.call("recommend", {
            "company": app["company"], "verdict": rubric.VERDICT_LABEL[verdict], "total": total, "coverage": coverage,
            "gates": "; ".join(gate_fail) or "none",
            "scores": "\n".join(f"{c['criterion']}: {c['score']} cites {c['claim_ids']} - {c['rationale']}" for c in crit),
            "gaps": "\n".join(f"- {g['missing']}" for g in gaps) or "none"},
            app_id=app.get("id"), step="recommend",
            mock_ctx={"scores": crit, "gate_failures": gate_fail, "verdict": verdict, "total": total,
                      "coverage": coverage, "company": app["company"]})
        s["output"] = rec
    result = {"criteria": crit, "gates": gates, "total": total, "coverage": coverage, "verdict": verdict,
              "gaps": gaps, "rec": rec, "corrections_used": used, "memos": [m["memo_id"] for m in memos]}
    if persist:
        _persist(app["id"], result, found)
        if used:
            db.run(f"UPDATE corrections SET times_applied=times_applied+1 WHERE id IN ({','.join('?'*len(used))})", used)
    return result


def _persist(app_id, r, found):
    with db.tx() as c:
        c.execute("DELETE FROM scores WHERE app_id=?", (app_id,))
        c.execute("DELETE FROM gaps WHERE app_id=?", (app_id,))
        for s in r["criteria"]:
            c.execute("""INSERT INTO scores(app_id,criterion,score,ai_score,rationale,claim_ids,memo_refs,corrections_used,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (app_id, s["criterion"], s["score"], s["score"], s["rationale"], json.dumps(s["claim_ids"]),
                       json.dumps(s["memo_refs"]), json.dumps(r["corrections_used"]), db.now_iso()))
        for g in r["gaps"]:
            c.execute("INSERT INTO gaps(app_id,criterion,missing,cost) VALUES(?,?,?,?)",
                      (app_id, g["criterion"], g["missing"], g["cost"]))
        for f in found.get("flags", []):
            c.execute("INSERT INTO gaps(app_id,criterion,missing,cost) VALUES(?,?,?,?)",
                      (app_id, "safety", f, "Applicant text tried to instruct the AI; treated as data."))
        c.execute("""INSERT INTO recommendations(app_id,total,coverage,verdict,gate_failures,case_for,case_against,
                     two_liner,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(app_id) DO UPDATE SET total=excluded.total, coverage=excluded.coverage,
                     verdict=excluded.verdict, gate_failures=excluded.gate_failures, case_for=excluded.case_for,
                     case_against=excluded.case_against, two_liner=excluded.two_liner,
                     prompt_version=excluded.prompt_version, created_at=excluded.created_at""",
                  (app_id, r["total"], r["coverage"], r["verdict"], json.dumps(r["gates"]), r["rec"].get("case_for"),
                   r["rec"].get("case_against"), r["rec"].get("two_liner"),
                   json.dumps(prompts.all_versions()), db.now_iso()))


def preflight(app) -> str | None:
    """Cheap checks before spending money. Returns a 'needs attention' reason or None."""
    if not (app["deck_text"] or "").strip() and not app["website"]:
        return "Empty application: no deck text and no website. Ask the founder to resubmit."
    if not app["company"] or app["company"].strip().lower() in ("", "unknown"):
        return "Company name missing."
    if len(app["deck_text"] or "") > 60000:
        return "Deck text over 60k characters - likely a bad upload. Check the file."
    return None


def process(app_id: int):
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    problem = preflight(app)
    if problem:
        set_status(app_id, "needs_attention", problem)
        return
    meta = {"app_ref": app["ref"], "rubric": rubric.RUBRIC_VERSION, "prompt_versions": prompts.all_versions(),
            "llm_mode": config.LLM_MODE, "route": llm.route()}
    try:
        with tracing.trace(f"triage:{app['ref']}", app_id, metadata=meta, tags=["triage", app["batch"] or ""]) as tid:
            db.run("UPDATE applications SET trace_id=? WHERE id=?", (tid, app_id))
            with tracing.span("collect_info") as s:
                found = research.collect(app)
                s["output"] = {"claims": len(found["claims"]), "gaps": len(found["gaps"]), "flags": found["flags"]}
            if not found["claims"]:
                set_status(app_id, "needs_attention", "No usable claims could be extracted from any source.")
                return
            r = score(app, found)
        reason = rubric.VERDICT_LABEL[r["verdict"]]
        if found["flags"]:
            reason += " - safety flag: injection attempt ignored"
        set_status(app_id, "scored", reason)
    except cost.BudgetExceeded as e:
        set_status(app_id, "queued", f"Waiting: system paused ({e}).")
    except llm.TransientModelError as e:
        n = (db.q1("SELECT attempts FROM applications WHERE id=?", (app_id,)) or {}).get("attempts", 0)
        if n >= config.MAX_ATTEMPTS:
            set_status(app_id, "needs_attention", f"Model unavailable after {n} attempts: {e}")
        else:
            set_status(app_id, "queued", f"Retry {n}/{config.MAX_ATTEMPTS} after transient error: {e}")
    except llm.ModelError as e:
        set_status(app_id, "needs_attention", f"Model problem: {e}")
    except Exception as e:  # never lose an application to an unexpected bug
        log.exception("process failed")
        set_status(app_id, "needs_attention", f"Unexpected error: {type(e).__name__}: {e}"[:400])
