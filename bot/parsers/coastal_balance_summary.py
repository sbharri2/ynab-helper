"""Parse Coastal Federal Credit Union daily balance summary emails.

Subject: "Balance Summary Alert"

Body (plaintext):

    Dear STEVEN HARRIS JR,

    The following is a summary of your account balances :
     SPECIAL SAVINGS *649-S0100 $5.00
     JOINT CHECKING *964-S0010 $1,234.56


    Coastal Federal Credit Union
    800.868.4262

The balance line is `<NAME> *<suffix>-<sub> <amount>`. The suffix-block
(`*649-S0100`) encodes the membership/account number; the trailing
3-or-4-digit before the dash is what banks usually call the "account
last4" for matching.

Returns the `balance_summary` shape that `bot.ingest._ingest_balance`
expects:

    {
      "source": "balance_summary",
      "as_of_date": date,           # from the email header
      "account_balances": [
        {"account_label": str, "last4": str, "balance_cents": int}, ...
      ],
      "parse_status": ...,
      ...
    }
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

# Pattern: "<NAME> *<NNN>-S<NNNN> $<amount>"
# Examples we've seen:
#   SPECIAL SAVINGS *649-S0100 $5.00
#   JOINT CHECKING *964-S0010 $1,234.56
BALANCE_LINE_RE = re.compile(
    r"^\s*([A-Z][A-Z\s/]+?)\s*\*\s*(\d{3,4})-?S?(\d{0,4})\s*\$\s*([\d,]+\.\d{2})\s*$",
    re.M,
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

    balances: list[dict] = []
    for m in BALANCE_LINE_RE.finditer(text):
        label = m.group(1).strip()
        primary_num = m.group(2)  # the *NNN part
        sub_num = m.group(3) or ""  # the SNNNN part
        amount_dollars = float(m.group(4).replace(",", ""))
        # For matching against our local account table, use the leading
        # 3-or-4-digit "primary_num". Local accounts have `last4` populated
        # from setup; if the user encoded a different convention we may need
        # to widen this later.
        balances.append({
            "account_label": label,
            "last4": primary_num,
            "sub_account": sub_num,
            "balance_cents": int(round(amount_dollars * 100)),
        })

    out["account_balances"] = balances
    if not balances:
        missing.append("balances")

    out["as_of_date"] = _parse_date_header(date_header)
    if not out["as_of_date"]:
        missing.append("as_of_date")

    n = len(balances)
    if n:
        sample = balances[0]
        out["summary"] = (
            f"Coastal balance summary: {n} account(s); "
            f"e.g. {sample['account_label']} ${sample['balance_cents']/100:,.2f}"
        )
    else:
        out["summary"] = "Coastal balance summary (no balances parsed)"

    if not missing:
        out["parse_status"] = "ok"
    elif balances:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
