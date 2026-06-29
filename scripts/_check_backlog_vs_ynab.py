"""READ-ONLY diagnostic: are the bot's pending_txn rows still uncategorized
in YNAB *right now*?

For every pending_txn row (status='pending') we look up its ynab_txn_id in a
fresh YNAB pull and classify the live state:

  still_uncat   - category_id is null, no subtransactions -> genuinely uncategorized
  split_cat     - category_id is null BUT has subtransactions -> categorized via split
                  (shows as categorized in the YNAB app; bot can't see it)
  categorized   - has a real category_id now (user categorized it directly)
  transfer      - became/transfer; not an expense
  not_in_ynab   - id missing from the pull (deleted, or outside since-window)

Only `still_uncat` rows are legitimately part of the backlog. Everything else
is stale and should be reconciled out.

No writes. Run from repo root:
    python -m scripts._check_backlog_vs_ynab
"""
from __future__ import annotations

from datetime import timedelta

import ynab

from bot import storage
from bot.config import load_settings


def main() -> None:
    settings = load_settings()
    db_path = settings.paths.database

    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id, ynab_txn_id, payee, amount_cents, txn_date, "
            "       queue_lane, suggested_category "
            "FROM pending_txn WHERE status = 'pending' "
            "ORDER BY txn_date ASC, id ASC"
        ).fetchall()
    rows = [dict(r) for r in rows]
    print(f"pending_txn rows with status='pending': {len(rows)}")
    if not rows:
        return

    oldest = min(r["txn_date"] for r in rows)
    since = oldest - timedelta(days=3)
    print(f"pulling YNAB transactions since {since} ...")

    cfg = ynab.Configuration(access_token=settings.ynab_token)
    live: dict[str, object] = {}
    with ynab.ApiClient(cfg) as api_client:
        api = ynab.TransactionsApi(api_client)
        resp = api.get_transactions(
            plan_id=settings.ynab.budget_id, since_date=since,
        )
        for t in resp.data.transactions:
            live[str(t.id)] = t
    print(f"pulled {len(live)} live YNAB transactions\n")

    buckets: dict[str, list[dict]] = {
        "still_uncat": [], "split_cat": [], "categorized": [],
        "transfer": [], "not_in_ynab": [],
    }

    for r in rows:
        t = live.get(r["ynab_txn_id"])
        if t is None or getattr(t, "deleted", False):
            buckets["not_in_ynab"].append(r)
            continue
        if t.transfer_account_id is not None:
            buckets["transfer"].append(r)
            continue
        subs = [s for s in (t.subtransactions or []) if not getattr(s, "deleted", False)]
        if t.category_id is not None:
            cat = t.category_name or ""
            if cat.lower() == "uncategorized":
                buckets["still_uncat"].append(r)
            else:
                r["_live_cat"] = cat
                buckets["categorized"].append(r)
        elif subs:
            r["_split_n"] = len(subs)
            buckets["split_cat"].append(r)
        else:
            buckets["still_uncat"].append(r)

    print("=" * 64)
    print("SUMMARY (live YNAB state of the bot's backlog)")
    print("=" * 64)
    for k in ("still_uncat", "split_cat", "categorized", "transfer", "not_in_ynab"):
        print(f"  {k:14s} {len(buckets[k]):4d}")
    stale = (len(buckets["split_cat"]) + len(buckets["categorized"])
             + len(buckets["transfer"]) + len(buckets["not_in_ynab"]))
    print(f"  {'-'*20}")
    print(f"  genuinely uncategorized : {len(buckets['still_uncat'])}")
    print(f"  STALE (not uncat in YNAB): {stale}")
    print()

    def dump(title: str, key: str, extra: str | None = None) -> None:
        b = buckets[key]
        if not b:
            return
        print(f"\n--- {title} ({len(b)}) ---")
        for r in b:
            tail = ""
            if extra and extra in r:
                tail = f"  [{r[extra]}]"
            print(f"  {r['txn_date']}  ${r['amount_cents']/100:>9.2f}  "
                  f"{(r['payee'] or '')[:32]:32s}  lane={r['queue_lane']}{tail}")

    dump("CATEGORIZED directly in YNAB (stale)", "categorized", "_live_cat")
    dump("SPLIT in YNAB - shows categorized in app (stale)", "split_cat", "_split_n")
    dump("Now a TRANSFER (stale)", "transfer")
    dump("NOT in YNAB pull - deleted or out-of-window", "not_in_ynab")


if __name__ == "__main__":
    main()
