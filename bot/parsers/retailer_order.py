"""Parse retailer order-confirmation emails (Shopify-templated mostly).

Most non-Amazon retailers Allison forwards (Rhoback, Urban Outfitters,
Zappos, piper+ivy, Larke, Enzo's, Etsy, Barnes & Noble, etc.) use the
Shopify "Order #NNNN ... Order summary ... Subtotal/Shipping/Total $X.XX
USD" plaintext template. Bookshop.org and Squarespace stores use very
similar layouts. This parser handles them with one set of regexes.

Pure function — string in, dict out. No I/O.

Contract matches the other parsers in this package:
    parse(body: str, *, subject: str, date_header: str) -> dict

Returned dict shape:
    {
      "source": "retailer_order",
      "merchant": "<best-guess merchant name>",
      "order_id": "<order # or receipt #>",
      "order_date": date,
      "total_cents": int,
      "summary": "<merchant: items summary string>",
      "parse_status": "ok" | "partial" | "fail",
      "missing_fields": [<field names that weren't extracted>],
    }
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

# Shopify-templated order id: "Order #200408", "Order #3744909", "Order
# Number: TP56977756", "Receipt #4014342219". Permissive to absorb the
# variant shapes from Urban Outfitters and Etsy too.
ORDER_ID_PATTERNS = [
    re.compile(r"Order\s*#\s*([A-Z0-9-]{3,})", re.I),
    re.compile(r"Order\s*Number[:\s]*([A-Z0-9-]{3,})", re.I),
    re.compile(r"Receipt\s*#\s*([A-Z0-9-]{3,})", re.I),
    re.compile(r"\bOrder\s+([A-Z][0-9]{5,})\b"),  # bookshop "Order R951138371"
]

# Total amount: "Total\n$XX.XX USD" or "Total $XX.XX USD".
# CRITICAL: require word boundary BEFORE "Total" (so "Subtotal" doesn't
# match) AND require the "USD" suffix (which Shopify only puts on the
# grand total, not the subtotal). Without both, the first match would be
# the subtotal and the parsed amount would miss tax + shipping.
TOTAL_RE = re.compile(
    r"(?<![A-Za-z])Total[\s\n]+\$?([\d,]+\.\d{2})\s+USD\b", re.I,
)
# Fallback for retailers that don't suffix USD (rare): take the LAST
# "Total" match, on the theory that Shopify lists Subtotal → ... → Total.
TOTAL_FALLBACK_RE = re.compile(
    r"(?<![A-Za-z])Total[\s\n]+\$?([\d,]+\.\d{2})", re.I,
)

# Merchant from Shopify template header (RHOBACK, Shoplarke, etc.).
# These appear right after the leading "Thank you for your purchase!" line.
SHOPIFY_HEADER_RE = re.compile(
    r"^Thank you for your purchase!\s*\n+([A-Za-z][A-Za-z0-9 &+-]{1,40})",
    re.MULTILINE,
)

# Item-section extraction: text between "Order summary" and "Subtotal".
ITEMS_SECTION_RE = re.compile(
    r"Order summary[\s\S]*?-+\n([\s\S]*?)\nSubtotal", re.I,
)

# Known retailer domain → cleaner merchant name. Fallback is the domain.
DOMAIN_TO_MERCHANT = {
    "rhoback.com": "Rhoback",
    "shoplarke.com": "Larke",
    "lovemyenzos.com": "Enzos",
    "piperandivy.com": "Piper+Ivy",
    "urbanoutfitters.com": "Urban Outfitters",
    "st.urbanoutfitters.com": "Urban Outfitters",
    "zappos.com": "Zappos",
    "etsy.com": "Etsy",
    "bookshop.org": "Bookshop.org",
    "barnesandnoble.com": "Barnes & Noble",
    "t.barnesandnoble.com": "Barnes & Noble",
    "squarespace.info": None,  # use header instead — multi-tenant
}


def parse_email_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def _merchant_from_sender(sender: str) -> str | None:
    """Pull the domain off a 'support@rhoback.com' → 'Rhoback' lookup."""
    if not sender or "@" not in sender:
        return None
    domain = sender.split("@", 1)[1].strip().lower()
    if domain in DOMAIN_TO_MERCHANT:
        return DOMAIN_TO_MERCHANT[domain]
    # strip mailserver subdomain prefixes (mail.X, email.X, no-reply.X)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in DOMAIN_TO_MERCHANT:
            return DOMAIN_TO_MERCHANT[candidate]
    # Last resort: best-effort capitalization of root domain
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return None


def _extract_order_id(text: str) -> str | None:
    for pat in ORDER_ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _extract_total_cents(text: str) -> int | None:
    # Try the strict (USD-suffixed) match first.
    m = TOTAL_RE.search(text)
    if m:
        return int(round(float(m.group(1).replace(",", "")) * 100))
    # Fallback: take the LAST plain "Total" match. Shopify lists
    # Subtotal first, then Total, so the last occurrence is the
    # grand total. Empty list means no match at all.
    matches = list(TOTAL_FALLBACK_RE.finditer(text))
    if matches:
        return int(round(float(matches[-1].group(1).replace(",", "")) * 100))
    return None


def _extract_items_summary(text: str, *, max_chars: int = 200) -> str:
    """Pull the items block between 'Order summary' and 'Subtotal',
    collapsing whitespace and truncating to max_chars.

    If the section isn't found, returns "" — the caller will fall back
    to subject + order_id as the summary.
    """
    m = ITEMS_SECTION_RE.search(text)
    if not m:
        return ""
    block = m.group(1)
    # Drop empty lines and "Variant:" / size-only lines aren't worth
    # filtering — collapse whitespace and let the LLM see them.
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    # Filter out pure price lines ($XX.XX), shipping subtotals, etc.
    items = []
    for ln in lines:
        if re.fullmatch(r"\$?[\d,]+\.\d{2}", ln):
            continue  # bare price
        items.append(ln)
    joined = "; ".join(items)
    if len(joined) > max_chars:
        joined = joined[: max_chars - 1].rstrip() + "…"
    return joined


def parse(
    body: str, *, subject: str = "", date_header: str = "",
    sender: str = "",
) -> dict:
    """Returns the parsed dict; parse_status reflects coverage.

    The ``sender`` kwarg is optional but recommended — when provided, the
    merchant is recovered from the domain even if the Shopify header line
    is missing. ``bot/gmail_watcher._extract_body`` doesn't pass sender
    today; callers that have it can plumb it through later.
    """
    text = body or ""
    out: dict = {"source": "retailer_order"}
    missing: list[str] = []

    # Merchant
    merchant = None
    m = SHOPIFY_HEADER_RE.search(text)
    if m:
        merchant = m.group(1).strip()
    if not merchant:
        merchant = _merchant_from_sender(sender) or "Retailer"
    out["merchant"] = merchant
    if merchant == "Retailer":
        missing.append("merchant")

    # Order id
    order_id = _extract_order_id(text) or _extract_order_id(subject) or ""
    out["order_id"] = order_id
    if not order_id:
        missing.append("order_id")

    # Total
    total = _extract_total_cents(text)
    out["total_cents"] = total
    if total is None:
        missing.append("total")

    # Date
    d = parse_email_date_header(date_header)
    out["order_date"] = d
    if d is None:
        missing.append("order_date")

    # Items summary
    items = _extract_items_summary(text)
    summary_parts = [merchant]
    if items:
        summary_parts.append(items)
    elif subject:
        summary_parts.append(subject)
    out["summary"] = ": ".join(summary_parts)

    if missing == ["merchant"]:
        # merchant was a soft miss but everything else is there
        out["parse_status"] = "ok" if total else "partial"
    elif not missing:
        out["parse_status"] = "ok"
    elif total and d:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"

    out["missing_fields"] = missing
    return out
