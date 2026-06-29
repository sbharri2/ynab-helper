"""Queue-lane state machine for pending_txn rows.

States
------
    HOT   — push loop drips DMs one at a time, real-time.
    COLD  — wait for the user to type /batch, then bulk-process.
    HOLD  — Amazon CC alert without a matched order email yet.
            Sweep promotes to HOT on enrichment, or COLD after 24h.

Transitions
-----------

    new row
       │
       ▼
    [HOT] ──── 2h with no user reply ────▶ [COLD]
                                            ▲
    [HOLD] ─── enrichment succeeds ──▶ [HOT]
       │                                    │
       └────── 24h still unenriched ────────┘

Lane changes are timestamped in ``pending_txn.lane_changed_at`` so TTL
math doesn't need a separate event log.

Public surface:
    promote_hold_to_hot(db_path, pt_id, *, settings)
        — sweep helper: re-runs the matcher; if it finds an order, copies
          the rich summary onto the row and flips HOLD→HOT.
    demote_hot_to_cold(db_path)
        — sweep helper: every HOT row whose lane_changed_at is >2h old
          drops to COLD without a notification.
    abandon_stale_holds(db_path)
        — sweep helper: HOLD rows older than 24h give up waiting for
          their order email and drop to COLD.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)

HOT_TTL_HOURS = 2
# Default HOLD TTL for non-Amazon items (Apple, Venmo). Kept short because
# their match windows are tight — Apple receipts arrive within hours.
HOLD_TTL_HOURS = 24
# Amazon receipts can arrive days to weeks after the CC charge (third-party
# sellers, slow shipments). Per Steven's directive (2026-06-26): never
# auto-process an Amazon item without enrichment; surface the unmatched
# ones as a dedicated alert when they exceed this window.
AMAZON_HOLD_TTL_DAYS = 14


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def set_lane(db_path: Path | str, pt_id: int, lane: str,
             *, reason: str | None = None) -> None:
    """Move a row to ``lane`` and stamp lane_changed_at. Audit-logged so
    we can later see why anything moved."""
    with storage.connect(db_path) as con:
        prev = con.execute(
            "SELECT queue_lane FROM pending_txn WHERE id = ?", (pt_id,),
        ).fetchone()
        if not prev:
            return
        old = prev["queue_lane"]
        if old == lane:
            return
        con.execute(
            "UPDATE pending_txn SET queue_lane = ?, lane_changed_at = ? "
            "WHERE id = ?",
            (lane, _utcnow(), pt_id),
        )
    storage.audit(db_path, "queue_lane_change", {
        "pt_id": pt_id, "from": old, "to": lane, "reason": reason,
    })


def demote_hot_to_cold(db_path: Path | str) -> int:
    """Move HOT rows ignored for >HOT_TTL_HOURS into COLD.

    Single owner of HOT staleness — when a row goes COLD, this function
    also clears the matching bot_conversation.last_asked_id pointer so
    the push loop can advance past it. (Older split-responsibility logic
    in _push_loop was removed; this is the only path that demotes HOT.)

    Returns count.
    """
    cutoff = f"-{HOT_TTL_HOURS} hours"
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hot' AND status = 'pending' "
            "  AND last_pushed_at IS NOT NULL "
            "  AND last_pushed_at <= datetime('now', ?)",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join(["?"] * len(ids))
        con.execute(
            f"UPDATE pending_txn SET queue_lane = 'cold', "
            f"lane_changed_at = ? WHERE id IN ({placeholders})",
            [_utcnow(), *ids],
        )
        # Also clear any in-flight pointers that reference these rows,
        # so the next push loop tick can DM the next HOT item without
        # the user having to /skip or interact with a stale DM.
        con.execute(
            f"UPDATE bot_conversation SET last_asked_id = NULL, "
            f"last_asked_message_id = NULL "
            f"WHERE last_asked_kind = 'txn' AND last_asked_id IN ({placeholders})",
            ids,
        )
    storage.audit(db_path, "queue_lane_change", {
        "to": "cold", "from": "hot", "reason": "hot_ttl_expired",
        "count": len(ids),
    })
    log.info("demote_hot_to_cold: %d rows", len(ids))
    return len(ids)


def abandon_stale_holds(db_path: Path | str) -> int:
    """HOLD rows that exhaust their TTL get an alert + move to COLD.

    Per-source TTL:
      * Amazon  — 14 days. Items that exceed this become 'amazon_aged_out'
        in audit log so the daily Amazon tracker can surface them.
      * Other   — 24 hours.

    We split the sweep into two queries (per TTL) to keep the rule
    explicit. The lane_changed_at re-stamp keeps subsequent COLD-side
    accounting straightforward.
    """
    total = 0
    with storage.connect(db_path) as con:
        # Amazon (long TTL) — by payee match
        amazon_rows = con.execute(
            "SELECT id, payee FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%') "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (f"-{AMAZON_HOLD_TTL_DAYS} days",),
        ).fetchall()
        if amazon_rows:
            ids = [r["id"] for r in amazon_rows]
            placeholders = ",".join(["?"] * len(ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'cold', "
                f"lane_changed_at = ? WHERE id IN ({placeholders})",
                [_utcnow(), *ids],
            )
            storage.audit(db_path, "amazon_aged_out", {
                "count": len(ids), "pt_ids": ids,
                "ttl_days": AMAZON_HOLD_TTL_DAYS,
            })
            log.warning("abandon_stale_holds: %d Amazon items aged out "
                         "(>%dd unmatched)", len(ids), AMAZON_HOLD_TTL_DAYS)
            total += len(ids)

        # Non-Amazon HOLD — short TTL
        other_rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND NOT (UPPER(payee) LIKE '%AMAZON%' "
            "           OR UPPER(payee) LIKE '%AMZN%') "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (f"-{HOLD_TTL_HOURS} hours",),
        ).fetchall()
        if other_rows:
            ids = [r["id"] for r in other_rows]
            placeholders = ",".join(["?"] * len(ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'cold', "
                f"lane_changed_at = ? WHERE id IN ({placeholders})",
                [_utcnow(), *ids],
            )
            storage.audit(db_path, "queue_lane_change", {
                "to": "cold", "from": "hold",
                "reason": "hold_ttl_expired", "count": len(ids),
            })
            log.info("abandon_stale_holds: %d non-Amazon rows", len(ids))
            total += len(ids)
    return total


def promote_holds_to_hot(db_path: Path | str, *, settings) -> int:
    """Re-run the matcher on every HOLD row. If we now find a matching
    pending_order, enrich raw_summary, set suggested_category from the
    order if the user already categorized it, and promote to HOT.
    """
    from bot.ingest import _enrich_from_pending_order

    with storage.connect(db_path) as con:
        holds = con.execute(
            "SELECT id, txn_date, payee, amount_cents, raw_summary, "
            "       assigned_to_user_id "
            "FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending'"
        ).fetchall()
    if not holds:
        return 0

    promoted = 0
    for h in holds:
        parsed = {
            "summary": h["raw_summary"] or "",
            "amount_cents": h["amount_cents"],
            "posted_date": h["txn_date"],
        }
        matched = _enrich_from_pending_order(db_path, parsed, h["payee"] or "")
        if matched is None:
            continue
        # Phase 2 enrichment succeeded — copy the rich summary onto the row
        # and (if the matching order is already user-categorized) adopt
        # that as the suggestion.
        new_summary = parsed.get("summary") or h["raw_summary"]
        new_cat = matched.get("chosen_category")
        # If the matched order is owned by a different user (e.g. an Amazon
        # CC alert arrived through Steven's inbox but the matching order
        # confirmation came through Allison's), flip the pending_txn over
        # to that user's queue so the enriched DM lands on the right phone.
        cur_assignee = h["assigned_to_user_id"]
        new_assignee = matched.get("assigned_to_user_id") or cur_assignee
        with storage.connect(db_path) as con:
            if new_cat:
                con.execute(
                    "UPDATE pending_txn SET raw_summary = ?, "
                    "suggested_category = ?, queue_lane = 'hot', "
                    "assigned_to_user_id = ?, "
                    "last_pushed_at = NULL, lane_changed_at = ? WHERE id = ?",
                    (new_summary, new_cat, new_assignee, _utcnow(), h["id"]),
                )
            else:
                con.execute(
                    "UPDATE pending_txn SET raw_summary = ?, "
                    "queue_lane = 'hot', assigned_to_user_id = ?, "
                    "last_pushed_at = NULL, lane_changed_at = ? WHERE id = ?",
                    (new_summary, new_assignee, _utcnow(), h["id"]),
                )
        storage.audit(db_path, "queue_lane_change", {
            "pt_id": h["id"], "from": "hold", "to": "hot",
            "reason": "enrichment_found",
            "matched_order_id": matched.get("id"),
        })
        if new_assignee != cur_assignee:
            storage.audit(db_path, "routed", {
                "kind": "txn", "id": h["id"],
                "from_user": cur_assignee, "to_user": new_assignee,
                "reason": "enrichment_matched_other_user_order",
                "matched_order_id": matched.get("id"),
            })
        promoted += 1
    if promoted:
        log.info("promote_holds_to_hot: %d", promoted)
    return promoted


def sweep_lanes(db_path: Path | str, *, settings) -> dict:
    """Run all three sweeps in sequence. Called every 30 min by the bot's
    background loop. Returns a counts dict for logging.
    """
    return {
        "promoted_hold_to_hot": promote_holds_to_hot(db_path, settings=settings),
        "demoted_hot_to_cold": demote_hot_to_cold(db_path),
        "abandoned_holds": abandon_stale_holds(db_path),
    }
