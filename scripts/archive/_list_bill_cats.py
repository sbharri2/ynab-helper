from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("Non-spending categories (bills + named goals + savings):")
    for r in con.execute(
        "SELECT id, group_id, name FROM category "
        "WHERE is_spending=0 AND hidden=0 ORDER BY name"
    ).fetchall():
        d = dict(r)
        print(f"  {d['name']}")
