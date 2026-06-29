"""Parse Citi credit-card "daily account balance alert" emails.

Subject: "Your daily account balance alert"
Sender: alerts@info6.citi.com

After HTML stripping, the relevant fragment looks like:

    ... Account ending in 5674 Your account balance as of June 6, 2026
        is $2,490.99 You're receiving this email...

Citi reports the CC balance as a positive dollar amount that represents
the OUTSTANDING DEBT. To match the chase_balance_summary and YNAB ledger
convention (CC accounts carry a negative balance), we emit a NEGATIVE
balance_cents.

Returned dict shape (the balance-summary contract used by
``bot.ingest._ingest_balance``):

    {
      "source": "balance_summary",
      "as_of_date": date,            # parsed from the email body "as of …"
      "account_balances": [
        {"account_label": "Citi Double Cash",
         "last4": "5674",
         "balance_cents": -249099},
      ],
      "parse_status": "ok" | "partial" | "fail",
      "missing_fields": [...],
    }
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

# "Account ending in 5674"
LAST4_RE = re.compile(r"Account\s+ending\s+in\s+(\d{4})", re.I)

# "Your account balance as of June 6, 2026 is $2,490.99"
BALANCE_RE = re.compile(
    r"account\s+balance\s+as\s+of\s+"
    r"([A-Za-z]+\s+\d{1,2},\s*\d{4})"
    r"\s+is\s+\$\s*([\d,]+\.\d{2})",
    re.I,
)


def _parse_as_of(s: str) -> date | None:
    # "June 6, 2026"
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").date()
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
    out: dict = {"source": "balance_summary"}
    missing: list[str] = []

    bal_m = BALANCE_RE.search(text)
    last4_m = LAST4_RE.search(text)

    last4 = last4_m.group(1) if last4_m else None

    as_of: date | None = None
    balance_cents: int | None = None
    if bal_m:
        as_of = _parse_as_of(bal_m.group(1))
        balance_dollars = float(bal_m.group(2).replace(",", ""))
        balance_cents = -int(round(balance_dollars * 100))
    if as_of is None:
        as_of = _parse_date_header(date_header)

    out["as_of_date"] = as_of
    if as_of is None:
        missing.append("as_of_date")
    if balance_cents is None:
        missing.append("balance")
    if not last4:
        missing.append("last4")

    if balance_cents is None or not last4:
        out["account_balances"] = []
        out["summary"] = "Citi balance summary (parse failed)"
        out["parse_status"] = "fail"
        out["missing_fields"] = missing
        return out

    out["account_balances"] = [{
        "account_label": "Citi Double Cash",
        "last4": last4,
        "balance_cents": balance_cents,
    }]
    out["summary"] = (
        f"Citi balance: Double Cash ${-balance_cents/100:,.2f} owed "
        f"(as of {as_of.isoformat() if as_of else '?'})"
    )
    out["parse_status"] = "ok" if not missing else "partial"
    out["missing_fields"] = missing
    return out
