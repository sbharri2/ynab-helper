"""Where does the $73k of uncategorized transactions come from?

Break down by source_signal, date range, and ynab_txn_id presence so
we know whether YNAB has the categories already (port-over candidate)
or we need to re-categorize from scratch.
"""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    print("─── totals ──────────────────────────────────────────────────")
    r = con.execute("""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(-amount_cents), 0) AS total
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
    """).fetchone()
    print(f"  rows: {r['n']:>5,}    total outflows: ${r['total']/100:>14,.2f}")

    print("\n─── by source_signal ────────────────────────────────────────")
    rows = con.execute("""
        SELECT COALESCE(source_signal, '(null)') AS src,
               COUNT(*) AS n,
               COALESCE(SUM(-amount_cents), 0) AS total
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
        GROUP BY src
        ORDER BY total DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['src'][:25]:25s}  {r['n']:>5,} rows  ${r['total']/100:>12,.2f}")

    print("\n─── by year ─────────────────────────────────────────────────")
    rows = con.execute("""
        SELECT substr(posted_date, 1, 4) AS y,
               COUNT(*) AS n,
               COALESCE(SUM(-amount_cents), 0) AS total
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
        GROUP BY y
        ORDER BY y DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['y']}  {r['n']:>5,} rows  ${r['total']/100:>12,.2f}")

    print("\n─── ynab_txn_id presence (port-over candidates) ─────────────")
    r = con.execute("""
        SELECT
          SUM(CASE WHEN ynab_txn_id IS NOT NULL AND ynab_txn_id NOT LIKE 'ledger:%'
                   THEN 1 ELSE 0 END) AS has_real_uuid,
          SUM(CASE WHEN ynab_txn_id LIKE 'ledger:%' THEN 1 ELSE 0 END) AS synthetic,
          SUM(CASE WHEN ynab_txn_id IS NULL THEN 1 ELSE 0 END) AS no_link
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
    """).fetchone()
    print(f"  with real YNAB UUID: {r['has_real_uuid']:>5,}   ← can be re-pulled from YNAB")
    print(f"  synthetic ledger:N:  {r['synthetic']:>5,}   ← email-only, no YNAB row")
    print(f"  no ynab_txn_id:      {r['no_link']:>5,}   ← orphans (history-import?)")

    print("\n─── top 15 uncategorized rows ───────────────────────────────")
    rows = con.execute("""
        SELECT id, posted_date, -amount_cents AS out_cents, payee,
               source_signal, ynab_txn_id
        FROM ledger_txn
        WHERE category_id IS NULL AND amount_cents < 0
        ORDER BY -amount_cents DESC LIMIT 15
    """).fetchall()
    for r in rows:
        ytid_kind = ("real" if r['ynab_txn_id'] and not r['ynab_txn_id'].startswith('ledger:')
                     else "ledger" if r['ynab_txn_id'] else "none")
        print(f"  #{r['id']:>6}  {r['posted_date']}  "
              f"${r['out_cents']/100:>10,.2f}  "
              f"{(r['payee'] or '')[:30]:30s}  "
              f"{r['source_signal'] or '':15s}  ynab={ytid_kind}")
