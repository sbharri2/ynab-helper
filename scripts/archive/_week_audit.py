"""Audit last week's bot activity.

Goals:
  1. How many items did the bot try to categorize, and via which path?
       - override (rule-based)
       - prior (historical bypass)
       - matched_order (Phase 2 enrichment)
       - LLM
  2. Which of those did the user CONFIRM vs OVERRIDE vs SKIP?
       The "user override" cases are the bot's mistakes — items where
       it suggested X but the user typed/tapped Y.
  3. Which payees had a clear "should-have-known" miss (the override map
       or strong prior would have produced the right answer but didn't run)?
  4. Surface counts and the most damaging errors.
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from bot import storage
from bot.payee_overrides import resolve_payee_override

DB = "ynab_helper.db"
CUTOFF = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

with storage.connect(DB) as con:
    cat_name = {
        r["id"]: r["name"]
        for r in con.execute("SELECT id, name FROM category").fetchall()
    }

    # ----------------------------------------------------------------
    # 1) Categorization PATHS taken in the last 7 days
    # ----------------------------------------------------------------
    paths = Counter()
    path_events = defaultdict(list)
    rows = con.execute(
        "SELECT event, details, ts FROM audit_log "
        "WHERE event IN ('categorize_via_override','categorize_via_prior',"
        "                'categorize_via_matched_order','ingest_enriched_from_order')"
        "  AND ts >= ? ORDER BY id",
        (CUTOFF,),
    ).fetchall()
    for r in rows:
        paths[r["event"]] += 1
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {}
        path_events[r["event"]].append((d, r["ts"]))

    print("=" * 78)
    print("CATEGORIZATION PATHS (last 7 days)")
    print("=" * 78)
    for path, n in paths.most_common():
        print(f"  {path:<35} {n:>4}")

    # ----------------------------------------------------------------
    # 2) USER CATEGORIZATION CHOICES — confirms vs overrides
    # ----------------------------------------------------------------
    print("\n" + "=" * 78)
    print("USER CONFIRMATIONS in last 7 days")
    print("=" * 78)
    cat_events = con.execute(
        "SELECT details, ts FROM audit_log "
        "WHERE event = 'categorized' AND ts >= ? "
        "ORDER BY id DESC",
        (CUTOFF,),
    ).fetchall()
    print(f"  total user categorize taps:  {len(cat_events)}")

    # Pair each user choice with the row's original suggestion
    confirmed_match = 0
    overrode_suggestion = 0
    no_suggestion_typed = 0
    mismatches: list[dict] = []
    for ev in cat_events:
        try:
            d = json.loads(ev["details"] or "{}")
        except Exception:
            continue
        if d.get("kind") != "txn":
            continue
        pt_id = d.get("id")
        chosen = d.get("category")
        row = con.execute(
            "SELECT id, payee, amount_cents, txn_date, raw_summary, suggested_category "
            "FROM pending_txn WHERE id = ?",
            (pt_id,),
        ).fetchone()
        if not row:
            continue
        suggested = row["suggested_category"]
        if not suggested:
            no_suggestion_typed += 1
            continue
        if suggested == chosen:
            confirmed_match += 1
        else:
            overrode_suggestion += 1
            mismatches.append({
                "pt_id": pt_id,
                "payee": row["payee"],
                "amount": row["amount_cents"],
                "date": row["txn_date"],
                "suggested": cat_name.get(suggested, suggested[:8] if suggested else "(none)"),
                "chosen": cat_name.get(chosen, chosen[:8] if chosen else "(none)"),
                "raw_summary": (row["raw_summary"] or "")[:70],
                "ts": ev["ts"],
            })

    print(f"  confirmed bot's suggestion:  {confirmed_match}")
    print(f"  user overrode the bot:       {overrode_suggestion}")
    print(f"  no-suggestion + user typed:  {no_suggestion_typed}")

    accuracy = (confirmed_match / max(1, confirmed_match + overrode_suggestion))
    print(f"\n  bot accuracy (where it guessed): {accuracy*100:.0f}%")

    # ----------------------------------------------------------------
    # 3) SKIP / EXPIRE events — items the user gave up on
    # ----------------------------------------------------------------
    skip_events = con.execute(
        "SELECT details, ts FROM audit_log "
        "WHERE event IN ('skipped','ignored_dm_skipped') AND ts >= ? "
        "ORDER BY id DESC",
        (CUTOFF,),
    ).fetchall()
    print(f"\n  user skips / TTL-expired:    {len(skip_events)}")

    # ----------------------------------------------------------------
    # 4) THE BOT'S MISTAKES — items it suggested wrong
    # ----------------------------------------------------------------
    print("\n" + "=" * 78)
    print("BOT'S MISTAKES — suggested vs what the user actually picked")
    print("=" * 78)
    if not mismatches:
        print("  (none in window — every guess was confirmed)")
    for m in mismatches[:30]:
        amt = m["amount"] / 100
        print(f"  {str(m['date'])[:10]} ${amt:+8.2f}  {m['payee'][:32]:<32}  "
              f"sug={m['suggested'][:24]:<24} -> picked={m['chosen'][:24]}")

    # ----------------------------------------------------------------
    # 5) "SHOULD HAVE KNOWN" check — pendings with NULL suggestion that
    #    the override map OR a strong prior would have caught.
    # ----------------------------------------------------------------
    print("\n" + "=" * 78)
    print("'SHOULD HAVE KNOWN' — NULL-suggestion rows the bot could have caught")
    print("=" * 78)
    null_rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, status "
        "FROM pending_txn WHERE suggested_category IS NULL "
        "  AND created_at >= ? "
        "ORDER BY id",
        (CUTOFF,),
    ).fetchall()
    print(f"  NULL-suggestion rows in window:  {len(null_rows)}")
    fixable = 0
    for r in null_rows:
        payee = r["payee"] or ""
        ov = resolve_payee_override(DB, payee)
        if ov:
            print(f"    pt#{r['id']:>4}  '{payee[:30]:<30}'  "
                  f"override -> {ov['category_name']}")
            fixable += 1
            continue
        sp = storage.get_strongest_payee_category(DB, payee)
        if sp:
            print(f"    pt#{r['id']:>4}  '{payee[:30]:<30}'  "
                  f"prior {sp['pct']*100:.0f}% -> {sp['category_name']}")
            fixable += 1
    print(f"\n  fixable via override/prior:  {fixable} / {len(null_rows)}")

    # ----------------------------------------------------------------
    # 6) THE LLM PATH — how often was it consulted, what did it produce?
    # ----------------------------------------------------------------
    # Rows that took the LLM path are those where a new pending_txn got
    # a suggestion but NO categorize_via_* audit event fired in the same
    # ingest. Approximate.
    print("\n" + "=" * 78)
    print("PATHS BY VOLUME (override + prior + match are deterministic; rest is LLM)")
    print("=" * 78)
    new_pts = con.execute(
        "SELECT COUNT(*) AS n FROM pending_txn "
        "WHERE created_at >= ? AND suggested_category IS NOT NULL",
        (CUTOFF,),
    ).fetchone()
    deterministic = sum(paths[k] for k in paths
                        if k in ('categorize_via_override',
                                  'categorize_via_prior',
                                  'categorize_via_matched_order'))
    llm_estimate = max(0, new_pts["n"] - deterministic)
    print(f"  total new pending_txns w/ suggestion : {new_pts['n']}")
    print(f"  via deterministic paths              : {deterministic}")
    print(f"  via LLM (estimate)                   : {llm_estimate}")
