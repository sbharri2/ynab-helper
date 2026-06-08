"""Parse Chase credit-card transaction alert emails.

Phase 1. Samples from `alerts.chase.com` follow a clean two-line layout
in the HTML body (which `gmail_watcher._extract_body` returns as plain
text via BeautifulSoup):

    Transaction alert
    You made a $21.44 transaction
    Account
    Prime Visa (...8477)
    Date
    Jun 8, 2026 at 2:33 AM ET
    Merchant
    AMAZON MKTPLACE PMTS
    Amount
    $21.44

The subject also contains the amount + merchant:

    "You made a $21.44 transaction with AMAZON MKTPLACE PMTS"

We use the subject as a fallback when body extraction fails.

Returned dict mirrors citi_alert.py so `bot.ingest._resolve_account_id`
treats them identically:

    {
      "source": "chase_alert",
      "merchant": str,
      "amount_cents": int,   # NEGATIVE — outflow
      "account_last4": str,  # "8477" for Prime Visa
      "posted_date": date,
      "summary": str,
      "parse_status": "ok" | "partial" | "fail",
      "missing_fields": [...],
    }
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

# Body patterns: label on a line, value on the next.
AMOUNT_RE = re.compile(r"\bAmount\s*\n\s*\$?([\d,]+\.\d{2})", re.I)
ACCOUNT_LINE_RE = re.compile(
    r"\bAccount\s*\n\s*([^\n]+?)\s*\n", re.I,
)
LAST4_RE = re.compile(r"\(\.{3}(\d{4})\)")  # "(...8477)"
DATE_RE = re.compile(
    r"\bDate\s*\n\s*([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.I,
)
MERCHANT_RE = re.compile(
    r"\bMerchant\s*\n\s*([^\n]+?)\s*\n", re.I,
)

# Subject fallback: "You made a $21.44 transaction with AMAZON MKTPLACE PMTS"
SUBJECT_RE = re.compile(
    r"You made a \$\s*([\d,]+\.\d{2})\s+transaction(?:\s+with\s+([^\n]+?))?\s*$",
    re.I,
)


def _parse_subject(subject: str) -> tuple[int | None, str | None]:
    """Return (amount_cents, merchant) parsed from the subject, both None on miss."""
    m = SUBJECT_RE.search(subject or "")
    if not m:
        return None, None
    amt = int(round(float(m.group(1).replace(",", "")) * 100))
    merch = (m.group(2) or "").strip() or None
    return amt, merch


def _parse_body_amount(text: str) -> int | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def _parse_body_account_last4(text: str) -> str | None:
    m = ACCOUNT_LINE_RE.search(text or "")
    if not m:
        return None
    line = m.group(1)
    last4 = LAST4_RE.search(line)
    return last4.group(1) if last4 else None


def _parse_body_merchant(text: str) -> str | None:
    m = MERCHANT_RE.search(text or "")
    return m.group(1).strip() if m else None


def _parse_body_date(text: str) -> date | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d, %Y").date()
    except ValueError:
        return None


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(body: str, *, subject: str = "", date_header: str = "") -> dict:
    text = body or ""
    out: dict = {"source": "chase_alert"}
    missing: list[str] = []

    # Amount: body first, subject fallback
    amount_cents = _parse_body_amount(text)
    subj_amt, subj_merch = _parse_subject(subject)
    if amount_cents is None:
        amount_cents = subj_amt
    if amount_cents is None:
        out["amount_cents"] = None
        missing.append("amount")
    else:
        out["amount_cents"] = -abs(amount_cents)  # Chase alerts are outflows

    # Last4
    last4 = _parse_body_account_last4(text)
    out["account_last4"] = last4
    if not last4:
        missing.append("account_last4")

    # Merchant: body first, subject fallback
    merchant = _parse_body_merchant(text) or subj_merch
    out["merchant"] = merchant
    out["payee"] = merchant or "Chase charge"
    if not merchant:
        missing.append("merchant")

    # Posted date: body first, then email header
    posted = _parse_body_date(text) or _parse_date_header(date_header)
    out["posted_date"] = posted
    if not posted:
        missing.append("posted_date")

    amt_str = (
        f"${(out['amount_cents'] or 0) / -100:.2f}"
        if out.get("amount_cents") else "$?"
    )
    out["summary"] = (
        f"Chase {amt_str} at {merchant or '(unknown merchant)'} "
        f"on {posted.isoformat() if posted else '(unknown date)'}"
    )

    if not missing:
        out["parse_status"] = "ok"
    elif out["amount_cents"] and posted:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
