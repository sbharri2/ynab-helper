"""Parse Amazon shipment-tracking emails (from shipment-tracking@amazon.com).

These are the "Shipped: ..." emails that arrive when Amazon charges your
card for an individual package - one per shipment. Unlike order-confirmation
emails (auto-confirm@amazon.com), the per-shipment Total here matches the
per-shipment YNAB charge exactly, which is what the matcher actually needs.

Body shape (verified against real emails):

    Order #
    112-1777079-2635414
    ...
    * <item name>
      Quantity: <N>
      <unit_price> USD
    Total
    <shipment_total> USD

All functions are pure - string in, dict/value out. No I/O.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

ORDER_ID_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")

ITEM_RE = re.compile(
    r"\*\s+(.+?)\n\s+Quantity:\s+(\d+)\s*\n\s+([\d.]+)\s+USD",
    re.DOTALL,
)

# "Total\n  <amount> USD" - the per-shipment total, distinct from any
# per-item line. Anchored on its own line to avoid catching item subtotals.
TOTAL_RE = re.compile(r"\bTotal\s*\n\s*([\d,]+\.\d+)\s+USD", re.I)


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def parse(
    body: str,
    *,
    subject: str = "",
    date_header: str = "",
    today: date | None = None,
) -> dict:
    """Parse a shipment-tracking email body. Returns the same dict shape as
    bot.parsers.amazon.parse so the rest of the pipeline can treat both
    uniformly."""
    order_id_match = ORDER_ID_RE.search(body)
    order_id = order_id_match.group(1) if order_id_match else None

    items_raw = ITEM_RE.findall(body)
    items: list[str] = []
    for name, qty, _price in items_raw:
        name = name.strip()
        # Item names in these emails sometimes have trailing inch-marks
        # (6.3") that look like an unterminated quote; trim trailing
        # punctuation/whitespace for readability.
        items.append(f"{qty}x {name}" if int(qty) > 1 else name)

    if not items:
        items = ["(could not parse items)"]

    total_match = TOTAL_RE.search(body)
    total_cents: int | None
    if total_match:
        total_cents = int(round(float(total_match.group(1).replace(",", "")) * 100))
    else:
        total_cents = None

    # Shipment-tracking emails carry the shipment date, which is what
    # Amazon charges your card on - so it's the right thing to feed the
    # matcher against the YNAB txn_date.
    order_date = _parse_date_header(date_header) or today or date.today()

    missing = [field for field, val in
               [("order_id", order_id), ("total_cents", total_cents)]
               if val is None]
    if items == ["(could not parse items)"]:
        missing.append("items")

    summary = (f"{len(items)} item(s): " + ", ".join(items[:3])
               + (f" (+{len(items) - 3} more)" if len(items) > 3 else ""))

    return {
        "source": "amazon",  # Same source label - matcher treats it identically
        "order_id": order_id,
        "total_cents": total_cents,
        "order_date": order_date,
        "items": items,
        "summary": summary,
        "parse_status": "ok" if not missing else "partial",
        "missing_fields": missing,
    }
