"""Find ledger_txn duplicates created by ynab_full_sync.

A 'dupe' is two rows on the SAME account with the SAME absolute amount
and posted_date within ±2 days of each other, where one row has a
synthetic ynab_txn_id (NULL or starts with 'ledger:') and the other
has a real YNAB UUID.

The synthetic row came from a CC alert (bot/ingest.py created it
before YNAB posted the txn). The real-UUID row came from a later
ynab_full_sync, which didn't dedupe properly.
"""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    rows = con.execute("""
        SELECT
          a.id AS id_a, a.posted_date AS dt_a, a.amount_cents,
          a.payee AS payee_a, a.ynab_txn_id AS yid_a, a.memo AS memo_a,
          b.id AS id_b, b.posted_date AS dt_b,
          b.payee AS payee_b, b.ynab_txn_id AS yid_b, b.memo AS memo_b,
          acc.name AS account_name
        FROM ledger_txn a
        JOIN ledger_txn b ON
              b.account_id = a.account_id
          AND b.amount_cents = a.amount_cents
          AND b.id > a.id
          AND ABS(julianday(b.posted_date) - julianday(a.posted_date)) <= 2
        JOIN account acc ON acc.id = a.account_id
        WHERE a.amount_cents < 0
          -- one synthetic, one real
          AND (
              (
                (a.ynab_txn_id IS NULL OR a.ynab_txn_id LIKE 'ledger:%')
                AND b.ynab_txn_id IS NOT NULL
                AND b.ynab_txn_id NOT LIKE 'ledger:%'
              )
              OR
              (
                (b.ynab_txn_id IS NULL OR b.ynab_txn_id LIKE 'ledger:%')
                AND a.ynab_txn_id IS NOT NULL
                AND a.ynab_txn_id NOT LIKE 'ledger:%'
              )
          )
        ORDER BY a.posted_date DESC, a.amount_cents
    """).fetchall()

print(f"found {len(rows)} duplicate pairs")
print()
for r in rows[:40]:
    print(f"  ${-r['amount_cents']/100:>9,.2f}  {r['account_name'][:18]:18s}")
    print(f"    #{r['id_a']:>5}  {r['dt_a']}  {(r['payee_a'] or '')[:32]:32s}  yid={(r['yid_a'] or 'NULL')[:14]}")
    print(f"    #{r['id_b']:>5}  {r['dt_b']}  {(r['payee_b'] or '')[:32]:32s}  yid={(r['yid_b'] or 'NULL')[:14]}")
    if r['memo_a']:
        print(f"      memo (A): {r['memo_a'][:80]}")
    if r['memo_b']:
        print(f"      memo (B): {r['memo_b'][:80]}")
    print()
