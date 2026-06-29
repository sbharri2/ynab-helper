"""Verify the new matcher handles typos + aliases."""
from bot.telegram_bot import _match_category_by_name
from bot import storage

with storage.connect("ynab_helper.db") as con:
    cats = [{"id": r["id"], "name": r["name"]}
            for r in con.execute("SELECT id, name FROM category WHERE hidden=0").fetchall()]

TESTS = [
    "Read to assign",     # typo of Ready to Assign
    "ready to assign",    # alias
    "rta",                # alias
    "inflow",             # alias
    "income",             # alias
    "Dining",             # alias
    "groceries",          # exact spending category
    "dining out",         # substring
    "exrcise",            # typo of Exercise
    "Househld Items",     # typo
    "asdfqwerty",         # nothing
    "Allison Reimb",      # substring (used to be the wrong choice for Read to assign)
]

with storage.connect("ynab_helper.db") as con:
    id_to_name = {r["id"]: r["name"]
                  for r in con.execute("SELECT id, name FROM category").fetchall()}

for q in TESTS:
    cid = _match_category_by_name("ynab_helper.db", q, cats)
    name = id_to_name.get(cid, "(none)") if cid else "(none)"
    print(f"  {q!r:<30} -> {name}")
