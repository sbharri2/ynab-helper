"""Detect the travel pattern in last week's pendings."""
from datetime import datetime, timezone, timedelta
from bot import storage

DB = "ynab_helper.db"
with storage.connect(DB) as con:
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, suggested_category "
        "FROM pending_txn "
        "WHERE created_at >= ? AND status='skipped' "
        "ORDER BY txn_date",
        ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
    ).fetchall()

    # Look for non-North Carolina locations
    travel_tokens = ("DALLAS", "SAN FRANCISCO", "BALTIMORE", "PUNTA",
                      "AIR VENTURES", "FISHER TOURS", "SCAPE PARK", "HIGHPOINT")
    nc_tokens = ("HOLLY SPRINGS", "RALEIGH", "CARY", "APEX", "DURHAM",
                  "HUNTERSVILLE", "MOORESVIL")
    travel_rows = []
    home_rows = []
    for r in rows:
        p = (r["payee"] or "").upper()
        if any(t in p for t in travel_tokens):
            travel_rows.append(r)
        elif any(t in p for t in nc_tokens):
            home_rows.append(r)

    print("TRAVEL-LOCATION transactions (likely Vacation):")
    travel_total = 0
    for r in travel_rows:
        amt = r["amount_cents"] / 100
        travel_total += r["amount_cents"]
        print(f"  pt#{r['id']:>4}  {r['txn_date']}  ${amt:>+9.2f}  {(r['payee'] or '')[:50]}")
    print(f"  TOTAL: ${travel_total/100:+,.2f}")

    print(f"\nNC HOME-AREA transactions (likely regular life):")
    home_total = 0
    for r in home_rows:
        amt = r["amount_cents"] / 100
        home_total += r["amount_cents"]
        print(f"  pt#{r['id']:>4}  {r['txn_date']}  ${amt:>+9.2f}  {(r['payee'] or '')[:50]}")
    print(f"  TOTAL: ${home_total/100:+,.2f}")
