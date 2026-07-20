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
import re
from pathlib import Path
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
    TypeHandler,
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


# Short hand-curated aliases for common categories the user types frequently.
# Case-insensitive lookup. Add entries here for any typo / shorthand that
# the substring + Levenshtein fallback don't catch on their own.
_CATEGORY_ALIASES: dict[str, str] = {
    "rta": "Inflow: Ready to Assign",
    "ready to assign": "Inflow: Ready to Assign",
    "ready": "Inflow: Ready to Assign",
    "inflow": "Inflow: Ready to Assign",
    "income": "Inflow: Ready to Assign",
    "paycheck": "Inflow: Ready to Assign",
    "salary": "Inflow: Ready to Assign",
    "dining": "Dining Out/Entertainment",
    "entertainment": "Dining Out/Entertainment",
    "restaurants": "Dining Out/Entertainment",
    "groceries": "Groceries",
    "household": "Household Items",
    "gifts": "Gifts",
    "kids": "Kids Necessities  and Activities",
    "medical": "Medical",
    "vacation": "Vacation",
    "home improvement": "Home Maintenance and Improvement",
    "home maintenance": "Home Maintenance and Improvement",
}


def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """Compute Levenshtein distance between ``a`` and ``b``, short-circuiting
    at ``cap`` for performance. Returns ``cap+1`` when the real distance
    exceeds the cap.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    # Standard DP, single-row optimization.
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(
                prev[j] + 1,         # deletion
                cur[j - 1] + 1,      # insertion
                prev[j - 1] + cost,  # substitution
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[lb]


def _match_category_by_name(
    db_path: str, query: str, categories: list[dict],
) -> str | None:
    """Match user-typed text against any category (incl. non-spending).

    Strategy, in priority order:
      1. Hand-curated alias map (rta → Ready to Assign, dining → ...)
      2. Exact case-insensitive match against full name
      3. Exact match after stripping parenthesized day-of-month suffixes
      4. Substring match (query is contained in normalized category name)
      5. Levenshtein distance ≤ 2 against the normalized name — catches
         typos like "Read to assign" → "Ready to Assign"
      6. None — caller falls through to LLM

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

    # Tier 0: alias map (catches "rta", "ready to assign", "dining" etc.)
    alias_target = _CATEGORY_ALIASES.get(q)
    if alias_target:
        for c in pool:
            if c["name"].lower() == alias_target.lower():
                return c["id"]

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
    # Tier 4: Levenshtein-distance fallback for whole-string typos. Cap at
    # 2 so we don't falsely match arbitrary short strings; gate on
    # |query| >= 4 to avoid two-letter wildcard matches.
    if not candidates and len(q_norm) >= 4:
        best_id, best_dist = None, 3
        ambiguous = False
        for c in pool:
            name_norm = _norm_cat_name(c["name"])
            if abs(len(name_norm) - len(q_norm)) > 2:
                continue
            d = _levenshtein(q_norm, name_norm, cap=2)
            if d < best_dist:
                best_id, best_dist = c["id"], d
                ambiguous = False
            elif d == best_dist and d <= 2:
                ambiguous = True
        if best_id is not None and best_dist <= 2 and not ambiguous:
            return best_id

    # Tier 5: fuzzy substring — slide a window through each category looking
    # for a place where the query approximately appears. Catches "Read to
    # assign" inside "Inflow: Ready to Assign" — the substring check from
    # Tier 3 missed because of the 'y' diff in "Read" vs "Ready".
    if not candidates and len(q_norm) >= 4:
        n = len(q_norm)
        best_id, best_dist = None, 3
        ambiguous = False
        for c in pool:
            name_norm = _norm_cat_name(c["name"])
            if len(name_norm) < n - 2:
                continue
            # Try windows around the query length, ±2.
            local_best = 3
            for w in range(max(1, n - 2), n + 3):
                if w > len(name_norm):
                    break
                for start in range(0, len(name_norm) - w + 1):
                    window = name_norm[start:start + w]
                    d = _levenshtein(q_norm, window, cap=2)
                    if d < local_best:
                        local_best = d
                    if local_best == 0:
                        break
                if local_best == 0:
                    break
            if local_best < best_dist:
                best_id, best_dist = c["id"], local_best
                ambiguous = False
            elif local_best == best_dist and local_best <= 2:
                ambiguous = True
        if best_id is not None and best_dist <= 2 and not ambiguous:
            return best_id

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

    Callback-data conventions (current — stamps the item's kind+id so taps
    on old DMs resolve to the right row, not whatever's currently in-flight):
      ``cat:<kind>:<item_id>:<cat_id>``  — pick a specific category by id
      ``skip:<kind>:<item_id>``          — skip this item
      ``other:<kind>:<item_id>``         — user wants to type a free-text category

    Backward compat: ``cat:<id>``, ``skip``, ``other`` (no kind/id segments)
    is the LEGACY format from older DMs. Still parsed by _handle_callback,
    which falls back to the bot_conversation.last_asked_id pointer.
    """
    name_by_id = {c["id"]: c["name"] for c in categories}
    item_kind = item.get("kind") or "txn"
    item_id = item.get("id")
    # 64-byte Telegram limit on callback_data — category UUIDs are 36 chars;
    # prefix + kind + numeric id puts us around ~50 bytes. Comfortable.
    item_tag = f"{item_kind}:{item_id}"
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
        row.append(InlineKeyboardButton(label, callback_data=f"cat:{item_tag}:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("🔤 Other", callback_data=f"other:{item_tag}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{item_tag}"),
    ])
    # Phase 6.5 multi-user routing: a single "route to the OTHER spouse"
    # button. Its label flips based on who currently owns the row so the
    # text reads naturally — Steven sees "➡ For Allison", Allison sees
    # "↩ Back to Steven". Resolver in _handle_callback swaps assigned_to.
    assigned_to = (item.get("assigned_to_user_id") or "steven").lower()
    if assigned_to == "steven":
        route_label = "➡ For Allison"
        route_target = "allison"
    else:
        route_label = "↩ Back to Steven"
        route_target = "steven"
    buttons.append([
        InlineKeyboardButton(
            route_label,
            callback_data=f"route:{item_tag}:{route_target}",
        ),
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


def _bot_for_chat(app: Application, chat_id: int):
    """Return the Bot instance that can DM ``chat_id``.

    With the multi-bot setup (one bot per spouse), every chat_id is
    reachable through exactly one Application. The map is built at
    startup in :func:`run` and stored on ``app.bot_data["chat_to_bot"]``.
    Falls back to the caller's own ``app.bot`` if the chat isn't
    registered — preserves single-bot behavior for any pre-multi-bot
    code paths.
    """
    chat_to_bot = app.bot_data.get("chat_to_bot") or {}
    return chat_to_bot.get(int(chat_id), app.bot)


def _annotate_with_suggestion_name(
    item: dict, categories: list[dict], db_path: str | None = None,
) -> dict:
    """Attach the human-readable suggestion name for prompt rendering.

    The cached `categories` list passed in by the bot is spending-only —
    so when the categorizer's override or strong-prior picked a non-
    spending bill envelope (e.g. "Mass Mutual Insurances (17th)" or
    "Mosquito Treatment (16th)"), the lookup missed and the DM rendered
    "No guess yet" despite a perfectly valid suggestion sitting on the
    row. Fall back to the local DB so every visible category resolves.
    """
    sid = item.get("suggested_category")
    if not sid:
        return item
    name = next((c["name"] for c in categories if c["id"] == sid), None)
    if not name and db_path:
        with storage.connect(db_path) as con:
            row = con.execute(
                "SELECT name FROM category WHERE id = ?", (sid,),
            ).fetchone()
            if row:
                name = row["name"]
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
        await _bot_for_chat(app, chat_id).edit_message_reply_markup(
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

        # NOTE: an earlier guard here used to NULL out any suggestion that
        # landed in a non-spending category (CC-payment / scheduled-bill /
        # named-goal envelope). That guard was correct when the LLM was
        # the only suggester — the LLM is restricted to is_spending=1
        # rows, so a non-spending suggestion meant a bug.
        #
        # The override map + strong-prior matcher now DELIBERATELY route to
        # non-spending bill envelopes (Mass Mutual Insurances (17th),
        # Cell Phone (4th), Water and Trash (16th), Mosquito Treatment
        # (16th), Hulu (7th), Amica Car Insurance (27th), etc.). Wiping
        # those was destroying every override-driven suggestion since
        # those layers shipped — see audit incidents on pt#985, pt#987,
        # pt#1032. The guard is intentionally removed.
        item = _annotate_with_suggestion_name(
            item, categories, db_path=settings.paths.database,
        )
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
            sent = await _bot_for_chat(app, chat_id).send_message(
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
            # Record that we just pushed this row so the queue rotates
            # past it on the next pick (see next_item_for_user's
            # last_pushed_at ordering).
            table = "pending_order" if item["kind"] == "order" else "pending_txn"
            con.execute(
                f"UPDATE {table} SET last_pushed_at = ? WHERE id = ?",
                (_utcnow(), item["id"]),
            )
    return True


# _propagate_order_category_to_ledger removed in the 2026-06-26 cleanup —
# pending_orders no longer go through the categorize path. They serve
# only as enrichment data for bot.ingest._enrich_from_pending_order.


def _apply_route(
    settings: Settings, *,
    chat_id: int, kind: str, item_id: int, to_user: str,
) -> str:
    """Move a pending row to another user's queue and free the current chat
    to push its next item. Audits every routing decision so we can later
    spot mis-routed charges in the daily summary.
    """
    table = "pending_order" if kind == "order" else "pending_txn"
    with storage.connect(settings.paths.database) as con:
        row = con.execute(
            f"SELECT assigned_to_user_id, status FROM {table} WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return "Couldn't find that item anymore."
        if row["status"] != "pending":
            return f"Already handled — status was '{row['status']}'."
        from_user = row["assigned_to_user_id"]
        if from_user == to_user:
            return f"Already on {to_user}'s queue."
        con.execute(
            f"UPDATE {table} SET assigned_to_user_id = ?, "
            "last_pushed_at = NULL "
            "WHERE id = ?",
            (to_user, item_id),
        )
        # Clear the in-flight pointer ONLY if this row was the one the
        # current chat was waiting on — otherwise leave it alone (mirrors
        # the same rule used in _apply_choice for stale-DM taps).
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL "
            "WHERE chat_id = ? AND last_asked_id = ?",
            (chat_id, item_id),
        )
    storage.audit(settings.paths.database, "routed", {
        "kind": kind, "id": item_id,
        "from_user": from_user, "to_user": to_user,
    })
    nice = "Allison" if to_user == "allison" else "Steven"
    return f"Routed to {nice}."


def _apply_choice(
    settings: Settings,
    categorizer: Categorizer,
    categories: list[dict],
    *,
    chat_id: int,
    user_id: str,
    choice_kind: str,
    payload: Any,
    override_kind: str | None = None,
    override_id: int | None = None,
) -> str:
    """Apply a user's choice to a specific pending item.

    Resolves the chosen category from one of three sources:
      - ``"confirm"`` — use the row's stored ``suggested_category``
      - ``"callback:<id>"`` — exact category id from an inline button
      - ``"text"`` — free-text. Try a case-insensitive name match first,
        then fall back to the Categorizer to disambiguate.

    Item targeting:
      - If ``override_kind`` + ``override_id`` are provided (modern inline
        buttons stamp the item kind+id on every callback), apply the choice
        to THAT row regardless of bot_conversation state — this is what
        lets taps on old/stale DMs resolve to the right item.
      - Otherwise (free-text replies, legacy buttons), fall back to the
        bot_conversation.last_asked_id pointer.

    Returns a short human-readable status string for the bot to echo.
    """
    if override_id is not None and override_kind is not None:
        kind = override_kind
        item_id = override_id
    else:
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

    # When targeting via the button-stamped item_id, guard against double-
    # taps / stale DMs whose item has already been handled. (For the
    # last_asked_id path, we know it was pending when we asked.)
    if (override_id is not None and item.get("status")
            and item["status"] not in {"pending", "matched"}):
        return f"Already handled — status was '{item['status']}'."

    # --- skip -----------------------------------------------------------
    if choice_kind == "skip":
        # Both kinds need a status change, or the next push picks the same
        # row again — exactly the "tap skip, get same item" loop the user
        # hit on a parser-broken Amazon order.
        if kind == "txn":
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    "UPDATE pending_txn SET status = 'skipped' WHERE id = ?",
                    (item_id,),
                )
        else:
            # pending_order has CHECK status IN
            # ('pending','categorized','matched','expired'). Use 'expired'
            # as the user-deferred bucket — order won't surface again
            # until the matching YNAB charge arrives.
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    "UPDATE pending_order SET status = 'expired', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item_id,),
                )
        # Only clear the in-flight pointer if THIS item is what the bot was
        # waiting on. Skip-tap on a stale DM shouldn't drag the queue.
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE bot_conversation SET last_asked_id = NULL, "
                "last_asked_message_id = NULL "
                "WHERE chat_id = ? AND last_asked_id = ?",
                (chat_id, item_id),
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
    # kind=="order" is now unreachable — pending_orders no longer flow
    # through the push loop (see conversation.next_item_for_user) and
    # /amazon operates on pending_txn rows. The old branch + the
    # _propagate_order_category_to_ledger helper were removed in this
    # cleanup pass; pending_orders remain in the DB strictly as
    # enrichment data for bot.ingest._enrich_from_pending_order.
    if False:  # kind == "order" — removed
        pass
    else:
        # txn — categorize locally only. The daily ynab_writer pushes
        # the choice to YNAB overnight; we no longer talk to YNAB
        # synchronously on every tap (Phase 7+ writer redesign).
        #
        # For synthetic "ledger:N" ids the bot owns the ledger_txn row
        # directly; we update its category_id. For real YNAB UUIDs we
        # update the local mirror so envelope math reflects the choice
        # immediately. Either way, ynab_writer.run_once stamps
        # synced_to_ynab_at after pushing tomorrow.
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE pending_txn SET chosen_category = ?, "
                "chosen_at = ?, status = 'categorized', filed_by = ? "
                "WHERE id = ?",
                (category_id, _utcnow(), user_id, item_id),
            )
        # Cross-surface: a DM filing closes any open group question too.
        from bot.group_chat import resolve_open_questions_for_item
        resolve_open_questions_for_item(
            settings.paths.database, "txn", item_id, user_id)
        ynab_id = item.get("ynab_txn_id") or ""
        if ynab_id.startswith("ledger:"):
            try:
                ledger_id = int(ynab_id.split(":", 1)[1])
                with storage.connect(settings.paths.database) as con:
                    con.execute(
                        "UPDATE ledger_txn SET category_id = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (category_id, ledger_id),
                    )
            except (ValueError, IndexError):
                log.warning("malformed ledger: id %s", ynab_id)
        elif ynab_id:
            # Real YNAB UUID — mirror the choice into local ledger_txn
            # so envelope math reflects it instantly.
            try:
                with storage.connect(settings.paths.database) as con:
                    con.execute(
                        "UPDATE ledger_txn SET category_id = ?, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE ynab_txn_id = ?",
                        (category_id, ynab_id),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("local ledger update for ynab_id %s failed: %s",
                            ynab_id, e)

    # Clear the conversation pointer ONLY when the row we just categorized is
    # the one the bot is currently waiting on. A tap on a stale DM (where
    # override_id != last_asked_id) should leave the current in-flight
    # question alone, not drag the queue along behind it.
    with storage.connect(settings.paths.database) as con:
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL "
            "WHERE chat_id = ? AND last_asked_id = ?",
            (chat_id, item_id),
        )
    storage.audit(settings.paths.database, "categorized",
                  {"kind": kind, "id": item_id, "category": category_id})

    # Show the envelope balance so the user knows where the pot stands. We
    # recompute the single category first so the number reflects the txn
    # that was just categorized (synthetic ledger: case already updated
    # ledger_txn above; real-YNAB case will sync within ~5 min, so the
    # number may be one txn stale for a few minutes — acceptable).
    try:
        from bot.envelope import available_for_category
        remaining = available_for_category(
            settings.paths.database, category_id=category_id,
        )
        sign = "-" if remaining < 0 else ""
        remaining_str = f"{sign}${abs(remaining) / 100:,.2f}"
        return (
            f"Categorized as {cat_name}.\n"
            f"{cat_name}: {remaining_str} left this month."
        )
    except Exception as e:  # noqa: BLE001 — never let a display lookup break categorization
        log.warning("remaining-balance lookup failed: %s", e)
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


# /pending and /digest commands removed in the Phase 7 redesign — the
# bulk surfaces (/batch, /amazon) replaced their "show me what's queued"
# function, and the push loop handles HOT items automatically. The
# handlers were dead UX.


# ──────────────────────────────────────────────────────────────────────
# Telegram 409 Conflict watchdog — auto-kills Claude Code's Telegram
# plugin (bun.exe) when it steals our long-poll lease. Per the memory
# project_telegram_409_debug: a 409 means another process is calling
# getUpdates on the same bot token. On Steven's machine that's always
# bun.exe (Claude Code's Telegram plugin which auto-restarts).
#
# Threshold-based to avoid killing bun on a single transient conflict
# (e.g., the user testing something). 3+ conflicts in 60s triggers the
# kill. After killing, the bot wins the lease on the next poll cycle.
# ──────────────────────────────────────────────────────────────────────

from collections import deque
import subprocess
import time

# Module-level state — async error_handler doesn't have a clean place for
# instance state.
_CONFLICT_TIMESTAMPS: deque[float] = deque(maxlen=20)
_LAST_BUN_KILL_AT: float = 0.0
_BUN_KILL_COOLDOWN_S = 30.0  # don't re-kill within 30s of a successful kill

def _kill_bun_processes() -> int:
    """Send taskkill to every bun.exe. Returns kill count.

    Runs under the bot's user. Most cases bun.exe is owned by the same
    Windows user so no elevation needed. If elevation IS required, the
    kill fails silently and we just log it — the user gets a Telegram
    notification (via the audit log → daily summary) but the bot keeps
    running.
    """
    try:
        # /F = force, /IM = image name. Returns 0 if killed, 128 if not found.
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "bun.exe"],
            capture_output=True, text=True, timeout=5,
        )
        # stdout looks like "SUCCESS: The process \"bun.exe\" with PID NNNN has been terminated."
        killed = result.stdout.count("SUCCESS")
        return killed
    except Exception as e:  # noqa: BLE001
        log.warning("taskkill bun.exe failed: %s", e)
        return 0


async def _handle_telegram_error(
    update: object, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Telegram error handler. Detects 409 Conflict (lease hijack) and
    runs the bun.exe kill recipe when persistent."""
    global _LAST_BUN_KILL_AT
    err = context.error
    err_str = str(err) if err else ""
    is_conflict = (
        "Conflict" in err_str
        or "409" in err_str
        or "terminated by other getUpdates" in err_str.lower()
    )

    if not is_conflict:
        # Full traceback — the scheduled task discards stdout, so this
        # rides the WARNING file handler in __main__ to bot_errors.log.
        log.warning("telegram error: %s", err, exc_info=err)
        # A swallowed handler exception reads as the bot ignoring a human
        # (2026-07-20: "Nails" hit a NameError and the group just went
        # silent). Best-effort: tell the chat something broke.
        msg = getattr(update, "effective_message", None)
        if msg is not None:
            try:
                await msg.reply_text(
                    "⚠️ That one hit an internal error — it's logged. "
                    "Try again, or file it from the app.")
            except Exception:
                pass
        return

    now = time.time()
    _CONFLICT_TIMESTAMPS.append(now)

    # Count conflicts in the last 60s
    recent = sum(1 for t in _CONFLICT_TIMESTAMPS if now - t <= 60)
    log.warning("Telegram 409 Conflict #%d in last 60s: %s", recent, err_str[:200])

    # Cooldown to prevent kill-storm
    if now - _LAST_BUN_KILL_AT < _BUN_KILL_COOLDOWN_S:
        return

    # Threshold: 3+ in 60s = real hijack
    if recent < 3:
        return

    settings: Settings = context.application.bot_data["settings"]
    log.info("conflict threshold hit — attempting to kill bun.exe")
    killed = _kill_bun_processes()
    _LAST_BUN_KILL_AT = now
    storage.audit(
        settings.paths.database, "watchdog_killed_bun",
        {"killed_count": killed, "conflicts_in_window": recent,
         "trigger_error": err_str[:200]},
    )
    if killed > 0:
        log.info("watchdog killed %d bun.exe process(es)", killed)
        # Clear the window so we don't immediately re-trigger
        _CONFLICT_TIMESTAMPS.clear()


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
    parts = data.split(":")
    target_kind: str | None = None
    target_id: int | None = None
    # Parse the kind+id stamp from any of the new-format actions.
    #   cat:<kind>:<id>:<cat_id>  (4 parts)
    #   skip:<kind>:<id>           (3 parts)
    #   other:<kind>:<id>          (3 parts)
    #   route:<kind>:<id>:<to_user> (4 parts)
    if len(parts) >= 3 and parts[0] in {"cat", "skip", "other", "route"}:
        target_kind = parts[1]
        try:
            target_id = int(parts[2])
        except ValueError:
            target_kind = None
            target_id = None

    # Phase 7 checkbox-batch — bt:<n> toggles, bsub commits, bcan cancels.
    if parts[0] in {"bt", "bsub", "bcan"}:
        await _handle_batch_callback(update, context, parts)
        return

    # Phase 6.5 — route this row to the other user's queue.
    if parts[0] == "route" and target_id is not None and len(parts) >= 4:
        to_user = parts[3]
        status = _apply_route(
            settings,
            chat_id=chat_id,
            kind=target_kind or "txn",
            item_id=target_id,
            to_user=to_user,
        )
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
        # Push the next item to the current chat so Steven keeps moving
        await _push_next_item(
            context.application, settings, categories, chat_id, user_id,
            include_txns=True,
        )
        return

    # For "other": adopt the tapped item as the in-flight question so the
    # subsequent free-text reply resolves against THIS row, not whatever
    # was last asked. Then prompt the user to type.
    if parts[0] == "other":
        if target_id is not None and target_kind is not None:
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    """UPDATE bot_conversation
                       SET last_asked_kind = ?, last_asked_id = ?,
                           last_action_at = ?
                       WHERE chat_id = ?""",
                    (target_kind, target_id, _utcnow(), chat_id),
                )
        await query.answer()
        await query.edit_message_text(
            (query.message.text or "") + "\n\nType the category name…"
        )
        return

    if parts[0] == "skip":
        status = _apply_choice(
            settings, categorizer, categories,
            chat_id=chat_id, user_id=user_id,
            choice_kind="skip", payload=None,
            override_kind=target_kind, override_id=target_id,
        )
    elif parts[0] == "cat":
        # New format: cat:<kind>:<id>:<cat_id> → cat_id is parts[3:]
        # Legacy format: cat:<cat_id> → cat_id is parts[1:]
        if len(parts) >= 4:
            cat_id = ":".join(parts[3:])
        else:
            cat_id = ":".join(parts[1:])
        status = _apply_choice(
            settings, categorizer, categories,
            chat_id=chat_id, user_id=user_id,
            choice_kind=f"callback:{cat_id}", payload=None,
            override_kind=target_kind, override_id=target_id,
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


def _looks_like_batch_reply(text: str) -> bool:
    """Heuristic: a batch reply either starts with 'all' or contains a digit
    followed by '=' / 'skip' / 'back' / space-then-number. Avoids false
    positives on category names like '1Password' (no digit-after pattern).
    """
    t = text.strip().lower()
    if not t:
        return False
    if re.match(r"^all\b", t):
        return True
    # Numbered specifiers — a digit followed by a separator, end of string,
    # or another digit somewhere later in the string.
    if re.search(r"\b\d+\s*=\s*\S", t):
        return True
    if re.search(r"\b\d+\s+(skip|back|s|b)\b", t):
        return True
    # Plain space-separated numbers: "1 2 4 5"
    if re.match(r"^\d+(\s+\d+){1,}\s*$", t):
        return True
    return False


async def _handle_batch_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str],
) -> None:
    """Phase 7 checkbox batch — handle ``bt:<n>`` (toggle), ``bsub``
    (commit checked + DM each flagged row), and ``bcan`` (drop the batch).
    """
    from bot import batch_processor
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]
    query = update.callback_query
    chat_id = query.message.chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    db_path = settings.paths.database

    if parts[0] == "bcan":
        batch_processor.mark_batch_consumed(db_path, chat_id)
        await query.answer(text="Batch cancelled.")
        try:
            await query.edit_message_text(
                (query.message.text or "")
                + "\n\n❌ Cancelled. Nothing changed."
            )
        except Exception as e:  # noqa: BLE001
            log.warning("edit on cancel failed: %s", e)
        return

    if parts[0] == "bt" and len(parts) >= 2:
        try:
            n = int(parts[1])
        except ValueError:
            await query.answer()
            return
        payload = batch_processor.toggle_batch_item(db_path, chat_id, n)
        if payload is None:
            await query.answer(text="Batch context expired. Try /batch again.")
            return
        # Re-render BOTH body and keyboard so the ☑/☐ marker stays inline
        # with each row's data. Lock items to the saved payload's pt_ids
        # so the body matches what the user originally saw even if new
        # COLD items arrived since the DM. Use the batch's own builder
        # (Amazon or general) so we filter the right side.
        is_amazon_batch = payload.get("header_label") == "Amazon"
        if is_amazon_batch:
            items = batch_processor.build_amazon_batch(db_path, user_id=user_id)
            total = batch_processor.count_amazon_ready(db_path, user_id=user_id)
        else:
            items = batch_processor.build_batch(db_path, user_id=user_id)
            total = batch_processor.count_cold_batch(db_path, user_id=user_id)
        payload_pt_ids = {it["pt_id"] for it in payload.get("items", [])}
        items_filtered = [it for it in items if it["pt_id"] in payload_pt_ids]
        order = {it["pt_id"]: it["n"] for it in payload.get("items", [])}
        items_filtered.sort(key=lambda it: order.get(it["pt_id"], 9999))

        new_body = batch_processor.render_batch_body(
            items_filtered, total, payload.get("items", []),
            verbose=payload.get("verbose", False),
            header_emoji=payload.get("header_emoji", "📦"),
            header_label=payload.get("header_label", "Batch"),
        )
        new_keyboard = batch_processor.render_batch_buttons(
            items_filtered, payload.get("items", []),
        )
        await query.answer()
        try:
            await query.edit_message_text(
                new_body, reply_markup=new_keyboard, parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text(toggle) failed: %s", e)
        return

    if parts[0] == "bsub":
        payload = batch_processor.load_batch_context(db_path, chat_id)
        if not payload:
            await query.answer(text="Batch context expired. Try /batch again.")
            return
        confirmed_pt_ids = [it["pt_id"] for it in payload.get("items", [])
                             if it.get("checked")]
        flagged_pt_ids = [it["pt_id"] for it in payload.get("items", [])
                          if not it.get("checked")]
        # Build decisions for the checked rows
        decisions = [{"pt_id": pid, "action": "confirm", "value": None}
                     for pid in confirmed_pt_ids]
        result = await asyncio.to_thread(
            batch_processor.apply_decisions,
            db_path,
            decisions=decisions,
            settings=settings,
            categorizer=categorizer,
            categories=categories,
            chat_id=chat_id,
            user_id=user_id,
        )
        batch_processor.mark_batch_consumed(db_path, chat_id)

        # Count remaining /batch + /amazon backlog for the footer summary
        remaining = batch_processor.count_cold_batch(db_path, user_id=user_id)
        amazon_ready = batch_processor.count_amazon_ready(db_path, user_id=user_id)
        summary = batch_processor.format_summary(
            result, remaining, amazon_ready=amazon_ready,
        )
        if flagged_pt_ids:
            summary = (f"{summary}\n\n"
                       f"Will DM {len(flagged_pt_ids)} flagged item(s) "
                       f"one-by-one for review.")
        await query.answer()
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n" + summary
            )
        except Exception as e:  # noqa: BLE001
            log.warning("edit on submit failed: %s", e)

        # Stash the flagged ids on bot_conversation so the push loop
        # picks them up next. Easier than a new column: mark each
        # flagged row's queue_lane back to 'hot' so the normal push
        # loop DMs them with the single-item keyboard.
        if flagged_pt_ids:
            with storage.connect(db_path) as con:
                placeholders = ",".join(["?"] * len(flagged_pt_ids))
                con.execute(
                    f"UPDATE pending_txn SET queue_lane = 'hot', "
                    f"lane_changed_at = ?, last_pushed_at = NULL "
                    f"WHERE id IN ({placeholders})",
                    [datetime.now(timezone.utc), *flagged_pt_ids],
                )
            storage.audit(db_path, "batch_flagged_for_review", {
                "pt_ids": flagged_pt_ids,
            })

        # Auto-rebuild: if there are still cold-lane items waiting (more
        # than the one batch could fit, OR new items that arrived during
        # this batch's lifetime), immediately send the next batch so the
        # user doesn't have to type /batch again.
        if remaining > 0:
            new_items = batch_processor.build_batch(db_path, user_id=user_id)
            with storage.connect(db_path) as con:
                new_total = con.execute(
                    "SELECT COUNT(*) FROM pending_txn "
                    "WHERE assigned_to_user_id = ? AND status='pending' "
                    "  AND queue_lane='cold'",
                    (user_id,),
                ).fetchone()[0]
            if new_items:
                initial_payload = [
                    {"n": i + 1, "pt_id": it["pt_id"], "checked": True}
                    for i, it in enumerate(new_items)
                ]
                next_body = batch_processor.render_batch_body(
                    new_items, new_total, initial_payload,
                )
                next_kb = batch_processor.render_batch_buttons(
                    new_items, initial_payload,
                )
                try:
                    sent = await context.bot.send_message(
                        chat_id=chat_id,
                        text=next_body,
                        reply_markup=next_kb,
                        parse_mode="Markdown",
                    )
                    batch_processor.save_batch_context(
                        db_path, chat_id, new_items, sent.message_id,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("auto-batch follow-up failed: %s", e)
        return


async def _handle_batch_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    batch: dict, text: str,
) -> None:
    """Parse a batch reply, apply decisions, send summary."""
    from bot import batch_processor
    settings: Settings = context.application.bot_data["settings"]
    categorizer: Categorizer = context.application.bot_data["categorizer"]
    categories: list[dict] = context.application.bot_data["categories"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)

    items_meta = batch.get("items", [])
    parsed = batch_processor.parse_reply(text, items_meta)
    if not parsed["decisions"]:
        msg = "Couldn't read any decisions from that reply."
        if parsed["unparsed"]:
            msg += f" Unparsed: {parsed['unparsed'][:4]}"
        await update.message.reply_text(msg)
        return

    result = await asyncio.to_thread(
        batch_processor.apply_decisions,
        settings.paths.database,
        decisions=parsed["decisions"],
        settings=settings,
        categorizer=categorizer,
        categories=categories,
        chat_id=chat_id,
        user_id=user_id,
    )
    batch_processor.mark_batch_consumed(settings.paths.database, chat_id)

    # Count remaining /batch + /amazon backlog for the footer
    remaining = batch_processor.count_cold_batch(
        settings.paths.database, user_id=user_id,
    )
    amazon_ready = batch_processor.count_amazon_ready(
        settings.paths.database, user_id=user_id,
    )
    body = batch_processor.format_summary(
        result, remaining, amazon_ready=amazon_ready,
    )
    if parsed["unparsed"]:
        body += f"\n\n(ignored: {parsed['unparsed'][:4]})"
    await update.message.reply_text(body)


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

    # Phase 7 /batch reply — if there's an unconsumed batch context for
    # this chat AND the text looks like a batch reply (contains 'all' or
    # a digit), parse and apply. Anything else falls through to the AI.
    from bot import batch_processor
    batch = batch_processor.load_batch_context(
        settings.paths.database, chat_id,
    )
    if batch and _looks_like_batch_reply(text):
        await _handle_batch_reply(update, context, batch, text)
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
    # 10s — snappy enough that newly-arrived emails surface within ~70s of
    # hitting the inbox (60s gmail poll + 10s push). Cheap; the loop just
    # SELECTs one row when there's no work to do.
    poll_interval = 10

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
                        "SELECT last_asked_id, last_asked_kind, "
                        "       quiet_until, last_action_at "
                        "FROM bot_conversation WHERE chat_id = ?",
                        (chat_id,),
                    ).fetchone()
                if row and row["last_asked_id"] is not None:
                    # An in-flight question is waiting for a user reply.
                    # Don't double-prompt — wait. Staleness is owned by
                    # bot.queue_lane.demote_hot_to_cold (runs every 30
                    # min from _lane_sweep_loop) which also clears this
                    # pointer when the row's HOT_TTL elapses, so we just
                    # skip and let that path unblock us.
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
    """Spawn all background loops after Application startup."""
    # Redesign-v2 cutover (2026-07-09): the proactive DM ask loop is OFF.
    # Instant items surface as group pings (group_ping_loop below); the
    # desktop Inbox holds everything else. /batch, /pending, /skip still
    # work on demand — only the unprompted button-DMs are gone.
    app.bot_data["daily_task"] = asyncio.create_task(_daily_summary_loop(app))
    log.info("daily summary loop started")
    app.bot_data["weekly_task"] = asyncio.create_task(_weekly_summary_loop(app))
    log.info("weekly summary loop started")
    # Real-time intake — polls Gmail (1 min) and YNAB (5 min) inside the
    # same event loop so new charges / orders surface within seconds of
    # arrival, not whenever the user happens to /pending.
    app.bot_data["gmail_task"] = asyncio.create_task(_gmail_poll_loop(app))
    log.info("gmail poll loop started")
    # ynab_watcher's 5-min polling loop was removed in the Phase 7+
    # writer redesign — the bot no longer asks YNAB "what's
    # uncategorized?" because we don't react to YNAB-only charges
    # anymore. The daily ynab_writer pushes bot decisions to YNAB; the
    # 6-hour ynab_full_sync pulls YNAB state for our local mirror.
    app.bot_data["ynab_full_task"] = asyncio.create_task(_ynab_full_sync_loop(app))
    log.info("ynab full-sync loop started")
    app.bot_data["lane_sweep_task"] = asyncio.create_task(_lane_sweep_loop(app))
    log.info("queue-lane sweep loop started")
    # Redesign-v2 cutover (2026-07-09): awareness pings ("N batch items
    # ready") are OFF — Steven got three in one afternoon and the desktop
    # Inbox already shows the backlog. The daily summary keeps one count.
    # Redesign-v2 Phase 4 — instant pings to the household group. Dormant
    # (returns immediately) when telegram.group_chat_id is unset.
    from bot.group_chat import group_ping_loop
    app.bot_data["group_ping_task"] = asyncio.create_task(group_ping_loop(app))
    log.info("group ping loop started")
    # Phase 3 (UI writes) — localhost HTTP API for the Tauri desktop app.
    # Stays loopback-only with a shared secret. The UI calls /categorize,
    # /envelope/move, /budget/set; the bot is still the single writer.
    app.bot_data["ui_api_task"] = asyncio.create_task(_ui_api_loop(app))
    log.info("ui_api server task started")


async def _gmail_poll_loop(app: Application) -> None:
    """Every 60s, call gmail_watcher.poll_once. Wrapped in to_thread because
    the underlying HTTP + LLM calls are blocking."""
    settings: Settings = app.bot_data["settings"]
    from bot import gmail_watcher
    INTERVAL = 60
    while True:
        try:
            await asyncio.to_thread(gmail_watcher.poll_once, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("gmail_poll iteration failed: %s", e)
        await asyncio.sleep(INTERVAL)


# _ynab_poll_loop and bot/ynab_watcher.py were removed in Phase 7+. The
# bot no longer reacts to YNAB-uncategorized charges in real-time.
# ynab_writer.run_once (called from _daily_summary_loop) is now the
# single push-to-YNAB path. /ynab triggers it on demand.


# _awareness_ping_loop (Phase 7 Slice 3: thrice-daily "N batch items
# ready" DMs) was removed in the redesign-v2 cutover 2026-07-09 — the
# desktop Inbox and the daily summary already carry the count.


async def _ui_api_loop(app: Application) -> None:
    """Run the Tauri UI's HTTP API. Restarts on crash (10 s back-off)."""
    settings: Settings = app.bot_data["settings"]
    from bot import http_api
    while True:
        try:
            await http_api.serve(settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("ui_api crashed: %s", e)
            await asyncio.sleep(10)


async def _lane_sweep_loop(app: Application) -> None:
    """Phase 7 queue redesign — every 30 minutes:

      * Re-run Phase 2 enrichment on HOLD rows; promote to HOT on success.
      * Demote HOT rows the user has ignored for 2h+ to COLD.
      * Give up on HOLD rows older than 24h and drop them to COLD.

    Cheap to run: each sweep is 3 SQL statements + (for promotions) one
    matcher call per HOLD row. With a healthy queue these all return
    fast no-ops.
    """
    settings: Settings = app.bot_data["settings"]
    from bot import queue_lane
    INTERVAL = 30 * 60  # 30 minutes
    while True:
        try:
            counts = await asyncio.to_thread(
                queue_lane.sweep_lanes, settings.paths.database,
                settings=settings,
            )
            if any(counts.values()):
                log.info("lane_sweep: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("lane_sweep iteration failed: %s", e)
        await asyncio.sleep(INTERVAL)


async def _ynab_full_sync_loop(app: Application) -> None:
    """Mirror every YNAB transaction into the local ledger every 6 hours.

    The 5-minute ``_ynab_poll_loop`` only pulls UNCATEGORIZED txns for the
    user-prompt flow. This loop is the long-term shadow ledger: it pulls
    ALL transactions (categorized + transfers) so the bot's reconciler
    can validate its math against YNAB and the bank, and so the bot has
    a complete copy when YNAB eventually gets turned off (Phase 7).

    Runs once at startup, then every 6 hours. 6h cadence is plenty —
    YNAB updates aren't that frequent and the full sync replays a 7-day
    overlap window each time to forgive late edits.
    """
    settings: Settings = app.bot_data["settings"]
    from bot import ynab_full_sync
    INTERVAL = 6 * 3600  # 6 hours
    while True:
        try:
            await asyncio.to_thread(ynab_full_sync.full_sync, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("ynab_full_sync iteration failed: %s", e)
        await asyncio.sleep(INTERVAL)


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
    """Background coroutine: fire each user's daily summary at THEIR
    configured ``user_pref.daily_summary_time`` (falling back to the
    global ``settings.telegram.daily_summary_time``).

    Steven wants his report at 06:30 and Allison's at 08:00 — two distinct
    fires per day, not one global fire that loops recipients. We rebuild
    the schedule at the start of each iteration so an edit to user_pref
    (via a script or the UI) takes effect on the next firing without a
    bot restart.

    On the EARLIEST fire of the day we also run the shared per-day work
    (recompute envelopes + reconcile yesterday + YNAB writer + Amazon
    aged-out alert). Later fires just send that user's summary; rerunning
    the writer for every user would push the same write batch N times.
    """
    settings: Settings = app.bot_data["settings"]
    from bot.reporters.daily import send_daily_summaries
    from bot.envelope import recompute_month
    from bot.reconciler import reconcile_all_observed
    last_shared_run_date: "str | None" = None
    while True:
        try:
            # Build today's schedule: list of (fire_at, user_id) sorted.
            recipients = storage.list_recipients_for_period(
                settings.paths.database, "daily",
            )
            now = datetime.now()
            group_mode = bool(int(getattr(settings.telegram,
                                          "group_chat_id", 0) or 0))
            schedule: list[tuple[datetime, str]] = []
            if group_mode and recipients:
                # Redesign-v2 (2026-07-09): one household summary to the
                # group at the earliest configured time — per-user DM
                # fires would post the same report twice.
                fire_strs = [(r.get("daily_summary_time")
                              or settings.telegram.daily_summary_time)
                             for r in recipients]
                # zfill: "6:30" must sort as "06:30"
                earliest = min(fire_strs, key=lambda s: s.strip().zfill(5))
                schedule.append((_next_fire_at(earliest), "household"))
            else:
                for r in recipients:
                    fire_str = (r.get("daily_summary_time")
                                or settings.telegram.daily_summary_time)
                    schedule.append((_next_fire_at(fire_str), r["user_id"]))
            if not schedule:
                log.info("daily: no opted-in recipients; sleeping 1h")
                await asyncio.sleep(3600)
                continue
            schedule.sort(key=lambda t: t[0])

            fire_at, user_id = schedule[0]
            sleep_for = max(1.0, (fire_at - now).total_seconds())
            log.info("daily summary for %s will fire at %s (in %.0fs)",
                     user_id, fire_at, sleep_for)
            await asyncio.sleep(sleep_for)

            today_key = datetime.now().strftime("%Y-%m-%d")
            is_first_today = last_shared_run_date != today_key
            if is_first_today:
                try:
                    recompute_month(
                        settings.paths.database,
                        datetime.now().strftime("%Y-%m"),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("daily: recompute failed: %s", e)
                try:
                    from datetime import date as _date_cls, timedelta as _td
                    yesterday = _date_cls.today() - _td(days=1)
                    results = await asyncio.to_thread(
                        reconcile_all_observed,
                        settings.paths.database, yesterday,
                    )
                    ok = sum(1 for r in results if r["status"] == "ok")
                    mm = sum(1 for r in results if r["status"] == "mismatch")
                    log.info("daily reconcile: %d ok, %d mismatch", ok, mm)
                except Exception as e:  # noqa: BLE001
                    log.warning("daily: reconcile failed: %s", e)

            await send_daily_summaries(
                app,
                only_user_id=None if user_id == "household" else user_id,
            )

            if is_first_today:
                # Writer + Amazon aged-out alert are once-per-day, shared
                # operations — run them right after the earliest fire so
                # later-firing users get the most-recent state in their
                # report too.
                try:
                    from bot.ynab_writer import send_writer_report
                    await send_writer_report(app)
                except Exception as e:  # noqa: BLE001
                    log.warning("ynab_writer report send failed: %s", e)
                try:
                    from bot.amazon_tracker import send_aged_out_alert_if_new
                    await send_aged_out_alert_if_new(app)
                except Exception as e:  # noqa: BLE001
                    log.warning("amazon aged-out alert failed: %s", e)
                last_shared_run_date = today_key
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
            # Phase 7+ — Sunday evening also DMs the YNAB-sunset
            # progress report so Steven sees the email-first-coverage
            # trend climbing toward the threshold at which he can flip
            # ynab.mode to read_only.
            try:
                from bot.reporters.ynab_progress import send_progress_report
                await send_progress_report(app)
            except Exception as e:  # noqa: BLE001
                log.warning("ynab_progress send failed: %s", e)
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


# /digest handler removed — see comment near _pending_cmd.


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
        "*Slash commands*\n"
        "  /start    — link this chat (one-time)\n"
        "  /pending  — show next queued item\n"
        "  /digest   — force-push next item regardless of window\n"
        "  /skip     — skip the current item\n"
        "  /undo     — revert your last categorization (5 min window)\n"
        "  /quiet on|off — pause/resume notifications for 24h\n"
        "  /samples [<label>] [mark <id>] — Phase 0 sample inspection\n"
        "  /help     — this message\n\n"

        "*During a categorize prompt*\n"
        "  ✅ button or `y` / `yes` — confirm the suggestion\n"
        "  tap a button — pick that category\n"
        "  type a category name — `groceries`, `cell phone`, `sewing class`\n"
        "  `skip` — defer\n\n"

        "*Talk to the bot in plain English (AI agent)*\n"
        "Queue navigation:\n"
        "  `next` / `what's next?` — push the next pending item\n"
        "  `pull new` / `check email` / `run catchup` — manual poll\n"
        "  `unskip everything` / `revisit skipped`\n"
        "  `unskip etsy ones` (payee filter)\n\n"
        "Budget queries:\n"
        "  `how much is left in groceries?`\n"
        "  `where are we tight this month?`\n"
        "  `show me amazon last 30 days`\n"
        "  `joint checking balance`\n\n"
        "Budget changes:\n"
        "  `put $200 in groceries`\n"
        "  `move $50 from dining to groceries`\n"
        "  `apply historical budget` — seed all categories from last 12 months\n\n"
        "Category management:\n"
        "  `create a category for password manager` (defaults to Monthly Bills)\n"
        "  `create Password Manager for $5/mo under Annual or Seasonal Costs`\n"
        "  `move Password Manager to Annual or Seasonal Costs`\n"
        "  `rename Password Manager to 1Password`\n\n"
        "Notifications:\n"
        "  `pause for 2 hours` / `quiet` / `mute`\n",
        parse_mode="Markdown",
    )


async def _ynab_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the YNAB writer NOW (don't wait for tomorrow's
    daily run). Useful when Steven wants YNAB updated before opening the
    YNAB app at a specific moment.
    """
    from bot import ynab_writer
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return
    await update.message.reply_text("🔄 Running YNAB writer…")
    try:
        report = await asyncio.to_thread(ynab_writer.run_once, settings)
        body = ynab_writer.format_report(report)
    except Exception as e:  # noqa: BLE001
        log.exception("ynab_writer manual run failed: %s", e)
        body = f"❌ YNAB writer crashed: {e}"
    await update.message.reply_text(body)


async def _amazon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 7 dedicated Amazon UI — checkbox-batch with verbose per-item
    detail. Two DMs sent in sequence:

      1. Status header: ready/waiting/aged-out counts.
      2. Rich /batch-style DM scoped to Amazon items only (if any are
         ready to categorize). Same checkbox tile UX as /batch.
    """
    from bot import amazon_tracker, batch_processor
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return
    db_path = settings.paths.database

    # Header: tracker counts (always sent first)
    snapshot = amazon_tracker.get_tracker_snapshot(db_path)
    header_body = amazon_tracker.format_tracker_message(snapshot)
    await update.message.reply_text(header_body)

    # Then: the actionable Amazon-only batch
    items = batch_processor.build_amazon_batch(db_path, user_id=user_id)
    if not items:
        # Status header already covered it; nothing to action
        return
    total = batch_processor.count_amazon_ready(db_path, user_id=user_id)
    initial_payload = [
        {"n": i + 1, "pt_id": it["pt_id"], "checked": True}
        for i, it in enumerate(items)
    ]
    body = batch_processor.render_batch_body(
        items, total, initial_payload,
        verbose=True, header_emoji="📦", header_label="Amazon",
    )
    keyboard = batch_processor.render_batch_buttons(items, initial_payload)
    sent = await update.message.reply_text(
        body, reply_markup=keyboard, parse_mode="Markdown",
    )
    batch_processor.save_batch_context(
        db_path, chat_id, items, sent.message_id,
        verbose=True, header_emoji="📦", header_label="Amazon",
    )


async def _batch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 7 /batch command — bulk-process COLD-lane pending_txns.

    Sends a single DM listing up to 15 oldest COLD items numbered, with
    each item's best-guess category. The user replies with a shorthand
    string (see bot.batch_processor.parse_reply) and the bot applies all
    decisions in one transaction.
    """
    from bot import batch_processor
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = _resolve_user_id_for_chat(settings, chat_id)
    if user_id is None:
        await update.message.reply_text("This chat isn't linked. See /start.")
        return

    items = batch_processor.build_batch(
        settings.paths.database, user_id=user_id,
    )
    with storage.connect(settings.paths.database) as con:
        total = con.execute(
            "SELECT COUNT(*) FROM pending_txn "
            "WHERE assigned_to_user_id = ? AND status='pending' "
            "  AND queue_lane='cold'",
            (user_id,),
        ).fetchone()[0]

    if items:
        # All start checked; payload_items mirrors initial state.
        initial_payload = [
            {"n": i + 1, "pt_id": it["pt_id"], "checked": True}
            for i, it in enumerate(items)
        ]
        body = batch_processor.render_batch_body(items, total, initial_payload)
        keyboard = batch_processor.render_batch_buttons(items, initial_payload)
        # parse_mode='Markdown' renders the ```code block``` as monospace
        # so the columns align even on a phone screen.
        sent = await update.message.reply_text(
            body, reply_markup=keyboard, parse_mode="Markdown",
        )
        batch_processor.save_batch_context(
            settings.paths.database, chat_id, items, sent.message_id,
        )
    else:
        await update.message.reply_text(
            batch_processor.render_batch_body([], 0),
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

def _register_handlers(app: Application) -> None:
    """Attach the same handler set to every Application instance.

    With per-user bot tokens (one bot per spouse), inbound messages route
    to whichever Application owns the bot the user is DM'ing. Handlers
    themselves are bot-agnostic — they look up the right outbound bot via
    ``_bot_for_chat`` and operate on the shared DB.
    """
    # Redesign-v2 Phase 1: log EVERY inbound update before real handlers run.
    # Group -100 fires first; the callback never raises ApplicationHandlerStop,
    # so it can't block anything. Outbound is logged by ChatLogRateLimiter.
    from bot.chat_log import log_inbound_update
    app.add_handler(TypeHandler(Update, log_inbound_update), group=-100)

    # Redesign-v2 Phase 4: household group traffic routes to its own handler
    # and NEVER reaches the DM machinery below (within one handler group,
    # PTB runs only the first match — this is registered ahead of the DM
    # text handlers so group chatter can't trip "This chat isn't linked").
    settings = app.bot_data.get("settings")
    group_id = int(getattr(getattr(settings, "telegram", None),
                           "group_chat_id", 0) or 0)
    if group_id:
        from bot.group_chat import handle_group_message
        app.add_handler(MessageHandler(
            filters.Chat(group_id) & filters.TEXT & ~filters.COMMAND,
            handle_group_message,
        ))

    app.add_handler(CommandHandler("start", _start_cmd))
    app.add_handler(CommandHandler("skip", _skip_cmd))
    app.add_handler(CommandHandler("quiet", _quiet_cmd))
    app.add_handler(CommandHandler("undo", _undo_cmd))
    app.add_handler(CommandHandler("help", _help_cmd))
    app.add_handler(CommandHandler("samples", _samples_cmd))
    app.add_handler(CommandHandler("batch", _batch_cmd))
    app.add_handler(CommandHandler("amazon", _amazon_cmd))
    app.add_handler(CommandHandler("ynab", _ynab_cmd))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_error_handler(_handle_telegram_error)
    # AI-mediated chat (Phase 3.5) — qwen3:32b processes free text. Registered
    # before _handle_text so it gets first crack at non-trivial messages.
    # Trivial replies (y/skip/undo) and short single-word category names
    # during in-flight categorization are forwarded back to _handle_text.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_ai_agent))


async def _run_apps(apps: list[Application]) -> None:
    """Drive N Applications concurrently in one event loop.

    PTB v22's ``Application.run_polling`` is synchronous and manages its
    own loop, which won't fit a multi-Application setup. The manual
    initialize/start/start_polling lifecycle (same pattern as
    first_run_setup.py) lets us run several Apps side-by-side and shut
    them all down cleanly on SIGTERM.
    """
    for app in apps:
        await app.initialize()
        # PTB only invokes post_init from run_polling()/run_webhook(), never
        # from the manual initialize()/start() lifecycle we use here for the
        # multi-Application setup. Without this call _post_init never runs, so
        # NONE of the background loops (push, daily/weekly, gmail, ynab sync,
        # lane sweep, awareness, and the localhost ui_api on :8765) start —
        # the bot answers DMs but does nothing proactive and Bot Control sees
        # a dead API. Mirror run_polling's behavior explicitly.
        if app.post_init:
            await app.post_init(app)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    log.info("telegram long-poll active on %d application(s)", len(apps))
    try:
        await asyncio.Event().wait()
    finally:
        for app in apps:
            try:
                await app.updater.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("updater.stop failed: %s", e)
            try:
                await app.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("app.stop failed: %s", e)
            try:
                await app.shutdown()
            except Exception as e:  # noqa: BLE001
                log.warning("app.shutdown failed: %s", e)


def run(settings: Settings | None = None) -> None:
    """Build all Applications (one per distinct bot token), register
    handlers, and block on long-polling.

    Multi-bot mode kicks in when any ``gmail_accounts`` entry sets
    ``telegram_bot_token_env`` — that user gets their own Application on
    that token. The "primary" Application (the one whose token matches
    ``settings.telegram_bot_token``) hosts ALL background loops — push,
    daily/weekly summaries, gmail/ynab poll, queue-lane sweep, UI API.
    Other Applications are input-only; their outbound DMs are routed
    through them via the shared ``chat_to_bot`` map.
    """
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

    from bot.config import resolve_user_bot_token
    token_to_accounts: dict[str, list] = {}
    for acct in settings.gmail_accounts:
        tok = resolve_user_bot_token(settings, acct.user_id)
        if not tok:
            log.warning("no bot token resolvable for user %s — skipping",
                        acct.user_id)
            continue
        token_to_accounts.setdefault(tok, []).append(acct)

    if not token_to_accounts:
        raise RuntimeError(
            "No Telegram bot tokens configured. Set TELEGRAM_BOT_TOKEN in "
            ".env or telegram_bot_token_env on a gmail_accounts entry."
        )

    # Primary app = the one whose token matches settings.telegram_bot_token,
    # if that token is in use; otherwise the first app we build. The primary
    # owns all background loops.
    primary_token = (
        settings.telegram_bot_token
        if settings.telegram_bot_token in token_to_accounts
        else next(iter(token_to_accounts))
    )

    # Single shared push-lock dict across all Apps so a callback on bot A
    # and a background push on bot B for the SAME chat_id can't race —
    # both call _get_push_lock and end up serialized on the same lock.
    shared_push_locks: dict[int, asyncio.Lock] = {}

    apps: list[Application] = []
    apps_by_token: dict[str, Application] = {}
    for tok, accounts in token_to_accounts.items():
        is_primary = (tok == primary_token)
        # ChatLogRateLimiter observes every outbound API call (sendMessage)
        # and logs it to chat_message — one choke point instead of wrapping
        # ~40 send call sites. It does no actual rate limiting.
        from bot.chat_log import ChatLogRateLimiter
        builder = (
            ApplicationBuilder()
            .token(tok)
            .rate_limiter(ChatLogRateLimiter(str(settings.paths.database)))
        )
        if is_primary:
            builder = builder.post_init(_post_init)
        app = builder.build()
        app.bot_data["settings"] = settings
        app.bot_data["categories"] = categories
        app.bot_data["categorizer"] = categorizer
        app.bot_data["is_primary_bot"] = is_primary
        app.bot_data["push_locks"] = shared_push_locks
        apps_by_token[tok] = app
        apps.append(app)
        _register_handlers(app)
        log.info("telegram app built for %d account(s); primary=%s; "
                 "users=%s",
                 len(accounts), is_primary,
                 [a.user_id for a in accounts])

    # chat_id → Bot map shared across every Application so any handler /
    # background loop can resolve the right outbound bot regardless of
    # which app it's running on.
    chat_to_bot: dict[int, "object"] = {}
    for tok, accounts in token_to_accounts.items():
        for acct in accounts:
            chat_to_bot[int(acct.chat_id)] = apps_by_token[tok].bot
    # Redesign-v2 Phase 4: the household group is served by group_bot_user's
    # bot (the one Steven added to the group). Mapping it here means every
    # existing _bot_for_chat / chat_to_bot call site can address the group
    # like any other chat.
    group_id = int(getattr(settings.telegram, "group_chat_id", 0) or 0)
    if group_id:
        group_tok = resolve_user_bot_token(
            settings, getattr(settings.telegram, "group_bot_user", "steven"))
        if group_tok and group_tok in apps_by_token:
            chat_to_bot[group_id] = apps_by_token[group_tok].bot
        else:
            log.warning("group_chat_id set but no bot resolves for user %r",
                        getattr(settings.telegram, "group_bot_user", None))
    for app in apps:
        app.bot_data["chat_to_bot"] = chat_to_bot

    log.info("starting telegram long-poll (%d app(s))", len(apps))
    asyncio.run(_run_apps(apps))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # The scheduled task runs headless — stdout vanishes. Keep WARNING+
    # (handler tracebacks included) in a file we can actually read.
    _fh = logging.FileHandler(
        Path(__file__).resolve().parent.parent / "bot_errors.log",
        encoding="utf-8",
    )
    _fh.setLevel(logging.WARNING)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(_fh)
    run()
