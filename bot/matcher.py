"""Pure scoring function for matching YNAB transactions to pending orders.

The matcher contains no I/O — it operates on plain dicts so it is trivial
to unit-test and reason about. The scoring formula combines four signals:

    score = amount_score * 0.50    # exact=1.0, linear falloff to $10 cap
          + date_score   * 0.30    # same day=1.0, 14+ days=0.0
          + payee_score  * 0.20    # AMAZON / VENMO regex match
          + memo_bonus   * 0.10    # +0.10 if memo contains the order id

Max possible score is 1.10 (when the memo bonus fires). Callers should
use a threshold around 0.85 plus an ambiguity gap (e.g. 0.10) to reject
matches where two candidates score within a hair of each other.

If the order date is more than 14 days from the transaction date, the
score collapses to 0 — a stale order should never auto-link to a fresh
charge no matter how perfectly the amount and payee align.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Source-aware payee regexes. Anything not in this map falls back to a
# permissive "any payee scores 0.5" rule.
_PAYEE_PATTERNS = {
    "amazon": re.compile(r"AMZN|AMAZON", re.IGNORECASE),
    "venmo":  re.compile(r"VENMO",        re.IGNORECASE),
}

# Weights — keep in sync with the docstring above.
_W_AMOUNT = 0.50
_W_DATE   = 0.30
_W_PAYEE  = 0.20
_W_MEMO   = 0.10

# Tuning knobs.
_AMOUNT_CAP_CENTS = 1000   # $10 absolute cap on tolerance
_AMOUNT_PCT_CAP = 0.10     # ±10% of order total
_DATE_WINDOW_DAYS = 14     # date diffs beyond this kill the score


def _amount_score(order_total_cents: int, txn_amount_cents: int) -> float:
    """Linear falloff from 1.0 at exact match to 0.0 at the tolerance edge.

    Tolerance per spec: min(10% of order total, $10). So a $47 order
    tolerates ±$4.70, while a $500 order tolerates ±$10 (capped).
    YNAB amounts are negative (outflow); we compare absolute values.
    """
    o = abs(order_total_cents)
    diff = abs(abs(txn_amount_cents) - o)
    if diff == 0:
        return 1.0
    tolerance = min(int(o * _AMOUNT_PCT_CAP), _AMOUNT_CAP_CENTS)
    if tolerance == 0 or diff >= tolerance:
        return 0.0
    return 1.0 - (diff / tolerance)


def _date_score(order_date, txn_date) -> float:
    """1.0 same day, linear to 0.0 at 14 days. Charges before the order
    date (txn < order) score 0 — Amazon doesn't bill before ordering.
    """
    delta_days = (txn_date - order_date).days
    if delta_days < 0 or delta_days >= _DATE_WINDOW_DAYS:
        return 0.0
    return 1.0 - (delta_days / _DATE_WINDOW_DAYS)


def _payee_score(payee: str, source: str) -> float:
    """1.0 if the source-specific regex hits, 0.0 otherwise.

    Unknown sources score 0 (fail closed) rather than a neutral 0.5 —
    we'd rather miss a match than auto-link the wrong payee.
    """
    if not payee:
        return 0.0
    pattern = _PAYEE_PATTERNS.get(source)
    if pattern is None:
        return 0.0
    return 1.0 if pattern.search(payee) else 0.0


def _memo_bonus(memo: str, external_id: Optional[str]) -> float:
    """1.0 if the order's external id appears verbatim in the memo."""
    if not memo or not external_id:
        return 0.0
    return 1.0 if external_id in memo else 0.0


def match_score(
    order: dict[str, Any],
    txn: dict[str, Any],
    source: str = "amazon",
) -> float:
    """Score how well ``txn`` matches ``order``. Range: 0.0 to 1.10.

    If the date drift exceeds the 14-day window the score short-circuits
    to 0 — a strong amount match cannot rescue a stale order.
    """
    date_s = _date_score(order["order_date"], txn["txn_date"])
    if date_s == 0.0:
        return 0.0

    amount_s = _amount_score(order["total_cents"], txn["amount_cents"])
    payee_s  = _payee_score(txn.get("payee", ""), source)
    memo_s   = _memo_bonus(txn.get("memo", ""), order.get("external_id"))

    return (
        amount_s * _W_AMOUNT
        + date_s  * _W_DATE
        + payee_s * _W_PAYEE
        + memo_s  * _W_MEMO
    )


def find_best_match(
    pending_orders: list[dict[str, Any]],
    txn: dict[str, Any],
    threshold: float = 0.85,
    ambiguity_gap: float = 0.10,
    source: str = "amazon",
) -> Optional[dict[str, Any]]:
    """Return the single best order match, or None if unsafe to auto-link.

    Returns None when:
      - No order scores at or above ``threshold``, or
      - Two or more orders score above threshold and the gap between the
        top two is smaller than ``ambiguity_gap`` (caller should ask the
        user to disambiguate rather than guess).
    """
    if not pending_orders:
        return None

    scored = [(match_score(o, txn, source=source), o) for o in pending_orders]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    top_score, top_order = scored[0]
    if top_score < threshold:
        return None

    if len(scored) > 1:
        runner_up_score = scored[1][0]
        if (top_score - runner_up_score) < ambiguity_gap:
            return None

    return top_order
