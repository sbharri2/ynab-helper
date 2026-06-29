"""Find where the $70k is coming from — check every view's totals."""
from datetime import date
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    print("─── BACKLOG: uncategorized rows total ─────────────────────────")
    r = con.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(-amount_cents),0) total
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
          AND (payee IS NULL OR payee NOT LIKE 'Transfer :%')
    """).fetchone()
    print(f"  {r['n']:>5} rows, ${r['total']/100:>10,.2f}")

    print("\n─── TREEMAP this-year (2026 YTD), expenses, with all filters ──")
    # Mirror the exact Rust query for treemap categories with expenses mode
    r = con.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(-lt.amount_cents),0) total
        FROM ledger_txn lt
        LEFT JOIN category c ON c.id = lt.category_id
        LEFT JOIN category_group g ON g.id = c.group_id
        WHERE lt.posted_date >= '2026-01-01' AND lt.posted_date < '2027-01-01'
          AND lt.amount_cents < 0
          AND (lt.payee IS NULL OR lt.payee NOT LIKE 'Transfer :%')
          AND (c.is_spending IS NULL OR c.is_spending = 1)
          AND (g.name IS NULL OR g.name != 'Internal Master Category')
    """).fetchone()
    print(f"  {r['n']:>5} rows, ${r['total']/100:>10,.2f}")

    print("\n─── TREEMAP this-month, expenses ──────────────────────────────")
    r = con.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(-lt.amount_cents),0) total
        FROM ledger_txn lt
        LEFT JOIN category c ON c.id = lt.category_id
        LEFT JOIN category_group g ON g.id = c.group_id
        WHERE lt.posted_date >= '2026-06-01' AND lt.posted_date < '2026-07-01'
          AND lt.amount_cents < 0
          AND (lt.payee IS NULL OR lt.payee NOT LIKE 'Transfer :%')
          AND (c.is_spending IS NULL OR c.is_spending = 1)
          AND (g.name IS NULL OR g.name != 'Internal Master Category')
    """).fetchone()
    print(f"  {r['n']:>5} rows, ${r['total']/100:>10,.2f}")

    print("\n─── TREEMAP last-12-months, expenses ──────────────────────────")
    r = con.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(-lt.amount_cents),0) total
        FROM ledger_txn lt
        LEFT JOIN category c ON c.id = lt.category_id
        LEFT JOIN category_group g ON g.id = c.group_id
        WHERE lt.posted_date >= date('now', '-12 months')
          AND lt.amount_cents < 0
          AND (lt.payee IS NULL OR lt.payee NOT LIKE 'Transfer :%')
          AND (c.is_spending IS NULL OR c.is_spending = 1)
          AND (g.name IS NULL OR g.name != 'Internal Master Category')
    """).fetchone()
    print(f"  {r['n']:>5} rows, ${r['total']/100:>10,.2f}")

    print("\n─── Top 10 (uncategorized) by amount ──────────────────────────")
    rows = con.execute("""
        SELECT id, posted_date, -amount_cents AS out, payee, source_signal
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
          AND (payee IS NULL OR payee NOT LIKE 'Transfer :%')
        ORDER BY -amount_cents DESC LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  ${r['out']/100:>10,.2f}  {r['posted_date']}  "
              f"{(r['payee'] or '')[:32]:32s}  src={r['source_signal']}")
