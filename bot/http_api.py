"""Localhost HTTP API for the Tauri UI's write actions.

Phase 3 (2026-06-26 → onward): the bot stays the single writer for the
ledger. The Tauri UI is read-only against SQLite for everything else;
when it needs to mutate (recategorize a transaction, move money between
envelopes, set a budgeted amount), it POSTs to this API. The bot reuses
its existing envelope + categorize logic so envelope math, audit log,
and the daily ynab_writer all keep working as one coherent system.

Binding:
  * Loopback only — 127.0.0.1:8765 by default.
  * Token auth via ``X-API-Token`` header. Token comes from the
    ``YNABHELPER_API_TOKEN`` env var. If unset on first start, we
    generate one and persist it to ``ui_api_token.txt`` next to the
    config so the Tauri side can read it without extra plumbing.

Endpoints:
  * ``GET  /healthz``         — liveness probe
  * ``POST /categorize``      — set chosen_category on a pending_txn OR
                                 set category_id on a ledger_txn
  * ``POST /envelope/move``   — move budgeted_cents between two
                                 category-months
  * ``POST /budget/set``      — set absolute budgeted_cents for one
                                 category-month
  * ``GET  /categories``      — return all spending + non-spending
                                 categories (for the picker)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from bot import envelope, storage
from bot.config import Settings

log = logging.getLogger(__name__)

# Where the token gets cached when YNABHELPER_API_TOKEN env var isn't set.
_TOKEN_FILE = Path(__file__).resolve().parent.parent / "ui_api_token.txt"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_or_generate_token() -> str:
    env = os.environ.get("YNABHELPER_API_TOKEN", "").strip()
    if env:
        return env
    if _TOKEN_FILE.exists():
        cached = _TOKEN_FILE.read_text().strip()
        if cached:
            os.environ["YNABHELPER_API_TOKEN"] = cached
            return cached
    import secrets as _secrets
    token = _secrets.token_hex(24)
    _TOKEN_FILE.write_text(token)
    os.environ["YNABHELPER_API_TOKEN"] = token
    log.info("ui_api: generated new token; wrote to %s", _TOKEN_FILE)
    return token


def _require_token(x_api_token: str = Header(...)) -> None:
    expected = _load_or_generate_token()
    if x_api_token != expected:
        raise HTTPException(status_code=401, detail="bad token")


# ────────────────────────────────────────────────────────────────────────────
# Request models — MUST live at module scope. When these were nested inside
# build_app() FastAPI's body-vs-query detection misclassified them as query
# params (it relies on `typing.get_type_hints` which can't resolve closure-
# scoped names), and every POST returned 422 "Field required" before the
# handler even ran.
# ────────────────────────────────────────────────────────────────────────────


class CategorizeBody(BaseModel):
    pt_id: int | None = None
    ledger_txn_id: int | None = None
    category_id: str


class MoveBody(BaseModel):
    month: str           # "YYYY-MM"
    from_category_id: str
    to_category_id: str
    cents: int           # positive — amount to shift


class SetBudgetBody(BaseModel):
    month: str
    category_id: str
    cents: int           # absolute target budgeted_cents


class RollForwardBody(BaseModel):
    month: str           # target month "YYYY-MM" to copy budgets INTO


class NewIncomeSource(BaseModel):
    display_name: str
    expected_amount_cents: int
    cadence: str         # "biweekly" | "semi-monthly" | "monthly"
    first_expected_date: str  # "YYYY-MM-DD"


class JobChangeBody(BaseModel):
    retire_payee_key: str | None = None
    new_source: NewIncomeSource | None = None


class AskInChatBody(BaseModel):
    pt_id: int | None = None
    ledger_txn_id: int | None = None


class CategoryCreateBody(BaseModel):
    name: str
    group_id: str | None = None       # existing group…
    new_group_name: str | None = None  # …or create one
    is_spending: int = 1


class ReconcileMonthBody(BaseModel):
    month: str                # "YYYY-MM"
    dry_run: bool = True      # preview by default; apply needs explicit False


class MarkTransferBody(BaseModel):
    ledger_txn_id: int
    # Optional: force the counterparty account (needed for one-sided
    # transfers, e.g. to an off-budget account whose side we never see).
    counterpart_account_id: str | None = None


class YnabPushBody(BaseModel):
    ledger_txn_id: int


def build_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="ynabhelper UI API",
        description="Localhost write API for the Tauri desktop UI.",
        version="0.1.0",
    )
    db_path = settings.paths.database
    _load_or_generate_token()  # warm cache on startup

    # ── Routes ────────────────────────────────────────────────────────────

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """One plain-language source of truth for every product surface.

        This endpoint is loopback-only and intentionally contains timestamps
        and counts, never credentials or transaction contents.  A live process
        is not necessarily a healthy assistant: these freshness signals make
        silent email, Telegram, and YNAB-sync failures visible.
        """
        with storage.connect(db_path) as con:
            def scalar(sql: str, params: tuple = ()) -> Any:
                row = con.execute(sql, params).fetchone()
                return row[0] if row else None

            return {
                "ok": True,
                "db": str(db_path),
                "api_version": app.version,
                "last_email_capture": scalar(
                    "SELECT MAX(received_at) FROM ledger_signal"
                ),
                "last_ynab_sync": scalar(
                    "SELECT MAX(ts) FROM audit_log WHERE event='ynab_full_sync_run'"
                ),
                "last_telegram_in": scalar(
                    "SELECT MAX(ts) FROM chat_message WHERE direction='in'"
                ),
                "last_telegram_out": scalar(
                    "SELECT MAX(ts) FROM chat_message WHERE direction='out'"
                ),
                "needs_attention": scalar(
                    "SELECT COUNT(*) FROM pending_txn "
                    "WHERE chosen_category IS NULL AND status IN ('pending','skipped')"
                ) or 0,
                "open_questions": scalar(
                    "SELECT COUNT(*) FROM question WHERE state='open'"
                ) or 0,
            }

    @app.get("/categories", dependencies=[Depends(_require_token)])
    def list_categories() -> list[dict[str, Any]]:
        """Every visible category — for the UI's category picker.

        Excludes the 'Internal Master Category' group (YNAB plumbing like
        'Uncategorized') EXCEPT 'Inflow: Ready to Assign' — that's where
        income deposits belong (paychecks, refunds, Venmo income), shown
        under a friendlier 'Income' group label (Steven, 2026-07-10).
        """
        with storage.connect(db_path) as con:
            rows = con.execute(
                "SELECT c.id, c.name, c.is_spending, "
                "  CASE WHEN g.name = 'Internal Master Category' "
                "       THEN 'Income' ELSE g.name END AS group_name "
                "FROM category c JOIN category_group g ON g.id = c.group_id "
                "WHERE c.hidden = 0 AND g.hidden = 0 "
                "  AND (g.name != 'Internal Master Category' "
                "       OR c.name = 'Inflow: Ready to Assign') "
                "ORDER BY CASE WHEN c.name = 'Inflow: Ready to Assign' "
                "         THEN 0 ELSE 1 END, g.sort_order, c.name"
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/category_groups", dependencies=[Depends(_require_token)])
    def category_groups() -> list[dict[str, Any]]:
        """Unhidden category groups, for the new-category modal's group
        picker. Excludes the YNAB-internal group (Income lives there)."""
        with storage.connect(db_path) as con:
            rows = con.execute(
                "SELECT id, name FROM category_group "
                "WHERE hidden = 0 AND name != 'Internal Master Category' "
                "ORDER BY sort_order, name"
            ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/category/create", dependencies=[Depends(_require_token)])
    def category_create(body: CategoryCreateBody) -> dict[str, Any]:
        """Create a category (and optionally its group) locally.

        The bot's budget is its own truth (YNAB is on its way out), so
        app-created categories get a local uuid and NO ynab_category_id —
        ynab_writer counts them as 'local_only' instead of pushing.
        (Steven, 2026-07-11: add-category + Hobbies group.)
        """
        import uuid

        name = (body.name or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        if body.group_id is None and not (body.new_group_name or "").strip():
            raise HTTPException(400, "group_id or new_group_name required")

        with storage.connect(db_path) as con:
            dupe = con.execute(
                "SELECT id FROM category WHERE hidden = 0 "
                "AND lower(name) = lower(?)", (name,),
            ).fetchone()
            if dupe:
                raise HTTPException(409, f"a category named '{name}' already exists")

            if body.group_id is not None:
                grp = con.execute(
                    "SELECT id, name FROM category_group WHERE id = ?",
                    (body.group_id,),
                ).fetchone()
                if not grp:
                    raise HTTPException(404, "unknown category group")
                group_id, group_name = grp["id"], grp["name"]
            else:
                group_name = body.new_group_name.strip()
                existing = con.execute(
                    "SELECT id, name FROM category_group "
                    "WHERE hidden = 0 AND lower(name) = lower(?)",
                    (group_name,),
                ).fetchone()
                if existing:
                    group_id, group_name = existing["id"], existing["name"]
                else:
                    group_id = f"local-{uuid.uuid4()}"
                    max_sort = con.execute(
                        "SELECT COALESCE(MAX(sort_order), 0) AS m "
                        "FROM category_group"
                    ).fetchone()["m"]
                    con.execute(
                        "INSERT INTO category_group (id, name, sort_order) "
                        "VALUES (?, ?, ?)",
                        (group_id, group_name, max_sort + 1),
                    )

            category_id = f"local-{uuid.uuid4()}"
            con.execute(
                "INSERT INTO category (id, group_id, name, is_spending) "
                "VALUES (?, ?, ?, ?)",
                (category_id, group_id, name, 1 if body.is_spending else 0),
            )

        storage.audit(db_path, "ui_category_create", {
            "category_id": category_id, "name": name,
            "group_id": group_id, "group_name": group_name,
        })
        # Seed this month's month_category row — the Budget grid reads
        # FROM month_category, so without it the new category is invisible.
        try:
            envelope.recompute_month(
                db_path, datetime.now().strftime("%Y-%m"),
                category_ids=[category_id])
        except Exception as e:  # noqa: BLE001
            log.warning("recompute after category create failed: %s", e)
        return {"ok": True, "category_id": category_id,
                "group_id": group_id, "group_name": group_name}

    @app.post("/ask_in_chat", dependencies=[Depends(_require_token)])
    def ask_in_chat(body: AskInChatBody) -> dict[str, Any]:
        """Push one item to the household group as a ping (question row +
        Telegram message), so it can be answered per chat. Desktop 'Ask in
        chat' button (Steven, 2026-07-10).

        Accepts either a pending_txn id (Inbox rows) or a ledger_txn id
        (Transactions tab, 2026-07-11). A ledger row is resolved to its
        pending_txn via ynab_txn_id — or gets a fresh pending row
        backfilled from the ledger data, the same trick /categorize uses,
        so the group-reply filing path has something to stamp.
        """
        from bot.group_chat import post_instant_ping_sync
        if body.pt_id is None and body.ledger_txn_id is None:
            raise HTTPException(400, "pt_id or ledger_txn_id required")

        pt_id = body.pt_id
        if pt_id is None:
            with storage.connect(db_path) as con:
                lt = con.execute(
                    "SELECT id, posted_date, amount_cents, payee, memo, "
                    "category_id, ynab_txn_id "
                    "FROM ledger_txn WHERE id = ?",
                    (body.ledger_txn_id,),
                ).fetchone()
                if not lt:
                    raise HTTPException(404, "unknown ledger_txn")
                if lt["category_id"]:
                    raise HTTPException(409, "item is already categorized")
                yid = lt["ynab_txn_id"] or f"ledger:{lt['id']}"
                existing_pt = con.execute(
                    "SELECT id FROM pending_txn WHERE ynab_txn_id = ?",
                    (yid,),
                ).fetchone()
                if existing_pt:
                    pt_id = existing_pt["id"]
                else:
                    cur = con.execute(
                        "INSERT INTO pending_txn "
                        "  (user_id, ynab_txn_id, payee, amount_cents, "
                        "   txn_date, memo, status, queue_lane) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'cold')",
                        ("steven", yid,
                         (lt["payee"] or "")[:200],
                         int(lt["amount_cents"]),
                         str(lt["posted_date"])[:10],
                         (lt["memo"] or "")[:500]),
                    )
                    pt_id = cur.lastrowid

        with storage.connect(db_path) as con:
            pt = con.execute(
                "SELECT id, status FROM pending_txn WHERE id = ?",
                (pt_id,),
            ).fetchone()
        if not pt:
            raise HTTPException(404, "unknown pending_txn")
        if pt["status"] not in ("pending", "skipped"):
            raise HTTPException(409, "item is already categorized")
        try:
            message_id = post_instant_ping_sync(settings, pt_id)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "message_id": message_id}

    @app.post("/categorize", dependencies=[Depends(_require_token)])
    def categorize(body: CategorizeBody) -> dict[str, Any]:
        """Set the category on either a pending_txn OR a ledger_txn.

        ``pt_id`` path mirrors `_apply_choice` in the Telegram bot:
          1. Stamp pending_txn.chosen_category + status='categorized'
          2. Mirror onto the linked ledger_txn so envelope math reflects
             the choice instantly.
          3. ynab_writer's next run pushes to YNAB.

        ``ledger_txn_id`` path is for transactions that arrived purely
        via ``ynab_full_sync`` and never had a pending_txn (typically
        rows older than the bot's email coverage). We just set the
        ledger_txn.category_id directly; the writer will pick it up if
        the row has a ynab_txn_id.
        """
        if not body.category_id:
            raise HTTPException(400, "category_id required")
        if body.pt_id is None and body.ledger_txn_id is None:
            raise HTTPException(400, "pt_id or ledger_txn_id required")

        # The ledger row's PRE-change category + posted date drive the
        # envelope recompute below: recategorizing must refresh the OLD
        # category too, back to the transaction's month.
        old_row = None

        with storage.connect(db_path) as con:
            cat = con.execute(
                "SELECT name FROM category WHERE id = ? AND hidden = 0",
                (body.category_id,),
            ).fetchone()
            if not cat:
                raise HTTPException(404, "unknown category")

            if body.pt_id is not None:
                pt = con.execute(
                    "SELECT id, ynab_txn_id FROM pending_txn WHERE id = ?",
                    (body.pt_id,),
                ).fetchone()
                if not pt:
                    raise HTTPException(404, "unknown pending_txn")
                pre_yid = pt["ynab_txn_id"] or ""
                if pre_yid.startswith("ledger:"):
                    try:
                        old_row = con.execute(
                            "SELECT category_id, posted_date FROM ledger_txn "
                            "WHERE id = ?",
                            (int(pre_yid.split(":", 1)[1]),),
                        ).fetchone()
                    except (ValueError, IndexError):
                        pass
                elif pre_yid:
                    old_row = con.execute(
                        "SELECT category_id, posted_date FROM ledger_txn "
                        "WHERE ynab_txn_id = ?",
                        (pre_yid,),
                    ).fetchone()
                con.execute(
                    "UPDATE pending_txn SET chosen_category = ?, "
                    "chosen_at = ?, status = 'categorized', "
                    # Desktop UI writes are Steven acting (his machine).
                    "filed_by = 'steven' WHERE id = ?",
                    (body.category_id, _utcnow(), body.pt_id),
                )
                yid = pt["ynab_txn_id"] or ""
                if yid.startswith("ledger:"):
                    try:
                        lid = int(yid.split(":", 1)[1])
                        con.execute(
                            "UPDATE ledger_txn SET category_id = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (body.category_id, lid),
                        )
                    except (ValueError, IndexError):
                        pass
                elif yid:
                    con.execute(
                        "UPDATE ledger_txn SET category_id = ?, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE ynab_txn_id = ?",
                        (body.category_id, yid),
                    )
                target = {"pt_id": body.pt_id}
            else:
                lt = con.execute(
                    "SELECT id, posted_date, amount_cents, payee, memo, "
                    "category_id, ynab_txn_id "
                    "FROM ledger_txn WHERE id = ?",
                    (body.ledger_txn_id,),
                ).fetchone()
                if not lt:
                    raise HTTPException(404, "unknown ledger_txn")
                old_row = lt
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (body.category_id, body.ledger_txn_id),
                )
                # The ynab_writer scans pending_txn, not ledger_txn. Without
                # a matching pending_txn row, this categorization stays
                # local-only and never propagates to YNAB. Create/refresh
                # a pending_txn entry tied to the ledger row so tomorrow
                # morning's writer DM picks it up.
                #
                # Skip when the ledger row has no ynab_txn_id at all:
                # writer's _find_ynab_txn_for_ledger needs SOMETHING to
                # match against. (Rare path: bot-only ledger_txn that
                # never reached YNAB.)
                yid = lt["ynab_txn_id"]
                if yid:
                    existing_pt = con.execute(
                        "SELECT id FROM pending_txn WHERE ynab_txn_id = ?",
                        (yid,),
                    ).fetchone()
                    if existing_pt:
                        # Already a pending_txn for this YNAB id — flip it
                        # to categorized + park the chosen category. Don't
                        # touch synced_to_ynab_at: if it's NULL, writer
                        # will see it as work to do; if non-NULL we want
                        # the writer to detect a category change.
                        con.execute(
                            "UPDATE pending_txn SET "
                            "  chosen_category = ?, chosen_at = ?, "
                            "  status = 'categorized', "
                            "  synced_to_ynab_at = NULL "
                            "WHERE id = ?",
                            (body.category_id, _utcnow(), existing_pt["id"]),
                        )
                    else:
                        # Backfill a pending_txn row from the ledger data
                        # so writer's SELECT pt.id ... finds it tomorrow.
                        con.execute(
                            "INSERT INTO pending_txn "
                            "  (user_id, ynab_txn_id, payee, amount_cents, "
                            "   txn_date, memo, suggested_category, "
                            "   chosen_category, chosen_at, status, "
                            "   queue_lane) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                            "        'categorized', 'cold')",
                            ("steven", yid,
                             (lt["payee"] or "")[:200],
                             int(lt["amount_cents"]),
                             str(lt["posted_date"])[:10],
                             (lt["memo"] or "")[:500],
                             body.category_id,        # suggested_category
                             body.category_id,        # chosen_category
                             _utcnow()),
                        )
                target = {"ledger_txn_id": body.ledger_txn_id}

        # Close any open group-chat question about this item so the two
        # surfaces agree (redesign-v2 cross-surface rule). MUST run after
        # the write transaction commits: it opens its own connection, and
        # doing that inside the with-block deadlocked on SQLite's write
        # lock — every desktop pt-path categorize 500'd (found 2026-07-12
        # filing Knights Play to Golf).
        if body.pt_id is not None:
            from bot.group_chat import resolve_open_questions_for_item
            resolve_open_questions_for_item(
                db_path, "txn", body.pt_id, "steven-desktop")

        storage.audit(db_path, "ui_categorize", {
            **target,
            "category_id": body.category_id,
            "category_name": cat["name"],
        })
        # Re-derive envelope state for BOTH old + new category, from the
        # transaction's month through the current month — available chains
        # forward, so fixing June must refresh July too (Budget activity
        # popup recategorizes past months, 2026-07-11). Capped at 24
        # months of chain for ancient rows.
        try:
            affected = {body.category_id}
            now = datetime.now()
            start = now.strftime("%Y-%m")
            if old_row is not None:
                if old_row["category_id"]:
                    affected.add(old_row["category_id"])
                start = min(start, str(old_row["posted_date"])[:7])
            months = []
            y, m = int(start[:4]), int(start[5:7])
            while (y, m) <= (now.year, now.month):
                months.append(f"{y:04d}-{m:02d}")
                m += 1
                if m > 12:
                    m, y = 1, y + 1
            for mo in months[-24:]:
                envelope.recompute_month(db_path, mo,
                                         category_ids=sorted(affected))
        except Exception as e:  # noqa: BLE001
            log.warning("recompute after categorize failed: %s", e)
        return {"ok": True}

    @app.post("/mark_transfer", dependencies=[Depends(_require_token)])
    def mark_transfer(body: MarkTransferBody) -> dict[str, Any]:
        """Mark a ledger row as a transfer leg (Steven, 2026-07-19).

        Auto-detects the counterpart: opposite amount in a different
        account within ±4 days that isn't already a transfer. When found,
        links BOTH rows (mutual transfer_account_id, category cleared) and
        retires their pending items + open questions — transfers need no
        category and no human decision. Without a counterpart row,
        ``counterpart_account_id`` is required and the row is linked
        one-sided (off-budget destinations like Marcus).
        """
        from bot.group_chat import resolve_open_questions_for_item

        resolved_pts: list[int] = []
        with storage.connect(db_path) as con:
            lt = con.execute(
                "SELECT id, account_id, posted_date, amount_cents, payee, "
                "transfer_account_id, ynab_txn_id "
                "FROM ledger_txn WHERE id = ?",
                (body.ledger_txn_id,),
            ).fetchone()
            if not lt:
                raise HTTPException(404, "unknown ledger_txn")
            if lt["transfer_account_id"]:
                raise HTTPException(409, "already marked as a transfer")

            counterpart = con.execute(
                """SELECT id, account_id FROM ledger_txn
                   WHERE amount_cents = ? AND account_id != ?
                     AND transfer_account_id IS NULL AND is_split = 0
                     AND ABS(julianday(posted_date) - julianday(?)) <= 4
                     AND (? IS NULL OR account_id = ?)
                   ORDER BY ABS(julianday(posted_date) - julianday(?))
                   LIMIT 1""",
                (-lt["amount_cents"], lt["account_id"], lt["posted_date"],
                 body.counterpart_account_id, body.counterpart_account_id,
                 lt["posted_date"]),
            ).fetchone()

            if counterpart is None and not body.counterpart_account_id:
                raise HTTPException(
                    404, "no matching opposite transaction found — pick "
                         "the counterparty account explicitly")

            other_acct = (counterpart["account_id"] if counterpart
                          else body.counterpart_account_id)
            acct_ok = con.execute(
                "SELECT 1 FROM account WHERE id = ?", (other_acct,),
            ).fetchone()
            if not acct_ok:
                raise HTTPException(404, "unknown counterpart account")

            legs = [(lt["id"], other_acct)]
            if counterpart:
                legs.append((counterpart["id"], lt["account_id"]))
            for leg_id, xfer_acct in legs:
                real_yid = con.execute(
                    "SELECT ynab_txn_id FROM ledger_txn WHERE id = ?",
                    (leg_id,),
                ).fetchone()["ynab_txn_id"]
                con.execute(
                    "UPDATE ledger_txn SET transfer_account_id = ?, "
                    "category_id = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (xfer_acct, leg_id),
                )
                # Transfer legs need no human decision — retire pendings
                # (keyed by either the synthetic ledger:N or the real yid).
                for pt in con.execute(
                    "SELECT id FROM pending_txn WHERE ynab_txn_id IN (?, ?)",
                    (f"ledger:{leg_id}", real_yid or ""),
                ).fetchall():
                    con.execute(
                        "UPDATE pending_txn SET status = 'categorized', "
                        "chosen_category = NULL, filed_by = 'transfer', "
                        "memo = COALESCE(NULLIF(memo,''),'') || "
                        "  ' [marked transfer]' WHERE id = ?",
                        (pt["id"],),
                    )
                    resolved_pts.append(pt["id"])

        for pt_id in resolved_pts:
            resolve_open_questions_for_item(
                db_path, "txn", pt_id, "steven-desktop")
        storage.audit(db_path, "ui_mark_transfer", {
            "ledger_txn_id": body.ledger_txn_id,
            "counterpart_ledger_txn_id":
                counterpart["id"] if counterpart else None,
            "counterpart_account_id": other_acct,
            "pendings_retired": resolved_pts,
        })
        return {
            "ok": True,
            "linked_pair": counterpart is not None,
            "counterpart_ledger_txn_id":
                counterpart["id"] if counterpart else None,
        }

    @app.post("/envelope/reconcile_month", dependencies=[Depends(_require_token)])
    def envelope_reconcile_month(body: ReconcileMonthBody) -> dict[str, Any]:
        """Settle a month's envelopes: cover every negative spending
        envelope to $0 from same-group surpluses (largest first), then
        Ready to Assign. Sinking-fund groups excluded. dry_run=True
        returns the plan without applying (Steven, 2026-07-11)."""
        import re as _re
        if not _re.fullmatch(r"\d{4}-\d{2}", body.month):
            raise HTTPException(400, "month must be YYYY-MM")
        return envelope.reconcile_overspending(
            db_path, body.month, dry_run=body.dry_run)

    @app.post("/envelope/move", dependencies=[Depends(_require_token)])
    def envelope_move(body: MoveBody) -> dict[str, Any]:
        if body.cents <= 0:
            raise HTTPException(400, "cents must be positive")
        if body.from_category_id == body.to_category_id:
            raise HTTPException(400, "from == to")
        result = envelope.move_money(
            db_path,
            month=body.month,
            from_category_id=body.from_category_id,
            to_category_id=body.to_category_id,
            cents=body.cents,
        )
        storage.audit(db_path, "ui_envelope_move", {
            "month": body.month, "from": body.from_category_id,
            "to": body.to_category_id, "cents": body.cents,
        })
        return {"ok": True, "result": result}

    # ── Investments ───────────────────────────────────────────────────────

    @app.get("/investments/files", dependencies=[Depends(_require_token)])
    def investments_files() -> dict[str, Any]:
        """List available snapshot xlsx files (newest first)."""
        from bot import investments as inv
        return {"folder": str(inv.SNAPSHOTS_DIR), "files": inv.list_snapshots()}

    @app.get("/investments/snapshot", dependencies=[Depends(_require_token)])
    def investments_snapshot() -> dict[str, Any]:
        """Parsed snapshot from the most recent xlsx in the folder."""
        from bot import investments as inv
        latest = inv.find_latest_snapshot()
        if not latest:
            raise HTTPException(
                404,
                f"No xlsx files in {inv.SNAPSHOTS_DIR}. "
                "Drop your exported sheet there.",
            )
        try:
            return inv.parse_snapshot(latest)
        except Exception as e:  # noqa: BLE001
            log.exception("snapshot parse failed: %s", e)
            raise HTTPException(500, f"parse failed: {e}")

    # ── Reconciler inspector (read-only diagnostics) ──────────────────────

    @app.get("/reconciler/ledger-vs-ynab", dependencies=[Depends(_require_token)])
    def reconciler_ledger_vs_ynab(days: int = 90) -> dict[str, Any]:
        """Three-column diff: our ledger vs the live YNAB ledger."""
        from bot import recon_inspect
        days = max(1, min(int(days), 730))
        try:
            return recon_inspect.ledger_vs_ynab_report(
                db_path, settings, days=days,
            )
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("ledger-vs-ynab failed: %s", e)
            raise HTTPException(502, f"YNAB fetch failed: {e}")

    @app.get("/reconciler/matching", dependencies=[Depends(_require_token)])
    def reconciler_matching() -> dict[str, Any]:
        """Order ↔ YNAB-charge matching state + score breakdowns."""
        from bot import recon_inspect
        return recon_inspect.matching_report(db_path)

    @app.get("/reconciler/dedupe", dependencies=[Depends(_require_token)])
    def reconciler_dedupe() -> dict[str, Any]:
        """Email-alert ↔ ynab_sync dedupe state (merged / orphan / dupes)."""
        from bot import recon_inspect
        return recon_inspect.dedupe_report(db_path)

    @app.get("/reconciler/balance", dependencies=[Depends(_require_token)])
    def reconciler_balance() -> dict[str, Any]:
        """Per-account ledger-vs-bank balance reconciliation (no writes)."""
        from bot import recon_inspect
        return recon_inspect.balance_report(db_path)

    @app.get("/reconciler/balance_walk", dependencies=[Depends(_require_token)])
    def reconciler_balance_walk(days: int = 45) -> dict[str, Any]:
        """Day-by-day bank-delta vs ledger-delta walk — flags windows where
        a transaction slipped through capture (Steven, 2026-07-11)."""
        from bot import recon_inspect
        days = max(3, min(int(days), 365))
        return recon_inspect.balance_walk(db_path, days=days)

    @app.post("/income/job-change", dependencies=[Depends(_require_token)])
    def income_job_change(body: JobChangeBody) -> dict[str, Any]:
        """Handle a job-change transition: retire one source, add another.

        Either or both fields can be present. ``retire_payee_key`` marks
        an auto-detected source so it's excluded from RTA expectations.
        ``new_source`` adds a manual source that gets counted until
        auto-detection picks up the real payee. Idempotent on payee_key.
        """
        if not body.retire_payee_key and not body.new_source:
            raise HTTPException(400, "must specify retire_payee_key or new_source")

        retired_key = None
        added_key = None

        with storage.connect(db_path) as con:
            # Retire path
            if body.retire_payee_key:
                key = body.retire_payee_key.strip().upper()
                con.execute(
                    "INSERT INTO income_source_override "
                    "  (payee_key, status, retired_at) "
                    "VALUES (?, 'retired', ?) "
                    "ON CONFLICT(payee_key) DO UPDATE SET "
                    "  status = 'retired', retired_at = excluded.retired_at",
                    (key, _utcnow()),
                )
                retired_key = key

            # Add path
            if body.new_source:
                src = body.new_source
                # Compute median_delta_days from cadence
                delta = (
                    14 if src.cadence == "biweekly"
                    else 15 if src.cadence == "semi-monthly"
                    else 30  # monthly
                )
                new_key = src.display_name.strip().upper()
                con.execute(
                    "INSERT INTO income_source_override "
                    "  (payee_key, status, display_name, expected_amount_cents, "
                    "   cadence, first_expected_date, median_delta_days) "
                    "VALUES (?, 'active', ?, ?, ?, ?, ?) "
                    "ON CONFLICT(payee_key) DO UPDATE SET "
                    "  status = 'active', "
                    "  display_name = excluded.display_name, "
                    "  expected_amount_cents = excluded.expected_amount_cents, "
                    "  cadence = excluded.cadence, "
                    "  first_expected_date = excluded.first_expected_date, "
                    "  median_delta_days = excluded.median_delta_days, "
                    "  retired_at = NULL",
                    (new_key, src.display_name, src.expected_amount_cents,
                     src.cadence, src.first_expected_date, delta),
                )
                added_key = new_key

        storage.audit(db_path, "ui_income_job_change", {
            "retired": retired_key, "added": added_key,
        })
        return {"ok": True, "retired": retired_key, "added": added_key}

    @app.post("/budget/roll-forward", dependencies=[Depends(_require_token)])
    def budget_roll_forward(body: RollForwardBody) -> dict[str, Any]:
        """Copy last month's per-category budgeted_cents into ``body.month``.

        Idempotent + safe:
          * Only fires if every existing month_category row for the
            target month has budgeted_cents = 0 (or there are no rows).
            That tells us the user hasn't hand-set anything yet for this
            month — safe to copy from history.
          * If ANY row in the target month is already non-zero, we treat
            the month as "user-touched" and skip entirely (returns
            ``copied=0, reason='already-set'``).
          * Recomputes envelope math after the copy so available_cents
            reflects the rolled-forward budgets immediately.

        Returns ``{ok: bool, copied: int, source_month: str}``.
        """
        target = body.month
        # Previous month string
        from datetime import date as _date
        y, m = (int(x) for x in target.split("-"))
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        source = f"{py}-{pm:02d}"

        with storage.connect(db_path) as con:
            # Has the user already started budgeting this month?
            r = con.execute(
                "SELECT COALESCE(SUM(budgeted_cents), 0) AS s "
                "FROM month_category WHERE month = ?",
                (target,),
            ).fetchone()
            if r and (r["s"] or 0) > 0:
                return {
                    "ok": True, "copied": 0, "source_month": source,
                    "reason": "already-set",
                }

            # Pull source month's per-category budgets.
            src_rows = con.execute(
                "SELECT category_id, budgeted_cents "
                "FROM month_category WHERE month = ? AND budgeted_cents > 0",
                (source,),
            ).fetchall()
            if not src_rows:
                return {
                    "ok": True, "copied": 0, "source_month": source,
                    "reason": "no-source",
                }

            copied = 0
            for sr in src_rows:
                con.execute(
                    """INSERT INTO month_category
                         (month, category_id, budgeted_cents, activity_cents,
                          available_cents)
                       VALUES (?, ?, ?, 0, 0)
                       ON CONFLICT(month, category_id) DO UPDATE SET
                         budgeted_cents = excluded.budgeted_cents""",
                    (target, sr["category_id"], sr["budgeted_cents"]),
                )
                copied += 1

        # Re-derive activity/available for the target month so the UI's
        # next fetch reflects the copied budgets correctly. Cheap.
        try:
            envelope.recompute_month(db_path, target)
        except Exception as e:  # noqa: BLE001
            log.warning("recompute after roll-forward failed: %s", e)

        storage.audit(db_path, "ui_budget_roll_forward", {
            "source_month": source, "target_month": target, "copied": copied,
        })
        return {
            "ok": True, "copied": copied, "source_month": source,
            "reason": "rolled-forward",
        }

    @app.post("/budget/set", dependencies=[Depends(_require_token)])
    def budget_set(body: SetBudgetBody) -> dict[str, Any]:
        """Set absolute budgeted_cents for one category-month.

        Computed as a delta on top of the current value because the
        envelope module only exposes the additive ``assign_to_category``.
        Net result: month_category.budgeted_cents = body.cents.
        """
        with storage.connect(db_path) as con:
            existing = con.execute(
                "SELECT budgeted_cents FROM month_category "
                "WHERE month = ? AND category_id = ?",
                (body.month, body.category_id),
            ).fetchone()
        current = int(existing["budgeted_cents"]) if existing else 0
        delta = body.cents - current
        result = envelope.assign_to_category(
            db_path, body.month, body.category_id, delta,
        )
        storage.audit(db_path, "ui_budget_set", {
            "month": body.month, "category_id": body.category_id,
            "from_cents": current, "to_cents": body.cents,
        })
        return {"ok": True, "result": result}

    @app.post("/ynab/push", dependencies=[Depends(_require_token)])
    def ynab_push(body: YnabPushBody) -> dict[str, Any]:
        """Push one local ledger_txn to YNAB. Stamps ynab_txn_id locally
        on success so the row drops off the unsynced list.

        Used during the parallel-process period when YNAB lacks live bank
        sync. The UI's Sync to YNAB panel calls this per-row.

        Idempotent via YNAB's import_id (= ledger:<id>) — pushing twice
        is a no-op on the YNAB side.
        """
        with storage.connect(db_path) as con:
            row = con.execute(
                """SELECT t.id, t.posted_date, t.amount_cents, t.payee,
                          t.memo, t.cleared, t.category_id, t.ynab_txn_id,
                          a.ynab_account_id, a.name AS account_name
                   FROM ledger_txn t
                   JOIN account a ON a.id = t.account_id
                   WHERE t.id = ?""",
                (body.ledger_txn_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "ledger_txn not found"}
        if row["ynab_txn_id"]:
            return {"ok": False, "error": "already synced",
                    "ynab_txn_id": row["ynab_txn_id"]}
        if not row["ynab_account_id"]:
            return {"ok": False, "error": "account has no ynab_account_id"}

        from bot.ynab_client import YnabClient
        from datetime import date as _date
        client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
        try:
            # Resolve YNAB category_id from local category_id if available.
            ynab_cat_id = None
            if row["category_id"]:
                with storage.connect(db_path) as con:
                    c = con.execute(
                        "SELECT ynab_category_id FROM category WHERE id = ?",
                        (row["category_id"],),
                    ).fetchone()
                    ynab_cat_id = c["ynab_category_id"] if c else None

            posted = _date.fromisoformat(str(row["posted_date"]))
            created = client.create_transaction(
                account_id=row["ynab_account_id"],
                posted_date=posted,
                amount_cents=int(row["amount_cents"]),
                payee_name=row["payee"] or "(unknown)",
                memo=row["memo"],
                category_id=ynab_cat_id,
                cleared=row["cleared"] or "cleared",
                approved=False,    # leave unapproved so user can review in YNAB app
                import_id=f"ledger:{row['id']}",
            )
        except Exception as e:  # noqa: BLE001
            storage.audit(db_path, "ui_ynab_push_failed", {
                "ledger_txn_id": row["id"], "error": str(e)[:300],
            })
            return {"ok": False, "error": str(e)}

        # Stamp ynab_txn_id locally so subsequent queries skip this row.
        with storage.connect(db_path) as con:
            con.execute(
                "UPDATE ledger_txn SET ynab_txn_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (created["id"], row["id"]),
            )
        storage.audit(db_path, "ui_ynab_push_ok", {
            "ledger_txn_id": row["id"], "ynab_txn_id": created["id"],
        })
        return {"ok": True, "ynab_txn_id": created["id"]}

    return app


# ────────────────────────────────────────────────────────────────────────────
# Background-task launcher used from telegram_bot._post_init.
# ────────────────────────────────────────────────────────────────────────────


async def serve(settings: Settings, *, host: str = "127.0.0.1",
                 port: int = 8765) -> None:
    """Run uvicorn in the same event loop as the Telegram bot.

    Crashes are logged but never escape — same convention as the other
    background tasks in telegram_bot.
    """
    import uvicorn  # imported here so a missing dep doesn't break the bot

    app = build_app(settings)
    config = uvicorn.Config(
        app, host=host, port=port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    log.info("ui_api: serving on http://%s:%s", host, port)
    await server.serve()
