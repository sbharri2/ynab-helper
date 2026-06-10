"""Pure conversation logic — what to ask the user next, and how to format it.

Has zero Telegram dependency. The telegram_bot module calls these functions
and renders the results.
"""
from __future__ import annotations

import json
from pathlib import Path

from bot import storage


def next_item_for_user(
    db_path: Path | str, *, user_id: str, include_txns: bool = True
) -> dict | None:
    """Return the next item to ask the user about, or None if the queue is empty.

    Priority: pending_order (real-time email items) before pending_txn (digest items).
    Within each, oldest first.

    When ``include_txns`` is False, only pending_orders are returned — used by
    the push loop to gate non-Amazon/Venmo transactions to the daily digest
    window (see ``_in_digest_window`` in telegram_bot).
    """
    with storage.connect(db_path) as con:
        # Linear order by id — the natural arrival order. When the
        # user ignores a DM, the push_loop's staleness guard marks the
        # row as 'skipped' (see telegram_bot._push_loop) so it doesn't
        # block the queue forever. User can `/unskip` to revisit.
        order = con.execute(
            """SELECT * FROM pending_order
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id ASC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if order:
            d = dict(order)
            d["kind"] = "order"
            d["raw_payload"] = json.loads(d.get("raw_payload") or "{}")
            return d

        if not include_txns:
            return None

        txn = con.execute(
            """SELECT * FROM pending_txn
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id ASC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if txn:
            d = dict(txn)
            d["kind"] = "txn"
            return d

    return None


def format_item_prompt(item: dict) -> str:
    """Plain-text message body. Inline keyboard rendered separately."""
    if item["kind"] == "order":
        source = item["source"]
        emoji = "🛒" if source == "amazon" else "💸"
        amount = item["total_cents"] / 100
        order_date = item["order_date"]
        # Portable date format: avoid POSIX-only strftime("%-m/%-d") which fails on Windows.
        date_str = (
            f"{order_date.month}/{order_date.day}"
            if hasattr(order_date, "month")
            else str(order_date)
        )
        summary = item.get("raw_summary", "")
        suggestion = item.get("suggested_category_name") or "(no suggestion)"
        return (
            f"{emoji} {source.title()} — ${amount:.2f} · {date_str}\n"
            f"{summary}\n\n"
            f"Best guess: {suggestion}"
        )
    else:  # txn
        amount = abs(item["amount_cents"]) / 100
        txn_date = item.get("txn_date")
        date_str = (
            f"{txn_date.month}/{txn_date.day}"
            if hasattr(txn_date, "month")
            else str(txn_date)
        )
        payee = item.get("payee", "?")
        context = (item.get("raw_summary") or item.get("memo") or "").strip()
        context_line = f"{context}\n" if context else ""
        # Render the bottom line differently depending on whether we have a guess.
        # The no-guess case is now common (Phase 0.5 dropped the filter); show
        # a clear prompt so the user knows the bot needs their input from scratch.
        suggestion_name = item.get("suggested_category_name")
        if suggestion_name:
            bottom = f"Best guess: {suggestion_name}"
        else:
            bottom = "No guess yet — pick a category below."
        return (
            f"📋 ${amount:.2f} {payee} · {date_str}\n"
            f"{context_line}\n"
            f"{bottom}"
        )
