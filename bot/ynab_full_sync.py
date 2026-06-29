"""Daily full-sync from YNAB into the local ledger.

Why this exists: ``bot/ynab_watcher.py`` only pulls *uncategorized* YNAB
transactions (so the bot can ask Steven to categorize them). For
accounts that auto-categorize in YNAB (Business Checking, certain
recurring debits), transactions are categorized BEFORE the watcher ever
sees them, so they never enter the local ledger. After a few weeks the
ledger drifts and the reconciler reports phantom mismatches that are
really just "bot is behind YNAB".

The full-sync runs once a day, pulls every non-deleted YNAB transaction
since the previous sync, upserts each into ``ledger_txn`` (keyed by
``ynab_txn_id``), and refreshes ``account.balance_cents`` so the
reconciler has a fresh anchor.

This is the long-term mechanism that lets the bot run **in parallel
with YNAB** while Steven verifies the ledger over several months before
turning YNAB off entirely.

Design choices:
  - YNAB is the source of truth for fields it owns: amount, date, payee,
    memo, category, cleared, transfer_account_id.
  - Local-only fields (source_signal, source_email_id) are preserved on
    update.
  - Transfers ARE included (one row per side) so account balance math
    closes; the import-history path already handles transfers correctly.
  - ``since_date`` defaults to (last_sync_date - 7 days) to forgive any
    late YNAB edits on previously-pulled rows.
  - Audit log records ``ynab_full_sync_run`` with counts.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from bot import storage
from bot.config import Settings
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


def _last_sync_date(db_path: Path | str) -> date | None:
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT MAX(ts) AS ts FROM audit_log "
            "WHERE event = 'ynab_full_sync_run'"
        ).fetchone()
    ts = row["ts"] if row else None
    if not ts:
        return None
    try:
        return date.fromisoformat(str(ts)[:10])
    except ValueError:
        return None


def _resolve_local_account_id(db_path: Path | str,
                              ynab_account_id: str) -> str | None:
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT id FROM account WHERE ynab_account_id = ?",
            (ynab_account_id,),
        ).fetchone()
    return row["id"] if row else None


def _resolve_local_category_id(db_path: Path | str,
                               ynab_category_id: str | None) -> str | None:
    if not ynab_category_id:
        return None
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT id FROM category WHERE ynab_category_id = ?",
            (ynab_category_id,),
        ).fetchone()
    return row["id"] if row else None


_GENERIC_PAYEE_WORDS = {
    "deposit", "withdrawal", "ach", "check", "purchase", "debit", "credit",
    "pos", "atm", "transfer", "usa", "the", "inc", "llc",
}


def _payee_tokens(payee: str | None) -> set[str]:
    """Merchant-name tokens (len ≥ 4, generic banking words stripped)."""
    if not payee:
        return set()
    out: set[str] = set()
    cur = ""
    for ch in payee.lower():
        if ch.isalnum():
            cur += ch
        else:
            if len(cur) >= 4 and cur not in _GENERIC_PAYEE_WORDS:
                out.add(cur)
            cur = ""
    if len(cur) >= 4 and cur not in _GENERIC_PAYEE_WORDS:
        out.add(cur)
    return out


def _find_adoptable_row(con, *, account_id: str, amount_cents: int,
                        txn_date, payee: str | None):
    """Find an existing CC/bank-alert row that is the SAME real charge as the
    incoming YNAB transaction, so the sync adopts it instead of inserting a
    duplicate.

    Two passes, both guarded so we never collapse two distinct charges:

      1. EXACT amount, widened to ±10 days — covers the common case where
         YNAB posts a charge several days after the instant alert (Amazon
         ship→post, foreign-card settlement lag).
      2. TIP / FX drift — outflow only, existing pre-settlement amount up to
         40% smaller than YNAB's settled amount, within ±7 days, AND a shared
         merchant token (so a $50 dinner can't adopt a $55 unrelated charge).

    Safe by construction: each pass adopts ONLY when exactly one unclaimed
    candidate is in range. If two plausible rows exist (e.g. two identical
    $50 fills a week apart) we refuse and let a duplicate be created — a
    visible, cleanable duplicate beats a silent wrong merge.
    """
    exact = con.execute(
        "SELECT id, category_id FROM ledger_txn "
        "WHERE account_id = ? AND amount_cents = ? "
        "  AND ABS(julianday(posted_date) - julianday(?)) <= 10 "
        "  AND (ynab_txn_id IS NULL OR ynab_txn_id LIKE 'ledger:%')",
        (account_id, amount_cents, txn_date),
    ).fetchall()
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # ambiguous — don't guess

    if amount_cents >= 0:
        return None  # tips/FX only apply to outflows
    settled = abs(amount_cents)
    lo = int(settled / 1.4)
    cands = con.execute(
        "SELECT id, category_id, payee FROM ledger_txn "
        "WHERE account_id = ? AND amount_cents < 0 "
        "  AND ABS(amount_cents) BETWEEN ? AND ? "
        "  AND ABS(julianday(posted_date) - julianday(?)) <= 7 "
        "  AND (ynab_txn_id IS NULL OR ynab_txn_id LIKE 'ledger:%')",
        (account_id, lo, settled, txn_date),
    ).fetchall()
    toks = _payee_tokens(payee)
    hits = [c for c in cands if toks & _payee_tokens(c["payee"])]
    return hits[0] if len(hits) == 1 else None


def _upsert_ledger_txn(db_path: Path | str, *, ytx: dict,
                       local_account_id: str,
                       local_category_id: str | None,
                       is_split: bool = False) -> tuple[str, int]:
    """Upsert one YNAB-side transaction into ``ledger_txn``.

    ``is_split`` marks this as a split parent: its category is forced NULL
    (the children carry the categories + the money) and ``is_split=1`` so
    money-math sums can exclude it.

    Returns ``(action, ledger_txn_id)`` where action is ``'new'`` or
    ``'updated'`` and ledger_txn_id is the local row id (needed to attach
    split children).
    """
    payee = (ytx.get("payee") or "")[:200]
    memo = (ytx.get("memo") or "")[:500]
    cleared = ytx.get("cleared") or "uncleared"
    txn_date = ytx["txn_date"]
    amount_cents = int(ytx["amount_cents"])
    ynab_txn_id = ytx["ynab_txn_id"]
    dedupe_key = f"{local_account_id}|{txn_date}|{abs(amount_cents)}"
    # YNAB's transfer link — both fields are None for non-transfer txns.
    # transfer_account_id is the other account's YNAB id; we need to map
    # it to the local account.id (same lookup as the row's own account).
    ynab_transfer_acct = ytx.get("transfer_account_id")
    local_transfer_acct: str | None = None
    if ynab_transfer_acct:
        local_transfer_acct = _resolve_local_account_id(
            db_path, ynab_transfer_acct
        )
    transfer_txn_id = ytx.get("transfer_transaction_id")

    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT id, category_id FROM ledger_txn WHERE ynab_txn_id = ?",
            (ynab_txn_id,),
        ).fetchone()
        # ADOPTION: if no row matches the real UUID, look for an existing
        # row that came from a CC/bank alert (synthetic ynab_txn_id / NULL)
        # representing the same real charge — YNAB just posted it days after
        # the alert hit the inbox, sometimes at a tip/FX-adjusted amount.
        # Without this we'd INSERT a duplicate. _find_adoptable_row widens
        # the old ±2-day/exact-amount window to ±10d exact + ±7d tip-tolerant,
        # but only adopts when exactly one unclaimed candidate exists.
        if not row:
            adopt = _find_adoptable_row(
                con, account_id=local_account_id, amount_cents=amount_cents,
                txn_date=txn_date, payee=payee,
            )
            if adopt:
                row = adopt
        if row:
            # NEVER overwrite a local category with YNAB's null. The bot
            # writes its decisions to local ledger_txn at categorize-tap
            # time (via _apply_choice); the daily ynab_writer pushes them
            # to YNAB the next morning. Between those two events, YNAB
            # still shows uncategorized — if the 6h sync runs in that
            # window and we blindly write local_category_id=None, the
            # bot's choice gets wiped and envelope math goes wrong until
            # the writer fires and the next sync reads it back.
            #
            # The fix: when local has a non-null category and YNAB has
            # null, keep the local one. When YNAB has a non-null
            # category, that's authoritative (could be a manual YNAB
            # edit; conflict policy is "bot wins" but the writer handles
            # that the next morning by re-pushing the bot's choice).
            # A split parent never carries a single category — force NULL so
            # its children own the money. Otherwise apply the usual guard:
            # keep the local category when YNAB reports null (the bot's
            # not-yet-pushed decision), else take YNAB's value.
            if is_split:
                effective_cat = None
            elif local_category_id is None and row["category_id"] is not None:
                effective_cat = row["category_id"]
            else:
                effective_cat = local_category_id
            # ynab_txn_id needs writing too: in the adoption path the
            # existing row had NULL or a synthetic ledger:N id and we're
            # now attaching the real YNAB UUID.
            con.execute(
                """UPDATE ledger_txn SET
                     account_id = ?, posted_date = ?, amount_cents = ?,
                     payee = ?, memo = ?, category_id = ?, cleared = ?,
                     dedupe_key = ?, ynab_txn_id = ?,
                     transfer_account_id = ?, transfer_transaction_id = ?,
                     is_split = ?,
                     updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (local_account_id, txn_date, amount_cents, payee, memo,
                 effective_cat, cleared, dedupe_key, ynab_txn_id,
                 local_transfer_acct, transfer_txn_id,
                 1 if is_split else 0, row["id"]),
            )
            return "updated", int(row["id"])
        cur = con.execute(
            """INSERT INTO ledger_txn
                 (account_id, posted_date, amount_cents, payee, memo,
                  category_id, cleared, source_signal, source_email_id,
                  ynab_txn_id, dedupe_key,
                  transfer_account_id, transfer_transaction_id, is_split)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'ynab_sync', NULL, ?, ?, ?, ?, ?)""",
            (local_account_id, txn_date, amount_cents, payee, memo,
             None if is_split else local_category_id, cleared, ynab_txn_id,
             dedupe_key, local_transfer_acct, transfer_txn_id,
             1 if is_split else 0),
        )
        return "new", int(cur.lastrowid)


def _sync_split_children(db_path: Path | str, *, parent_local_id: int,
                         ytx: dict, local_account_id: str) -> int:
    """Mirror a split's subtransactions as child ``ledger_txn`` rows.

    Each child is keyed by its YNAB subtransaction id (UNIQUE), carries
    its own category + portion of the amount, points at the parent via
    ``parent_txn_id``, and inherits the parent's date + cleared status.
    Children that YNAB no longer reports (the user removed a split line)
    are deleted. Passing a txn with no subtransactions deletes any
    children left over from a since-un-split transaction.

    Returns the number of child rows upserted.
    """
    subs = ytx.get("subtransactions") or []
    txn_date = ytx["txn_date"]
    cleared = ytx.get("cleared") or "uncleared"
    upserted = 0
    current_uuids: list[str] = []
    with storage.connect(db_path) as con:
        for st in subs:
            sub_uuid = st["ynab_txn_id"]
            current_uuids.append(sub_uuid)
            child_cat = _resolve_local_category_id(
                db_path, st.get("ynab_category_id"))
            ynab_transfer = st.get("transfer_account_id")
            local_transfer = (
                _resolve_local_account_id(db_path, ynab_transfer)
                if ynab_transfer else None
            )
            payee = (st.get("payee") or "")[:200]
            memo = (st.get("memo") or "")[:500]
            amount_cents = int(st["amount_cents"])
            existing = con.execute(
                "SELECT id FROM ledger_txn WHERE ynab_txn_id = ?",
                (sub_uuid,),
            ).fetchone()
            if existing:
                con.execute(
                    """UPDATE ledger_txn SET
                         account_id = ?, posted_date = ?, amount_cents = ?,
                         payee = ?, memo = ?, category_id = ?, cleared = ?,
                         parent_txn_id = ?, is_split = 0,
                         transfer_account_id = ?, transfer_transaction_id = ?,
                         updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (local_account_id, txn_date, amount_cents, payee, memo,
                     child_cat, cleared, parent_local_id, local_transfer,
                     st.get("transfer_transaction_id"), existing["id"]),
                )
            else:
                con.execute(
                    """INSERT INTO ledger_txn
                         (account_id, posted_date, amount_cents, payee, memo,
                          category_id, cleared, source_signal, source_email_id,
                          ynab_txn_id, dedupe_key, parent_txn_id, is_split,
                          transfer_account_id, transfer_transaction_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'ynab_sync', NULL,
                               ?, NULL, ?, 0, ?, ?)""",
                    (local_account_id, txn_date, amount_cents, payee, memo,
                     child_cat, cleared, sub_uuid, parent_local_id,
                     local_transfer, st.get("transfer_transaction_id")),
                )
            upserted += 1
        # Prune children that are no longer part of this split.
        if current_uuids:
            ph = ",".join("?" * len(current_uuids))
            con.execute(
                f"DELETE FROM ledger_txn WHERE parent_txn_id = ? "
                f"AND ynab_txn_id NOT IN ({ph})",
                (parent_local_id, *current_uuids),
            )
        else:
            con.execute(
                "DELETE FROM ledger_txn WHERE parent_txn_id = ?",
                (parent_local_id,),
            )
    return upserted


# YNAB account types (camelCase) → the bot's account.type CHECK values.
_YNAB_TYPE_MAP = {
    "creditCard": "credit_card", "lineOfCredit": "line_of_credit",
    "otherAsset": "other_asset", "otherLiability": "other_liability",
    "autoLoan": "other_liability", "studentLoan": "other_liability",
    "personalLoan": "other_liability", "medicalDebt": "other_liability",
    "mortgage": "other_liability",
}
_ALLOWED_ACCT_TYPES = {
    "checking", "savings", "credit_card", "tracking", "cash",
    "line_of_credit", "other_asset", "other_liability",
}


def _map_account_type(ynab_type: str, on_budget: bool) -> str:
    t = _YNAB_TYPE_MAP.get(ynab_type, ynab_type)
    if t in _ALLOWED_ACCT_TYPES:
        return t
    return "other_asset" if on_budget else "tracking"


def _refresh_account_balances(db_path: Path | str,
                              ynab_accounts: list[dict]) -> dict:
    """Mirror YNAB accounts into the local ``account`` table.

    For each YNAB account: update balance + cleared + on_budget if it
    already exists locally, otherwise INSERT it (keyed by the YNAB id,
    which doubles as the local id for sync-created accounts). This is how
    a newly-created YNAB account — e.g. an account flipped to Tracking via
    create-new + transfer — shows up in the bot without a manual import.

    Returns ``{"updated": n, "added": n}``.
    """
    updated = added = 0
    with storage.connect(db_path) as con:
        for a in ynab_accounts:
            on_b = 1 if a.get("on_budget", True) else 0
            row = con.execute(
                "SELECT id FROM account WHERE ynab_account_id = ?",
                (a["ynab_account_id"],),
            ).fetchone()
            if row:
                con.execute(
                    """UPDATE account SET
                         balance_cents = ?, cleared_balance_cents = ?,
                         on_budget = ?
                       WHERE id = ?""",
                    (a["balance_cents"], a["cleared_balance_cents"],
                     on_b, row["id"]),
                )
                updated += 1
            else:
                acct_type = _map_account_type(
                    a.get("type", "checking"), a.get("on_budget", True))
                con.execute(
                    """INSERT INTO account
                         (id, name, type, on_budget, ynab_account_id, closed,
                          balance_cents, cleared_balance_cents)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (a["ynab_account_id"], a["name"], acct_type, on_b,
                     a["ynab_account_id"], a["balance_cents"],
                     a["cleared_balance_cents"]),
                )
                added += 1
    return {"updated": updated, "added": added}


def full_sync(settings: Settings, *,
              since_date: date | None = None,
              overlap_days: int = 7) -> dict:
    """Mirror YNAB → local ledger. Returns counts.

    Arguments:
      since_date: if provided, only pull YNAB transactions since this
        date. Otherwise, defaults to (last_successful_run - overlap_days)
        — the overlap forgives YNAB edits on previously-synced txns.
      overlap_days: window of replay protection (see above). Defaults 7.
    """
    db_path = settings.paths.database
    if since_date is None:
        last = _last_sync_date(db_path)
        if last:
            since_date = last - timedelta(days=overlap_days)
        else:
            # First-ever run — let YNAB decide (defaults to all txns).
            since_date = None

    client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    log.info("ynab_full_sync: since_date=%s", since_date)

    # Step 1 — account balances + types refresh
    try:
        accts = client.list_accounts()
    except Exception as e:  # noqa: BLE001
        log.error("list_accounts failed: %s", e)
        accts = []
    acct_result = _refresh_account_balances(db_path, accts)
    accounts_refreshed = acct_result["updated"]
    accounts_added = acct_result["added"]
    log.info("ynab_full_sync: refreshed %d accounts, added %d new",
             accounts_refreshed, accounts_added)

    # Step 2 — full transaction list (all categorized + uncategorized)
    try:
        txns = client.list_all_transactions(since=since_date)
    except Exception as e:  # noqa: BLE001
        log.error("list_all_transactions failed: %s", e)
        txns = []
    new = updated = orphan = children = 0
    for ytx in txns:
        local_acct = _resolve_local_account_id(db_path, ytx["ynab_account_id"])
        if not local_acct:
            # No matching account in local — likely a closed/hidden one we
            # never imported. Skip but count for visibility.
            orphan += 1
            continue
        local_cat = _resolve_local_category_id(db_path, ytx.get("ynab_category_id"))
        is_split = bool(ytx.get("subtransactions"))
        action, parent_id = _upsert_ledger_txn(
            db_path,
            ytx=ytx,
            local_account_id=local_acct,
            local_category_id=local_cat,
            is_split=is_split,
        )
        # Always reconcile children: when split, upsert the current legs;
        # when not, a no-op delete cleans up any legs left from a prior
        # split (cheap — indexed on parent_txn_id).
        children += _sync_split_children(
            db_path,
            parent_local_id=parent_id,
            ytx=ytx,
            local_account_id=local_acct,
        )
        if action == "new":
            new += 1
        else:
            updated += 1

    summary = {
        "since_date": str(since_date) if since_date else None,
        "accounts_refreshed": accounts_refreshed,
        "accounts_added": accounts_added,
        "txns_pulled": len(txns),
        "txns_new": new,
        "txns_updated": updated,
        "txns_orphan": orphan,
        "split_children": children,
    }
    storage.audit(db_path, "ynab_full_sync_run", summary)
    log.info("ynab_full_sync: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from bot.config import load_settings
    full_sync(load_settings())
