"""Resolve the Fresh Chef pre-auth/tip duplicate (#957 vs #952)."""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    con.execute(
        "UPDATE pending_txn SET status = 'skipped', "
        "memo = COALESCE(NULLIF(memo, ''), '') || "
        "  ' [tip-duplicate of pending_txn #952; pre-auth $16.54 settled at $19.54]' "
        "WHERE id = 957 AND status = 'pending'"
    )
    con.execute("DELETE FROM ledger_signal WHERE ledger_txn_id = 23630")
    con.execute("DELETE FROM ledger_txn WHERE id = 23630")
    # If 957 is the in-flight one, clear it
    con.execute(
        "UPDATE bot_conversation SET last_asked_id = NULL, last_asked_kind = NULL "
        "WHERE last_asked_id = 957 AND last_asked_kind = 'txn'"
    )

with storage.connect("ynab_helper.db") as con:
    r1 = con.execute("SELECT id, status FROM pending_txn WHERE id = 957").fetchone()
    r2 = con.execute("SELECT id FROM ledger_txn WHERE id = 23630").fetchone()
print(f"pending_txn 957: status={r1['status'] if r1 else 'N/A'}")
print(f"ledger_txn 23630: {'DELETED ✓' if r2 is None else 'still exists'}")

storage.audit("ynab_helper.db", "preauth_tip_dupe_fix", {
    "pending_txn_skipped": 957, "ledger_txn_deleted": 23630, "twin_of": 952,
})
print("done")
