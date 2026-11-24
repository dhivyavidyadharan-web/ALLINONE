"""Loaded by pytest before any test module: point the app at a throwaway database and the offline model,
so no test can ever touch Neon, Gemini or the real seed data."""
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
# Never touch a real database from tests: only an explicit TEST_DATABASE_URL (a throwaway Postgres) is used.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
os.environ["RUN_BACKGROUND_WORKERS"] = "0"
os.environ["DATA_DIR"] = str(Path(__file__).resolve().parent.parent / "data" / "samples")  # fictional fixtures
os.environ["LLM_MODE"] = "mock"
os.environ["MOCK_LATENCY_S"] = "0.01"
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["PORTKEY_API_KEY"] = ""
os.environ["APP_PASSWORD"] = ""

os.environ["LIVE_WEB"] = "0"
