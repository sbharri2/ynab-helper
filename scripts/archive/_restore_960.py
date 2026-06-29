"""Restore #960's suggestion to Dining Out/Entertainment (was wrongly
overwritten to Website Squarespace by the looser priors before I tightened them).
"""
from bot import storage

DINING_OUT = "f4b8c004-92d9-4aa4-bef9-96f097fe2586"

with storage.connect("ynab_helper.db") as con:
    con.execute(
        "UPDATE pending_txn SET suggested_category = ? WHERE id = 960",
        (DINING_OUT,),
    )
    r = con.execute(
        "SELECT id, payee, suggested_category FROM pending_txn WHERE id = 960"
    ).fetchone()
    print(f"#{r['id']} {r['payee']!r} suggested={r['suggested_category']}")
print("done")
