"""Task 1 - minimise what reaches the model.

Rules enforced here (and checked on the /audit page against every recorded payload):
  * founders are referred to by founder ID (F-xxxx), never by email or phone;
  * founder names in deck text are replaced by the founder ID;
  * web pages are reduced to extracted claims before scoring (see research.py);
  * only the top memo passages are sent, never every memo (see memory.py).
"""
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_CAND = re.compile(r"(?<![\w-])\+?\d[\d\s-]{8,}\d(?![\w-])")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _PhoneRe:
    """Phone-like runs with at least 10 digits; ISO dates are not phones."""

    @staticmethod
    def _ok(s):
        return sum(ch.isdigit() for ch in s) >= 10 and not _ISO_DATE.match(s.strip())

    def search(self, text):
        return next((m for m in _PHONE_CAND.finditer(text) if self._ok(m.group(0))), None)

    def sub(self, repl, text):
        return _PHONE_CAND.sub(lambda m: repl if self._ok(m.group(0)) else m.group(0), text)


PHONE_RE = _PhoneRe()
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def scrub(text: str, founder_id: str = "", founder_name: str = "") -> str:
    if not text:
        return ""
    t = HTML_COMMENT_RE.sub(" ", text)
    t = EMAIL_RE.sub("[email removed]", t)
    t = PHONE_RE.sub("[phone removed]", t)
    if founder_name and founder_name.strip() and founder_name.lower() != "unknown":
        name = founder_name.strip()
        label = f"the founder ({founder_id})" if founder_id else "the founder"
        t = re.sub(rf"(?:\b(?:co-)?founder\s+)?{re.escape(name)}", label, t, flags=re.I)
        # also first-name-only and surname-only mentions
        parts = [p for p in re.split(r"\s+", name.replace("Dr.", "").strip()) if len(p) > 2]
        for p in parts:
            t = re.sub(rf"\b{re.escape(p)}\b", label, t, flags=re.I)
        t = re.sub(rf"(?:{re.escape(label)}\s*){{2,}}", label + " ", t)
    return t.strip()


def pii_findings(payload: str) -> list[str]:
    """What the audit looks for in anything that was sent to a model."""
    found = []
    if EMAIL_RE.search(payload or ""):
        found.append("email address")
    if PHONE_RE.search(payload or ""):
        found.append("phone number")
    return found
