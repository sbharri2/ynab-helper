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

_DAY_OF_MONTH_SUFFIX_RE = __import__("re").compile(
    r"\s*\(\s*\d{1,2}(?:st|nd|rd|th)\s*\)\s*$", __import__("re").IGNORECASE,
)


def _norm_cat_name(name: str) -> str:
    """Strip parenthesized day-of-month suffix + lowercase for matching.

    Examples:
        "Cell Phone (4th)"       -> "cell phone"
        "HOA - Woodcreek (14th)" -> "hoa - woodcreek"
        "Groceries"              -> "groceries"
    """
    if not name:
        return ""
    return _DAY_OF_MONTH_SUFFIX_RE.sub("", name).strip().lower()


def _match_category_by_name(
    db_path: str, query: str, categories: list[dict],
) -> str | None:
    """Match user-typed text against any category (incl. non-spending).

    Strategy, in priority order:
      1. Exact case-insensitive match against full name
      2. Exact match after stripping parenthesized day-of-month suffixes
      3. Substring match (query is contained in normalized category name)
      4. None — caller falls through to LLM

    User has explicitly typed a category name, so non-spending categories
    (scheduled bills, savings, etc.) are eligible — the spending-only
    filter is just for auto-suggestions.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    # Build a richer category pool that includes ALL local categories,
    # not just the YnabClient-fetched list passed in. This matters when
    # the bot's `categories` are spending-only — bills like Cell Phone
    # (4th) wouldn't be in that list.
    pool: list[dict] = list(categories)
    seen_ids = {c["id"] for c in pool}
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id, name FROM category WHERE hidden = 0"
        ).fetchall()
    for r in rows:
        if r["id"] not in seen_ids:
            pool.append({"id": r["id"], "name": r["name"]})

    # Tier 1: exact full-name match
    for c in pool:
        if c["name"].lower() == q:
            return c["id"]
    # Tier 2: exact match after suffix strip
    q_norm = _norm_cat_name(q)
    for c in pool:
        if _norm_cat_name(c["name"]) == q_norm:
            return c["id"]
    # Tier 3: substring (query inside normalized name)
    candidates = [c for c in pool if q_norm and q_norm in _norm_cat_name(c["name"])]
    if len(candidates) == 1:
        return candidates[0]["id"]
    # Multiple substring hits → ambiguous, let LLM disambiguate
    return None


def _llm_topn_picks(
    settings: Settings,
    categorizer: Categorizer,
    categories: list[dict],
    item: dict,
    payee: str,
    priors: list[dict],
) -> list[dict]:
    """Sync helper: fetch payee intel, run topn ranker, return [{category_id, confidence}].

    Wrapped by asyncio.to_thread in the caller because the LLM calls (qwen
    description + topn ranker) are blocking HTTP. Returns [] on any failure
    — caller treats that as "no extra picks" and falls back to the priors
    layout. Restricts the LLM's choices to spending categories only.
    """
    try:
        from bot.payee_intel import get_intel
        intel_row = get_intel(
            settings.paths.database, payee,
            endpoint=settings.ollama.endpoint, model=settings.ollama.model,
        )
        intel = (intel_row or {}).get("description")
        spending = storage.list_categories_for_spending(settings.paths.database)
        cats = (
            [{"id": c["id"], "name": c["name"], "group_name": c["group_name"]}
             for c in spending]
            if spending else categories
        )
        summary = item.get("raw_summary") or payee
        amount = item.get("amount_cents") or item.get("total_cents") or 0
        date_str = str(item.get("txn_date") or item.get("order_date") or "")
        return categorizer.suggest_topn(
            summary=summary,
            amount_cents=amount,
            date_str=date_str,
            source=("amazon" if "amazon" in payee.lower() else "ynab"),
            categories=cats,
            priors=priors,
            intel=intel,
            n=4,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("_llm_topn_picks failed: %s", e)
        return []


def _build_keyboard(
    item: dict,
    categories: list[dict],
    priors: list[dict] | None = None,
    extra_picks: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    """Up to 4 educated guesses + Other + Skip.

    Educated guesses are drawn from (in priority order):
      1. The LLM's ``suggested_category`` on the item (marked with ✅)
      2. The payee's historical priors (most-used categories for this
         merchant), passed in by the caller via ``priors``

    If priors give fewer than 4 total picks, the keyboard is just shorter —
    we deliberately avoid padding with random categories, because that's
    what made the old "first 3 in the list" alternatives feel useless.

    Callback-data conventions:
      ``cat:<id>``  — pick a specific category by id
      ``skip``      — skip this item
      ``other``     — user wants to type a free-text category
    """
    name_by_id = {c["id"]: c["name"] for c in categories}
    suggested_id = item.get("suggested_category")

    picks: list[str] = []
    if suggested_id and suggested_id in name_by_id:
        picks.append(suggested_id)
    if priors:
        for p in priors:
            cid = p.get("category_id")
            if cid and cid in name_by_id and cid not in picks:
                picks.append(cid)
            if len(picks) >= 4:
                break
    # If priors + LLM suggestion didn't give us 4 picks, fill with the
    # ranked top-N from the LLM (with payee intel injected). This is the
    # "unknown payee" path — caller paid for the slow LLM call and we
    # finish populating the row.
    if extra_picks and len(picks) < 4:
        for p in extra_picks:
            cid = p.get("category_id")
            if cid and cid in name_by_id and cid not in picks:
                picks.append(cid)
            if len(picks) >= 4:
                break

    # Lay out picks 2-per-row. The first pick (LLM suggestion, if any) gets
    # the ✅ marker so the user can still spot the model's top pick.
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, cid in enumerate(picks):
        label = name_by_id[cid]
        if i == 0 and suggested_id == cid:
            label = f"✅ {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"cat:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

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


def _get_push_lock(app: Application, chat_id: int) -> asyncio.Lock:
    """Per-chat asyncio.Lock so only one push runs at a time for a given chat.

    Without this, the 30s push loop and an interactive handler (callback or
    text reply) can both clear last_asked_id, query the queue, and send a
    message concurrently — surfacing two "open" questions to the user.
    """
    locks = app.bot_data.setdefault("push_locks", {})
    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock
    return lock


async def _strip_previous_keyboard(
    app: Application, chat_id: int, message_id: int | None
) -> None:
    """Remove the inline keyboard from the previous in-flight question.

    Once a new question is about to be sent, the old one is stale — leaving
    its keyboard live confuses the user about which item they're answering.
    """
    if not message_id:
        return
    try:
        await app.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None,
        )
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        # Common: "Message is not modified" if it was already stripped, or
        # "Message to edit not found" if too old. Both safe to ignore.
        log.debug("strip keyboard skipped (chat=%s msg=%s): %s",
                  chat_id, message_id, e)


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

    Holds the per-chat push lock across the (fetch next item → check it's
    not already in flight → strip old keyboard → send → record) sequence
    so concurrent callers can't end up double-sending. Returns True if an
    item was sent, False if the queue is empty or another caller already
    pushed the same item.
    """
    if include_txns is None:
        include_txns = _in_digest_window(
            datetime.now(), settings.telegram.daily_digest_time
        )

    lock = _get_push_lock(app, chat_id)
    async with lock:
        item = next_item_for_user(
            settings.paths.database, user_id=user_id, include_txns=include_txns,
        )
        if item is None:
            return False

        # Race guard: if another caller already pushed this same item,
        # don't double-send.
        with storage.connect(settings.paths.database) as con:
            row = con.execute(
                "SELECT last_asked_id, last_asked_message_id FROM bot_conversation "
                "WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row and row["last_asked_id"] == item["id"]:
            return False

        item = _annotate_with_suggestion_name(item, categories)
        body = format_item_prompt(item)
        # Educated-guess buttons: pull this payee's historical priors so the
        # 4 non-skip buttons are real candidates instead of arbitrary
        # alphabetical alternatives.
        payee = item.get("payee") or item.get("counterparty") or ""
        priors = storage.get_category_priors_for_payee(
            settings.paths.database, payee, top_n=5,
        ) if payee else []
        # Count how many distinct buttons priors+suggestion give us. If
        # under 4, fall back to LLM top-N ranker with payee intel (qwen's
        # training knowledge first, DDG web search if qwen doesn't know).
        # This is the slow path (~10-20s) — only invoke when the cheap
        # priors-based row is insufficient.
        extra_picks: list[dict] = []
        prior_ids = {p["category_id"] for p in priors}
        if item.get("suggested_category"):
            prior_ids.add(item["suggested_category"])
        if len(prior_ids) < 4 and payee:
            categorizer: Categorizer = app.bot_data["categorizer"]
            extra_picks = await asyncio.to_thread(
                _llm_topn_picks,
                settings, categorizer, categories, item, payee, priors,
            )
        keyboard = _build_keyboard(
            item, categories, priors=priors, extra_picks=extra_picks,
        )

        # Strip the inline keyboard off the prior question so the chat only
        # ever has one tappable question at a time.
        prev_msg_id = row["last_asked_message_id"] if row else None
        await _strip_previous_keyboard(app, chat_id, prev_msg_id)

        try:
            sent = await app.bot.send_message(
                chat_id=chat_id, text=body, reply_markup=keyboard,
            )
        except Exception as e:  # noqa: BLE001 - never crash the push loop
            log.error("send_message failed for chat %s: %s", chat_id, e)
            return False

        with storage.connect(settings.paths.database) as con:
            con.execute(
                """
                INSERT INTO bot_conversation
                  (chat_id, user_id, last_asked_kind, last_asked_id,
                   last_asked_message_id, last_action_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  last_asked_kind = excluded.last_asked_kind,
                  last_asked_id = excluded.last_asked_id,
                  last_asked_message_id = excluded.last_asked_message_id,
                  last_action_at = excluded.last_action_at
                """,
                (chat_id, user_id, item["kind"], item["id"],
                 sent.message_id, _utcnow()),
            )
    return True


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
        # When the user types a category name, they've already decided.
        # Match against ALL categories (including non-spending bills like
        # "Cell Phone (4th)", "Internet (12th)", "HOA - Woodcreek (14th)")
        # — the spending-only filter is for LLM auto-suggestions, not
        # explicit user picks. Strip parenthesized day-of-month suffixes
        # for comparison so "cell phone" matches "Cell Phone (4th)".
        category_id = _match_category_by_name(
            settings.paths.database, payload or "", categories,
        )
        if not category_id:
            # Last-resort LLM disambig. Same all-categories pool — never
            # narrow the user's options when they're explicitly typing.
            summary = item.get("raw_summary") or item.get("payee") or ""
            amount = item.get("total_cents") or item.get("amount_cents") or 0
            date_str = str(item.get("order_date") or item.get("txn_date") or "")
            source = item.get("source") or "ynab"
            payee = item.get("payee") or item.get("counterparty") or source
            priors = storage.get_category_priors_for_payee(
                settings.paths.database, payee, top_n=5,
            )
            suggestion = categorizer.suggest(
                summary=f"{summary} (user hint: {payload})",
                amount_cents=amount,
                date_str=date_str,
                source=source,
                categories=categories,
                priors=priors,
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
        include_txns=True,
    )
    if not sent:
        await update.message.reply_text("Nothing pending. ✨")


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]

    query = update.callback_query
    chat_id = query.message.chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await query.answer()
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
        await query.answer()
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

    # Telegram callback-query answers can carry a toast popup (max 200 chars).
    # We use it so the user gets visible confirmation of their tap — the
    # message edit alone is silent UX on the phone.
    try:
        await query.answer(text=status[:200], show_alert=False)
    except Exception as e:  # noqa: BLE001
        log.warning("query.answer failed: %s", e)
    try:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n→ {status}"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("edit_message_text failed: %s", e)

    # Push the next item immediately so the user can keep going. Always
    # include pending_txns - if the user is actively replying they're in
    # an interactive session, not the background-digest case the window
    # was designed to gate.
    await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
        include_txns=True,
    )


def _is_trivial_reply(text: str) -> bool:
    """True when the message is a known confirm/skip/undo token.

    These take the fast path through _handle_text (~instant) instead of the
    AI agent (~10s). Anything else gets the AI treatment.
    """
    if not text:
        return False
    norm = text.strip().lower()
    return (
        norm in _CONFIRM_TOKENS
        or norm in _SKIP_TOKENS
        or norm in _UNDO_TOKENS
    )


async def _handle_ai_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 3.5: free-text Telegram → qwen3:32b with tools.

    Two fast paths bypass the AI:
      1. Trivial confirm/skip/undo tokens — still resolved by _handle_text.
      2. Mid-categorization (in-flight last_asked_id) where the user types a
         single short word — usually a category name; let _handle_text resolve
         via the existing fuzzy match + LLM fallback, which is faster.
    """
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    text = (update.message.text or "").strip()

    if _is_trivial_reply(text):
        await _handle_text(update, context)
        return

    # If the user is mid-categorization, almost any short reply is a
    # category-name hint, not an agent query. _handle_text fuzzy-matches
    # against category names and falls back to LLM disambiguation, which
    # turns "sewing lessons" into "Sewing Class" automatically.
    #
    # Route to the AI agent ONLY when the reply looks like a question or
    # an explicit command (action verbs that match an agent tool).
    text_l = text.lower()
    QUESTION_STARTERS = (
        "how", "what", "where", "when", "why", "who",
        "show", "list", "give", "tell", "explain", "can you",
        "/agent",
    )
    # Action-verb starters that map to agent tools. Routing on these
    # prevents "move X to Y" / "create new category for Z" / "put $50
    # in groceries" / "next" / "skip" from being treated as category
    # name hints when mid-categorization.
    COMMAND_STARTERS = (
        "move", "create", "add", "put", "shift", "assign", "set",
        "change", "rename", "delete", "make", "pause", "quiet", "mute",
        "next", "advance",
    )
    first_word = text_l.split(" ", 1)[0] if text_l else ""
    looks_like_command = (
        text_l.startswith(QUESTION_STARTERS)
        or text.endswith("?")
        or first_word in COMMAND_STARTERS
    )
    if not looks_like_command and len(text) <= 60:
        with storage.connect(settings.paths.database) as con:
            row = con.execute(
                "SELECT last_asked_id FROM bot_conversation WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row and row["last_asked_id"] is not None:
            await _handle_text(update, context)
            return

    # Route through qwen3:32b with tools.
    from bot.agent import run_agent_turn
    from bot.agent_tools import ADVANCE_QUEUE_SENTINEL
    try:
        reply = await asyncio.to_thread(
            run_agent_turn,
            settings.paths.database,
            user_text=text,
            chat_id=chat_id,
            user_id=user_id,
            settings=settings,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("agent failed: %s", e)
        reply = "Something went wrong with the assistant. Try /help."

    # Special-case: agent returned the queue-advance sentinel. Don't echo it
    # to the user — just immediately push the next pending item.
    if reply == ADVANCE_QUEUE_SENTINEL:
        categories: list[dict] = context.application.bot_data["categories"]
        sent = await _push_next_item(
            context.application, settings, categories, chat_id, user_id,
            include_txns=True,
        )
        if not sent:
            await update.message.reply_text("Queue is empty. ✨")
        return

    # Telegram caps message bodies at 4096; chunk long replies.
    MAX = 4000
    while reply:
        chunk = reply[:MAX]
        try:
            await update.message.reply_text(chunk)
        except Exception as e:  # noqa: BLE001
            log.warning("reply_text failed: %s", e)
            break
        reply = reply[MAX:]


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

    # Push the next item. Active reply -> include pending_txns regardless
    # of digest window (the window only gates background-pushed DMs).
    await _push_next_item(
        context.application, settings, categories, chat_id, user_id,
        include_txns=True,
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

                # Always include pending_txns: the bot should be proactive
                # within the quiet_hours window, not gated to a 1-hour
                # digest-time slot. The digest window only mattered when
                # the bot was passively dripping unsuggested items; now
                # daily_catchup pre-suggests, so every queued row is ready.
                await _push_next_item(
                    app, settings, categories, chat_id, user_id,
                    include_txns=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let the loop die
            log.exception("push_loop iteration failed: %s", e)

        await asyncio.sleep(poll_interval)


async def _post_init(app: Application) -> None:
    """Spawn the push loop + Phase 5 summary loops after Application startup."""
    app.bot_data["push_task"] = asyncio.create_task(_push_loop(app))
    log.info("push loop started")
    app.bot_data["daily_task"] = asyncio.create_task(_daily_summary_loop(app))
    log.info("daily summary loop started")
    app.bot_data["weekly_task"] = asyncio.create_task(_weekly_summary_loop(app))
    log.info("weekly summary loop started")


def _next_fire_at(target_time: str, target_weekday: int | None = None) -> datetime:
    """Return the next local datetime when the summary should fire.

    `target_time` is "HH:MM" (24-hour local).
    `target_weekday` is Python weekday (Monday=0); if None, fire daily.
    """
    now = datetime.now()
    hh, mm = map(int, target_time.split(":"))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target_weekday is None:
        # Daily
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    # Weekly
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0 and candidate <= now:
        days_ahead = 7
    return candidate + timedelta(days=days_ahead)


async def _daily_summary_loop(app: Application) -> None:
    """Background coroutine: at config.telegram.daily_summary_time every day,
    DM the daily summary to opted-in recipients.

    Recomputes the current month's envelope state right before sending so
    the snapshot reflects all activity ingested overnight.
    """
    settings: Settings = app.bot_data["settings"]
    from bot.reporters.daily import send_daily_summaries
    from bot.envelope import recompute_month
    while True:
        try:
            fire_at = _next_fire_at(settings.telegram.daily_summary_time)
            sleep_for = max(1.0, (fire_at - datetime.now()).total_seconds())
            log.info("daily summary will fire at %s (in %.0fs)", fire_at, sleep_for)
            await asyncio.sleep(sleep_for)
            try:
                recompute_month(
                    settings.paths.database,
                    datetime.now().strftime("%Y-%m"),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("daily: recompute failed: %s", e)
            await send_daily_summaries(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("daily summary loop iter failed: %s", e)
            await asyncio.sleep(60)


async def _weekly_summary_loop(app: Application) -> None:
    """Background coroutine: at config.telegram.weekly_summary_time on the
    configured weekday, DM the weekly summary.
    """
    settings: Settings = app.bot_data["settings"]
    from bot.reporters.weekly import send_weekly_summaries
    while True:
        try:
            fire_at = _next_fire_at(
                settings.telegram.weekly_summary_time,
                settings.telegram.weekly_summary_day,
            )
            sleep_for = max(1.0, (fire_at - datetime.now()).total_seconds())
            log.info("weekly summary will fire at %s (in %.0fs)", fire_at, sleep_for)
            await asyncio.sleep(sleep_for)
            await send_weekly_summaries(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("weekly summary loop iter failed: %s", e)
            await asyncio.sleep(60)


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
        include_txns=True,
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
        "  /samples [<label>] [mark <id>] — Phase 0 sample inspection\n"
        "  /help     — this message\n\n"
        "Replies during a categorization prompt:\n"
        "  y / yes / ✅   — confirm the suggested category\n"
        "  <category name> — use that category (e.g. 'Groceries')\n"
        "  <free text>    — LLM picks the best-matching category\n"
        "  skip           — defer this item"
    )


async def _samples_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 0 operator command. Inspect raw_email_sample contents.

    Usage:
      /samples              -> per-label counts + age of newest sample
      /samples <label>      -> latest 5 from that label (subject + snippet)
      /samples mark <id>    -> mark reviewed=1
    """
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    args = context.args or []

    # /samples mark <id>
    if len(args) >= 2 and args[0].lower() == "mark":
        try:
            sample_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /samples mark <id>")
            return
        with storage.connect(settings.paths.database) as con:
            cur = con.execute(
                "UPDATE raw_email_sample SET reviewed = 1 WHERE id = ?",
                (sample_id,),
            )
        if cur.rowcount:
            await update.message.reply_text(f"Sample #{sample_id} marked reviewed.")
        else:
            await update.message.reply_text(f"No sample #{sample_id} found.")
        return

    # /samples <label>
    if args:
        label = args[0]
        with storage.connect(settings.paths.database) as con:
            rows = con.execute(
                """SELECT id, subject, date_header, snippet, reviewed
                   FROM raw_email_sample
                   WHERE sender_label = ?
                   ORDER BY inserted_at DESC LIMIT 5""",
                (label,),
            ).fetchall()
        if not rows:
            await update.message.reply_text(f"No samples for '{label}' yet.")
            return
        lines = [f"Latest 5 [{label}]:"]
        for r in rows:
            mark = "✓" if r["reviewed"] else " "
            subj = (r["subject"] or "")[:60]
            snippet = (r["snippet"] or "")[:80].replace("\n", " ")
            lines.append(f"[{mark}] #{r['id']}  {subj}\n    {snippet}")
        await update.message.reply_text("\n\n".join(lines))
        return

    # /samples (no args) — summary across all labels
    with storage.connect(settings.paths.database) as con:
        rows = con.execute(
            """SELECT sender_label,
                      COUNT(*) AS n,
                      SUM(CASE WHEN reviewed = 1 THEN 1 ELSE 0 END) AS n_reviewed,
                      MAX(inserted_at) AS newest
               FROM raw_email_sample
               GROUP BY sender_label
               ORDER BY newest DESC"""
        ).fetchall()
    if not rows:
        await update.message.reply_text(
            "No samples collected yet. Sample collector runs hourly."
        )
        return
    lines = ["Sample inventory:"]
    for r in rows:
        label = r["sender_label"] or "(no label)"
        n_unreviewed = r["n"] - (r["n_reviewed"] or 0)
        newest = (r["newest"] or "")[:19]
        lines.append(
            f"  {label:30}  {r['n']:>3} total  ({n_unreviewed} unreviewed)  newest: {newest}"
        )
    lines.append("\nUse /samples <label> to inspect, /samples mark <id> to flag reviewed.")
    await update.message.reply_text("\n".join(lines))


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
    app.add_handler(CommandHandler("samples", _samples_cmd))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    # AI-mediated chat (Phase 3.5) — qwen3:32b processes free text. Registered
    # before _handle_text so it gets first crack at non-trivial messages.
    # Trivial replies (y/skip/undo) and short single-word category names
    # during in-flight categorization are forwarded back to _handle_text.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_ai_agent))

    log.info("starting telegram long-poll")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
