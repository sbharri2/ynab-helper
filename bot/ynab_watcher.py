"""YNAB poller: match new charges to pending orders, enqueue the rest."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from bot import storage
from bot.config import Settings
from bot.matcher import find_best_match
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)

PAYEE_AMAZON_RE = re.compile(r"\bAMAZON", re.I)
PAYEE_VENMO_RE = re.compile(r"\bVENMO", re.I)


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (matches storage._utcnow pattern)."""
    return datetime.now(timezone.utc)


def _is_amazon_or_venmo(payee: str) -> str | None:
    if PAYEE_AMAZON_RE.search(payee or ""):
        return "amazon"
    if PAYEE_VENMO_RE.search(payee or ""):
        return "venmo"
    return None


def poll_once(settings: Settings) -> dict:
    """Returns {matched: N, enqueued: M, expired: E}."""
    storage.init_db(settings.paths.database)
    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)

    txns = ynab.list_uncategorized()
    pending_orders = storage.list_unmatched_amazon_orders(settings.paths.database)

    matched = 0
    enqueued = 0

    for txn in txns:
        source = _is_amazon_or_venmo(txn["payee"])
        if source in {"amazon", "venmo"}:
            candidates = [o for o in pending_orders if o["source"] == source]
            best = find_best_match(candidates, txn, source=source)
            if best is None:
                _enqueue(settings, txn)
                enqueued += 1
                continue
            category_id = best["chosen_category"]
            if not category_id:
                continue
            try:
                ynab.set_category(txn["ynab_txn_id"], category_id)
                storage.record_match(
                    settings.paths.database,
                    pending_order_id=best["id"],
                    ynab_txn_id=txn["ynab_txn_id"],
                )
                storage.audit(
                    settings.paths.database, "matched",
                    {"order_id": best["id"], "txn": txn["ynab_txn_id"]},
                )
                matched += 1
            except Exception as e:
                log.error("set_category failed: %s", e)
        else:
            _enqueue(settings, txn)
            enqueued += 1

    expired = _expire_stale_orders(settings.paths.database, days=30)
    storage.audit(settings.paths.database, "ynab_poll",
                  {"matched": matched, "enqueued": enqueued, "expired": expired})
    return {"matched": matched, "enqueued": enqueued, "expired": expired}


def _enqueue(settings: Settings, txn: dict) -> None:
    storage.insert_pending_txn(
        settings.paths.database,
        user_id=settings.gmail_accounts[0].user_id,
        ynab_txn_id=txn["ynab_txn_id"],
        ynab_account_id=txn["ynab_account_id"],
        payee=txn["payee"],
        amount_cents=txn["amount_cents"],
        txn_date=txn["txn_date"],
        memo=txn["memo"],
    )


def _expire_stale_orders(db_path, *, days: int = 30) -> int:
    """Transition categorized-but-never-matched orders to status='expired' after N days."""
    now = _utcnow()
    cutoff = now - timedelta(days=days)
    with storage.connect(db_path) as con:
        cur = con.execute(
            """UPDATE pending_order
               SET status = 'expired', updated_at = ?
               WHERE status = 'categorized'
                 AND id NOT IN (SELECT pending_order_id FROM matched_charge)
                 AND created_at < ?""",
            (now, cutoff),
        )
        return cur.rowcount


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from bot.config import load_settings
    result = poll_once(load_settings())
    log.info("ynab_watcher: %s", result)
