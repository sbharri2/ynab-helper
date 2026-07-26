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


def _order_id_positions(text: str) -> list[tuple[str, int]]:
    """First-occurrence offset of each DISTINCT order id, in document order.

    Deduped because Amazon repeats the order number in tracking links and
    "view or edit order" URLs — a repeat is not a second order.
    """
    seen: dict[str, int] = {}
    for m in ORDER_ID_RE.finditer(text):
        seen.setdefault(m.group(0), m.start())
    return list(seen.items())


def extract_additional_orders(text: str) -> list[dict]:
    """Orders 2..N of a multi-order confirmation email.

    Amazon groups one checkout into several order numbers and sends a
    single "Ordered:" email with a block per order:

        Order # / <id> / <items> / Grand Total: / $<amount>

    Each block is delimited by the next order id, so we slice on those
    offsets and re-run the single-order extractors per slice. The FIRST
    order is deliberately not returned here — `parse()` keeps reading it
    from the whole body exactly as before, so single-order emails and the
    primary fields of multi-order emails are bit-for-bit unchanged.

    Observed live 2026-07-04: a $278.82 order rode along with a $139.41
    one and was silently dropped, leaving an unmatchable card charge.
    """
    positions = _order_id_positions(text)
    if len(positions) < 2:
        return []

    extras: list[dict] = []
    for idx in range(1, len(positions)):
        order_id, start = positions[idx]
        end = positions[idx + 1][1] if idx + 1 < len(positions) else len(text)
        segment = text[start:end]

        total_cents = extract_total_cents(segment)
        items = extract_items(segment)
        if items == ["(could not parse items)"]:
            items = []
        # A block with neither a total nor items isn't an order — it's a
        # stray id in boilerplate. Dropping it keeps phantom rows out of
        # the queue.
        if total_cents is None and not items:
            continue

        extras.append({
            "order_id": order_id,
            "total_cents": total_cents,
            "items": items,
            "summary": _summarize(items),
        })
    return extras


def _summarize(items: list[str]) -> str:
    return (f"{len(items)} item(s): " + ", ".join(items[:3])
            + (f" (+{len(items) - 3} more)" if len(items) > 3 else ""))


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

    When no `* ` bullets are found (some templates / HTML-stripped bodies
    don't have them), fall back to scanning between known section markers
    for product-looking text. The fallback is conservative: short headers
    ("Hi Steven,"), shipping addresses, and price-only lines are skipped.
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
    if items:
        return items

    # Fallback: pull lines from between "Order Details" / "Items Ordered"
    # / "Your order" markers and "Subtotal" / "Order Total" / "Shipping
    # Address". Useful when Amazon's HTML template drops the * bullets.
    fallback = _extract_items_between_markers(body)
    return fallback or ["(could not parse items)"]


_ITEM_SECTION_START_RE = re.compile(
    r"(?:Order Details|Items Ordered|Your order|Order summary)",
    re.I,
)
_ITEM_SECTION_END_RE = re.compile(
    r"(?:Subtotal|Order Total|Grand Total|Shipping Address|Items shipped)",
    re.I,
)
_PRICE_ONLY_RE = re.compile(r"^\$?[\d,]+\.\d{2}$")
_QTY_PRICE_RE = re.compile(r"^(?:Quantity|Sold by|Condition|Item subtotal)", re.I)

# Amazon's current template puts an item title on its own line, then
# "Quantity: N" on the next. Walk lines backwards from each "Quantity:"
# marker and grab the nearest non-trivial title.
_QUANTITY_LINE_RE = re.compile(r"^\s*Quantity\s*[:.]?\s*\d", re.I)
_NAV_NOISE = {
    "your orders", "your account", "buy again", "thanks for your order!",
    "ordered", "shipped", "out for delivery", "delivered", "amazon.com",
    "view or edit order", "view your order", "track package", "view all orders",
    "track your package",
}


def _extract_items_between_markers(body: str) -> list[str]:
    """Best-effort body-side item extraction.

    Strategy 1 (preferred): find lines immediately *before* each
    "Quantity:" line — Amazon's current template puts the item title
    there. Skips nav noise ("Your Account", "Buy Again", etc.).

    Strategy 2 (fallback): legacy "between Order Details and Subtotal"
    section scan. Kept for older templates.
    """
    lines = body.splitlines()
    out: list[str] = []

    # Strategy 1: walk backwards from each Quantity: line
    for i, ln in enumerate(lines):
        if not _QUANTITY_LINE_RE.match(ln):
            continue
        # Walk backwards looking for the item title line
        for back in range(1, 6):
            j = i - back
            if j < 0:
                break
            cand = lines[j].strip()
            if not cand or len(cand) < 5 or len(cand) > 300:
                continue
            if cand.lower() in _NAV_NOISE:
                continue
            if cand.lower().startswith((
                "http", "www.", "click", "view", "track",
                "order #", "$", "arriving", "steven -", "allison -",
            )):
                continue
            if _PRICE_ONLY_RE.match(cand):
                continue
            if cand in out:
                break
            out.append(cand)
            break
        if len(out) >= 5:
            break
    if out:
        return out

    # Strategy 2: legacy section scan
    start_idx = end_idx = None
    for i, ln in enumerate(lines):
        if start_idx is None and _ITEM_SECTION_START_RE.search(ln):
            start_idx = i + 1
            continue
        if start_idx is not None and _ITEM_SECTION_END_RE.search(ln):
            end_idx = i
            break
    if start_idx is None:
        return []
    if end_idx is None:
        end_idx = min(start_idx + 30, len(lines))

    for ln in lines[start_idx:end_idx]:
        s = ln.strip()
        if not s or len(s) < 5 or len(s) > 300:
            continue
        if s.lower() in _NAV_NOISE:
            continue
        if s.lower().startswith(("http", "www.", "click", "view", "track")):
            continue
        if _PRICE_ONLY_RE.match(s):
            continue
        if _QTY_PRICE_RE.match(s):
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= 5:
            break
    return out


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


_ORDERED_SUBJECT_RE = re.compile(r"^Ordered:\s*(.+)$", re.I)


_BIDI_RE = re.compile(r"[⁦-⁩‪-‮ - ]")


def _items_from_subject(subject: str) -> list[str] | None:
    """Amazon's current notification subjects say what was ordered:

        "Ordered: \"Amazon Echo Pop (newest...)\" and 2 more items"
        "Ordered: \"Lodge Cast Iron Skillet 12\\\"\""

    When present, this is the best item description we'll get without a
    structured parse of the body. Returns a single-item list with the
    cleaned summary string, or None if subject doesn't match.

    Strips Amazon's invisible Unicode bidi controls (U+2066–U+2069 etc.)
    that surround quantity numbers — they read as garbage like
    "and ⁦2⁩ more items" if you keep them.
    """
    if not subject:
        return None
    m = _ORDERED_SUBJECT_RE.match(subject.strip())
    if not m:
        return None
    rest = _BIDI_RE.sub("", m.group(1)).strip()
    # Collapse runs of whitespace introduced by the bidi strip
    rest = re.sub(r"\s+", " ", rest)
    # Strip an outer pair of quotes if Amazon wrapped a single item in them
    if rest.startswith('"') and rest.endswith('"'):
        rest = rest[1:-1]
    return [rest] if rest else None


def parse(body: str, *, subject: str = "", date_header: str = "",
          today: date | None = None) -> dict:
    """Top-level parser. Never raises — returns partial dict on failure.

    body: plain-text OR HTML message body. If HTML is detected (starts with '<'),
          it's converted to text first.
    subject: email subject line. Amazon's current template puts the item
             list in here ("Ordered: ... and N more items"), so we use it
             as the primary source when present.
    date_header: RFC 2822 Date header from the email — used as order_date.
                 Falls back to `today` arg, then date.today() if not provided.
    """
    text = _html_to_text(body) if body.lstrip().startswith("<") else body
    order_id = extract_order_id(text)
    total_cents = extract_total_cents(text)

    # Prefer subject-derived items (Amazon's current template). Fall back
    # to body-based "* " bullets, then the section-marker heuristic.
    items = _items_from_subject(subject) or extract_items(text)

    order_date = (parse_email_date_header(date_header)
                  or today or date.today())

    missing = [
        name for name, val in
        [("order_id", order_id), ("total_cents", total_cents)]
        if val is None
    ]
    if items == ["(could not parse items)"]:
        missing.append("items")
    # Status grading:
    #   ok      — everything found
    #   partial — minor field missing (e.g. order_id) but total + items present
    #   fail    — no total AND no items; it's almost certainly not an order
    #             confirmation (refund, shipment update, address change, etc.)
    #             and shouldn't pollute the user's queue.
    if not missing:
        status = "ok"
    elif total_cents is None and items == ["(could not parse items)"]:
        status = "fail"
    else:
        status = "partial"
    return {
        "source": "amazon",
        "order_id": order_id,
        "total_cents": total_cents,
        "order_date": order_date,
        "items": items,
        "summary": _summarize(items),
        # Orders 2..N when Amazon packs several into one email. Always
        # present (empty for the common single-order case) so callers can
        # fan out unconditionally.
        "additional_orders": extract_additional_orders(text),
        "parse_status": status,
        "missing_fields": missing,
    }
