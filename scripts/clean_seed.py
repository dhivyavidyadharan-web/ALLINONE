"""Remove markdown escape backslashes (\\& \\_ \\~ ...) that the Drive PDF text extraction added.
data/seed/raw/ keeps the verbatim text; only data/seed/applications.json is cleaned.

    python scripts/clean_seed.py
"""
import json
import re
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "data" / "seed" / "applications.json"
ESC = re.compile(r"\\([_&~<>#*().!+|$\[\]-])")


def unesc(s):
    return ESC.sub(r"\1", s) if isinstance(s, str) else s


d = json.loads(P.read_text(encoding="utf-8"))
changed = 0
for a in d["applications"]:
    for k in ("deck_text", "company", "website", "city"):
        new = unesc(a.get(k, ""))
        changed += new != a.get(k, "")
        a[k] = new
    a["form"] = {unesc(k): unesc(v) for k, v in a["form"].items()}
    a["founder"] = {k: unesc(v) for k, v in a["founder"].items()}
if "escape backslashes" not in d["_note"]:
    d["_note"] += " Markdown escape backslashes from PDF extraction removed here; raw/ keeps the verbatim text."
P.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
for a in d["applications"]:
    print(a["ref"], a["company"], "|", a["sector"], "|", a["stage"], "| deck", len(a["deck_text"]),
          "| form Qs", len(a["form"]), "|", a["founder"]["name"])
left = sum(len(re.findall(r"\\[_&~]", json.dumps(a, ensure_ascii=False))) for a in d["applications"])
print("fields changed:", changed, "| escapes left:", left)
