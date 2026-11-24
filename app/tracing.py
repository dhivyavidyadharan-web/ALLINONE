"""Task 4 - tracing. Every application run is one trace; every step is a span; every model
call is a generation with its prompt version, tokens and cost.

Traces are always stored locally (the /traces page) and, when LANGFUSE_* keys are set,
also shipped to Langfuse through its public ingestion API in a background thread.
"""
import base64
import logging
import queue


import uuid
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from . import config, db

log = logging.getLogger("tracing")
_current_trace: ContextVar[str | None] = ContextVar("trace", default=None)
_current_span: ContextVar[str | None] = ContextVar("span", default=None)
_current_app: ContextVar[int | None] = ContextVar("app", default=None)
_outbox: "queue.Queue[dict]" = queue.Queue()


def _ev(type_: str, body: dict) -> None:
    if config.langfuse_enabled():
        _outbox.put({"id": str(uuid.uuid4()), "timestamp": db.now_iso(), "type": type_, "body": body})


def current_trace():
    return _current_trace.get()


def current_span():
    return _current_span.get()


def current_app():
    return _current_app.get()


@contextmanager
def trace(name: str, app_id: int | None, metadata: dict | None = None, tags: list | None = None):
    tid = uuid.uuid4().hex
    start = db.now_iso()
    db.run("INSERT INTO traces(id,app_id,name,start_at,status,prompt_versions) VALUES(?,?,?,?,?,?)",
           (tid, app_id, name, start, "running", db.jdump(metadata or {})))
    _ev("trace-create", {"id": tid, "name": name, "timestamp": start, "metadata": metadata or {},
                         "tags": tags or [], "sessionId": f"app-{app_id}" if app_id else None})
    t1, t2, t3 = _current_trace.set(tid), _current_span.set(None), _current_app.set(app_id)
    status = "ok"
    try:
        yield tid
    except Exception as e:
        status = f"error: {type(e).__name__}"
        raise
    finally:
        cost = (db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE trace_id=?", (tid,)) or {}).get("c", 0)
        db.run("UPDATE traces SET end_at=?, status=?, cost_usd=? WHERE id=?", (db.now_iso(), status, cost, tid))
        _ev("trace-create", {"id": tid, "output": {"status": status, "cost_usd": cost}})
        _current_trace.reset(t1); _current_span.reset(t2); _current_app.reset(t3)
        flush()


@contextmanager
def span(name: str, input=None, metadata: dict | None = None):
    tid = _current_trace.get()
    sid = uuid.uuid4().hex
    parent = _current_span.get()
    start = db.now_iso()
    box = {"output": None, "status": "ok"}
    tok = _current_span.set(sid)
    try:
        yield box
    except Exception as e:
        box["status"] = f"error: {e}"[:300]
        raise
    finally:
        _current_span.reset(tok)
        end = db.now_iso()
        db.run("INSERT INTO spans(id,trace_id,parent_id,app_id,name,kind,start_at,end_at,status,input,output,metadata) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, tid, parent, _current_app.get(), name, "span", start, end, box["status"],
                db.jdump(input)[:4000], db.jdump(box["output"])[:4000], db.jdump(metadata or {})))
        if tid:
            _ev("span-create", {"id": sid, "traceId": tid, "parentObservationId": parent, "name": name,
                                "startTime": start, "endTime": end, "input": input, "output": box["output"],
                                "metadata": metadata or {}, "level": "DEFAULT" if box["status"] == "ok" else "ERROR",
                                "statusMessage": None if box["status"] == "ok" else box["status"]})


def generation(*, span_id, name, model, prompt_version, system, user, output, usage_in, usage_out,
               cost, start, end, status, route):
    tid = _current_trace.get()
    db.run("INSERT INTO spans(id,trace_id,parent_id,app_id,name,kind,start_at,end_at,status,input,output,metadata) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
           (span_id, tid, _current_span.get(), _current_app.get(), name, "generation", start, end, status,
            db.jdump({"system": system[:1500], "user": user[:2500]}), db.jdump(output)[:4000],
            db.jdump({"model": model, "prompt_version": prompt_version, "tokens_in": usage_in,
                      "tokens_out": usage_out, "cost_usd": cost, "route": route})))
    if tid:
        _ev("generation-create", {
            "id": span_id, "traceId": tid, "parentObservationId": _current_span.get(), "name": name,
            "startTime": start, "endTime": end, "model": model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "output": output, "version": prompt_version,
            "metadata": {"prompt_version": prompt_version, "route": route},
            "usage": {"input": usage_in, "output": usage_out, "total": usage_in + usage_out, "unit": "TOKENS",
                      "totalCost": cost},
            "level": "DEFAULT" if status == "ok" else "ERROR",
        })


def langfuse_trace_url(trace_id: str) -> str | None:
    if not config.langfuse_enabled():
        return None
    return f"{config.LANGFUSE_HOST}/trace/{trace_id}"


def flush():
    """Ship queued events to Langfuse now. Synchronous on purpose: on Vercel nothing may run after
    the response, so each run flushes its own trace when it ends."""
    if not config.langfuse_enabled() or _outbox.empty():
        return
    batch = []
    while not _outbox.empty() and len(batch) < 500:
        batch.append(_outbox.get_nowait())
    auth = base64.b64encode(f"{config.LANGFUSE_PUBLIC_KEY}:{config.LANGFUSE_SECRET_KEY}".encode()).decode()
    try:
        r = httpx.post(f"{config.LANGFUSE_HOST}/api/public/ingestion", json={"batch": batch},
                       headers={"Authorization": f"Basic {auth}"}, timeout=10)
        if r.status_code >= 300:
            log.warning("Langfuse ingestion %s: %s", r.status_code, r.text[:300])
    except Exception as e:  # tracing must never break the pipeline
        log.warning("Langfuse unreachable: %s", e)
