"""Apply the new override/prior to in-flight pending_txn rows whose
suggested_category is wrong or missing.

Sweeps all 'pending' rows. For each, if the override or strong prior
produces a different (or non-NULL) answer than what's currently stored,
overwrite the suggestion.
"""
from __future__ import annotations
from bot import storage
from bot.payee_overrides import resolve_payee_override

DB = "ynab_helper.db"

with storage.connect(DB) as con:
    cat_name = {r["id"]: r["name"]
                for r in con.execute("SELECT id, name FROM category").fetchall()}
    rows = con.execute(
        "SELECT id, payee, suggested_category, queue_lane FROM pending_txn "
        "WHERE status = 'pending'"
    ).fetchall()

    n_changed = 0
    for r in rows:
        payee = r["payee"] or ""
        new = None
        why = None
        ov = resolve_payee_override(DB, payee)
        if ov:
            new = ov["category_id"]
            why = f"override -> {ov['category_name']}"
        else:
            sp = storage.get_strongest_payee_category(DB, payee)
            if sp:
                new = sp["category_id"]
                why = f"prior {sp['pct']*100:.0f}% via {sp.get('prefix_used','?')!r} -> {sp['category_name']}"

        if not new:
            continue
        if r["suggested_category"] == new:
            continue
        old_name = cat_name.get(r["suggested_category"], "(none)")
        new_name = cat_name.get(new, "?")
        # Also promote HOLD -> HOT if we found a deterministic answer
        new_lane = r["queue_lane"]
        if new_lane == "hold":
            new_lane = "hot"
        con.execute(
            "UPDATE pending_txn SET suggested_category = ?, "
            "queue_lane = ?, lane_changed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (new, new_lane, r["id"]),
        )
        print(f"  pt#{r['id']:>4} {payee[:38]:<38}  "
              f"{old_name[:20]:<20} -> {new_name[:25]:<25}  ({why})")
        n_changed += 1

print(f"\nUpdated {n_changed} suggestions.")
storage.audit(DB, "backfill_override_v2", {"updated": n_changed})
