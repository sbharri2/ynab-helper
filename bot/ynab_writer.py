"""Daily YNAB writer — pushes bot categorizations to YNAB.

Phase 7+ architectural shift (2026-06-26): the bot is the system of
record. YNAB is a mirror that the bot writes to once a day. User taps
in Telegram no longer talk to YNAB directly — they only update local
ledger_txn. The writer reconciles overnight.

What this fixes:
  * The dual-id period bug — when a CC alert beats YNAB to a charge,
    the bot's categorize used to silently skip the YNAB push (no UUID
    yet). The writer waits for YNAB to surface the charge and then
    pushes the bot's existing categorization.
  * The 6h-sync overwrite bug — full_sync used to overwrite local
    category_id with whatever YNAB had. With the writer pushing daily,
    bot categorizations propagate to YNAB before the next sync, so
    they never get overwritten.
  * The "instant YNAB write fails silently" bug — a failed set_category
    during a tap left the local row "categorized" but YNAB uncategorized.
    Now: the writer retries every day until success.

Conflict policy: BOT WINS. When YNAB shows a category that differs
from the bot's stored choice (someone manually edited in the YNAB
app), the writer overwrites YNAB and logs the conflict in the report.

Public entry points:
    run_once(settings) → dict     — push all unsynced + return report
    format_report(report) → str   — render the report for DM
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot import storage
from bot.config import Settings
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _local_id_to_ynab_id(db_path: Path | str, category_id: str) -> str | None:
    """Resolve local category id → YNAB category id via the category table.
    Returns None for local-only categories (shouldn't happen for synced ones)."""
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT ynab_category_id FROM category WHERE id = ?",
            (category_id,),
        ).fetchone()
    return row["ynab_category_id"] if row and row["ynab_category_id"] else None


def _find_ynab_txn_for_ledger(
    settings: Settings, ledger_txn_id: int,
) -> str | None:
    """For a pending_txn with synthetic ynab_txn_id='ledger:N', look up
    the matching YNAB transaction by (account, |amount|, date ±2 days).
    Returns the real YNAB UUID or None if YNAB hasn't surfaced this
    charge yet.
    """
    db_path = settings.paths.database
    with storage.connect(db_path) as con:
        lt = con.execute(
            "SELECT account_id, posted_date, amount_cents "
            "FROM ledger_txn WHERE id = ?",
            (ledger_txn_id,),
        ).fetchone()
        if not lt:
            return None
        acct = con.execute(
            "SELECT ynab_account_id FROM account WHERE id = ?",
            (lt["account_id"],),
        ).fetchone()
    if not acct or not acct["ynab_account_id"]:
        return None

    client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    # Pull a small window of recent YNAB transactions; cheap enough to do
    # per-call. For high-volume deployments we'd cache this per run.
    from datetime import date as _date_cls, timedelta as _td
    since = (lt["posted_date"]
              if isinstance(lt["posted_date"], _date_cls)
              else _date_cls.fromisoformat(str(lt["posted_date"])))
    since = since - _td(days=3)
    try:
        ytxns = client.list_all_transactions(since=since)
    except Exception as e:  # noqa: BLE001
        log.warning("ynab_writer: list_all_transactions failed: %s", e)
        return None

    target_acct = acct["ynab_account_id"]
    target_amt = abs(int(lt["amount_cents"]))
    target_date = since + _td(days=3)
    candidates = []
    for ytx in ytxns:
        if ytx["ynab_account_id"] != target_acct:
            continue
        if abs(int(ytx["amount_cents"])) != target_amt:
            continue
        d = ytx["txn_date"]
        if not isinstance(d, _date_cls):
            d = _date_cls.fromisoformat(str(d)[:10])
        if abs((d - target_date).days) > 2:
            continue
        candidates.append(ytx)
    if len(candidates) == 1:
        return candidates[0]["ynab_txn_id"]
    return None


def _push_to_ynab(
    settings: Settings, *,
    ynab_txn_id: str, bot_category_id: str,
) -> tuple[bool, str | None]:
    """Send the category to YNAB. Returns (ok, error_message)."""
    client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    ynab_cat = _local_id_to_ynab_id(settings.paths.database, bot_category_id)
    if not ynab_cat:
        return False, f"local category {bot_category_id[:8]} has no ynab_category_id"
    try:
        client.set_category(ynab_txn_id, ynab_cat)
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def run_once(settings: Settings) -> dict:
    """Drain the bot's categorization decisions to YNAB.

    For each pending_txn with status='categorized' and synced_to_ynab_at
    NULL:

      1. If ynab_txn_id starts 'ledger:N' → look up the YNAB UUID via
         account+amount+date matching. If found, rewire the row's id.
      2. Pull YNAB's current category for that UUID. Compare to bot's
         chosen_category:
            - same      → already in sync; just stamp synced_to_ynab_at
            - differ    → push bot's choice (bot wins); log conflict
            - YNAB null → push bot's choice
      3. On any push failure (network, 4xx), log as 'failed' and leave
         synced_to_ynab_at NULL so we retry tomorrow.

    Returns a structured report. format_report() renders it for DM.
    """
    db_path = settings.paths.database
    report = {
        "ts": _utcnow().isoformat(),
        "pushed": 0,
        "already_synced": 0,
        "rewired": 0,
        "no_ynab_match": [],     # (pt_id, payee, amount_cents, date)
        "conflicts": [],         # (pt_id, payee, bot_cat_name, ynab_cat_name)
        "failed": [],            # (pt_id, error)
    }

    # Pull the work queue
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT pt.id AS pt_id, pt.ynab_txn_id, pt.chosen_category,
                      pt.payee, pt.amount_cents, pt.txn_date,
                      c.name AS bot_cat_name, c.ynab_category_id
               FROM pending_txn pt
               JOIN category c ON c.id = pt.chosen_category
               WHERE pt.status = 'categorized'
                 AND pt.synced_to_ynab_at IS NULL"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        log.info("ynab_writer: nothing to sync")
        return report

    client = YnabClient(settings.ynab_token, settings.ynab.budget_id)

    # Cache the full YNAB transaction list so the per-row lookups are
    # cheap (one API call up front instead of N).
    from datetime import date as _date_cls, timedelta as _td
    earliest = min(_date_cls.fromisoformat(str(r["txn_date"])[:10])
                    for r in rows)
    try:
        ynab_txns = client.list_all_transactions(since=earliest - _td(days=3))
    except Exception as e:  # noqa: BLE001
        log.error("ynab_writer: list_all_transactions failed: %s", e)
        return report
    by_uuid = {t["ynab_txn_id"]: t for t in ynab_txns}

    # Resolve YNAB-local category map for conflict detection
    with storage.connect(db_path) as con:
        ynab_cat_to_local_name = {
            r["ynab_category_id"]: r["name"]
            for r in con.execute(
                "SELECT name, ynab_category_id FROM category "
                "WHERE ynab_category_id IS NOT NULL"
            ).fetchall()
        }

    for r in rows:
        pt_id = r["pt_id"]
        yid = r["ynab_txn_id"] or ""
        bot_cat = r["chosen_category"]
        bot_cat_name = r["bot_cat_name"]

        # Step 1: resolve to a real YNAB UUID if we don't have one yet
        if yid.startswith("ledger:"):
            try:
                ledger_id = int(yid.split(":", 1)[1])
            except (ValueError, IndexError):
                report["failed"].append({
                    "pt_id": pt_id, "error": f"malformed id {yid}",
                })
                continue
            resolved = _find_ynab_txn_for_ledger(settings, ledger_id)
            if not resolved:
                report["no_ynab_match"].append({
                    "pt_id": pt_id, "payee": r["payee"],
                    "amount_cents": r["amount_cents"],
                    "date": str(r["txn_date"])[:10],
                })
                continue
            # If another pending_txn already owns the target YNAB id
            # (leftover from the old ynab_watcher duplicate-create path),
            # consolidate by marking that older row 'skipped' with a memo
            # and freeing its id. Then rewire ours.
            holder_id_for_audit: int | None = None
            with storage.connect(db_path) as con:
                holder = con.execute(
                    "SELECT id, status FROM pending_txn "
                    "WHERE ynab_txn_id = ? AND id != ?",
                    (resolved, pt_id),
                ).fetchone()
                if holder:
                    sentinel = f"merged:{holder['id']}"
                    con.execute(
                        "UPDATE pending_txn SET status='skipped', "
                        "ynab_txn_id=?, "
                        "memo = COALESCE(NULLIF(memo, ''), '') || "
                        "  ' [merged into pt#' || ? || ' by ynab_writer]' "
                        "WHERE id=?",
                        (sentinel, pt_id, holder["id"]),
                    )
                    holder_id_for_audit = holder["id"]
                con.execute(
                    "UPDATE pending_txn SET ynab_txn_id = ? WHERE id = ?",
                    (resolved, pt_id),
                )
            yid = resolved
            report["rewired"] += 1
            # Audits outside the with-block so they don't conflict with
            # the connection's lock.
            if holder_id_for_audit is not None:
                storage.audit(db_path, "ynab_writer_consolidated_dupe", {
                    "kept": pt_id, "merged_into_kept": holder_id_for_audit,
                })
            storage.audit(db_path, "ynab_writer_rewired", {
                "pt_id": pt_id, "from": r["ynab_txn_id"], "to": resolved,
            })

        # Step 2: compare against YNAB's current category
        ytx = by_uuid.get(yid)
        # If the rewire just happened or the cached list doesn't have it,
        # we don't know YNAB's current category. Treat as 'YNAB null' →
        # just push.
        ynab_cat_id = ytx.get("ynab_category_id") if ytx else None

        if ynab_cat_id and ynab_cat_id == r["ynab_category_id"]:
            # Already in sync
            with storage.connect(db_path) as con:
                con.execute(
                    "UPDATE pending_txn SET synced_to_ynab_at = ? WHERE id = ?",
                    (_utcnow(), pt_id),
                )
            report["already_synced"] += 1
            continue

        # Step 3: push bot's choice (covers ynab=null AND conflict cases)
        was_conflict = bool(ynab_cat_id and ynab_cat_id != r["ynab_category_id"])
        ok, err = _push_to_ynab(
            settings, ynab_txn_id=yid, bot_category_id=bot_cat,
        )
        if ok:
            with storage.connect(db_path) as con:
                con.execute(
                    "UPDATE pending_txn SET synced_to_ynab_at = ? WHERE id = ?",
                    (_utcnow(), pt_id),
                )
            report["pushed"] += 1
            if was_conflict:
                report["conflicts"].append({
                    "pt_id": pt_id, "payee": r["payee"],
                    "amount_cents": r["amount_cents"],
                    "date": str(r["txn_date"])[:10],
                    "bot_cat_name": bot_cat_name,
                    "ynab_cat_name": ynab_cat_to_local_name.get(
                        ynab_cat_id, "(unknown)",
                    ),
                })
        else:
            report["failed"].append({"pt_id": pt_id, "error": err})

    storage.audit(db_path, "ynab_writer_run", {
        "pushed": report["pushed"],
        "already_synced": report["already_synced"],
        "rewired": report["rewired"],
        "no_ynab_match": len(report["no_ynab_match"]),
        "conflicts": len(report["conflicts"]),
        "failed": len(report["failed"]),
    })
    log.info("ynab_writer: %s", {k: v for k, v in report.items() if k != "ts"})
    return report


def format_report(report: dict) -> str:
    """DM-friendly summary of a writer run."""
    pushed = report.get("pushed", 0)
    already = report.get("already_synced", 0)
    rewired = report.get("rewired", 0)
    no_match = report.get("no_ynab_match", []) or []
    conflicts = report.get("conflicts", []) or []
    failed = report.get("failed", []) or []

    lines = [f"🔄 YNAB writer"]
    total_handled = pushed + already + rewired + len(no_match) + len(failed)
    if total_handled == 0:
        lines.append("Nothing to sync — bot ledger is already in line with YNAB.")
        return "\n".join(lines)

    if pushed:
        lines.append(f"✅ Pushed {pushed} categorization(s) to YNAB.")
    if rewired:
        lines.append(f"🔗 Rewired {rewired} synthetic id(s) to real YNAB UUIDs.")
    if already:
        lines.append(f"= {already} already in sync (no-op).")

    if no_match:
        lines.append("")
        lines.append(f"⏳ Waiting for YNAB to surface ({len(no_match)}):")
        for r in no_match[:8]:
            amt = (r["amount_cents"] or 0) / 100
            lines.append(
                f"   pt#{r['pt_id']}  {r['date']}  ${amt:>+9.2f}  "
                f"{(r['payee'] or '')[:34]}"
            )
        if len(no_match) > 8:
            lines.append(f"   … and {len(no_match) - 8} more")

    if conflicts:
        lines.append("")
        lines.append(f"⚠ Conflicts — overrode YNAB ({len(conflicts)}):")
        for c in conflicts[:8]:
            amt = (c["amount_cents"] or 0) / 100
            lines.append(
                f"   pt#{c['pt_id']}  {c['date']}  ${amt:>+9.2f}  "
                f"{(c['payee'] or '')[:30]}"
            )
            lines.append(
                f"     bot={c['bot_cat_name'][:22]}  was-ynab={c['ynab_cat_name'][:22]}"
            )
        if len(conflicts) > 8:
            lines.append(f"   … and {len(conflicts) - 8} more")

    if failed:
        lines.append("")
        lines.append(f"❌ Push failed ({len(failed)}) — will retry tomorrow:")
        for f in failed[:5]:
            lines.append(f"   pt#{f['pt_id']}  {f['error'][:60]}")

    return "\n".join(lines)


async def send_writer_report(app) -> dict:
    """Run the writer and DM the result to opted-in users."""
    settings = app.bot_data["settings"]
    db_path = settings.paths.database
    report = await __import__("asyncio").to_thread(run_once, settings)
    body = format_report(report)

    recipients = storage.list_recipients_for_period(db_path, "daily")
    user_to_chat = {a.user_id: a.chat_id for a in settings.gmail_accounts}
    for r in recipients:
        chat_id = user_to_chat.get(r["user_id"])
        if not chat_id:
            continue
        try:
            from bot.telegram_bot import _bot_for_chat
            await _bot_for_chat(app, chat_id).send_message(
                chat_id=chat_id, text=body,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ynab_writer report send to %s failed: %s",
                         r["user_id"], e)
    storage.audit(db_path, "ynab_writer_report_sent", {
        "pushed": report.get("pushed", 0),
        "conflicts": len(report.get("conflicts", [])),
    })
    return report
