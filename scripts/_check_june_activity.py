"""Where does the $51k June activity come from?

Break down the current month's activity by category, then drill into the
largest categories to see the actual transactions driving them.
"""
from bot import storage
from bot.config import load_settings

s = load_settings()

with storage.connect(s.paths.database) as con:
    print("─── 1. month_category rows for 2026-06 (totals view) ──────────────")
    rows = con.execute("""
        SELECT mc.category_id, c.name, g.name AS group_name,
               COALESCE(c.is_spending, 1) AS is_spending,
               mc.budgeted_cents, mc.activity_cents, mc.available_cents
        FROM month_category mc
        JOIN category c ON c.id = mc.category_id
        JOIN category_group g ON g.id = c.group_id
        WHERE mc.month = '2026-06'
        ORDER BY mc.activity_cents ASC
    """).fetchall()

    tot_b = tot_a = tot_av = 0
    spending_a = nonspending_a = 0
    for r in rows:
        tot_b += r["budgeted_cents"]
        tot_a += r["activity_cents"]
        tot_av += r["available_cents"]
        if r["is_spending"]:
            spending_a += r["activity_cents"]
        else:
            nonspending_a += r["activity_cents"]
    print(f"  Total rows           : {len(rows)}")
    print(f"  Total budgeted       : ${tot_b/100:>14,.2f}")
    print(f"  Total activity       : ${tot_a/100:>14,.2f}")
    print(f"     of which spending : ${spending_a/100:>14,.2f}")
    print(f"     of which NON-spend: ${nonspending_a/100:>14,.2f}")
    print(f"  Total available      : ${tot_av/100:>14,.2f}")

    print("\n─── 2. top 15 categories by absolute activity ───────────────────")
    for r in rows[:15]:
        flag = " " if r["is_spending"] else "[non-spending]"
        print(f"  {r['activity_cents']/100:>12,.2f}  "
              f"{r['name'][:30]:30s}  ({r['group_name'][:20]:20s})  {flag}")

    print("\n─── 3. top 10 individual ledger_txn rows in 2026-06 ─────────────")
    txns = con.execute("""
        SELECT lt.id, lt.posted_date, lt.amount_cents, lt.payee,
               c.name AS category_name, g.name AS group_name,
               COALESCE(c.is_spending, 1) AS is_spending
        FROM ledger_txn lt
        LEFT JOIN category c ON c.id = lt.category_id
        LEFT JOIN category_group g ON g.id = c.group_id
        WHERE lt.posted_date >= '2026-06-01' AND lt.posted_date < '2026-07-01'
          AND lt.amount_cents < 0
        ORDER BY lt.amount_cents ASC LIMIT 10
    """).fetchall()
    for r in txns:
        flag = "" if r["is_spending"] else "[NON-SPEND]"
        print(f"  {r['amount_cents']/100:>12,.2f}  {r['posted_date']}  "
              f"{(r['payee'] or '')[:25]:25s}  "
              f"{(r['category_name'] or 'UNCATEGORIZED')[:25]:25s}  {flag}")
