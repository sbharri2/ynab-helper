"""Fix the $90 Venmo duplicate (pending_txn 945, ledger_txn 23622) caused by
the OLD Joint Checking placeholder account swallowing some Coastal alert
account-resolution lookups.

Three actions:
  1. Mark pending_txn 945 as 'skipped' with a memo pointing to its twin (933)
  2. Delete ledger_txn 23622 (and any ledger_signal pointing to it)
  3. Close account "OLD Joint Checking" so future ingests can't resolve to it
"""
from __future__ import annotations
from bot import storage

OLD_JOINT_ID = "1f13d193-1f7b-4f91-8637-b4d737445d0f"

with storage.connect("ynab_helper.db") as con:
    print("Step 1: mark pending_txn #945 as 'skipped' (twin of #933)")
    con.execute(
        "UPDATE pending_txn SET status = 'skipped', "
        "memo = COALESCE(NULLIF(memo, ''), '') || "
        "  ' [duplicate of pending_txn #933; OLD Joint Checking acct resolution bug]' "
        "WHERE id = 945 AND status = 'pending'"
    )

    print("Step 2: drop ledger_signal + ledger_txn 23622")
    con.execute("DELETE FROM ledger_signal WHERE ledger_txn_id = 23622")
    con.execute("DELETE FROM ledger_txn WHERE id = 23622")

    print(f"Step 3: close account {OLD_JOINT_ID} (OLD Joint Checking)")
    con.execute(
        "UPDATE account SET closed = 1 WHERE id = ?",
        (OLD_JOINT_ID,),
    )

    # Verify
    row = con.execute(
        "SELECT id, name, closed FROM account WHERE id = ?", (OLD_JOINT_ID,)
    ).fetchone()
    print(f"  -> {dict(row)}")
    row = con.execute(
        "SELECT id, status FROM pending_txn WHERE id = 945"
    ).fetchone()
    print(f"  -> pending_txn 945: {dict(row)}")
    row = con.execute(
        "SELECT id FROM ledger_txn WHERE id = 23622"
    ).fetchone()
    print(f"  -> ledger_txn 23622: {dict(row) if row else 'DELETED ✓'}")

    # Also: clear bot_conversation if it's pointing at 945
    row = con.execute(
        "SELECT last_asked_id, last_asked_kind FROM bot_conversation"
    ).fetchone()
    if row and row["last_asked_id"] == 945:
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL, "
            "last_asked_kind = NULL"
        )
        print("  -> cleared bot_conversation pointer (was on #945)")

storage.audit("ynab_helper.db", "venmo_dupe_repair", {
    "pending_txn_skipped": 945,
    "ledger_txn_deleted": 23622,
    "account_closed": OLD_JOINT_ID,
    "twin_of": 933,
})
print("\ndone")
