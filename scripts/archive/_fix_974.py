"""Retroactively apply Phase 2 enrichment + category to pending_txn #974
($10.71 Chase Amazon 6/15 → cat water fountain), since it ingested
before the at-ingest enrichment code shipped.
"""
from datetime import date
from bot import storage
from bot.ingest import _enrich_from_pending_order

with storage.connect("ynab_helper.db") as con:
    # Look up #28's chosen category name
    cn = con.execute(
        "SELECT c.name FROM category c "
        "JOIN pending_order o ON o.chosen_category = c.id "
        "WHERE o.id = 28"
    ).fetchone()
    print(f"pending_order #28 was categorized as: {cn['name'] if cn else '(none)'}")

# Simulate the Phase 2 enrichment that would have fired
parsed = {
    "summary": "Chase $10.71 at AMAZON MKTPLACE PMTS on 2026-06-15",
    "amount_cents": -1071,
    "posted_date": date(2026, 6, 15),
}
matched = _enrich_from_pending_order("ynab_helper.db", parsed, "AMAZON MKTPLACE PMTS")
print(f"\nmatched_order: {matched['id'] if matched else None}")
print(f"enriched summary: {parsed.get('summary')!r}")
print(f"order chosen_category: {matched.get('chosen_category') if matched else None}")

if matched:
    new_summary = parsed["summary"]
    new_cat = matched.get("chosen_category")
    with storage.connect("ynab_helper.db") as con:
        # Apply both the rich summary AND the order's chosen category
        if new_cat:
            con.execute(
                "UPDATE pending_txn SET raw_summary = ?, suggested_category = ? "
                "WHERE id = 974",
                (new_summary, new_cat),
            )
        else:
            con.execute(
                "UPDATE pending_txn SET raw_summary = ? WHERE id = 974",
                (new_summary,),
            )
        r = con.execute(
            "SELECT raw_summary, suggested_category FROM pending_txn WHERE id = 974"
        ).fetchone()
        print(f"\nafter: raw_summary={r['raw_summary']!r}")
        print(f"       suggested_category={r['suggested_category']}")
        cn = con.execute(
            "SELECT name FROM category WHERE id = ?", (r["suggested_category"],),
        ).fetchone()
        print(f"       category name: {cn['name'] if cn else '(none)'}")

storage.audit("ynab_helper.db", "retroactive_phase2", {"pt_id": 974, "matched_order": 28})
print("done")
