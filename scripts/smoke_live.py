"""Check the core services after adding keys to .env. Never prints secret values.

    python scripts/smoke_live.py

1. Neon: connects and runs read-only queries (no writes).
2. Gemini: ONE small real call (reply classification, a fraction of a cent) on the extraction
   model, with the fallback chain shown. Uses a throwaway local database, not Neon.
3. Langfuse (optional): sends one test trace.
"""
import base64
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "smoke.db")
os.environ["RUN_BACKGROUND_WORKERS"] = "0"

import httpx  # noqa: E402

from app import config, db, llm  # noqa: E402


def mark(ok):
    return "set" if ok else "MISSING"


print("Settings (values not shown):")
print(f"  DATABASE_URL     {mark(bool(config.DATABASE_URL))}")
print(f"  GEMINI_API_KEY   {mark(bool(config.GEMINI_API_KEY))}")
print(f"  APP_PASSWORD     {mark(bool(config.APP_PASSWORD))}   CRON_SECRET {mark(bool(config.CRON_SECRET))}")
print(f"  PORTKEY          {'on' if config.portkey_enabled() else 'off (optional)'}")
print(f"  LANGFUSE         {'on' if config.langfuse_enabled() else 'off (optional)'}")
print(f"  models           score={config.MODEL_SCORE} extract={config.MODEL_EXTRACT} write={config.MODEL_WRITE}")
failed = False

print("\n1) Neon")
if not config.DATABASE_URL:
    print("   SKIP DATABASE_URL not set")
else:
    try:
        r = db.q1("SELECT version() AS v, current_database() AS d")
        host = db.backend_label()
        print(f"   OK   {host}: {r['v'][:30]}... database '{r['d']}'")
        if "-pooler" not in config.DATABASE_URL:
            print("   NOTE use Neon's *pooled* connection string (host has '-pooler') for Vercel.")
    except Exception as e:
        failed = True
        print(f"   FAIL {type(e).__name__}: {str(e)[:300]}")
    db._reset()

print(f"\n2) Gemini ({config.MODEL_EXTRACT}, fallback {config.FALLBACK_MODELS['MODEL_EXTRACT']})")
if not config.GEMINI_API_KEY:
    failed = True
    print("   FAIL GEMINI_API_KEY not set")
else:
    config.DATABASE_URL = ""  # the test call is recorded in a throwaway SQLite file, not in Neon
    config.LLM_MODE = "live"
    db.init(force=True)
    try:
        out = llm.call("parse_reply", {"today": "Friday 2026-11-20", "slots": "  1. Mon 23 Nov, 10:00 AM IST",
                                        "reply": "Option 1 works for me, thanks!"}, step="smoke")
        for row in db.q("SELECT model, status, input_tokens, output_tokens, cost_usd, latency_ms, error FROM llm_calls ORDER BY id"):
            print(f"   {'OK  ' if row['status'] == 'ok' else 'try '} {row['model']}: {row['input_tokens']} in / "
                  f"{row['output_tokens']} out, ${row['cost_usd']:.5f}, {row['latency_ms']} ms {row['error'] or ''}")
        print(f"   parsed: intent={out.get('intent')} slot={out.get('chosen_slot')} (expected accept / 1)")
    except Exception as e:
        failed = True
        print(f"   FAIL {type(e).__name__}: {str(e)[:400]}")
        for row in db.q("SELECT model, error FROM llm_calls ORDER BY id"):
            print(f"        {row['model']}: {row['error']}")

print("\n3) Langfuse")
if not config.langfuse_enabled():
    print("   SKIP not configured (optional)")
else:
    tid = uuid.uuid4().hex
    auth = base64.b64encode(f"{config.LANGFUSE_PUBLIC_KEY}:{config.LANGFUSE_SECRET_KEY}".encode()).decode()
    ev = {"id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(), "type": "trace-create",
          "body": {"id": tid, "name": "smoke-test", "tags": ["smoke"]}}
    try:
        r = httpx.post(f"{config.LANGFUSE_HOST}/api/public/ingestion", json={"batch": [ev]},
                       headers={"Authorization": f"Basic {auth}"}, timeout=15)
        errs = (r.json().get("errors") if r.headers.get("content-type", "").startswith("application/json") else None) or []
        if r.status_code < 300 and not errs:
            print(f"   OK   HTTP {r.status_code}. Trace: {config.LANGFUSE_HOST}/trace/{tid}")
        else:
            failed = True
            print(f"   FAIL HTTP {r.status_code} {str(errs or r.text)[:300]}")
    except Exception as e:
        failed = True
        print(f"   FAIL {type(e).__name__}: {e}")

print("\nAll good." if not failed else "\nFix the failures above and re-run.")
sys.exit(1 if failed else 0)
