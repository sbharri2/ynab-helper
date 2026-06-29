"""Create the 'Personal Business' category group + two categories
(Steven Writing, Allison Cross Stitch) in YNAB and mirror them into the
local ledger so both YNAB and the Tauri app show them.

is_spending=1: side-business expenses are real outflows (and income nets in
activity, like reimbursables). Flip to 0 later if you'd rather exclude them
from spending analytics.

Idempotent — re-running skips anything that already exists by name. Run with
the bot stopped is ideal but not required (writes are tiny):
    python -m scripts.create_personal_business
"""
from __future__ import annotations

from bot import storage
from bot.config import load_settings
from bot.ynab_client import YnabClient

GROUP_NAME = "Personal Business"
CATEGORIES = ["Steven Writing", "Allison Cross Stitch"]


def main() -> None:
    settings = load_settings()
    db = settings.paths.database
    yc = YnabClient(settings.ynab_token, settings.ynab.budget_id)

    # 1) Group — reuse if it already exists locally.
    with storage.connect(db) as con:
        row = con.execute(
            "SELECT id, name FROM category_group WHERE LOWER(name)=LOWER(?)",
            (GROUP_NAME,),
        ).fetchone()
    if row:
        group_id = row["id"]
        print(f"group exists: {row['name']} ({group_id[:8]})")
    else:
        grp = yc.create_category_group(GROUP_NAME)
        group_id = grp["id"]
        with storage.connect(db) as con:
            nxt = con.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM category_group"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO category_group (id, name, sort_order, hidden) "
                "VALUES (?, ?, ?, 0)",
                (group_id, grp["name"], nxt),
            )
        print(f"created group in YNAB + local: {grp['name']} ({group_id[:8]})")

    # 2) Categories.
    for name in CATEGORIES:
        with storage.connect(db) as con:
            exists = con.execute(
                "SELECT id FROM category WHERE LOWER(name)=LOWER(?)", (name,),
            ).fetchone()
        if exists:
            print(f"  category exists: {name} ({exists['id'][:8]})")
            continue
        created = yc.create_category(name, group_id)
        with storage.connect(db) as con:
            con.execute(
                """INSERT OR REPLACE INTO category
                   (id, group_id, name, ynab_category_id, hidden, is_spending)
                   VALUES (?, ?, ?, ?, 0, 1)""",
                (created["id"], created["group_id"], created["name"],
                 created["id"]),
            )
        print(f"  created category in YNAB + local: {name} ({created['id'][:8]})")

    storage.audit(db, "personal_business_categories_created",
                  {"group": GROUP_NAME, "categories": CATEGORIES})
    print("done.")


if __name__ == "__main__":
    main()
