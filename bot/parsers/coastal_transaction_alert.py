"""Parse Coastal Federal Credit Union transaction alert emails.

Subject: "Transaction Alert"

Body (plaintext, very short):

    Dear STEVEN HARRIS JR,

    1 transaction(s) occurred on your JOINT CHECKING account
    $90.00 Deposit ACH VENMO


    Coastal Federal Credit Union
    800.868.4262

The body shape is one line per transaction. The "type word" (Deposit /
Withdrawal / Check / etc.) tells us direction:

    Deposit / Credit / Refund    →  POSITIVE amount (inflow)
    Withdrawal / Debit / Check
        / POS / ATM / ACH (out)  →  NEGATIVE amount (outflow)
    Anything else                →  use raw amount, leave sign unsigned

Caller maps the account name ("JOINT CHECKING") to a local account by
substring against `account.name` — populated by `bot.ingest._resolve_account_id`.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

TXN_LINE_RE = re.compile(
    r"\$\s*([\d,]+\.\d{2})\s+([A-Za-z][A-Za-z\s/]*?)(?:\s{2,}|\n|$)",
)
ACCOUNT_RE = re.compile(
    r"occurred on your\s+([A-Z][A-Z\s]+?)\s+account",
)

# Direction hints in the type-word. WORD-BOUNDED — without that, "pos"
# matches inside "deposit" and we'd classify deposits as outflows. Use
# regex with \b on each side.
_INFLOW_TOKENS = ("deposit", "refund", "credit", "interest", "incoming")
_OUTFLOW_TOKENS = (
    "withdrawal", "debit", "check", "pos", "atm",
    "purchase", "payment", "fee", "outgoing",
)


def _classify_direction(type_word: str) -> int:
    """Returns +1 (inflow), -1 (outflow), or 0 (unknown).

    Inflow checks FIRST so that "Deposit ACH VENMO" can't be miscast as
    outflow via incidental substrings (e.g. "pos" inside "deposit").
    """
    t = type_word.lower().strip()
    for token in _INFLOW_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", t):
            return +1
    for token in _OUTFLOW_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", t):
            return -1
    return 0


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(body: str, *, subject: str = "", date_header: str = "") -> dict:
    text = body or ""
    out: dict = {"source": "coastal_transaction_alert"}
    missing: list[str] = []

    # Account name
    acct_m = ACCOUNT_RE.search(text)
    account_name = acct_m.group(1).strip() if acct_m else None
    out["account_name"] = account_name
    if not account_name:
        missing.append("account_name")

    # Transaction line(s). Coastal usually sends one txn per email but
    # the body says "N transaction(s)"; we parse the first line.
    txn_m = TXN_LINE_RE.search(text)
    amount_cents: int | None = None
    type_word: str | None = None
    direction = 0
    if txn_m:
        amount_dollars = float(txn_m.group(1).replace(",", ""))
        amount_cents = int(round(amount_dollars * 100))
        type_word = txn_m.group(2).strip()
        direction = _classify_direction(type_word)
        # Apply sign based on direction. Unknown direction → leave positive
        # and let the LLM / user disambiguate later.
        if direction == -1:
            amount_cents = -abs(amount_cents)
        elif direction == +1:
            amount_cents = abs(amount_cents)
    out["amount_cents"] = amount_cents
    if amount_cents is None:
        missing.append("amount")
    out["type_word"] = type_word
    # Coastal doesn't give us a merchant per se — the type word + remainder
    # of the line is the closest thing.
    out["payee"] = (type_word or "Coastal transaction")[:120]
    out["merchant"] = type_word

    # Posted date — fall back to email header (Coastal emails don't
    # include a date in the body for transaction alerts).
    out["posted_date"] = _parse_date_header(date_header)
    if not out["posted_date"]:
        missing.append("posted_date")

    out["summary"] = (
        f"Coastal {account_name or '?'} "
        f"${abs(amount_cents)/100 if amount_cents is not None else 0:.2f} "
        f"{type_word or '(unknown type)'}"
    )

    if not missing:
        out["parse_status"] = "ok"
    elif amount_cents is not None:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
