import sqlite3
con = sqlite3.connect('ynab_helper.db')
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT id, name, type FROM account WHERE name LIKE '%Rainy%' OR name LIKE '%Marcus%' OR name LIKE '%Savings%' OR name LIKE '%Goldman%'"
).fetchall()
for r in rows:
    print(dict(r))
print('---')
print('latest observed:')
for r in con.execute(
    "SELECT account_id, as_of_date, balance_cents FROM account_balance_observed ORDER BY as_of_date DESC LIMIT 20"
).fetchall():
    print(dict(r))
