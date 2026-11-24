"""Portkey routing, gateway-down fallback, and Langfuse payload redaction. No network: model calls are stubbed."""
import httpx
import pytest

from app import config, db, llm, tracing


@pytest.fixture
def live_portkey(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "LLM_MODE", "live")
    monkeypatch.setattr(config, "PORTKEY_API_KEY", "pk-test")
    monkeypatch.setattr(config, "RETRIES_PER_MODEL", 2)
    monkeypatch.setattr(llm.cost, "guard", lambda: None)
    calls = []
    monkeypatch.setattr(llm, "_gemini", lambda name, model, s, u: (calls.append(("gemini", name)) or ({"results": [], "ok": 1}, 1, 1)))
    yield calls


def _search_vars():
    return {"company": "Acme", "website": "acme.in", "sector": "agri", "city": "Pune",
            "founders": "Priya Raman", "hint": "farm inputs"}


def test_tool_prompts_skip_portkey(live_portkey, monkeypatch):
    monkeypatch.setattr(llm, "_portkey", lambda *a: pytest.fail("search must not go through Portkey"))
    llm.call("web_search", _search_vars(), app_id=None, step="search:test")
    assert live_portkey == [("gemini", "web_search")]
    row = db.q1("SELECT route FROM llm_calls WHERE step='search:test' ORDER BY id DESC")
    assert row["route"] == "gemini"


def test_gateway_down_falls_back_to_direct_gemini(live_portkey, monkeypatch):
    def down(*a, **kw):
        raise httpx.ConnectError("no route to portkey")
    monkeypatch.setattr(httpx, "post", down)
    name = next(n for n, p in llm.prompts.PROMPTS.items() if not p.get("tools"))
    vars_ = {k: "x" for k in llm.prompts.PROMPTS[name]["user"].split("{")[1:] for k in [k.split("}")[0]]}
    llm.call(name, vars_, step="gw:test")
    rows = db.q("SELECT route, status, error FROM llm_calls WHERE step LIKE 'gw:test%' ORDER BY id")
    assert rows[0]["route"] == "portkey" and rows[0]["error"].startswith("GatewayDown")
    assert rows[-1]["route"] == "gemini" and rows[-1]["status"] == "ok"


def test_founder_names_redacted_in_stored_payload_and_langfuse(live_portkey, monkeypatch):
    sent = []
    monkeypatch.setattr(tracing, "generation", lambda **kw: sent.append(kw))
    llm.call("web_search", _search_vars(), step="search:redact")
    row = db.q1("SELECT sent_payload FROM llm_calls WHERE step='search:redact' ORDER BY id DESC")
    assert "Priya" not in row["sent_payload"] and "[redacted]" in row["sent_payload"]
    assert all("Priya" not in kw["user"] for kw in sent)
