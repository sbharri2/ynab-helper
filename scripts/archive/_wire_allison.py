"""One-shot: provision Allison's user_pref row + audit the change.

After Allison /starts the bot from her phone, the bot will reply with her
chat_id. Paste that chat_id into config.yaml (see scripts/ALLISON_CONFIG.yml
for the exact block to append), then run THIS script to:

  1. Insert / update Allison's user_pref row with sensible defaults:
       - receives_daily   = 1   (gets the 8am summary)
       - receives_weekly  = 1   (gets Sunday weekly)
       - receives_per_txn = 1   (her routed items push immediately)
       - quiet_hours      = "22:00-08:00" (she sleeps until 8)

  2. Reassign any pending_txns/pending_orders whose user_id is 'allison'
     (created by Allison's Gmail account) so they actually land in her
     queue. If the auto-route at insertion didn't catch them — e.g. rows
     created before Phase 6.5 shipped — this backfills.

  3. Audit log.

Idempotent. Safe to re-run.
"""
from __future__ import annotations
from datetime import datetime, timezone
from bot import storage

ALLISON = "allison"

with storage.connect("ynab_helper.db") as con:
    # Step 1 — user_pref
    existing = con.execute(
        "SELECT user_id FROM user_pref WHERE user_id = ?", (ALLISON,)
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE user_pref SET "
            "  receives_daily = 1, receives_weekly = 1, receives_per_txn = 1, "
            "  quiet_hours = '22:00-08:00', updated_at = ? "
            "WHERE user_id = ?",
            (datetime.now(timezone.utc), ALLISON),
        )
        print(f"user_pref: updated (was: {dict(existing)})")
    else:
        con.execute(
            "INSERT INTO user_pref "
            "  (user_id, receives_per_txn, receives_daily, receives_weekly, "
            "   quiet_hours) "
            "VALUES (?, 1, 1, 1, '22:00-08:00')",
            (ALLISON,),
        )
        print(f"user_pref: inserted defaults for {ALLISON}")

    r = con.execute(
        "SELECT user_id, receives_per_txn, receives_daily, receives_weekly, "
        "quiet_hours FROM user_pref WHERE user_id = ?", (ALLISON,),
    ).fetchone()
    print(f"  -> {dict(r)}")

    # Step 2 — reassign any orphan rows whose creator was 'allison'
    txn_changed = con.execute(
        "UPDATE pending_txn SET assigned_to_user_id = ? "
        "WHERE user_id = ? AND assigned_to_user_id != ? AND status = 'pending'",
        (ALLISON, ALLISON, ALLISON),
    ).rowcount
    order_changed = con.execute(
        "UPDATE pending_order SET assigned_to_user_id = ? "
        "WHERE user_id = ? AND assigned_to_user_id != ? AND status = 'pending'",
        (ALLISON, ALLISON, ALLISON),
    ).rowcount
    print(f"reassigned to Allison: pending_txn={txn_changed}  pending_order={order_changed}")

storage.audit("ynab_helper.db", "wire_allison", {
    "pending_txn_reassigned": txn_changed,
    "pending_order_reassigned": order_changed,
})
print("done")
