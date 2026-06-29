"""Find ALL duplicate groups, not just CC-alert+sync pairs.

A group is: same account_id, same abs(amount_cents), posted_date within
±2 days of each other. Show their ynab_txn_ids to figure out the source.
"""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    # Find every cluster of >=2 rows on same (account, amount) within
    # a 5-day window. SQLite's window functions make this messy;
    # easier to bucket on (account_id, abs_amount, posted_date floor).
    clusters = con.execute("""
        WITH grouped AS (
            SELECT account_id, ABS(amount_cents) AS abs_amt, posted_date,
                   COUNT(*) AS n,
                   GROUP_CONCAT(id) AS ids,
                   GROUP_CONCAT(COALESCE(ynab_txn_id, 'NULL'), '|') AS yids,
                   GROUP_CONCAT(COALESCE(payee, ''), '|') AS payees,
                   GROUP_CONCAT(COALESCE(source_signal, ''), '|') AS sources
            FROM ledger_txn
            WHERE amount_cents < 0
              AND (payee IS NULL OR payee NOT LIKE 'Transfer :%')
            GROUP BY account_id, ABS(amount_cents), posted_date
            HAVING COUNT(*) >= 2
        )
        SELECT g.*, a.name AS account_name
        FROM grouped g
        JOIN account a ON a.id = g.account_id
        ORDER BY abs_amt DESC
    """).fetchall()

print(f"found {len(clusters)} same-day, same-amount, same-account clusters")
print(f"({sum(c['n'] for c in clusters)} rows total across all clusters)")
print()

# Bucket by N
from collections import Counter
counts = Counter(c['n'] for c in clusters)
print("cluster sizes:")
for n in sorted(counts):
    print(f"  {n} rows: {counts[n]} clusters")

print("\nTop 20 clusters by amount:")
for c in clusters[:20]:
    print(f"  ${c['abs_amt']/100:>10,.2f}  {c['account_name'][:18]:18s}  "
          f"{c['posted_date']}  n={c['n']}")
    yids = c['yids'].split('|')
    payees = c['payees'].split('|')
    sources = c['sources'].split('|')
    ids = c['ids'].split(',')
    for i in range(len(ids)):
        yid_kind = ("real" if yids[i] != 'NULL' and not yids[i].startswith('ledger:')
                    else "ledger" if yids[i].startswith('ledger:') else "none")
        print(f"      #{ids[i]:>5}  yid={yid_kind:<7}  src={sources[i]:<25s}  payee={payees[i][:35]}")

# Also check Southwest case specifically (re-open connection)
print("\nSouthwest Airlines $699.27 cluster (the user's report):")
with storage.connect(s.paths.database) as con2:
    rows = con2.execute("""
        SELECT id, posted_date, amount_cents, payee, ynab_txn_id, source_signal, memo,
               created_at, updated_at
        FROM ledger_txn
        WHERE payee LIKE '%Southwest%' AND ABS(amount_cents) = 69927
        ORDER BY posted_date, id
    """).fetchall()
    for r in rows:
        print(f"  #{r['id']}  {r['posted_date']}  payee='{r['payee']}'  "
              f"yid={(r['ynab_txn_id'] or 'NULL')[:18]}  src={r['source_signal']}")
        print(f"    created={r['created_at']}  updated={r['updated_at']}")
        print(f"    memo: {(r['memo'] or '(no memo)')[:80]}")
