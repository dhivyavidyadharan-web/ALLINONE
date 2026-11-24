"""The ONLY place a model is called.

Every call:  cost guard -> render versioned prompt -> Gemini (direct, or via Portkey if configured)
             -> retries with backoff -> fallback model chain -> validate JSON
             -> record cost + exact payload for EVERY attempt -> trace generation.
"""
import json
import logging
import random
import re
import time
import uuid

import httpx

from . import config, cost, db, mock_model, prompts, tracing

log = logging.getLogger("llm")
_client = None


class ModelError(Exception):
    """Non-retryable (bad request, auth, refusal, unusable output). App goes to 'needs attention'."""


class TransientModelError(Exception):
    """Retryable (rate limit, overload, 5xx, network). App goes back to the queue."""


class _Fatal(Exception):
    """Configuration problem (bad key, forbidden): don't try fallback models."""


def route() -> str:
    if config.LLM_MODE != "live":
        return "mock"
    return "portkey" if config.portkey_enabled() else "gemini"


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=config.GEMINI_API_KEY or None)
    return _client


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _classify(e: Exception) -> Exception:
    """Map any SDK/HTTP error to Transient / Model / Fatal."""
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
    name = type(e).__name__
    if isinstance(code, int):
        if code in (401, 403):
            return _Fatal(f"HTTP {code}: check GEMINI_API_KEY / project access. {str(e)[:200]}")
        if code in (408, 409, 429) or code >= 500:
            return TransientModelError(f"HTTP {code}: {str(e)[:200]}")
        return ModelError(f"HTTP {code}: {str(e)[:300]}")
    if any(k in name for k in ("Connection", "Timeout", "ContinuationInterrupted")) or isinstance(e, (httpx.TransportError, TimeoutError)):
        return TransientModelError(f"{name}: {str(e)[:200]}")
    return ModelError(f"{name}: {str(e)[:300]}")


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except ValueError as e:
        raise ModelError(f"Model returned invalid JSON: {t[:200]}") from e
    if not isinstance(data, dict):
        raise ModelError("Model returned JSON that is not an object.")
    return data


def _gemini(name, model, system, user):
    p = prompts.PROMPTS[name]
    kw = dict(
        model=model,
        input=user,
        system_instruction=system,
        generation_config={"thinking_level": p["effort"], "max_output_tokens": p["max_tokens"]},
        store=False,  # applicant data is not retained on Google's side
        timeout=config.MODEL_TIMEOUT_S,
    )
    if p.get("tools"):
        # Built-in tools (e.g. url_context) are not combined with a response schema; the prompt asks for JSON.
        kw["tools"] = p["tools"]
    else:
        kw["response_format"] = {"type": "text", "mime_type": "application/json", "schema": p["schema"]}
    resp = client().interactions.create(**kw)
    u = getattr(resp, "usage", None)
    tin = (getattr(u, "total_input_tokens", 0) or 0) if u else 0
    tout = ((getattr(u, "total_output_tokens", 0) or 0) + (getattr(u, "total_thought_tokens", 0) or 0)) if u else 0
    status = str(getattr(resp, "status", "completed"))
    if status in ("failed",):
        raise TransientModelError(f"Gemini status failed: {getattr(resp, 'errors', '')}")
    if status in ("incomplete", "budget_exceeded"):
        raise ModelError(f"Gemini stopped early ({status}); raise max_output_tokens for '{name}'.")
    data = _parse_json(resp.output_text or "")
    if p.get("tools"):
        data["_citations"] = _citations(resp)
    return data, tin, tout


def _citations(resp) -> list[dict]:
    """URL citations Google attached to the answer (what search/URL tools actually returned)."""
    out = []
    for step in getattr(resp, "steps", None) or []:
        for item in getattr(step, "content", None) or []:
            for a in getattr(item, "annotations", None) or []:
                if getattr(a, "type", "") == "url_citation" and getattr(a, "url", None):
                    out.append({"url": a.url, "title": getattr(a, "title", "") or ""})
    return out


class _GatewayDown(Exception):
    """Portkey itself failed (unreachable, its own 5xx, or rejected the Portkey key)."""


def uses_portkey(name: str) -> bool:
    """Prompts with Google's built-in tools (search, page reading) always go straight to Gemini: Portkey's
    OpenAI-style endpoint doesn't carry those tools or their citations."""
    return route() == "portkey" and not prompts.PROMPTS[name].get("tools")


def _portkey(name, model, system, user, app_id):
    """Optional: same Gemini model, routed through Portkey's gateway (retries, logs, cost on Portkey's side)."""
    p = prompts.PROMPTS[name]
    headers = {"x-portkey-api-key": config.PORTKEY_API_KEY}
    if config.PORTKEY_VIRTUAL_KEY:  # Gemini key kept in Portkey's vault
        headers["x-portkey-virtual-key"] = config.PORTKEY_VIRTUAL_KEY
    else:
        headers.update({"x-portkey-provider": "google", "Authorization": f"Bearer {config.GEMINI_API_KEY}"})
    headers.update({
        "x-portkey-config": json.dumps({"retry": {"attempts": config.PORTKEY_RETRY_ATTEMPTS,
                                                  "on_status_codes": [408, 429, 500, 502, 503, 504]}}),
        "x-portkey-trace-id": tracing.current_trace() or uuid.uuid4().hex,
        "x-portkey-metadata": json.dumps({"step": name, "app_id": str(app_id or ""), "prompt_version": prompts.version(name)}),
    })
    body = {"model": model, "max_tokens": p["max_tokens"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": p["schema"]}}}
    try:
        r = httpx.post(f"{config.PORTKEY_BASE_URL}/chat/completions", json=body, headers=headers,
                       timeout=config.MODEL_TIMEOUT_S)
    except httpx.TransportError as e:
        raise _GatewayDown(f"Portkey unreachable: {type(e).__name__}") from e
    if r.status_code == 401 and "portkey" in r.text.lower():
        raise _GatewayDown("Portkey rejected PORTKEY_API_KEY / PORTKEY_VIRTUAL_KEY")
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    return _parse_json(j["choices"][0]["message"]["content"]), u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def _mock(name, mock_ctx, system, user):
    time.sleep(config.MOCK_LATENCY_S * random.uniform(0.5, 1.5))
    if random.random() < config.MOCK_FAIL_RATE:
        raise TransientModelError("mock: simulated 503 overloaded")
    data = mock_model.HANDLERS[name]({}, mock_ctx or {})
    return data, _est_tokens(system + user), _est_tokens(json.dumps(data))


_FOUNDERS_LINE = re.compile(r"^Founders: .*$", re.M)


def _for_trace(name: str, user: str) -> str:
    """Founder names are sent to the search step only; stored payloads and Langfuse get them redacted."""
    return _FOUNDERS_LINE.sub("Founders: [redacted]", user) if name == "web_search" else user


def _record(*, app_id, span_id, step, name, pv, model, rt, tin, tout, ms, status, err, system, user, data, start):
    user = _for_trace(name, user)
    c = cost.price(config.MOCK_PRICE_AS if rt == "mock" else model, tin, tout)
    if rt != "mock" and status == "ok" and any(t.get("type") == "google_search" for t in prompts.PROMPTS[name].get("tools", [])):
        c += config.SEARCH_CALL_USD
    shown_model = f"mock({model})" if rt == "mock" else model
    db.run("""INSERT INTO llm_calls(app_id,trace_id,span_id,step,prompt_name,prompt_version,model,route,
              input_tokens,output_tokens,cost_usd,latency_ms,status,error,sent_payload,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
           (app_id, tracing.current_trace(), span_id, step, name, pv, shown_model, rt, tin, tout, c, ms,
            status, err, json.dumps({"system": system, "user": user}), db.now_iso()))
    tracing.generation(span_id=span_id, name=step, model=shown_model, prompt_version=pv, system=system, user=user,
                       output=data if data is not None else err, usage_in=tin, usage_out=tout, cost=c,
                       start=start, end=db.now_iso(), status=status, route=rt)


def call(name: str, vars: dict, *, app_id=None, step=None, mock_ctx=None) -> dict:
    system, user = prompts.render(name, vars)
    pv = prompts.version(name)
    model_key = prompts.PROMPTS[name]["model"]
    chain = [getattr(config, model_key)] + [m for m in config.FALLBACK_MODELS.get(model_key, []) if m]
    rt = route()
    if rt == "mock":
        chain = chain[:1]
    elif rt == "portkey" and not uses_portkey(name):
        rt = "gemini"
    last: Exception | None = None
    for mi, model in enumerate(chain):
        for attempt in range(config.RETRIES_PER_MODEL):
            cost.guard()  # raises BudgetExceeded -> caller re-queues, nothing is lost
            span_id, start, t0 = uuid.uuid4().hex, db.now_iso(), time.time()
            data, tin, tout, status, err = None, 0, 0, "ok", None
            try:
                if rt == "mock":
                    data, tin, tout = _mock(name, mock_ctx, system, user)
                elif rt == "portkey":
                    data, tin, tout = _portkey(name, model, system, user, app_id)
                else:
                    data, tin, tout = _gemini(name, model, system, user)
            except _GatewayDown as e:
                # The gateway is optional: if it is down, keep screening by calling Gemini directly.
                status, err, last = "error", f"GatewayDown: {e}"[:500], TransientModelError(str(e))
                log.warning("%s; switching to direct Gemini for this call", e)
            except (ModelError, TransientModelError) as e:
                status, err, last = "error", f"{type(e).__name__}: {e}"[:500], e
            except Exception as e:  # SDK / HTTP errors
                last = _classify(e)
                status, err = "error", f"{type(last).__name__}: {last}"[:500]
            finally:
                label = (step or name) + (f" [fallback:{model}]" if mi else "") + (f" [retry {attempt}]" if attempt else "")
                _record(app_id=app_id, span_id=span_id, step=label, name=name, pv=pv, model=model, rt=rt,
                        tin=tin, tout=tout, ms=int((time.time() - t0) * 1000), status=status, err=err,
                        system=system, user=user, data=data, start=start)
            if (err or "").startswith("GatewayDown"):
                rt = "gemini"  # recorded under 'portkey' above; later attempts go direct
            if status == "ok":
                return data
            if isinstance(last, _Fatal):
                raise ModelError(str(last))
            if isinstance(last, TransientModelError):
                if not (err or "").startswith("GatewayDown"):
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)) * (0.1 if rt == "mock" else 1))
                continue
            break  # ModelError on this model: no point retrying it, try the next model
    if isinstance(last, TransientModelError):
        raise last
    raise ModelError(str(last) if last else "no model available")
