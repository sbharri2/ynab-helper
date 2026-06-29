"""Show what's in the bot's pending queue right now."""
import sqlite3

con = sqlite3.connect("ynab_helper.db")
con.row_factory = sqlite3.Row

print("=== pending_txn schema ===")
cols = con.execute("PRAGMA table_info(pending_txn)").fetchall()
for c in cols:
    print(f"  {c['name']:<25} {c['type']}")
print()

print("=== pending_txn (status='pending') ===")
rows = con.execute("SELECT * FROM pending_txn WHERE status='pending'").fetchall()
print(f"count: {len(rows)}")
for r in rows[:50]:
    d = dict(r)
    print(d)

print()
print("=== pending_order schema ===")
cols = con.execute("PRAGMA table_info(pending_order)").fetchall()
for c in cols:
    print(f"  {c['name']:<25} {c['type']}")
print()
print("=== pending_order (status='pending') ===")
rows = con.execute("SELECT * FROM pending_order WHERE status='pending'").fetchall()
print(f"count: {len(rows)}")
for r in rows[:50]:
    print(dict(r))
