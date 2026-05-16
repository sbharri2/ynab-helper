"""Long-polling Telegram bot — the user-facing loop.

Responsibilities:

  1. Handle /start and /pending commands
  2. Render the next pending item (order or txn) with an inline keyboard
     (top suggestion + alternative chips + Other + Skip)
  3. Route inline-button callbacks AND free-text replies to a single
     ``_apply_choice`` resolver
  4. Run a background "push loop" that watches the DB for newly-inserted
     pending items and surfaces them automatically — except during the
     user's configured quiet hours

Everything outside ``run()`` is module-level state. We intentionally keep
the wiring shallow so the long-poll loop and the push loop share the
same Settings, YnabClient, and Categorizer instances.

The handlers are ``async def`` because python-telegram-bot v20+ moved
fully async. The push loop is a normal coroutine spawned via
``Application.post_init``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, timezone


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (replaces deprecated _utcnow())."""
    return datetime.now(timezone.utc)
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import storage
from bot.categorizer import Categorizer
from bot.config import Settings, load_settings
from bot.conversation import format_item_prompt, next_item_for_user
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (no Telegram, no DB) — easy to unit-test
# ---------------------------------------------------------------------------

_CONFIRM_TOKENS = {"y", "yes", "yep", "yeah", "ok", "okay", "✅", "👍"}
_SKIP_TOKENS = {"skip", "/skip", "n", "no", "nope"}
_UNDO_TOKENS = {"/undo", "undo"}


def _parse_user_reply(text: str) -> tuple[str, Any]:
    """Classify a free-text user reply into one of four actions.

    Returns a ``(kind, payload)`` tuple where ``kind`` is one of:
      - ``"confirm"`` — accept the suggested category (payload: None)
      - ``"skip"``    — skip this item        (payload: None)
      - ``"undo"``    — undo last action      (payload: None)
      - ``"text"``    — free-text category name / hint (payload: str)
    """
    if text is None:
        return ("text", "")
    stripped = text.strip()
    lower = stripped.lower()
    if lower in _CONFIRM_TOKENS:
        return ("confirm", None)
    if lower in _SKIP_TOKENS:
        return ("skip", None)
    if lower in _UNDO_TOKENS:
        return ("undo", None)
    return ("text", stripped)


def _in_quiet_hours(now: datetime, window: str) -> bool:
    """Is ``now`` inside the ``HH:MM-HH:MM`` quiet-hours window?

    Handles overnight wrap (start > end, e.g. ``"22:00-07:00"``) correctly.
    A malformed window string returns False so we don't accidentally
    silence the bot forever.
    """
    try:
        start_str, end_str = window.split("-")
        sh, sm = (int(x) for x in start_str.split(":"))
        eh, em = (int(x) for x in end_str.split(":"))
    except (ValueError, AttributeError):
        return False
    start = dtime(sh, sm)
    end = dtime(eh, em)
    cur = now.time()
    if start <= end:
        # Same-day window, e.g. 13:00-15:00
        return start <= cur < end
    # Overnight wrap, e.g. 22:00-07:00
    return cur >= start or cur < end


def _in_digest_window(
    now: datetime, digest_time_str: str, window_minutes: int = 60
) -> bool:
    """True if ``now`` is within ``window_minutes`` after ``digest_time_str``.

    ``digest_time_str`` is "HH:MM". Used to gate non-Amazon/Venmo pending_txn
    items so they only push during the daily digest window (e.g., 9-10am)
    rather than the moment they're detected.

    Malformed input returns False so a typo doesn't accidentally batch
    everything forever.
    """
    try:
        target = dtime.fromisoformat(digest_time_str)
    except (ValueError, TypeError):
        return False
    target_today = datetime.combine(now.date(), target)
    if now.tzinfo is not None:
        target_today = target_today.replace(tzinfo=now.tzinfo)
    delta_minutes = (now - target_today).total_seconds() / 60
    return 0 <= delta_minutes <= window_minutes


# ---------------------------------------------------------------------------
# Inline keyboard rendering
# ---------------------------------------------------------------------------

def _build_keyboard(item: dict, categories: list[dict]) -> InlineKeyboardMarkup:
    """Top suggestion (if any) + up to 3 alternative chips + Other + Skip.

    Callback-data conventions:
      ``cat:<id>``  — pick a specific YNAB category by id
      ``skip``      — skip this item
      ``other``     — user wants to type a free-text category
    """
    buttons: list[list[InlineKeyboardButton]] = []
    suggested_id = item.get("suggested_category")
    if suggested_id:
        suggested_name = next(
            (c["name"] for c in categories if c["id"] == suggested_id),
            None,
        )
        if suggested_name:
            buttons.append([
                InlineKeyboardButton(
                    f"✅ {suggested_name}",
                    callback_data=f"cat:{suggested_id}",
                )
            ])

    # Three alternative chips: pick the next 3 from the category list,
    # skipping the suggestion if present.
    alternatives = [c for c in categories if c["id"] != suggested_id][:3]
    if alternatives:
        buttons.append([
            InlineKeyboardButton(c["name"], callback_data=f"cat:{c['id']}")
            for c in alternatives
        ])

    buttons.append([
        InlineKeyboardButton("🔤 Other", callback_data="other"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip"),
    ])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Item rendering / apply
# ---------------------------------------------------------------------------

def _resolve_user_id_for_chat(settings: Settings, chat_id: int) -> str | None:
    for acct in settings.gmail_accounts:
        if acct.chat_id == chat_id:
            return acct.user_id
    return None


def _annotate_with_suggestion_name(item: dict, categories: list[dict]) -> dict:
    """Attach the human-readable suggestion name for prompt rendering."""
    sid = item.get("suggested_category")
    if sid:
        name = next((c["name"] for c in categories if c["id"] == sid), None)
        item["suggested_category_name"] = name
    return item


async def _push_item_to_user(
    app: Application,
    settings: Settings,
    categories: list[dict],
    chat_id: int,
    user_id: str,
    item: dict,
) -> bool:
    """Render a specific item and record it as the last-asked for this chat.

    Extracted from ``_push_next_item`` so commands like ``/digest`` can fetch
    an item with custom filters (e.g., forcing include_txns=True) and still
    use the same rendering + bookkeeping path.
    """
    item = _annotate_with_suggestion_name(item, categories)
    body = format_item_prompt(item)
    keyboard = _build_keyboard(item, categories)
    try:
        await app.bot.send_message(chat_id=chat_id, text=body, reply_markup=keyboard)
    except Exception as e:  # noqa: BLE001 - never crash the push loop
        log.error("send_message failed for chat %s: %s", chat_id, e)
        return False

    # Remember what we just asked so the next free-text reply can resolve.
    with storage.connect(settings.paths.database) as con:
        con.execute(
            """
            INSERT INTO bot_conversation
              (chat_id, user_id, last_asked_kind, last_asked_id, last_action_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              user_id = excluded.user_id,
              last_asked_kind = excluded.last_asked_kind,
              last_asked_id = excluded.last_asked_id,
              last_action_at = excluded.last_action_at
            """,
            (chat_id, user_id, item["kind"], item["id"], _utcnow()),
        )
    return True


async def _push_next_item(
    app: Application,
    settings: Settings,
    categories: list[dict],
    chat_id: int,
    user_id: str,
    *,
    include_txns: bool | None = None,
) -> bool:
    """Render and send the next pending item to the chat.

    If ``include_txns`` is None, gate on the daily-digest window so
    pending_txn items only push between digest_time and +window_minutes.
    pending_order items (Amazon/Venmo) are always eligible.

    Returns True if an item was sent, False if the queue is empty.
    """
    if include_txns is None:
        include_txns = _in_digest_window(
            datetime.now(), settings.telegram.daily_digest_time
        )
    item = next_item_for_user(
        settings.paths.database, user_id=user_id, include_txns=include_txns,
    )
    if item is None:
        return False
    return await _push_item_to_user(
        app, settings, categories, chat_id, user_id, item,
    )


def _apply_choice(
    settings: Settings,
    categorizer: Categorizer,
    categories: list[dict],
    *,
    chat_id: int,
    user_id: str,
    choice_kind: str,
    payload: Any,
) -> str:
    """Apply a user's choice to the *current* pending item.

    Resolves the chosen category from one of three sources:
      - ``"confirm"`` — use the row's stored ``suggested_category``
      - ``"callback:<id>"`` — exact category id from an inline button
      - ``"text"`` — free-text. Try a case-insensitive name match first,
        then fall back to the Categorizer to disambiguate.

    Returns a short human-readable status string for the bot to echo.
    """
    # Look up what we last asked this chat about.
    with storage.connect(settings.paths.database) as con:
        row = con.execute(
            "SELECT last_asked_kind, last_asked_id FROM bot_conversation "
            "WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if row is None or row["last_asked_id"] is None:
        return "Nothing pending — try /pending."

    kind = row["last_asked_kind"]
    item_id = row["last_asked_id"]
    table = "pending_order" if kind == "order" else "pending_txn"

    # Pull the row so we have access to suggested_category, summary, etc.
    with storage.connect(settings.paths.database) as con:
        item_row = con.execute(
            f"SELECT * FROM {table} WHERE id = ?", (item_id,),
        ).fetchone()
    if item_row is None:
        return "Couldn't find that item anymore."
    item = dict(item_row)

    # --- skip -----------------------------------------------------------
    if choice_kind == "skip":
        if kind == "txn":
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    "UPDATE pending_txn SET status = 'skipped' WHERE id = ?",
                    (item_id,),
                )
        # Orders stay 'pending' — we'll re-ask them later. We just clear
        # the conversation pointer so the next push picks the next item.
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE bot_conversation SET last_asked_id = NULL WHERE chat_id = ?",
                (chat_id,),
            )
        storage.audit(settings.paths.database, "skipped",
                      {"kind": kind, "id": item_id})
        return "Skipped."

    # --- resolve a category id ------------------------------------------
    category_id: str | None = None

    if choice_kind == "confirm":
        category_id = item.get("suggested_category")
        if not category_id:
            return "No suggestion to confirm — type a category name."
    elif choice_kind.startswith("callback:"):
        category_id = choice_kind.split(":", 1)[1]
    elif choice_kind == "text":
        name_query = (payload or "").strip().lower()
        # Try exact case-insensitive name match first
        exact = next(
            (c for c in categories if c["name"].lower() == name_query),
            None,
        )
        if exact:
            category_id = exact["id"]
        else:
            # Fall back to the LLM to interpret the free text
            summary = item.get("raw_summary") or item.get("payee") or ""
            amount = item.get("total_cents") or item.get("amount_cents") or 0
            date_str = str(item.get("order_date") or item.get("txn_date") or "")
            source = item.get("source") or "ynab"
            suggestion = categorizer.suggest(
                summary=f"{summary} (user hint: {payload})",
                amount_cents=amount,
                date_str=date_str,
                source=source,
                categories=categories,
            )
            category_id = suggestion.get("category_id")

    if not category_id:
        return "Sorry, I couldn't match that to a category. Try a button."

    # --- persist --------------------------------------------------------
    cat_name = next((c["name"] for c in categories if c["id"] == category_id),
                    category_id)
    if kind == "order":
        storage.mark_order_categorized(
            settings.paths.database, item_id, chosen_category=category_id,
        )
    else:
        # txn — categorize directly in YNAB
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE pending_txn SET chosen_category = ?, "
                "chosen_at = ?, status = 'categorized' WHERE id = ?",
                (category_id, _utcnow(), item_id),
            )
        try:
            ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
            ynab.set_category(item["ynab_txn_id"], category_id)
        except Exception as e:  # noqa: BLE001
            log.error("YNAB set_category failed: %s", e)

    # Clear the conversation pointer so the next push grabs the next item.
    with storage.connect(settings.paths.database) as con:
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL WHERE chat_id = ?",
            (chat_id,),
        )
    storage.audit(settings.paths.database, "categorized",
                  {"kind": kind, "id": item_id, "category": category_id})
    return f"Categorized as {cat_name}."


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text(
            "Hi! This chat isn't linked to a user in config.yaml.\n"
            f"Add chat_id={chat_id} to a gmail_accounts entry."
        )
        return
    await update.message.reply_text(
        f"Hi {user_id} — I'll ping you as items come in.\n"
        "Use /pending to see the next item now."
    )


async def _pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    categories: list[dict] = context.application.bot_data["categories"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return
    sent = await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
    )
    if not sent:
        await update.message.reply_text("Nothing pending. ✨")


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]

    query = update.callback_query
    await query.answer()  # dismiss the spinner
    chat_id = query.message.chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await query.edit_message_text("This chat isn't linked. See /start.")
        return

    data = query.data or ""
    if data == "skip":
        status = _apply_choice(
            settings, categorizer, categories,
            chat_id=chat_id, user_id=user_id,
            choice_kind="skip", payload=None,
        )
    elif data == "other":
        await query.edit_message_text(
            (query.message.text or "") + "\n\nType the category name…"
        )
        return
    elif data.startswith("cat:"):
        cat_id = data.split(":", 1)[1]
        status = _apply_choice(
            settings, categorizer, categories,
            chat_id=chat_id, user_id=user_id,
            choice_kind=f"callback:{cat_id}", payload=None,
        )
    else:
        status = "Unknown action."

    try:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n→ {status}"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("edit_message_text failed: %s", e)

    # Push the next item immediately so the user can keep going.
    await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
    )


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]

    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    kind, payload = _parse_user_reply(update.message.text or "")

    if kind == "undo":
        # Reuse the slash-command handler so text "undo" and "/undo" agree.
        await _undo_cmd(update, context)
        return

    status = _apply_choice(
        settings, categorizer, categories,
        chat_id=chat_id, user_id=user_id,
        choice_kind=kind, payload=payload,
    )
    await update.message.reply_text(status)

    # Push the next item.
    await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
    )


# ---------------------------------------------------------------------------
# Push loop — watches the DB for new pending items
# ---------------------------------------------------------------------------

async def _push_loop(app: Application) -> None:
    """Long-running background coroutine.

    Every 30 seconds:
      - Skip entirely if we're inside the configured quiet hours
      - Otherwise, for each linked chat, if there is no item currently
        "in flight" (no ``last_asked_id``) AND the queue has something,
        push the next item.
    """
    settings: Settings = app.bot_data["settings"]
    categories: list[dict] = app.bot_data["categories"]
    poll_interval = 30  # seconds

    while True:
        try:
            now = datetime.now()
            if _in_quiet_hours(now, settings.telegram.quiet_hours):
                await asyncio.sleep(poll_interval)
                continue

            for account in settings.gmail_accounts:
                chat_id = account.chat_id
                user_id = account.user_id

                # If a question is already in-flight for this chat, don't
                # double-prompt — wait for the user to answer. Also respect
                # per-chat /quiet (quiet_until column).
                with storage.connect(settings.paths.database) as con:
                    row = con.execute(
                        "SELECT last_asked_id, quiet_until FROM bot_conversation "
                        "WHERE chat_id = ?",
                        (chat_id,),
                    ).fetchone()
                if row and row["last_asked_id"] is not None:
                    continue
                if row and row["quiet_until"] is not None:
                    qu = row["quiet_until"]
                    # qu is a tz-naive datetime (stored as isoformat); compare
                    # against tz-naive `now` from datetime.now() above.
                    if isinstance(qu, datetime) and now < qu:
                        continue

                await _push_next_item(app, settings, categories, chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let the loop die
            log.exception("push_loop iteration failed: %s", e)

        await asyncio.sleep(poll_interval)


async def _post_init(app: Application) -> None:
    """Spawn the push loop after the Application has fully started."""
    app.bot_data["push_task"] = asyncio.create_task(_push_loop(app))
    log.info("push loop started")


async def _skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skip the current pending item (same behavior as replying 'skip')."""
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]

    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    status = _apply_choice(
        settings, categorizer, categories,
        chat_id=chat_id, user_id=user_id,
        choice_kind="skip", payload=None,
    )
    await update.message.reply_text(status)
    await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
    )


async def _digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-push the next item including pending_txns regardless of window.

    Useful when the user wants to clear their digest queue outside the
    configured daily_digest_time window.
    """
    settings: Settings = context.application.bot_data["settings"]
    categories: list[dict] = context.application.bot_data["categories"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    sent = await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
        include_txns=True,
    )
    if not sent:
        await update.message.reply_text("Queue is empty. ✨")


async def _quiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-chat notification mute. Usage: /quiet on  or  /quiet off."""
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    arg = " ".join(context.args or []).strip().lower()
    if arg == "on":
        until = datetime.now() + timedelta(hours=24)
        with storage.connect(settings.paths.database) as con:
            con.execute(
                """
                INSERT INTO bot_conversation (chat_id, user_id, quiet_until)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET quiet_until = excluded.quiet_until
                """,
                (chat_id, user_id, until),
            )
        await update.message.reply_text(
            "Notifications paused for 24h. /quiet off to resume."
        )
    elif arg == "off":
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE bot_conversation SET quiet_until = NULL WHERE chat_id = ?",
                (chat_id,),
            )
        await update.message.reply_text("Notifications resumed.")
    else:
        await update.message.reply_text("Usage: /quiet on  or  /quiet off")


async def _undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revert the most recent categorization within a 5-minute window.

    For pending_orders the revert is fully local (categorization hasn't been
    pushed to YNAB yet — that happens on match). For pending_txns the category
    was already applied to YNAB, so we revert local state and warn the user.
    """
    settings: Settings = context.application.bot_data["settings"]
    with storage.connect(settings.paths.database) as con:
        row = con.execute(
            """
            SELECT id, details FROM audit_log
            WHERE event = 'categorized'
              AND ts >= datetime('now', '-5 minutes')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            await update.message.reply_text("Nothing to undo (5-min window).")
            return
        try:
            details = json.loads(row["details"]) if row["details"] else {}
        except json.JSONDecodeError:
            details = {}
        kind = details.get("kind")
        item_id = details.get("id")
        if not kind or item_id is None:
            await update.message.reply_text("Couldn't parse last action — nothing undone.")
            return

        if kind == "order":
            con.execute(
                "UPDATE pending_order SET chosen_category = NULL, "
                "chosen_at = NULL, status = 'pending' WHERE id = ?",
                (item_id,),
            )
            msg = "Reverted. The order is back in your queue."
        else:
            con.execute(
                "UPDATE pending_txn SET chosen_category = NULL, "
                "chosen_at = NULL, status = 'pending' WHERE id = ?",
                (item_id,),
            )
            msg = (
                "Reverted locally. ⚠️ The category was already applied to YNAB — "
                "you'll need to edit it manually in YNAB if you want to clear it there too."
            )

    storage.audit(
        settings.paths.database, "undone", {"kind": kind, "id": item_id},
    )
    await update.message.reply_text(msg)


async def _help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "  /start    — link this chat (one-time setup)\n"
        "  /pending  — show the next queued item\n"
        "  /digest   — force the next pending txn even outside digest window\n"
        "  /skip     — skip the current item\n"
        "  /undo     — revert your last categorization (5-min window)\n"
        "  /quiet on|off — pause/resume notifications for 24h\n"
        "  /help     — this message\n\n"
        "Replies during a categorization prompt:\n"
        "  y / yes / ✅   — confirm the suggested category\n"
        "  <category name> — use that category (e.g. 'Groceries')\n"
        "  <free text>    — LLM picks the best-matching category\n"
        "  skip           — defer this item"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(settings: Settings | None = None) -> None:
    """Build the Application, register handlers, and block on long-polling."""
    if settings is None:
        settings = load_settings()
    storage.init_db(settings.paths.database)

    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    categories = ynab.list_categories() if settings.ynab_token else []
    categorizer = Categorizer(
        settings.ollama.endpoint,
        settings.ollama.model,
        settings.ollama.temperature,
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["categories"] = categories
    app.bot_data["categorizer"] = categorizer

    app.add_handler(CommandHandler("start", _start_cmd))
    app.add_handler(CommandHandler("pending", _pending_cmd))
    app.add_handler(CommandHandler("skip", _skip_cmd))
    app.add_handler(CommandHandler("digest", _digest_cmd))
    app.add_handler(CommandHandler("quiet", _quiet_cmd))
    app.add_handler(CommandHandler("undo", _undo_cmd))
    app.add_handler(CommandHandler("help", _help_cmd))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))

    log.info("starting telegram long-poll")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
