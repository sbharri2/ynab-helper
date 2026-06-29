"""Clean up the three known dupe pairs + restore Mochi → Medical.

For each dupe pair:
  * The ledger:N row (CC-alert ingest, rich raw_summary) is the KEEPER
  * Update its ynab_txn_id to the real YNAB id (so categorize → YNAB push)
  * Copy the keeper's suggested_category if the YNAB-side row had a
    better one (e.g. Brooke Holland Hai got the prior-based hairdresser
    category from the YNAB-side row)
  * Mark the YNAB-side row as 'skipped' with a memo
"""
from bot import storage

MOCHI_CORRECT = "MEDICAL"
DUPE_PAIRS = [
    # (ledger_row_id, ynab_row_id, prefer_suggestion_from)
    (1005, 1018, 1018),   # Brooke Holland Hai — YNAB-side has the better prior
    (1006, 1019, None),   # Spencer's Gifts — both have Gifts, either works
    (1010, 1020, 1010),   # Einstein Bagel — ledger-side Dining is right
]

with storage.connect("ynab_helper.db") as con:
    cat_map = {r["name"].lower(): r["id"]
               for r in con.execute("SELECT id, name FROM category").fetchall()}

    print("1. Restore Mochi → Medical")
    medical_id = cat_map.get(MOCHI_CORRECT.lower())
    if medical_id:
        rs = con.execute(
            "UPDATE pending_txn SET suggested_category = ? "
            "WHERE id = 1003 AND status = 'pending'",
            (medical_id,),
        )
        print(f"   updated {rs.rowcount} row -> Medical")

    print("\n2. Consolidating dupe pairs")
    for ledger_id, ynab_id, prefer in DUPE_PAIRS:
        ledger_row = con.execute(
            "SELECT ynab_txn_id, suggested_category, payee, raw_summary "
            "FROM pending_txn WHERE id = ?", (ledger_id,),
        ).fetchone()
        ynab_row = con.execute(
            "SELECT ynab_txn_id, suggested_category, payee FROM pending_txn WHERE id = ?",
            (ynab_id,),
        ).fetchone()
        if not ledger_row or not ynab_row:
            print(f"   skip pair ({ledger_id},{ynab_id}) — row missing")
            continue

        new_yid = ynab_row["ynab_txn_id"]
        new_sug = (ynab_row["suggested_category"]
                   if prefer == ynab_id else ledger_row["suggested_category"])
        # If the preferred one is NULL but the other has a value, use the other
        if not new_sug:
            new_sug = (ledger_row["suggested_category"]
                       or ynab_row["suggested_category"])

        # Step A: mark the YNAB-side row as skipped AND park its
        # ynab_txn_id under a unique sentinel so the UNIQUE index frees up.
        sentinel = f"merged:{ynab_id}"
        con.execute(
            "UPDATE pending_txn SET status = 'skipped', "
            "ynab_txn_id = ?, "
            "memo = COALESCE(NULLIF(memo, ''), '') || "
            "  ' [merged into pt#' || ? || ']' "
            "WHERE id = ?",
            (sentinel, ledger_id, ynab_id),
        )
        # Step B: promote the ledger:N row to own the real YNAB id
        con.execute(
            "UPDATE pending_txn SET ynab_txn_id = ?, suggested_category = ? "
            "WHERE id = ?",
            (new_yid, new_sug, ledger_id),
        )
        print(f"   merged pt#{ynab_id} into pt#{ledger_id}; "
              f"keeper now has ynab_id={new_yid[:18]}…")

storage.audit("ynab_helper.db", "consolidate_dupes_2026_06_26", {
    "pairs": [(a, b) for a, b, _ in DUPE_PAIRS],
    "mochi_fixed": True,
})
print("\ndone")
