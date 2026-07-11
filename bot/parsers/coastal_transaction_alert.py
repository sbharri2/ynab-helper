"""Parse Coastal Federal Credit Union transaction alert emails.

Subject: "Transaction Alert"

Body (plaintext, very short):

    Dear STEVEN HARRIS JR,

    4 transaction(s) occurred on your JOINT CHECKING account
    $90.00 Deposit ACH VENMO
    $110.01 MASSMUTUAL LIFE
    $79.88 MASSACHUSETTS MU


    Coastal Federal Credit Union
    800.868.4262

The body shape is ONE line per transaction. Coastal does pack multiple
transactions into a single email when several post the same day (real
example: 4 lines above). ``parse()`` returns one txn dict (the first line)
for the legacy single-txn ingest contract, plus ``additional_txns`` with
the rest so ``bot.ingest`` can fan them out individually.

The "type word" (Deposit / Withdrawal / Check / etc.) tells us direction:

    Deposit / Credit / Refund    →  POSITIVE amount (inflow)
    Withdrawal / Debit / Check
        / POS / ATM / ACH (out)  →  NEGATIVE amount (outflow)
    Anything else (merchant
        name only, like
        MASSMUTUAL LIFE)         →  NEGATIVE amount (outflow)

Why default-negative on unknown direction: Coastal includes the type word
on Deposit/Withdrawal but omits it on ACH-debit insurance/utility lines
that just show ``$X.XX MERCHANT NAME``. Empirically those are always
outflows (recurring premiums, autopay, etc.) — defaulting to positive
flipped MassMutual premium debits into phantom $110.01 inflows for
months. If a true unknown inflow arrives, the user can correct it.

Caller maps the account name ("JOINT CHECKING") to a local account by
substring against `account.name` — populated by `bot.ingest._resolve_account_id`.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

# Payee class deliberately wide: the old [A-Za-z\s/] class silently
# DROPPED any line whose payee contains a digit or punctuation — which
# turned out to include every paycheck ("O'BRIEN/ATKINS" — apostrophe),
# "VGI 529 ACH", "COINBASE INC.", "Actalent, Inc.", ITM/POS/Shared-Branch
# lines. Found 2026-07-11 when the balance walk showed $5,487.49 of Joint
# transactions the bank had and we didn't. Still requires a leading
# letter so we never eat into the next "$X.XX" amount.
TXN_LINE_RE = re.compile(
    r"\$\s*([\d,]+\.\d{2})\s+([A-Za-z][\w\s/&.,'#*:%-]*?)(?:\s{2,}|\n|$)",
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

    Coastal's direction word LEADS the line ("Deposit ACH VENMO"), so
    inflow tokens only count in first position — an inflow word buried
    mid-line is merchant text, not direction. Real bug this fixes
    (2026-07-11): "CHASE CREDIT CRD" (an ACH payment TO Chase) matched
    \\bcredit\\b and recorded $2,012.03 of card payments as deposits.
    Outflow tokens still match anywhere; unknown defaults negative too,
    so a false outflow word costs nothing.
    """
    t = type_word.lower().strip()
    words = t.split()
    if words and words[0] in _INFLOW_TOKENS:
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


def _signed_amount(raw_amount: int, type_word: str) -> int:
    """Apply a sign to a Coastal alert line amount.

    Direction inferred from the type word; defaults NEGATIVE on unknown so
    merchant-only debit lines (MASSMUTUAL LIFE etc.) don't show up as
    phantom inflows.
    """
    direction = _classify_direction(type_word)
    if direction == +1:
        return abs(raw_amount)
    # outflow OR unknown → both negative
    return -abs(raw_amount)


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

    # Parse EVERY transaction line. Coastal sometimes packs several into
    # one email (e.g. four MassMutual debits posting the same day).
    all_lines: list[tuple[int, str]] = []
    for m in TXN_LINE_RE.finditer(text):
        amount_dollars = float(m.group(1).replace(",", ""))
        amount_cents = int(round(amount_dollars * 100))
        type_word = m.group(2).strip()
        signed = _signed_amount(amount_cents, type_word)
        all_lines.append((signed, type_word))

    primary_amount: int | None = None
    primary_type_word: str | None = None
    if all_lines:
        primary_amount, primary_type_word = all_lines[0]

    out["amount_cents"] = primary_amount
    if primary_amount is None:
        missing.append("amount")
    out["type_word"] = primary_type_word
    # Coastal doesn't give us a merchant per se — the type word + remainder
    # of the line is the closest thing.
    out["payee"] = (primary_type_word or "Coastal transaction")[:120]
    out["merchant"] = primary_type_word

    # Additional transaction lines beyond the first. ``bot.ingest`` can
    # fan these out into separate ledger_txn rows so a multi-debit email
    # isn't silently truncated to the first line.
    out["additional_txns"] = [
        {"amount_cents": amt, "type_word": tw,
         "payee": (tw or "Coastal transaction")[:120],
         "merchant": tw}
        for (amt, tw) in all_lines[1:]
    ]

    # Posted date — fall back to email header (Coastal emails don't
    # include a date in the body for transaction alerts).
    out["posted_date"] = _parse_date_header(date_header)
    if not out["posted_date"]:
        missing.append("posted_date")

    out["summary"] = (
        f"Coastal {account_name or '?'} "
        f"${abs(primary_amount)/100 if primary_amount is not None else 0:.2f} "
        f"{primary_type_word or '(unknown type)'}"
        + (f" (+{len(all_lines)-1} more)" if len(all_lines) > 1 else "")
    )

    if not missing:
        out["parse_status"] = "ok"
    elif primary_amount is not None:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
