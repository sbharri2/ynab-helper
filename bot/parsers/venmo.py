"""Parse Venmo per-transaction emails into structured txn dicts.

Subject line is the primary source — it always follows one of these forms:
  - "Amanda Walter paid you $31.00"       (incoming)
  - "You paid Jennifer Gilbert $123.45"   (outgoing)
  - "Amanda Walter charged you $20.00"    (incoming charge request)
  - "You charged Jennifer Gilbert $20.00" (outgoing charge request)

Body is used for the note (the only thing not in the subject). Body format:
  "Amanda Walter paid you $31.00 Amanda Walter paid you$31.00 <NOTE>See transaction..."

Note that in observed fixtures the note sometimes butts directly up against
"See transaction" with no whitespace separator, so the NOTE_RE allows \\s*.

The user must enable per-transaction email notifications in the Venmo app:
  Me -> Settings -> Notifications -> Email -> Payments sent + Payments received
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

SUBJECT_INCOMING_RE = re.compile(
    r"^(?P<who>.+?)\s+(?P<verb>paid|charged)\s+you\s+\$(?P<amt>[\d,]+\.\d{2})\s*$"
)
SUBJECT_OUTGOING_RE = re.compile(
    r"^You\s+(?P<verb>paid|charged)\s+(?P<who>.+?)\s+\$(?P<amt>[\d,]+\.\d{2})\s*$"
)

# Body note: appears between the amount and "See transaction".
# In observed fixtures the body repeats the subject-like prefix
# ("Jane Doe paid you $31.00") multiple times before the real note, e.g.:
#   "...paid you $31.00 Jane Doe paid you $31.00Jane Doe paid you$31.00 NOTESee transaction..."
# so we anchor on the LAST "$X.XX" before "See transaction" by forbidding
# "$" inside the captured note. We also allow zero whitespace between the
# note and "See transaction" since the fixtures lack a separator there.
NOTE_RE = re.compile(
    r"\$\s*[\d,]+\.\d{2}\s+(?P<note>[^$]+?)\s*See\s+transaction",
    re.IGNORECASE | re.DOTALL,
)

# Body-fallback direction (when subject isn't passed)
DIRECTION_PATTERNS = [
    (re.compile(r"\bYou paid\b", re.I), "paid"),
    (re.compile(r"\bYou charged\b", re.I), "charged"),
    (re.compile(r"\bpaid you\b", re.I), "received"),
    (re.compile(r"\bcharged you\b", re.I), "charged_by"),
]
AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


def parse_subject(subject: str) -> dict:
    """Extract direction, counterparty, amount from the subject line."""
    out = {"direction": None, "counterparty": None, "amount_cents": None}
    if not subject:
        return out
    s = subject.strip()
    m = SUBJECT_INCOMING_RE.match(s)
    if m:
        out["counterparty"] = m.group("who").strip()
        out["direction"] = "received" if m.group("verb") == "paid" else "charged_by"
        out["amount_cents"] = int(round(float(m.group("amt").replace(",", "")) * 100))
        return out
    m = SUBJECT_OUTGOING_RE.match(s)
    if m:
        out["direction"] = m.group("verb")  # 'paid' or 'charged'
        out["counterparty"] = m.group("who").strip()
        out["amount_cents"] = int(round(float(m.group("amt").replace(",", "")) * 100))
    return out


def extract_direction_from_body(body: str) -> str | None:
    for regex, label in DIRECTION_PATTERNS:
        if regex.search(body):
            return label
    return None


def extract_amount_cents_from_body(body: str) -> int | None:
    m = AMOUNT_RE.search(body)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def extract_note_from_body(body: str) -> str:
    """Note appears between the amount and 'See transaction' in the body."""
    m = NOTE_RE.search(body)
    if m:
        note = m.group("note").strip()
        if 1 <= len(note) <= 300:
            return note
    return ""


def parse_email_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(body: str, *, subject: str = "", date_header: str = "",
          today: date | None = None) -> dict:
    """Top-level parser. Never raises.

    Prefers subject parsing (cleanest, always present). Falls back to body
    extraction for any missing fields.
    """
    text = _html_to_text(body) if body.lstrip().startswith("<") else body

    subj = parse_subject(subject)
    direction = subj["direction"] or extract_direction_from_body(text)
    counterparty = subj["counterparty"]
    amount = subj["amount_cents"] or extract_amount_cents_from_body(text)
    note = extract_note_from_body(text)
    order_date = (parse_email_date_header(date_header) or today or date.today())

    missing = [
        n for n, v in [("direction", direction), ("amount_cents", amount)] if v is None
    ]
    return {
        "source": "venmo",
        "direction": direction,
        "counterparty": counterparty,
        "note": note,
        "amount_cents": amount or 0,
        "order_date": order_date,
        "summary": (f"{direction or '?'} {counterparty or '?'} "
                    f"${(amount or 0)/100:.2f}"
                    + (f' — "{note}"' if note else "")),
        "parse_status": "ok" if not missing else "partial",
        "missing_fields": missing,
    }
