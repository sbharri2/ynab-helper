"""Queue-lane state machine for pending_txn rows.

States
------
    HOT   — push loop drips DMs one at a time, real-time.
    COLD  — wait for the user to type /batch, then bulk-process.
    HOLD  — a charge waiting on its receipt: a LARGE Amazon charge (>= the
            large-charge threshold) or an Apple/Venmo item. Sweep promotes to
            HOT on enrichment; large Amazon also promotes to HOT after 24h so
            it gets asked, everything else drops to COLD.

Transitions
-----------

    new row
       │
       ▼
    [HOT] ──── 2h with no user reply ────▶ [COLD]
                                            ▲
    [HOLD] ─── enrichment succeeds ──▶ [HOT]
       │                                    ▲
       ├── 24h, large Amazon ───────────────┘
       └── 24h, everything else ────────────▶ [COLD]

Lane changes are timestamped in ``pending_txn.lane_changed_at`` so TTL
math doesn't need a separate event log.

Public surface:
    promote_hold_to_hot(db_path, pt_id, *, settings)
        — sweep helper: re-runs the matcher; if it finds an order, copies
          the rich summary onto the row and flips HOLD→HOT.
    demote_hot_to_cold(db_path)
        — sweep helper: every HOT row whose lane_changed_at is >2h old
          drops to COLD without a notification.
    abandon_stale_holds(db_path, *, settings)
        — sweep helper: HOLD rows older than 24h give up waiting for
          their order email. Large Amazon rows promote to HOT so the
          user gets asked anyway; everything else drops to COLD.
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
# Large Amazon charges (spec 2026-07-25) wait this long for the order email
# so the question can carry item detail, then get asked anyway. Small Amazon
# charges never enter HOLD — they auto-bucket at ingest — so the old 14-day
# Amazon TTL and its aged-out alert are gone.
LARGE_AMAZON_HOLD_TTL_HOURS = 24
LARGE_AMAZON_THRESHOLD_CENTS = 15000


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


def abandon_stale_holds(db_path: Path | str, *, settings=None) -> int:
    """HOLD rows that exhaust their TTL move on.

      * Large Amazon — 24h, then promoted to HOT so the user is ASKED with
        whatever detail we have (amount + date). Spec 2026-07-25: an
        unanswered big charge must nag, not settle quietly into the cold pile.
      * Other — 24h, then COLD, unchanged.

    ``settings`` overrides the large-charge threshold when supplied; the
    module default applies otherwise.
    """
    threshold = LARGE_AMAZON_THRESHOLD_CENTS
    if settings is not None:
        threshold = getattr(
            getattr(settings, "amazon", None), "large_charge_cents", threshold
        )

    total = 0
    large_ids: list[int] = []
    other_ids: list[int] = []
    with storage.connect(db_path) as con:
        # Large Amazon — ask anyway.
        large_rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%') "
            "  AND amount_cents <= ? "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (-abs(threshold), f"-{LARGE_AMAZON_HOLD_TTL_HOURS} hours"),
        ).fetchall()
        if large_rows:
            large_ids = [r["id"] for r in large_rows]
            placeholders = ",".join(["?"] * len(large_ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'hot', "
                f"last_pushed_at = NULL, lane_changed_at = ? "
                f"WHERE id IN ({placeholders})",
                [_utcnow(), *large_ids],
            )

        # Everything else — COLD.
        other_rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (f"-{HOLD_TTL_HOURS} hours",),
        ).fetchall()
        if other_rows:
            other_ids = [r["id"] for r in other_rows]
            placeholders = ",".join(["?"] * len(other_ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'cold', "
                f"lane_changed_at = ? WHERE id IN ({placeholders})",
                [_utcnow(), *other_ids],
            )

    # Audit calls open their own connection, so they run after the block
    # above commits and closes — matching the pattern in demote_hot_to_cold
    # / set_lane elsewhere in this module. Firing them while the write
    # transaction above was still open caused "database is locked".
    if large_ids:
        storage.audit(db_path, "large_amazon_ask_unenriched", {
            "count": len(large_ids), "pt_ids": large_ids,
            "ttl_hours": LARGE_AMAZON_HOLD_TTL_HOURS,
        })
        log.warning("abandon_stale_holds: %d large Amazon items asked "
                    "without a receipt (>%dh)", len(large_ids),
                    LARGE_AMAZON_HOLD_TTL_HOURS)
        total += len(large_ids)
    if other_ids:
        storage.audit(db_path, "queue_lane_change", {
            "to": "cold", "from": "hold",
            "reason": "hold_ttl_expired", "count": len(other_ids),
        })
        log.info("abandon_stale_holds: %d rows to cold", len(other_ids))
        total += len(other_ids)
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
        # A confirmed user choice wins; otherwise carry the order's own
        # suggestion through so the prompt offers a starting guess instead
        # of a bare amount (order 83 carried a 0.6-confidence pick that
        # never reached the user).
        new_cat = matched.get("chosen_category") or matched.get(
            "suggested_category")
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
        "abandoned_holds": abandon_stale_holds(db_path, settings=settings),
    }
