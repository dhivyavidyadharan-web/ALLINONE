"""Whiteboard right half: Final decision -> Draft email -> (Riya edits) Send -> Meeting booking agent.

Riya always sends the first email herself. After that the booking agent handles the
busywork: reads replies, books accepted slots in both calendars, answers counter-proposals,
follows up after N days of silence, and hands anything unclear back to Riya.

Mail and calendar here are a built-in simulation (outbox/inbox/calendar tables) behind
small functions - swap `send_mail` / calendar functions for Gmail / Google Calendar APIs.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from . import config, cost, db, llm, memory, privacy, rubric, tracing

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_MIN = 30


# ---------------- calendar ----------------
def seed_calendar():
    if db.q1("SELECT COUNT(*) n FROM calendar_events WHERE owner='riya'")["n"]:
        return
    base = db.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    with db.tx() as c:
        for d in range(0, 21):
            day = base + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            c.execute("INSERT INTO calendar_events(owner,title,start_at,end_at,kind) VALUES('riya','Lunch',?,?,'busy')",
                      ((day + timedelta(hours=13)).isoformat(), (day + timedelta(hours=14)).isoformat()))
            if day.weekday() in (1, 3):
                c.execute("INSERT INTO calendar_events(owner,title,start_at,end_at,kind) VALUES('riya','Partner meeting',?,?,'busy')",
                          ((day + timedelta(hours=10)).isoformat(), (day + timedelta(hours=12)).isoformat()))
            if day.weekday() == 0:
                c.execute("INSERT INTO calendar_events(owner,title,start_at,end_at,kind) VALUES('riya','Portfolio check-ins',?,?,'busy')",
                          ((day + timedelta(hours=15)).isoformat(), (day + timedelta(hours=17)).isoformat()))


def _dt(s) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def is_free(owner: str, start: datetime, minutes=SLOT_MIN) -> bool:
    end = start + timedelta(minutes=minutes)
    for e in db.q("SELECT start_at,end_at FROM calendar_events WHERE owner=?", (owner,)):
        if _dt(e["start_at"]) < end and start < _dt(e["end_at"]):
            return False
    return True


# Partner notes PN-5: "Tuesday or Thursday afternoons, 30 minutes. Aditya joins only for deeptech."
CALL_DAYS = {1, 3}                      # Tue, Thu
CALL_START, CALL_END = (14, 0), (17, 30)  # last slot starts 17:00, ends 17:30


def business_hours(dt: datetime) -> bool:
    d = dt.astimezone(IST)
    return d.weekday() in CALL_DAYS and CALL_START <= (d.hour, d.minute) and \
        (d + timedelta(minutes=SLOT_MIN)).time() <= datetime.min.replace(hour=CALL_END[0], minute=CALL_END[1]).time()


def _app(app_id):
    return db.q1("SELECT * FROM applications WHERE id=?", (app_id,))


def attendees(app) -> list[str]:
    return ["riya", "kushal"] + (["aditya"] if (app.get("sector") or "").lower() == "deeptech" else [])


def find_slots(n=3, after: datetime | None = None, owners=("riya",)) -> list[str]:
    t = (after or db.now()).astimezone(IST) + timedelta(hours=18)
    t = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)
    out, per_day = [], {}
    for _ in range(48 * 21):
        if business_hours(t) and per_day.get(t.date(), 0) < 2 and all(is_free(o, t) for o in owners):
            out.append(t.isoformat())
            per_day[t.date()] = per_day.get(t.date(), 0) + 1
            if len(out) == n:
                break
            t += timedelta(minutes=60)  # spread slots within a day
            continue
        t += timedelta(minutes=30)
    return out


def fmt(s: str) -> str:
    return _dt(s).astimezone(IST).strftime("%a %d %b, %I:%M %p IST")


def slots_text(slots) -> str:
    return "\n".join(f"  {i+1}. {fmt(s)}" for i, s in enumerate(slots))


# ---------------- mail ----------------
def _first_name(app):
    f = db.q1("SELECT name FROM founders WHERE id=?", (app["founder_id"],)) or {}
    parts = [p for p in (f.get("name") or "").replace("Dr.", "").split() if p]
    return parts[0] if parts else "there"


def _outreach(app_id):
    return db.q1("SELECT * FROM outreach WHERE app_id=?", (app_id,))


def _set_outreach(app_id, **kw):
    if not _outreach(app_id):
        db.run("INSERT INTO outreach(app_id, stage) VALUES(?, 'new')", (app_id,))
    sets = ", ".join(f"{k}=?" for k in kw)
    db.run(f"UPDATE outreach SET {sets} WHERE app_id=?", (*kw.values(), app_id))


def signoff() -> str:
    """Riya's sign-off as learned from her most recent edited email."""
    return _house_style()[1].get("signoff") or DEFAULT_SIGNOFF


DEFAULT_SIGNOFF = "Best,\nRiya\nAll In Capital"


def send_mail(app_id, kind, subject, body, ai_draft=None) -> int:
    """Simulated send. Replace with Gmail API send for production."""
    body = body.replace(DEFAULT_SIGNOFF, signoff())
    return db.run("""INSERT INTO emails(app_id,direction,kind,subject,body,ai_draft,status,created_at,sent_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (app_id, "out", kind, subject, body, ai_draft, "sent", db.now_iso(), db.now_iso()))


def _house_style():
    ex = memory.style_examples()
    if not ex:
        return "(no edits yet)", {}
    text = "\n".join(f"- DRAFT: {e['ai_draft'][:300]!r}\n  SENT: {e['body'][:300]!r}" for e in ex)
    last = ex[0]["body"].strip().splitlines()
    opener = last[0] if last and last[0].lower().startswith(("hi", "hello", "dear", "hey")) else None
    tail = "\n".join(last[-3:]) if len(last) >= 3 else None
    ctx = {}
    if opener:
        ctx["opener"] = re.sub(r"(hi|hello|dear|hey)\s+[A-Za-z.]+", lambda m: m.group(1) + " [FIRST_NAME]", opener, flags=re.I)
    if tail and "riya" in tail.lower():
        ctx["signoff"] = tail
    return text, ctx


def draft(app_id: int, kind: str, reason: str = "") -> int:
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    best = db.q("SELECT claim_ids FROM scores WHERE app_id=? AND score IS NOT NULL ORDER BY score DESC LIMIT 2", (app_id,))
    cids = [c for r in best for c in json.loads(r["claim_ids"] or "[]")][:2]
    highlights = [r["text"] for cid in cids for r in db.q("SELECT text FROM claims WHERE app_id=? AND cid=?", (app_id, cid))]
    highlights = [privacy.scrub(h) for h in highlights]
    slots = find_slots(owners=tuple(attendees(app))) if kind == "intro" else []
    style_text, style_ctx = _house_style()
    with tracing.trace(f"outreach:{app['ref']}:draft_{kind}", app_id, metadata={"kind": kind}):
        out = llm.call("draft_email", {"kind": kind, "company": app["company"], "highlights": "; ".join(highlights) or "n/a",
                                       "reason": reason or "n/a", "slots": slots_text(slots) or "n/a", "style": style_text},
                       app_id=app_id, step=f"draft:{kind}",
                       mock_ctx={"kind": kind, "company": app["company"], "highlights": highlights, "reason": reason,
                                 "slots": slots_text(slots), **style_ctx})
    body = out["body"].replace("[FIRST_NAME]", _first_name(app))  # name filled in locally, never sent to the model
    eid = db.run("""INSERT INTO emails(app_id,direction,kind,subject,body,ai_draft,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (app_id, "out", kind, out["subject"], body, body, "draft", db.now_iso()))
    _set_outreach(app_id, stage="drafted", offered_slots=json.dumps(slots))
    return eid


def decide(app_id: int, decision: str, note: str, by: str) -> int | None:
    """Final decision (human). Records agreement with the AI verdict for the trust report."""
    rec = db.q1("SELECT verdict FROM recommendations WHERE app_id=?", (app_id,)) or {}
    ai = rec.get("verdict")
    agreed = None if ai in (None, "riya_review", "hold") else int((ai == "take_call") == (decision == "approve"))
    db.run("INSERT INTO decisions(app_id,decision,by_user,note,ai_verdict,agreed,created_at) VALUES(?,?,?,?,?,?,?)",
           (app_id, decision, by, note, ai, agreed, db.now_iso()))
    db.run("UPDATE applications SET status=?, status_reason=?, updated_at=? WHERE id=?",
           ("approved" if decision == "approve" else "rejected", f"{by}: {note or decision}", db.now_iso(), app_id))
    if cost.is_paused():
        return None
    return draft(app_id, "intro" if decision == "approve" else "rejection", reason=note)


def send_draft(email_id: int, subject: str, body: str):
    e = db.q1("SELECT * FROM emails WHERE id=?", (email_id,))
    db.run("UPDATE emails SET subject=?, body=?, status='sent', sent_at=? WHERE id=?", (subject, body, db.now_iso(), email_id))
    if e["kind"] == "intro":
        _set_outreach(e["app_id"], stage="awaiting_reply", last_out_at=db.now_iso(), follow_ups=0)
    else:
        _set_outreach(e["app_id"], stage="closed", last_out_at=db.now_iso())


def book(app_id: int, start_iso: str):
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    start = _dt(start_iso)
    end = start + timedelta(minutes=SLOT_MIN)
    title = f"All In Capital x {app['company']}"
    people = attendees(app)
    with db.tx() as c:  # fund side + founder's calendar
        for owner in people + [app["founder_id"]]:
            c.execute("INSERT INTO calendar_events(owner,title,start_at,end_at,app_id,kind) VALUES(?,?,?,?,?,'meeting')",
                      (owner, title, start.isoformat(), end.isoformat(), app_id))
    who = "Riya, Kushal and Aditya" if "aditya" in people else "Riya and Kushal"
    send_mail(app_id, "confirmation", f"Confirmed: {title}",
              f"Hi {_first_name(app)},\n\nConfirmed for {fmt(start.isoformat())} (30 minutes) with {who}. "
              f"A calendar invite is on its way with the video link.\n\nBest,\nRiya\nAll In Capital")
    _set_outreach(app_id, stage="booked", meeting_start=start.isoformat(), last_out_at=db.now_iso())


def _offer(app_id, slots, lead):
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    send_mail(app_id, "slots", f"Re: {app['company']} x All In Capital",
              f"Hi {_first_name(app)},\n\n{lead}\n\n{slots_text(slots)}\n\nJust reply with the number that works.\n\nBest,\nRiya\nAll In Capital")
    _set_outreach(app_id, stage="slots_offered", offered_slots=json.dumps(slots), last_out_at=db.now_iso())


def receive_reply(app_id: int, text: str) -> dict:
    """Meeting booking agent: one founder reply in, one action out."""
    app = db.q1("SELECT * FROM applications WHERE id=?", (app_id,))
    o = _outreach(app_id) or {}
    db.run("INSERT INTO emails(app_id,direction,kind,subject,body,status,created_at) VALUES(?,?,?,?,?,?,?)",
           (app_id, "in", "reply", f"Re: {app['company']}", text, "received", db.now_iso()))
    offered = json.loads(o.get("offered_slots") or "[]")
    today = db.now().astimezone(IST)
    with tracing.trace(f"outreach:{app['ref']}:reply", app_id, metadata={"stage": o.get("stage")}):
        clean = privacy.scrub(text)
        r = llm.call("parse_reply", {"today": today.strftime("%A %Y-%m-%d"), "slots": slots_text(offered) or "none",
                                     "reply": clean}, app_id=app_id, step="parse_reply",
                     mock_ctx={"reply": clean, "today_dt": today})
        with tracing.span("booking_action", input=r) as s:
            action = _act(app_id, r, offered)
            s["output"] = action
    return {"parsed": r, "action": action}


def _act(app_id, r, offered):
    intent = r.get("intent")
    if intent == "accept" and r.get("chosen_slot") and 1 <= r["chosen_slot"] <= len(offered):
        slot = offered[r["chosen_slot"] - 1]
        if all(is_free(o, _dt(slot)) for o in attendees(_app(app_id))):
            book(app_id, slot)
            return f"Booked {fmt(slot)}."
        _offer(app_id, find_slots(owners=tuple(attendees(_app(app_id)))), "That slot has just been taken, sorry. Here are the next openings:")
        return "Chosen slot no longer free; offered new slots."
    if intent == "counter" and r.get("proposed_times"):
        for t in r["proposed_times"]:
            try:
                dt = _dt(t)
            except ValueError:
                continue
            if dt > db.now() and business_hours(dt) and all(is_free(o, dt) for o in attendees(_app(app_id))):
                book(app_id, dt.isoformat())
                return f"Counter-proposal accepted: booked {fmt(dt.isoformat())}."
        near = find_slots(after=_dt(r["proposed_times"][0]) - timedelta(hours=18), owners=tuple(attendees(_app(app_id))))
        _offer(app_id, near, "We take founder calls on Tuesday and Thursday afternoons, so that time doesn't work for us. The closest openings are:")
        return "Counter-proposal clashed; offered nearest free slots."
    if intent == "decline":
        _set_outreach(app_id, stage="declined", note=r.get("summary"))
        return "Founder declined; thread closed."
    _set_outreach(app_id, stage="needs_riya", note=r.get("summary"))
    return "Handed to Riya: " + (r.get("summary") or intent)


def follow_up_tick():
    now = db.now()
    for o in db.q("SELECT * FROM outreach WHERE stage IN ('awaiting_reply','slots_offered')"):
        if not o["last_out_at"] or now - _dt(o["last_out_at"]) < timedelta(days=config.FOLLOW_UP_AFTER_DAYS):
            continue
        app = db.q1("SELECT * FROM applications WHERE id=?", (o["app_id"],))
        if o["follow_ups"] >= config.CLOSE_AFTER_FOLLOW_UPS:
            _set_outreach(o["app_id"], stage="closed_no_reply", note=f"No reply after {o['follow_ups']} follow-ups.")
            continue
        slots = find_slots(owners=tuple(attendees(app)))
        send_mail(o["app_id"], "follow_up", f"Re: {app['company']} x All In Capital",
                  f"Hi {_first_name(app)},\n\nFloating this up in case it got buried - we'd still love to talk. "
                  f"A few times that work this week:\n\n{slots_text(slots)}\n\nBest,\nRiya\nAll In Capital")
        _set_outreach(o["app_id"], follow_ups=o["follow_ups"] + 1, last_out_at=db.now_iso(),
                      offered_slots=json.dumps(slots), stage="slots_offered")
