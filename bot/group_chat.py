"""Redesign-v2 Phase 4: the household group chat.

One group (Steven + Allison + one bot) replaces per-person DM queues for
in-the-moment interactions. Two flows live here:

* **Instant pings** — as new pending items land, the bot posts one message
  per item to the group: the *option* to act now, never an obligation.
  Replying files it; ignoring is a no-op (the item is already in the
  desktop Inbox). ``_group_ping_loop`` sweeps for unpinged items.

* **Reply-to filing** — a group message that replies to a bot ping is
  resolved structurally: reply_to_message_id → question row → item. Only
  the *content* ("groceries", "skip", "not sure") needs interpretation,
  and category resolution is deterministic-first (fuzzy name match); no
  LLM in the loop for the common case.

Design rules (docs/redesign-v2.md): every question is a self-contained
message; no shared in-flight pointer; nothing stalls. Old DM flows remain
untouched until these are proven — additive cutover.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime

from telegram import Update
from telegram.ext import Application, ContextTypes

from bot import storage

log = logging.getLogger(__name__)

# Cap pings per sweep so a backlog can never flood the group.
_MAX_PINGS_PER_SWEEP = 4
_SWEEP_INTERVAL_S = 180
# Only ping items first seen within this window — older backlog belongs in
# the desktop Inbox, not in everyone's pocket.
_PING_MAX_AGE_HOURS = 36


def _in_quiet_hours(quiet: str, now: datetime | None = None) -> bool:
    """True when the household quiet window ('22:00-07:00') covers now."""
    try:
        start_s, end_s = quiet.split("-")
        start = dtime.fromisoformat(start_s.strip())
        end = dtime.fromisoformat(end_s.strip())
    except (ValueError, AttributeError):
        return False
    t = (now or datetime.now()).time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window wraps midnight


def _fmt_money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def resolve_group_user(settings, tg_user_id: int | None) -> str | None:
    """Telegram user id → 'steven'/'allison' (None for strangers)."""
    if tg_user_id is None:
        return None
    for acct in settings.gmail_accounts:
        try:
            if int(acct.chat_id) == int(tg_user_id):
                return acct.user_id
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Instant pings
# ---------------------------------------------------------------------------

async def group_ping_loop(app: Application) -> None:
    """Post one message per new needs-review item to the household group.

    Stateless by design: 'has this item been pinged?' is a question-table
    lookup, not a conversation pointer. Skips quiet hours; caps per sweep.
    """
    settings = app.bot_data["settings"]
    group_id = int(getattr(settings.telegram, "group_chat_id", 0) or 0)
    if not group_id:
        log.info("group_ping_loop: no group configured — dormant")
        return
    log.info("group_ping_loop: serving group %s", group_id)

    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            if _in_quiet_hours(settings.telegram.quiet_hours):
                continue
            await _sweep_once(app, settings, group_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("group_ping_loop iteration failed: %s", e)


async def _sweep_once(app: Application, settings, group_id: int) -> None:
    db_path = settings.paths.database
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT pt.id, pt.payee, pt.amount_cents, pt.txn_date,
                      pt.suggested_category, c.name AS suggested_name
               FROM pending_txn pt
               LEFT JOIN category c ON c.id = pt.suggested_category
               WHERE pt.status = 'pending'
                 AND pt.created_at >= datetime('now', ?)
                 AND NOT EXISTS (
                   SELECT 1 FROM question q
                   WHERE q.item_kind = 'txn' AND q.item_id = pt.id
                 )
               ORDER BY pt.id ASC
               LIMIT ?""",
            (f"-{_PING_MAX_AGE_HOURS} hours", _MAX_PINGS_PER_SWEEP),
        ).fetchall()
    if not rows:
        return

    bot = app.bot_data["chat_to_bot"].get(group_id)
    if bot is None:
        log.warning("group_ping_loop: no bot mapped for group %s", group_id)
        return

    for r in rows:
        hint = f" — suggest {r['suggested_name']}" if r["suggested_name"] else ""
        text = (
            f"🆕 {_fmt_money(r['amount_cents'])} {r['payee']} "
            f"({r['txn_date']}){hint}\n"
            f"Reply to this message with a category to file it — "
            f"or ignore it, it's in the Inbox."
        )
        try:
            sent = await bot.send_message(chat_id=group_id, text=text)
        except Exception as e:  # noqa: BLE001
            log.warning("group ping send failed for pt %s: %s", r["id"], e)
            continue
        with storage.connect(db_path) as con:
            con.execute(
                """INSERT INTO question
                     (kind, item_kind, item_id, tg_chat_id, asked_message_id)
                   VALUES ('instant', 'txn', ?, ?, ?)""",
                (r["id"], group_id, sent.message_id),
            )
        storage.audit(db_path, "group_ping_sent", {
            "pt_id": r["id"], "message_id": sent.message_id,
            "payee": r["payee"], "amount_cents": r["amount_cents"],
        })


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------

_SKIP_WORDS = {"skip", "not sure", "idk", "dunno", "no idea", "later", "?"}


async def handle_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """All text in the household group routes here (registered before the
    DM AI-agent handler, so group traffic never hits DM logic)."""
    settings = context.application.bot_data["settings"]
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return
    user = resolve_group_user(settings, msg.from_user.id)
    if user is None:
        return  # stranger — ignore silently

    reply_to = msg.reply_to_message
    if reply_to is not None:
        await _handle_question_reply(context.application, settings,
                                      msg, user, reply_to.message_id)
        return
    # Non-reply group chatter: with privacy mode on we only see @mentions
    # and commands. Referee mode / free-text Q&A lands in a later phase —
    # stay quiet rather than interject (restraint rule).
    return


async def _handle_question_reply(
    app: Application, settings, msg, user: str, replied_message_id: int,
) -> None:
    db_path = settings.paths.database
    with storage.connect(db_path) as con:
        q = con.execute(
            """SELECT * FROM question
               WHERE tg_chat_id = ? AND asked_message_id = ?
               ORDER BY id DESC LIMIT 1""",
            (msg.chat_id, replied_message_id),
        ).fetchone()
    if q is None:
        return  # reply to something that isn't a tracked question
    if q["state"] != "open":
        await msg.reply_text("Already handled — nothing to do. ✓")
        return

    text = (msg.text or "").strip()
    low = text.lower().rstrip(".!")

    if low in _SKIP_WORDS:
        _close_question(db_path, q["id"], "answered", user, text)
        await msg.reply_text(
            "No problem — it stays in the Inbox for later.")
        return

    from bot.agent_tools import _resolve_category
    cat = _resolve_category(db_path, text)
    if cat is None:
        await msg.reply_text(
            f"Couldn't match “{text}” to a category — "
            f"try the exact name, or say “skip”."
        )
        return

    filed = _file_item(db_path, q, cat["id"], user)
    if not filed:
        await msg.reply_text("That item seems to be gone — nothing filed.")
        _close_question(db_path, q["id"], "resolved_elsewhere", user, text)
        return

    _close_question(db_path, q["id"], "answered", user, text)
    storage.audit(db_path, "group_reply_filed", {
        "question_id": q["id"], "item_kind": q["item_kind"],
        "item_id": q["item_id"], "category_id": cat["id"], "by": user,
    })
    await msg.reply_text(
        f"✓ Filed {filed['payee']} ({_fmt_money(filed['amount_cents'])}) "
        f"→ {cat['name']}"
    )


def _file_item(db_path, q, category_id: str, user: str) -> dict | None:
    """Commit a category on the question's item. Mirrors the desktop
    /categorize endpoint: pending_txn + ledger promotion, filed_by stamped."""
    if q["item_kind"] != "txn":
        return None  # order questions arrive with the batch feature
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, payee, amount_cents, ynab_txn_id, status "
            "FROM pending_txn WHERE id = ?", (q["item_id"],),
        ).fetchone()
        if pt is None or pt["status"] not in ("pending", "skipped"):
            return None
        con.execute(
            "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
            "status = 'categorized', filed_by = ? WHERE id = ?",
            (category_id, storage._utcnow(), user, pt["id"]),
        )
        yid = pt["ynab_txn_id"] or ""
        if yid.startswith("ledger:"):
            try:
                lid = int(yid.split(":", 1)[1])
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (category_id, lid),
                )
            except (ValueError, IndexError):
                pass
        elif yid:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE ynab_txn_id = ?",
                (category_id, yid),
            )
        return dict(pt)


def _close_question(db_path, question_id: int, state: str,
                    user: str, answer_text: str) -> None:
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE question SET state = ?, answered_by = ?, "
            "answer_text = ?, resolved_at = ? WHERE id = ?",
            (state, user, answer_text, storage._utcnow(), question_id),
        )


def resolve_open_questions_for_item(db_path, item_kind: str, item_id: int,
                                     via: str) -> list[int]:
    """When an item is resolved on another surface (desktop Inbox, DM),
    close its open group questions so both surfaces agree. Returns the
    message ids that can be annotated."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT id, asked_message_id FROM question
               WHERE item_kind = ? AND item_id = ? AND state = 'open'""",
            (item_kind, item_id),
        ).fetchall()
        for r in rows:
            con.execute(
                "UPDATE question SET state = 'resolved_elsewhere', "
                "answered_by = ?, resolved_at = ? WHERE id = ?",
                (via, storage._utcnow(), r["id"]),
            )
    return [r["asked_message_id"] for r in rows if r["asked_message_id"]]
