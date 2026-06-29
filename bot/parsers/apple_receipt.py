"""Parse Apple receipt emails ("Your receipt from Apple.") into pending_order dicts.

Apple is the Family Sharing organizer pattern — all family receipts (Steven's,
Allison's, Kimberly's Apple IDs) arrive in sbharri2@gmail.com because Steven is
the organizer. So a single Gmail scrape catches everything.

Why this parser exists: Citi/Chase alerts for Apple charges only say
"APPLE.COM/BILL CUPERTINO USA $5.99" — opaque. The receipt email contains the
actual line items (e.g. "NYT Games (Wordle)", "Apple One Family", "Surfshark
VPN"). The matcher (bot/matcher.py) pairs the receipt's pending_order with the
card-side ledger_txn so the categorizer prompt sees the rich text instead of
just "APPLE.COM/BILL".

All functions are pure — string in, dict/value out. No I/O.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

ORDER_ID_RE = re.compile(r"Order\s+ID\s*[:.]?\s*([A-Z0-9]{6,12})", re.I)
APPLE_ACCOUNT_RE = re.compile(
    r"Apple\s+Account\s*[:.]?\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
    re.I,
)
# The "Billing & Payment" footer line is always preceded by the card total in
# the form: `MasterCard •••• 5674 $5.99` or `Visa •••• 1234 $12.34`. We grab
# the LAST such line in the body because that's always the grand total (some
# receipts have a per-item card line above the totals block; we want the bottom
# one).
CARD_TOTAL_RE = re.compile(
    r"(MasterCard|Visa|American\s+Express|Discover|Amex)\s*"
    r"[•·\*]{2,}\s*(\d{4})\s+\$([\d,]+\.\d{2})",
    re.I,
)
# Fallback total: Subtotal + Tax, useful when the card line was stripped.
SUBTOTAL_RE = re.compile(r"Subtotal\s+\$([\d,]+\.\d{2})", re.I)
TAX_RE = re.compile(r"^\s*Tax\s+\$([\d,]+\.\d{2})", re.I | re.M)

# Each item in an Apple receipt is recognizable by a parenthesized billing-
# period marker at the end of one of its lines: "(Monthly)", "(Annual)" etc.
# That marker is on the *tier* line; the product title is on the line ABOVE.
# Empirically — see scripts/_dump_apple_lines.py — the structure is:
#
#   Apple One                            ← product title (line above marker)
#   Family (Monthly)                     ← tier + marker
#   Renews July 7, 2026
#   Stevens Iphone
#   $25.95
#
# When the marker line stands alone (first item after promo footer), we use
# the marker prefix itself as the title (e.g. "iCloud+ with 200 GB (Monthly)"
# → "iCloud+ with 200 GB").
ITEM_LINE_RE = re.compile(
    r"^(.+?)\s*\((Monthly|Annual|Weekly|Yearly|Biweekly)\)\s*$",
    re.I,
)
# Lines that should never be treated as a product title — promo footnotes,
# section headers, lone footnote digits, price lines, device labels, the
# "Apple Account:" header itself, etc.
_FILLER_RE = re.compile(
    r"^(?:"
    r"\d+|›|\||"                            # footnote digits, arrows, pipes
    r"\$[\d,]+\.\d{2}|"                     # price lines
    r"receipt$|order\s+id|document|"        # receipt header
    r"apple\s+account|"                     # account header
    r"save\s+\d+%|apply\s+and\s+use|"       # Apple Card promo
    r"renews\s|"                            # "Renews June 28..."
    r"billing\s+and\s+payment|"             # footer start
    r"subtotal|tax|total"                   # totals block
    r")",
    re.I,
)
# Item section bounds — between the Apple Account line and the Billing footer.
ITEM_START_RE = re.compile(r"Apple\s+Account\s*[:.]", re.I)
ITEM_END_RE = re.compile(r"Billing\s+and\s+Payment", re.I)


def _parse_email_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def extract_order_id(body: str) -> str | None:
    m = ORDER_ID_RE.search(body)
    return m.group(1) if m else None


def extract_apple_account(body: str) -> str | None:
    m = APPLE_ACCOUNT_RE.search(body)
    return m.group(1).lower() if m else None


def extract_total_cents(body: str) -> tuple[int | None, str | None]:
    """Return (grand_total_cents, card_last4).

    Prefer the final `MasterCard •••• 5674 $X.XX` line — that's the grand
    total post-tax. Fall back to Subtotal+Tax if the card line is absent.
    """
    matches = list(CARD_TOTAL_RE.finditer(body))
    if matches:
        m = matches[-1]
        amount = int(round(float(m.group(3).replace(",", "")) * 100))
        return amount, m.group(2)
    sub = SUBTOTAL_RE.search(body)
    if sub:
        sub_cents = int(round(float(sub.group(1).replace(",", "")) * 100))
        tax = TAX_RE.search(body)
        tax_cents = (
            int(round(float(tax.group(1).replace(",", "")) * 100)) if tax else 0
        )
        return sub_cents + tax_cents, None
    return None, None


def _is_filler_line(s: str) -> bool:
    s = s.strip()
    if not s or len(s) < 3:
        return True
    return bool(_FILLER_RE.match(s))


def extract_items(body: str) -> list[str]:
    """One title per (Monthly|Annual|...) marker line. Prefer the line ABOVE
    the marker — that's where Apple puts the product name. Fall back to the
    marker line's prefix when the line above is a promo/section filler."""
    start = ITEM_START_RE.search(body)
    end = ITEM_END_RE.search(body)
    if not start or not end or end.start() <= start.end():
        section = body
        offset = 0
    else:
        section = body[start.end():end.start()]
        offset = start.end()

    lines = section.splitlines()
    items: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = ITEM_LINE_RE.match(line.strip())
        if not m:
            continue
        marker_prefix = m.group(1).strip()
        # Apple sometimes bakes "- Monthly" / "- Annual" into the title before
        # the parenthesized marker ("Wordle & Crossword Games - Monthly
        # (Monthly)"). Trim that trailing redundancy.
        marker_prefix = re.sub(r"\s*-\s*(?:Monthly|Annual|Weekly|Yearly)\s*$",
                                "", marker_prefix, flags=re.I)

        # Prefer the previous content line as the product title; fall back
        # to the marker prefix when nothing above qualifies.
        title: str | None = None
        if i > 0 and not _is_filler_line(lines[i - 1]):
            title = lines[i - 1].strip()
        if not title and marker_prefix:
            title = marker_prefix

        if not title or title in seen:
            continue
        if not (3 <= len(title) <= 200):
            continue
        seen.add(title)
        items.append(title)
    return items


def _account_short_label(apple_account: str | None) -> str | None:
    """Map the Apple Account email to a one-word family label for the summary.
    Returns None when unmapped — caller can fall back to the bare email.
    """
    if not apple_account:
        return None
    a = apple_account.lower()
    # These mappings are derived from sample receipts. Keep narrow — adding
    # other family members here is one line each, and the bare email is the
    # safe fallback when missing.
    if "sbharri" in a:
        return "Steven"
    if "allison" in a:
        return "Allison"
    if "kimberly" in a:
        return "Kimberly"
    return None


def parse(body: str, *, subject: str = "", date_header: str = "",
          today: date | None = None) -> dict:
    """Top-level parser. Never raises — returns partial dict on failure."""
    order_id = extract_order_id(body)
    total_cents, card_last4 = extract_total_cents(body)
    apple_account = extract_apple_account(body)
    items = extract_items(body)
    order_date = (_parse_email_date_header(date_header)
                  or today or date.today())

    who = _account_short_label(apple_account) or apple_account or ""
    if items:
        first = items[0]
        more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
        summary = f"Apple: {first}{more}" + (f" — {who}" if who else "")
    else:
        summary = f"Apple charge" + (f" — {who}" if who else "")

    missing = [
        name for name, val in
        [("order_id", order_id), ("total_cents", total_cents)]
        if val is None
    ]
    if not items:
        missing.append("items")

    # Grading: same convention as amazon.py
    if not missing:
        status = "ok"
    elif total_cents is None and not items:
        status = "fail"
    else:
        status = "partial"

    return {
        "source": "apple",
        "order_id": order_id,
        "total_cents": total_cents,
        "order_date": order_date,
        "items": items,
        "summary": summary,
        "apple_account": apple_account,
        "card_last4": card_last4,
        "parse_status": status,
        "missing_fields": missing,
    }
