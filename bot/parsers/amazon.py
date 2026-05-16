"""Parse Amazon order-confirmation emails into structured order dicts.

All functions are pure — string in, dict/value out. No I/O.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")

# Real Amazon emails use "Grand Total:" with the amount on the next line as
# "X.XX USD" (no leading $). We accept either format with permissive
# whitespace/newline between marker and amount. See parsing-knowledge.md
# for the empirical findings.
TOTAL_GRAND_RE = re.compile(
    r"Grand Total[:\s]+\$?([\d,]+\.\d{2})\s*(?:USD)?", re.I
)
TOTAL_ORDER_RE = re.compile(
    r"Order Total[:\s]+\$?([\d,]+\.\d{2})\s*(?:USD)?", re.I
)
TOTAL_FALLBACK_RE = re.compile(
    r"\bTotal[:\s]+\$?([\d,]+\.\d{2})\s*(?:USD)?", re.I
)


def extract_order_id(html: str) -> str | None:
    m = ORDER_ID_RE.search(html)
    return m.group(0) if m else None


def extract_total_cents(body: str) -> int | None:
    """Prefer 'Grand Total: X.XX USD' (current Amazon template),
    fall back to 'Order Total: $X.XX' (older), then any 'Total:'.
    """
    for regex in (TOTAL_GRAND_RE, TOTAL_ORDER_RE, TOTAL_FALLBACK_RE):
        m = regex.search(body)
        if m:
            dollars_str = m.group(1).replace(",", "")
            return int(round(float(dollars_str) * 100))
    return None


def parse_email_date_header(s: str) -> date | None:
    """Parse RFC 2822 date string (e.g. 'Fri, 8 May 2026 00:13:15 +0000') → date."""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


# Real Amazon order-confirmation plain-text bodies list items as lines starting
# with '* ' followed by the product title. Quantity and price appear on the
# next lines (indented). See parsing-knowledge.md for the fixture sample.


def extract_items(body: str) -> list[str]:
    """Items appear as plain-text lines starting with '* ' in Amazon emails.
    Multi-line continuations (Quantity/price/etc.) are not item titles — skip them.
    """
    items: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("* "):
            continue
        title = stripped[2:].strip()
        if not (5 <= len(title) <= 300):
            continue
        if title.lower().startswith(("http", "www.", "click")):
            continue
        if title in items:
            continue
        items.append(title)
    return items or ["(could not parse items)"]


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


def parse(body: str, *, subject: str = "", date_header: str = "",
          today: date | None = None) -> dict:
    """Top-level parser. Never raises — returns partial dict on failure.

    body: plain-text OR HTML message body. If HTML is detected (starts with '<'),
          it's converted to text first.
    subject: email subject line (currently unused but reserved for fallback parsing)
    date_header: RFC 2822 Date header from the email — used as order_date.
                 Falls back to `today` arg, then date.today() if not provided.
    """
    text = _html_to_text(body) if body.lstrip().startswith("<") else body
    order_id = extract_order_id(text)
    total_cents = extract_total_cents(text)
    items = extract_items(text)
    order_date = (parse_email_date_header(date_header)
                  or today or date.today())

    missing = [
        name for name, val in
        [("order_id", order_id), ("total_cents", total_cents)]
        if val is None
    ]
    if items == ["(could not parse items)"]:
        missing.append("items")
    return {
        "source": "amazon",
        "order_id": order_id,
        "total_cents": total_cents,
        "order_date": order_date,
        "items": items,
        "summary": f"{len(items)} item(s): " + ", ".join(items[:3])
                    + (f" (+{len(items)-3} more)" if len(items) > 3 else ""),
        "parse_status": "ok" if not missing else "partial",
        "missing_fields": missing,
    }
