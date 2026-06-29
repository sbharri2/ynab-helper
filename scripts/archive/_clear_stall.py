"""Clear the bot_conversation last_asked_id gate.

Phase 0.5's push loop refuses to push a new item if any prior item is still
"in flight" (last_asked_id != NULL). When the user ignores a DM, that gate
gets stuck forever. This script clears it so the push loop resumes.
"""
from __future__ import annotations

from bot import storage

with storage.connect("ynab_helper.db") as con:
    before = con.execute(
        "SELECT user_id, last_asked_id, last_asked_kind "
        "FROM bot_conversation"
    ).fetchall()
    print("before:")
    for r in before:
        print(f"  {dict(r)}")

    con.execute(
        "UPDATE bot_conversation "
        "SET last_asked_id=NULL, last_asked_kind=NULL"
    )

    after = con.execute(
        "SELECT user_id, last_asked_id, last_asked_kind "
        "FROM bot_conversation"
    ).fetchall()
    print("after:")
    for r in after:
        print(f"  {dict(r)}")

storage.audit(
    "ynab_helper.db",
    "queue_unstuck",
    {"reason": "manual clear after stall on pending_txn #913 (Rhoback no-guess)"},
)
print("audit logged")
