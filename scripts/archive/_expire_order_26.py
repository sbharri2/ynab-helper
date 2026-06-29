"""Mark pending_order #26 as expired so the push queue advances.

Order #26 (Amazon "Rubber Golf Tees", $15.00) was pushed 2026-06-14 11:00 UTC
and never answered. The push_loop's stale-as-skip safety net didn't fire
because the loop itself was wedged alongside the bot's inbound polling.

This is the same recovery the stale-as-skip would have done if it were
running. Recoverable via /unskip if Steven later wants to revisit it.
"""
from __future__ import annotations
from bot import storage

with storage.connect("ynab_helper.db") as con:
    row = con.execute("SELECT * FROM pending_order WHERE id = 26").fetchone()
    print(f"before: status={dict(row)['status']}")
    con.execute(
        "UPDATE pending_order SET status='expired', "
        "updated_at=CURRENT_TIMESTAMP WHERE id=26"
    )
    # Also clear the conversation pointer if it still references #26.
    con.execute(
        "UPDATE bot_conversation "
        "SET last_asked_id=NULL, last_asked_kind=NULL "
        "WHERE last_asked_id=26 AND last_asked_kind='order'"
    )
    row = con.execute("SELECT status FROM pending_order WHERE id = 26").fetchone()
    print(f"after:  status={dict(row)['status']}")

storage.audit(
    "ynab_helper.db",
    "ignored_dm_skipped",
    {
        "chat_id": 8405924742,
        "kind": "order",
        "id": 26,
        "reason": "manual expiry; push_loop was wedged so stale-as-skip never fired",
    },
)
print("audit logged")
