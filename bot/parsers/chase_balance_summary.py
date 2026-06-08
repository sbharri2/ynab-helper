"""Parse Chase credit-card balance summary emails.

Subject: "Your Prime Visa balance is $155.73"

Body (HTML, stripped by gmail_imap.extract_body):

    Account Alert
    Your balance of $155.73 is over the level you set
    Account
    Prime Visa (...8477)
    Balance
    $155.73

Chase sends a CC "balance" — for a credit card this is the OUTSTANDING
DEBT (what you owe). Our `account.balance_cents` and the daily summary
should treat it as a negative number (liability), matching the YNAB
convention where CC accounts have negative balances.

Returns the balance_summary shape that bot.ingest._ingest_balance
expects:

    {
      "source": "balance_summary",
      "as_of_date": date,
      "account_balances": [
        {"account_label": "Prime Visa", "last4": "8477",
         "balance_cents": <negative int>},
      ],
      "parse_status": ...,
    }
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

# "Your Prime Visa balance is $155.73"
SUBJECT_RE = re.compile(
    r"Your\s+([\w\s]+?)\s+balance\s+is\s+\$\s*([\d,]+\.\d{2})", re.I,
)

# Body: "Account\nPrime Visa (...8477)"
ACCOUNT_LINE_RE = re.compile(
    r"\bAccount\s*\n\s*([^\n]+?)\s*\n", re.I,
)
LAST4_RE = re.compile(r"\(\.{3}(\d{4})\)")

# "Balance\n$155.73"
BODY_BALANCE_RE = re.compile(
    r"\bBalance\s*\n\s*\$?([\d,]+\.\d{2})", re.I,
)


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(body: str, *, subject: str = "", date_header: str = "") -> dict:
    text = body or ""
    out: dict = {"source": "balance_summary"}
    missing: list[str] = []

    # Prefer body for accuracy; subject as fallback.
    bal_m = BODY_BALANCE_RE.search(text)
    sub_m = SUBJECT_RE.search(subject or "")
    if bal_m:
        balance_dollars = float(bal_m.group(1).replace(",", ""))
    elif sub_m:
        balance_dollars = float(sub_m.group(2).replace(",", ""))
    else:
        balance_dollars = None

    # Card label
    acct_m = ACCOUNT_LINE_RE.search(text)
    if acct_m:
        line = acct_m.group(1).strip()
        last4_m = LAST4_RE.search(line)
        last4 = last4_m.group(1) if last4_m else None
        # Strip the "(...NNNN)" suffix for the label
        label = LAST4_RE.sub("", line).strip()
    elif sub_m:
        label = sub_m.group(1).strip()
        last4 = None
    else:
        label = None
        last4 = None

    as_of = _parse_date_header(date_header)
    out["as_of_date"] = as_of
    if not as_of:
        missing.append("as_of_date")

    if balance_dollars is None or not label:
        out["account_balances"] = []
        missing.append("balance")
        out["summary"] = "Chase balance summary (parse failed)"
        out["parse_status"] = "fail"
        out["missing_fields"] = missing
        return out

    # Chase CC balance = liability → negative in our ledger model.
    cents = -int(round(balance_dollars * 100))
    out["account_balances"] = [{
        "account_label": label,
        "last4": last4 or "",
        "balance_cents": cents,
    }]

    out["summary"] = (
        f"Chase balance: {label} ${balance_dollars:,.2f} owed "
        f"(as of {as_of.isoformat() if as_of else '?'})"
    )
    out["parse_status"] = "ok" if not missing else "partial"
    out["missing_fields"] = missing
    return out
