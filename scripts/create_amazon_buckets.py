"""Create the per-person Amazon spending buckets in YNAB + local mirror.

Three categories under the existing "Personal Spending" group so they show up
in the Personal Spending tab (owner inferred from name):
    Amazon - Steven, Amazon - Allison, Amazon - Unassigned

is_spending=0: these are AUTO-assigned by the Amazon ingest pipeline, not the
LLM — keeping them out of the LLM's candidate pool prevents a non-Amazon charge
from being mislabeled into an Amazon bucket. They still get budget envelopes and
show in all spending analytics (which don't filter on is_spending).

Idempotent — skips categories that already exist by name.
"""
from __future__ import annotations
from bot import storage
from bot.config import load_settings
from bot.ynab_client import YnabClient

GROUP_NAME = "Personal Spending"
CATEGORIES = ["Amazon - Steven", "Amazon - Allison", "Amazon - Unassigned"]


def main() -> None:
    settings = load_settings()
    db = settings.paths.database
    yc = YnabClient(settings.ynab_token, settings.ynab.budget_id)

    with storage.connect(db) as con:
        row = con.execute(
            "SELECT id FROM category_group WHERE LOWER(name)=LOWER(?)",
            (GROUP_NAME,),
        ).fetchone()
    if not row:
        raise SystemExit(f"group {GROUP_NAME!r} not found locally")
    group_id = row["id"]
    print(f"group: {GROUP_NAME} ({group_id[:8]})")

    for name in CATEGORIES:
        with storage.connect(db) as con:
            exists = con.execute(
                "SELECT id FROM category WHERE LOWER(name)=LOWER(?)", (name,),
            ).fetchone()
        if exists:
            print(f"  exists: {name} ({exists['id'][:8]})")
            continue
        created = yc.create_category(name, group_id)
        with storage.connect(db) as con:
            con.execute(
                """INSERT OR REPLACE INTO category
                   (id, group_id, name, ynab_category_id, hidden, is_spending)
                   VALUES (?, ?, ?, ?, 0, 0)""",
                (created["id"], created["group_id"], created["name"],
                 created["id"]),
            )
        print(f"  created: {name} ({created['id'][:8]})")

    storage.audit(db, "amazon_buckets_created", {"categories": CATEGORIES})
    print("done.")


if __name__ == "__main__":
    main()
