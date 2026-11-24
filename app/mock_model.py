"""Deterministic offline stand-in for the model (LLM_MODE=mock).

It returns the SAME JSON shapes as the real prompts, so the whole pipeline, queue, cost
page, tracing and evals run without keys. It is a keyword heuristic - good enough to
demo and to test plumbing, NOT a substitute for the real model's judgement. Switch to
LLM_MODE=live for real scoring.
"""
import re
from datetime import datetime, timedelta

from . import rubric

# Instructions aimed at a reviewer/model - not mere mentions of AI (CAPilot sells "an AI assistant").
INJECTION_RE = re.compile(
    r"((ignore|disregard) (your|all|the|previous|prior)[^.]*(instructions|rubric|rules)[^.]*|note to the ai[^.]*|"
    r"(ai|model|assistant|reviewer|llm)\s*[:,-]?\s*(please\s+)?(rate|score|give|recommend|approve)[^.]*(10|5/5|out of|take the call|this (startup|company))[^.]*|"
    r"rate this (startup|company)[^.]*|give this company[^.]*)", re.I)
FACT_HINT = re.compile(
    r"\d|founder|previously|years|phd|patent|raised|grant|launched|live|pilot|beta|prototype|revenue|gmv|users|"
    r"customers|retention|repeat|reorder|renew|loi|letters of intent|partner|nbfc|balance sheet|token|deposit|agency|"
    r"project|kitchen|waitlist|asking|burn|margin|led|ran|built|taught|veterinarian|brand manager|series|seed|"
    r"bootstrapped|imports|why now|enabled|services|retainer|tips|landing page|idea", re.I)


def _sentences(text):
    return [s.strip(" -•\n") for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if len(s.strip()) > 12]


def extract_claims(v, ctx):
    text = ctx.get("text", "")
    inj = INJECTION_RE.search(text)
    claims = []
    for s in _sentences(text):
        if INJECTION_RE.search(s):
            continue
        if FACT_HINT.search(s) or len(claims) == 0:
            claims.append(s[:240])
    return {"claims": claims[:12], "injection_detected": bool(inj), "injection_text": inj.group(0)[:200] if inj else ""}


def _find(claims, pattern):
    rx = re.compile(pattern, re.I)
    return [c for c in claims if rx.search(c["text"])]


def _years(texts):
    ys = [int(m) for t in texts for m in re.findall(r"(\d+)\s*(?:\+\s*)?years", t, re.I)]
    rng = [int(b) - int(a) for t in texts for a, b in re.findall(r"\((20\d\d)\s*-\s*(20\d\d)\)", t)]
    return max(ys + rng + [0])


def _criterion(key, claims):
    """Return (score|None, [claims], rationale, missing)."""
    if key == "founder_quality":
        hits = _find(claims, r"founder|previously|years|phd|led|ran|head|director|manager|teacher|taught|veterinarian|"
                             r"chartered|built|graduated|family|first-time|robotics lead")
        if not hits:
            return None, [], "", "No information about the founders' background."
        texts = [c["text"] for c in hits]
        y = _years(texts)
        s = 2.5
        if re.search(r"graduated last year|first-time|freelance", " ".join(texts), re.I):
            s = 1.5
        if y >= 5 or re.search(r"phd|patent|head of|director|led robotics|robotics lead|ran claims|built the", " ".join(texts), re.I):
            s = 4.0
        if y >= 6:
            s = 4.5
        if re.search(r"agency|fantasy|growth at", " ".join(texts), re.I) and s < 3:
            s = 2.0
        if {c["source_type"] for c in hits} & {"web", "founder_profile", "news"} and len({c["source_type"] for c in hits}) > 1:
            s = min(5.0, s + 0.5)
        behaviour = re.search(r"customer|field|visited|talked|pilot|built|ran |led |owns|risk|cohort|repeat|candid|"
                              r"launched|shipped|years in|taught|practice", " ".join(texts), re.I)
        if not behaviour:
            s = min(s, 3.0)  # PN-3: credentials are not a business
        return s, hits[:4], f"Founder background: {y or 'unstated'} yrs relevant experience; " + ("corroborated by an independent source." if len({c['source_type'] for c in hits}) > 1 else "deck only, not corroborated."), ""
    if key == "path_to_market":
        hits = _find(claims, r"launched|live|shipped|pilot|beta|prototype|customers?|clients?|paying|distribution|channel|"
                             r"regulat|licen|partner|sales|since|delivered|disbursed|idea|landing page|coming soon")
        if not hits:
            return None, [], "", "Nothing on what has been built or shipped, or when."
        t = " ".join(c["text"] for c in hits)
        s = 2.5
        if re.search(r"prototype|beta", t, re.I): s = 3.0
        if re.search(r"live|launched|pilot|running", t, re.I): s = 3.5
        if re.search(r"month on month|mom|growing \d+%|in 6 weeks|weekly", t, re.I): s += 1.0
        if re.search(r"idea and a landing page|coming soon", t, re.I): s = 1.0
        if re.search(r"agreement in principle|no signed|not yet signed|once the tech", t, re.I):
            s = min(s, 2.5)
        return min(s, 5.0), hits[:3], "Who pays, how they are reached, and what is live.", ""
    if key == "market_unused":
        hits = _find(claims, r"market|why now|imports|enabled|regulation|pli|tier|india|lakh|crore|large|under-served|each .* serves")
        if not hits:
            return None, [], "", "No market size or why-now argument."
        t = " ".join(c["text"] for c in hits)
        s = 3.0
        if re.search(r"why now|enabled|pli|imports|regulat|serves about", t, re.I): s = 4.0
        if re.search(r"large and growing", t, re.I) and len(hits) == 1: s = 2.0
        return s, hits[:3], "Market argument " + ("with a specific unlock." if s >= 4 else "is generic."), ""
    if key == "evidence_of_pull":
        hits = _find(claims, r"revenue|gmv|mau|monthly active|users|paying|repeat|retention|reorder|renew|loi|letters of intent|"
                             r"waitlist|pilot|customers|throughput|downloads|deposits|disbursed|orders|claims a month|picks")
        if not hits:
            return None, [], "", "No traction data (users, revenue, pilots, retention)."
        t = " ".join(c["text"] for c in hits)
        s = 2.5
        if re.search(r"loi|letters of intent|waitlist|pilot", t, re.I): s = 3.0
        if re.search(r"revenue|gmv|paying|throughput|monthly active", t, re.I): s = 3.5
        if re.search(r"repeat|retention|reorder|renew", t, re.I): s = 4.5
        if re.search(r"not tracked|not shown|not measured", t, re.I): s = min(s, 2.0)  # PN-1: a missing number drags
        if re.search(r"5,000 gmv|inr 5,000", t, re.I): s = 1.0
        return s, hits[:4], "Traction signal graded on pull, not size.", ""
    if key == "defensibility":
        hits = _find(claims, r"without|instead|one-fifth|cut|never|only|first|reach .* without|voice-first|fresh-cooked|for indian|"
                             r"never hold|platform fee|formulated")
        if not hits:
            return None, [], "", "No stated insight or differentiation beyond the product description."
        s = 3.5 if len(hits) >= 2 else 3.0
        if _find(claims, r"crowded|incumbent|copy|competitors? with|well-funded"):
            s -= 1.0
        return s, hits[:2], "Differentiation stated by the founder; not independently tested.", ""
    if key == "capital_fit":
        hits = _find(claims, r"burn|asking|nbfc|contribution margin|grant|bootstrapped|balance sheet|loan book|marketing ahead|token treasury|second kitchen|hire more")
        if not hits:
            return None, [], "", "No burn or use-of-funds information."
        t = " ".join(c["text"] for c in hits)
        s = 3.0
        if re.search(r"burn.*inr [1-8] lakh|grant|contribution margin positive|never hold credit|nbfc partner", t, re.I): s = 4.0
        if re.search(r"contribution margin positive", t, re.I): s = 4.5
        if re.search(r"own balance sheet|fund the loan book|marketing ahead|token treasury|burn.*inr 2\d lakh|"
                     r"founder's savings|advances book|large raise|density|negative contribution", t, re.I): s = 1.5
        return s, hits[:3], "Use of funds vs. next milestone.", ""
    return None, [], "", "unknown criterion"


def _gates(claims, stage, sector):
    out = []
    first = [claims[0]["cid"]] if claims else []
    if sector and sector.lower() in rubric.SECTORS_IN_THESIS:
        out.append({"key": "sector", "passed": True, "claim_ids": first, "reason": f"Operates in {sector}."})
    else:
        out.append({"key": "sector", "passed": None if not sector else False, "claim_ids": first,
                    "reason": "Sector not stated." if not sector else f"{sector} is outside the thesis sectors."})
    late = _find(claims, r"series [ab]|usd \d+(\.\d+)?\s?m|seed round|series b")
    if late or (stage and stage.lower() not in ("pre-seed", "preseed", "")):
        out.append({"key": "stage", "passed": False, "claim_ids": [c["cid"] for c in late][:2],
                    "reason": "Already raised a priced round beyond pre-seed."})
    else:
        r = _find(claims, r"raised|grant|bootstrapped|nothing yet|none|family money|no equity")
        out.append({"key": "stage", "passed": True if r else None, "claim_ids": [c["cid"] for c in r][:2],
                    "reason": "Raised under the pre-seed line." if r else "Amount raised so far is not stated."})
    anti = [
        (r"per-project|delivered \d+ apps for clients|projects? delivered|our team of \d+ developers|managed campaigns|request a quote", "Services/agency revenue presented as a product."),
        (r"real-money|deposit money|win cash|deposits", "Real-money gaming."),
        (r"token sale|utility token|tokens reserved|crypto|web3|blockchain", "Crypto / web3 (M-10 Coinlane)."),
        (r"own balance sheet|fund the loan book", "Balance-sheet lending funded with equity."),
        (r"cloud kitchen in|second kitchen", "Single-location offline business."),
        (r"stock tips|guaranteed returns", "Unregistered investment advice."),
    ]
    for rx, why in anti:
        hit = _find(claims, rx)
        if hit:
            out.append({"key": "anti_thesis", "passed": False, "claim_ids": [c["cid"] for c in hit][:2], "reason": why})
            break
    else:
        out.append({"key": "anti_thesis", "passed": True if claims else None, "claim_ids": first,
                    "reason": "No anti-thesis category detected." if claims else "Nothing to check."})
    return out


def score_application(v, ctx):
    claims = ctx["claims"]
    corrections = ctx.get("corrections", [])
    memos = ctx.get("memos", [])
    crit = []
    for c in rubric.CRITERIA:
        s, hits, why, missing = _criterion(c["key"], claims)
        cal = [x for x in corrections if x["criterion"] == c["key"]]
        if s is not None and cal:
            delta = sum(x["corrected"] - x["original"] for x in cal) / len(cal)
            s = max(0.0, min(5.0, s + delta))
            why += f" Calibrated by {len(cal)} partner correction(s) on similar companies ({delta:+.1f})."
        crit.append({"key": c["key"], "score": None if s is None else round(s, 1),
                     "claim_ids": [h["cid"] for h in hits], "memo_refs": [m["memo_id"] for m in memos[:1]] if s is not None else [],
                     "rationale": why, "missing": missing})
    return {"gates": _gates(claims, ctx.get("stage", ""), ctx.get("sector", "")), "criteria": crit,
            "portfolio_overlap": None}


def recommend(v, ctx):
    sc = ctx["scores"]
    good = [s for s in sc if s["score"] is not None and s["score"] >= 4]
    bad = [s for s in sc if s["score"] is not None and s["score"] < 2.5]
    unknown = [s for s in sc if s["score"] is None]
    name = lambda s: rubric.CRITERIA_BY_KEY[s["criterion"]]["name"]
    case_for = "; ".join(f"{name(s)} {s['score']}/5 ({', '.join(s['claim_ids'][:2])})" for s in good) or "No criterion scores strongly."
    case_against = "; ".join(f"{name(s)} {s['score']}/5" for s in bad)
    if ctx["gate_failures"]:
        case_against = "Gate failed: " + "; ".join(ctx["gate_failures"]) + (". " + case_against if case_against else "")
    if unknown:
        case_against += (" " if case_against else "") + "Unknown: " + ", ".join(name(s) for s in unknown) + "."
    label = rubric.VERDICT_LABEL[ctx["verdict"]]
    top = name(good[0]) if good else "nothing standing out"
    biggest_unknown = name(unknown[0]) if unknown else (name(bad[0]) if bad else "none major")
    return {"case_for": case_for, "case_against": case_against or "No major weakness found in the evidence.",
            "two_liner": f"{ctx['company']}: {label} at {ctx['total']}/100 (coverage {int(ctx['coverage']*100)}%). "
                         f"Strongest on {top}; biggest open question: {biggest_unknown}."}


def draft_email(v, ctx):
    kind, company = ctx["kind"], ctx["company"]
    hl = ctx.get("highlights") or []
    sign = ctx.get("signoff") or "Best,\nRiya\nAll In Capital"
    opener = ctx.get("opener") or "Hi [FIRST_NAME],"
    if kind == "intro":
        body = (f"{opener}\n\nThanks for sending us {company}. "
                + (f"What stood out: {hl[0].rstrip('.')}." if hl else "")
                + "\n\nWe'd like to get on a 30-minute call with you and Kushal to learn more (we do these on Tuesday and Thursday afternoons). Would any of these work?\n\n"
                + ctx.get("slots", "") + "\n\nIf none of these suit, reply with a couple of times that do.\n\n" + sign)
        return {"subject": f"{company} x All In Capital - quick call?", "body": body}
    if kind == "rejection":
        body = (f"{opener}\n\nThank you for sharing {company} with us and for the time you put into the application. "
                f"After a careful look we won't be taking it forward at this stage. {ctx.get('reason','')}\n\n"
                "This is about fit with our current thesis rather than a verdict on you. We'd be glad to hear how things progress.\n\n" + sign)
        return {"subject": f"{company} - All In Capital", "body": body}
    return {"subject": f"{company}", "body": f"{opener}\n\n{ctx.get('reason','')}\n\n{sign}"}


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_reply(v, ctx):
    text = ctx["reply"].lower()
    today = ctx["today_dt"]
    if re.search(r"not interested|no thanks|decline|we'll pass|we have decided|not raising", text):
        return {"intent": "decline", "chosen_slot": None, "proposed_times": [], "summary": "Founder declined."}
    m = re.search(r"(option|slot)\s*(\d)|\b(first|second|third)\b (one|slot|option)?", text)
    if m and re.search(r"works|good|great|fine|confirm|perfect|ok|book", text):
        idx = int(m.group(2)) if m.group(2) else {"first": 1, "second": 2, "third": 3}[m.group(3)]
        return {"intent": "accept", "chosen_slot": idx, "proposed_times": [], "summary": f"Accepted slot {idx}."}
    for i, d in enumerate(DAYS):
        if d in text:
            tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text)
            h = int(tm.group(1)) % 12 + (12 if tm and tm.group(3) == "pm" else 0) if tm else 11
            mi = int(tm.group(2) or 0) if tm else 0
            ahead = (i - today.weekday()) % 7 or 7
            dt = (today + timedelta(days=ahead)).replace(hour=h, minute=mi, second=0, microsecond=0)
            return {"intent": "counter", "chosen_slot": None, "proposed_times": [dt.isoformat()],
                    "summary": f"Proposed {d.title()} {dt.strftime('%I:%M %p')} instead."}
    if "?" in text:
        return {"intent": "question", "chosen_slot": None, "proposed_times": [], "summary": "Founder asked a question."}
    return {"intent": "unclear", "chosen_slot": None, "proposed_times": [], "summary": "Could not tell what the founder wants."}


def backtest_explain(v, ctx):
    r = ctx["reasoning"].lower()
    if re.search(r"hindsight|may have been|shut down|too early|should have", r):
        return {"likely_right": "system", "explanation": "The memo itself records that the decision did not hold up "
                "(outcome noted in the memo). The system's caution/enthusiasm matched what later happened."}
    return {"likely_right": "fund", "explanation": "The partners' reasoning rests on information or judgement the system's "
            "evidence did not capture; no later outcome contradicts the decision."}


HANDLERS = {
    "web_search": lambda v, ctx: {"results": []},
    "read_url": lambda v, ctx: {"status": "unreadable", "title": "", "text": ""},
    "extract_claims": extract_claims, "score_application": score_application, "recommend": recommend,
    "draft_email": draft_email, "parse_reply": parse_reply, "backtest_explain": backtest_explain,
}
