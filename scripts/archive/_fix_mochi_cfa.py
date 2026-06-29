"""Restore Chick-fil-A and Mochi SF suggestions to Dining Out.

The strong-prior cascade matched 'chick-fil-a' to a historical mis-cat
of Personal Savings (likely an old user error), and 'mochi%' to other
unrelated merchants categorized as Medical. The LLM's original guess
(Dining Out/Entertainment) was correct in both cases.
"""
from bot import storage

DINING = "f4b8c004-92d9-4aa4-bef9-96f097fe2586"
with storage.connect("ynab_helper.db") as con:
    rs = con.execute(
        "UPDATE pending_txn SET suggested_category = ? "
        "WHERE id IN (1001, 1003) AND status = 'pending'",
        (DINING,),
    )
    print(f"Restored {rs.rowcount} rows -> Dining Out/Entertainment")
storage.audit("ynab_helper.db", "restore_cfa_mochi", {"to": "Dining"})
