"""Parse Citi credit-card transaction alert emails.

Phase 1. Real samples in `raw_email_sample` show that Citi sends per-charge
alerts from ``alerts@info6.citi.com`` with the structured payload in HTML —
the text/plain body is just "click here to view your message" boilerplate.
``bot.gmail_watcher._extract_body`` now prefers HTML-stripped text when
plaintext has no $ amounts, so by the time this parser runs it receives a
line-broken text dump like:

    A $117.74 transaction was made on your Citi® Double Cash account
    ...
    Amount: $117.74
    Card Ending In
    5674
    Merchant
    HLU*HULUPLUS SANTA MONICA USA
    Date
    06/07/2026
    Time
    05:21 AM ET

Returned dict shape:
    {
      "source": "citi_alert",
      "merchant": str,         # "HLU*HULUPLUS SANTA MONICA USA"
      "amount_cents": int,     # NEGATIVE — Citi alerts are outflows by definition
      "account_last4": str,    # "5674" — caller maps to account_id via account.last4
      "posted_date": date,     # 06/07/2026 → date(2026, 6, 7)
      "summary": str,
      "parse_status": "ok" | "partial" | "fail",
      "missing_fields": [...],
    }

The Citi alert subject already has the amount, so when the body parse
fails to find it we fall back to the subject. Same for date — fallback
is the email's Date header.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

# "Amount: $117.74" anywhere on a line
AMOUNT_RE = re.compile(r"Amount[:\s]+\$?([\d,]+\.\d{2})", re.I)
# Subject fallback: "A $117.74 transaction was made on your Citi..."
SUBJECT_AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})\s+transaction", re.I)

# "Card Ending In\n5674" — label on its own line, value on the next
CARD_LAST4_RE = re.compile(
    r"Card\s+Ending\s+In\s*\n\s*(\d{4})", re.I,
)

# "Merchant\nHLU*HULUPLUS SANTA MONICA USA"
MERCHANT_RE = re.compile(
    r"\bMerchant\s*\n\s*([^\n]+?)\s*\n", re.I,
)

# "Date\n06/07/2026"
DATE_RE = re.compile(
    r"\bDate\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})", re.I,
)


def _parse_subject_amount(subject: str) -> int | None:
    if not subject:
        return None
    m = SUBJECT_AMOUNT_RE.search(subject)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def _parse_body_amount(text: str) -> int | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(body: str, *, subject: str = "", date_header: str = "") -> dict:
    text = body or ""
    out: dict = {"source": "citi_alert"}
    missing: list[str] = []

    # Amount — prefer body, fall back to subject
    amount_cents = _parse_body_amount(text) or _parse_subject_amount(subject)
    if amount_cents is None:
        out["amount_cents"] = None
        missing.append("amount")
    else:
        # Citi transaction alerts represent outflows; store as negative
        # to match the ledger convention (positive = inflow, negative =
        # spending).
        out["amount_cents"] = -abs(amount_cents)

    # Last4 — body only (subject doesn't include it)
    m = CARD_LAST4_RE.search(text)
    out["account_last4"] = m.group(1) if m else None
    if not out["account_last4"]:
        missing.append("account_last4")

    # Merchant — body only
    m = MERCHANT_RE.search(text)
    merchant = m.group(1).strip() if m else None
    out["merchant"] = merchant
    out["payee"] = merchant or "Citi DC charge"
    if not merchant:
        missing.append("merchant")

    # Posted date — prefer body, fall back to email header
    posted: date | None = None
    m = DATE_RE.search(text)
    if m:
        try:
            posted = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            posted = None
    if not posted:
        posted = _parse_date_header(date_header)
    out["posted_date"] = posted
    if posted is None:
        missing.append("posted_date")

    # Summary string for the ledger memo / LLM context
    amount_str = (
        f"${(out['amount_cents'] or 0) / -100:.2f}"
        if out.get("amount_cents")
        else "$?"
    )
    out["summary"] = (
        f"Citi DC {amount_str} at {merchant or '(unknown merchant)'} "
        f"on {posted.isoformat() if posted else '(unknown date)'}"
    )

    # Status: ok if amount + merchant + date + last4 all present.
    if not missing:
        out["parse_status"] = "ok"
    elif out["amount_cents"] and posted:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
