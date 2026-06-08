"""Parse PayPal payment-receipt emails (`service@paypal.com`).

PayPal uses two visible templates (both seen 2026):

  Template A — "Your payment to MERCHANT has been processed"
  -----------------------------------------------------------
    Subject: Your payment to Uber Technologies, I... has been processed
    Body:
      You sent a payment of $14.25 USD on March 28, 2026 4:58:49 PM PDT to
        Uber Technologies, I... (paypal-us@uber.com).
      Payment Details
      Merchant: Uber Technologies, I...
      Date: March 28, 2026 4:58:49 PM PDT
      Transaction ID: 04N264196P959211H
      Authorization Amount: $14.25 USD
      Payment Amount: $14.25 USD
      Payment By: sbharri2@gmail.com
      Funding Sources Used (Total)
      COASTAL FEDERAL CREDIT UNION x-9649: $14.25 USD

  Template B — "MERCHANT: $X.XX USD"
  -----------------------------------
    Subject: Uber Technologies, I...: $5.00 USD
    Body:
      You paid $5.00 USD to Uber Technologies, I...
      Merchant: Uber Technologies, I...
      Transaction date: Apr 2, 2026
      Order ID: 6dF20MPDPNqfbvb2Gmwqhja0
      Subtotal: $5.00
      Total: $5.00 USD
      Paid Uber Technologies, I... with
      COASTAL FEDERAL CREDIT UNION
      Checking ••9649: $5.00 USD

Refund template:

    Subject: Your refund from MERCHANT is on the way
    Body: "You'll receive a refund of $X.XX USD from MERCHANT..."

Returned dict shape (matches citi_alert / chase_alert for ingest):

    {
      "source": "paypal_payment",
      "merchant": str,
      "amount_cents": int,    # NEGATIVE for outflows, POSITIVE for refunds
      "account_last4": str | None,  # funding source last4 (Coastal "9649")
      "posted_date": date,
      "summary": str,
      "parse_status": "ok" | "partial" | "fail",
      "missing_fields": [...],
    }
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

# Subject patterns
_SUBJECT_PROCESSED_RE = re.compile(
    r"Your payment to\s+(.+?)\s+has been processed", re.I,
)
_SUBJECT_AMOUNT_RE = re.compile(
    r"^(.+?):\s*\$\s*([\d,]+\.\d{2})\s*USD\s*$", re.I,
)
_SUBJECT_REFUND_RE = re.compile(
    r"Your refund from\s+(.+?)\s+is on", re.I,
)

# Body amount patterns
_BODY_SENT_AMOUNT_RE = re.compile(
    r"You sent a payment of\s+\$\s*([\d,]+\.\d{2})\s*USD", re.I,
)
_BODY_PAID_AMOUNT_RE = re.compile(
    r"You paid\s+\$\s*([\d,]+\.\d{2})\s*USD", re.I,
)
_BODY_REFUND_AMOUNT_RE = re.compile(
    r"refund of\s+\$\s*([\d,]+\.\d{2})\s*USD", re.I,
)
_BODY_PAYMENT_AMOUNT_RE = re.compile(
    r"Payment Amount[:\s]*\$\s*([\d,]+\.\d{2})", re.I,
)
_BODY_TOTAL_RE = re.compile(
    r"\bTotal[:\s\n]+\$\s*([\d,]+\.\d{2})\s*USD", re.I,
)

# Body merchant patterns
_BODY_MERCHANT_LABEL_RE = re.compile(
    r"\bMerchant[:\s]*\n?\s*([^\n]+?)\s*\n", re.I,
)

# Body date patterns
_BODY_TXN_DATE_RE = re.compile(
    r"(?:Transaction date|Date)[:\s]*\n?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.I,
)

# Funding source — both templates' last4
_FUNDING_LAST4_RE = re.compile(
    r"(?:x-|••)\s*(\d{3,4})", re.I,
)


def _parse_date_header(s: str) -> date | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def _parse_body_date(text: str) -> date | None:
    m = _BODY_TXN_DATE_RE.search(text)
    if not m:
        return None
    # Try a few formats — PayPal varies between "March 28, 2026" and "Apr 2, 2026"
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except ValueError:
            continue
    return None


def parse(body: str, *, subject: str = "", date_header: str = "") -> dict:
    text = body or ""
    out: dict = {"source": "paypal_payment"}
    missing: list[str] = []

    # ---- detect refund vs payment ----
    is_refund = bool(_SUBJECT_REFUND_RE.search(subject or ""))
    if not is_refund and _BODY_REFUND_AMOUNT_RE.search(text):
        is_refund = True

    # ---- merchant ----
    merchant: str | None = None
    if not is_refund:
        m = _SUBJECT_PROCESSED_RE.search(subject or "")
        if m:
            merchant = m.group(1).strip()
        else:
            m = _SUBJECT_AMOUNT_RE.match((subject or "").strip())
            if m:
                merchant = m.group(1).strip()
    else:
        m = _SUBJECT_REFUND_RE.search(subject or "")
        if m:
            merchant = m.group(1).strip()
    if not merchant:
        m = _BODY_MERCHANT_LABEL_RE.search(text)
        if m:
            merchant = m.group(1).strip()
    out["merchant"] = merchant
    out["payee"] = (merchant or "PayPal payment")[:120]
    if not merchant:
        missing.append("merchant")

    # ---- amount ----
    amount_cents: int | None = None
    for regex in (
        _BODY_SENT_AMOUNT_RE, _BODY_PAID_AMOUNT_RE, _BODY_REFUND_AMOUNT_RE,
        _BODY_PAYMENT_AMOUNT_RE, _BODY_TOTAL_RE,
    ):
        m = regex.search(text)
        if m:
            amount_cents = int(round(
                float(m.group(1).replace(",", "")) * 100
            ))
            break
    if amount_cents is None:
        # Subject fallback ("MERCHANT: $X.XX USD")
        m = _SUBJECT_AMOUNT_RE.match((subject or "").strip())
        if m:
            amount_cents = int(round(
                float(m.group(2).replace(",", "")) * 100
            ))
    if amount_cents is None:
        out["amount_cents"] = None
        missing.append("amount")
    else:
        # Sign: refunds positive (inflow), payments negative (outflow)
        out["amount_cents"] = (
            abs(amount_cents) if is_refund else -abs(amount_cents)
        )

    # ---- funding source last4 ----
    m = _FUNDING_LAST4_RE.search(text)
    out["account_last4"] = m.group(1) if m else None
    # last4 is optional — fallthrough to no resolution rather than fail

    # ---- date ----
    posted = _parse_body_date(text) or _parse_date_header(date_header)
    out["posted_date"] = posted
    if not posted:
        missing.append("posted_date")

    # ---- summary ----
    sign = "+" if is_refund else "-"
    amt_str = (
        f"{sign}${abs(out['amount_cents'] or 0) / 100:.2f}"
        if out.get("amount_cents") is not None else "$?"
    )
    direction = "refund from" if is_refund else "payment to"
    out["summary"] = (
        f"PayPal {amt_str} {direction} {merchant or '(unknown merchant)'} "
        f"on {posted.isoformat() if posted else '(unknown date)'}"
    )

    # ---- status ----
    if not missing:
        out["parse_status"] = "ok"
    elif out["amount_cents"] is not None and posted:
        out["parse_status"] = "partial"
    else:
        out["parse_status"] = "fail"
    out["missing_fields"] = missing
    return out
