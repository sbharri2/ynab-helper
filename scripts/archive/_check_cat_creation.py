"""Check when the categories used today were created (to confirm cache miss)."""
from bot import storage

USED = {
    "e593c118-3b44-4834-be57-a62fb8a1a09b": "category for #922",
    "05049099-1e98-42cd-8b29-0d54b7e59b72": "category for #923",
    "b5527483-247d-4997-8f0d-55cba9d58794": "category for #921",
    "f4b8c004-92d9-4aa4-bef9-96f097fe2586": "category for #924-928",
    "f1033dd3-4fc4-410e-b75f-0fa6b902d624": "category for #929",
}

with storage.connect("ynab_helper.db") as con:
    for cid, why in USED.items():
        r = con.execute(
            "SELECT id, name, group_id, is_spending, ynab_category_id "
            "FROM category WHERE id = ?",
            (cid,),
        ).fetchone()
        if r is None:
            print(f"  {cid[:8]}  (NOT FOUND)  — {why}")
            continue
        d = dict(r)
        ynab_tag = "ynab_id=" + (d["ynab_category_id"][:8] if d["ynab_category_id"] else "NONE")
        print(f"  {cid[:8]} | {d['name']:<30} | is_spending={d['is_spending']}  {ynab_tag}  — {why}")

    print("\nCategories created via agent today (audit log):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event LIKE 'category_%' OR event LIKE 'create_category%' "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {r['ts']} {r['event']}: {r['details']}")
