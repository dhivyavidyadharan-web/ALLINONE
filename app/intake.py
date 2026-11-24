"""Intake: every application is written to the database and queued BEFORE any processing,
so a burst of 100 in a day can never lose one. Founder contact details stay here; the
rest of the system refers to the founder by ID."""
import hashlib
import json

from . import config, db


def founder_id(email: str, name: str, ref: str) -> str:
    key = (email or "").strip().lower() or f"{name}|{ref}"
    return "F-" + hashlib.sha1(key.encode()).hexdigest()[:6].upper()


def submit(a: dict, batch: str | None = None, ref_suffix: str = "") -> int:
    f = a.get("founder", {}) or {}
    ref = (a.get("ref") or "APP") + ref_suffix
    fid = founder_id(f.get("email", ""), f.get("name", ""), ref)
    now = db.now_iso()
    with db.tx() as c:
        c.execute("""INSERT INTO founders(id,name,email,phone,linkedin,created_at) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(id) DO NOTHING""",
                  (fid, f.get("name", ""), f.get("email", ""), f.get("phone", ""), f.get("linkedin", ""), now))
        cur = c.execute("""INSERT INTO applications(ref,founder_id,company,website,sector,stage,city,source,deck_text,
                           form_json,batch,status,status_reason,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ref, fid, a.get("company", ""), a.get("website", ""), (a.get("sector") or "").lower(),
                         (a.get("stage") or "").lower(), a.get("city", ""), a.get("source", "public_form"),
                         a.get("deck_text", ""), json.dumps(a.get("form", {})), batch or a.get("batch", ""),
                         "queued", "Waiting for a worker.", now, now))
        return cur.lastrowid


def sample_apps() -> list[dict]:
    return json.loads((config.DATA_DIR / "applications.json").read_text(encoding="utf-8"))["applications"]


def load_batch(name: str) -> list[int]:
    return [submit(a) for a in sample_apps() if a.get("batch") == name and
            not db.q1("SELECT 1 FROM applications WHERE ref=?", (a["ref"],))]


def volume_test(n: int = 20) -> tuple[str, list[int]]:
    """Task 2: n applications submitted in one go."""
    tag = "volume-" + db.now().strftime("%H%M%S")
    apps = sample_apps()
    ids = []
    for i in range(n):
        a = dict(apps[i % len(apps)])
        ids.append(submit(a, batch=tag, ref_suffix=f"-{tag[-6:]}-{i+1:02d}"))
    return tag, ids
