"""Parse Amazon order-confirmation emails into structured order dicts.

All functions are pure — string in, dict/value out. No I/O.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

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
