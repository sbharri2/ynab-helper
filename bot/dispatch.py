"""Single owner of "what reaches Telegram".

Before this module, four independent code paths could each decide a
transaction never reached a phone: ynab_sync rows were never queued, ingest
auto-committed on learned priors, small Amazon charges bucketed silently, and
close_stale_pending adopted whatever category happened to be on the ledger
row. 213 July 2026 outflows produced 41 human decisions.

Now ingest and sync only ENQUEUE. This module decides:

    rule matches payee  -> file it, send an FYI, no reply expected
    no rule             -> leave pending; the group-ping sweep asks

Amazon is deliberately untouched here -- a separate effort owns that revamp.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot import payee_overrides, storage

log = logging.getLogger(__name__)


def classify(db_path: Path | str, pt_id: int) -> dict:
    """Decide what happens to one pending_txn. Pure: writes nothing."""
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, payee FROM pending_txn WHERE id = ?", (pt_id,),
        ).fetchone()
    if pt is None:
        return {"action": "ask", "pt_id": pt_id, "rule_id": None,
                "category_id": None}
    m = payee_overrides.match_rule(db_path, pt["payee"])
    if m is None:
        return {"action": "ask", "pt_id": pt_id, "rule_id": None,
                "category_id": None}
    return {"action": "fyi", "pt_id": pt_id, "rule_id": m["rule_id"],
            "category_id": m["category_id"]}


def file_by_rule(db_path: Path | str, pt_id: int, rule_id: int,
                 category_id: str) -> bool:
    """Commit a rule's category onto the pending row and its ledger row.

    Mirrors group_chat._file_item so a rule filing and a human filing leave
    the row in exactly the same shape -- the only difference is filed_by.
    synced_to_ynab_at is left NULL so ynab_writer picks the row up.
    """
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, ynab_txn_id, status FROM pending_txn WHERE id = ?",
            (pt_id,),
        ).fetchone()
        if pt is None or pt["status"] != "pending":
            return False
        con.execute(
            "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
            "status = 'categorized', filed_by = ?, "
            "synced_to_ynab_at = NULL WHERE id = ?",
            (category_id, storage._utcnow(), f"rule:{rule_id}", pt_id),
        )
        yid = pt["ynab_txn_id"] or ""
        if yid.startswith("ledger:"):
            try:
                lid = int(yid.split(":", 1)[1])
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (category_id, lid),
                )
            except (ValueError, IndexError):
                pass
        elif yid:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE ynab_txn_id = ?",
                (category_id, yid),
            )
    storage.bump_auto_rule_fire(db_path, rule_id)
    storage.audit(db_path, "rule_filed", {
        "pending_txn_id": pt_id, "rule_id": rule_id,
        "category_id": category_id,
    })
    return True


def dispatch_pending(db_path: Path | str) -> dict:
    """Classify every undecided pending row; file the rule hits.

    Idempotent: file_by_rule's status guard means a second pass over an
    already-filed row is a no-op, so fire_count cannot double-count.
    """
    with storage.connect(db_path) as con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM pending_txn WHERE status = 'pending' "
            "ORDER BY id ASC")]
    filed = asked = 0
    for pt_id in ids:
        got = classify(db_path, pt_id)
        if got["action"] == "fyi" and file_by_rule(
                db_path, pt_id, got["rule_id"], got["category_id"]):
            filed += 1
        else:
            asked += 1
    if filed:
        log.info("dispatch_pending: filed %d by rule, %d left to ask",
                 filed, asked)
    return {"filed": filed, "asked": asked}
