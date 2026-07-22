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
import difflib
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


def report_target(app) -> "tuple[object, int] | None":
    """(bot, group_chat_id) when the household group is configured.

    All reports (daily, weekly, writer, ops alerts) go to the group — one
    shared timeline instead of duplicate per-person DMs (Steven, 2026-07-09).
    Returns None when unconfigured so callers fall back to DMs.
    """
    settings = app.bot_data["settings"]
    gid = int(getattr(settings.telegram, "group_chat_id", 0) or 0)
    if not gid:
        return None
    bot = app.bot_data.get("chat_to_bot", {}).get(gid)
    if bot is None:
        log.warning("report_target: no bot mapped for group %s", gid)
        return None
    return bot, gid


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
    # Unanswered pings are the normal case — after a week, stop treating
    # one as "the current question" (it clutters the loose-reply
    # heuristic that wants a single open question). The item stays in
    # the Inbox, and a late reply-to on the old message still files it.
    with storage.connect(db_path) as con:
        expired = con.execute(
            """UPDATE question SET state = 'expired', resolved_at = ?
               WHERE state = 'open'
                 AND asked_at < datetime('now', '-7 days')""",
            (storage._utcnow(),),
        ).rowcount
    if expired:
        storage.audit(db_path, "group_questions_expired", {"count": expired})
    await _post_trip_confirms(app, settings, group_id)
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
        text = _ping_text(r)
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


async def _post_trip_confirms(app: Application, settings, group_id: int) -> None:
    """Suggestions-v2 trip windows: candidate trips get ONE confirmation
    question in the group. A 'y' reply arms the window."""
    db_path = settings.paths.database
    with storage.connect(db_path) as con:
        cands = con.execute(
            """SELECT * FROM trip
               WHERE state = 'candidate' AND confirm_message_id IS NULL
               ORDER BY id ASC LIMIT 3""",
        ).fetchall()
    if not cands:
        return
    bot = app.bot_data["chat_to_bot"].get(group_id)
    if bot is None:
        return
    for t in cands:
        text = (
            f"✈️ Looks like a trip around {t['start_date']} – {t['end_date']} "
            f"({t['detect_payee']}).\n"
            f"File away-from-home dining/transport in that window as "
            f"Vacation? Reply “y” to confirm or “no” to dismiss."
        )
        try:
            sent = await bot.send_message(chat_id=group_id, text=text)
        except Exception as e:  # noqa: BLE001
            log.warning("trip confirm send failed for trip %s: %s", t["id"], e)
            continue
        with storage.connect(db_path) as con:
            con.execute(
                "UPDATE trip SET tg_chat_id = ?, confirm_message_id = ? "
                "WHERE id = ?",
                (group_id, sent.message_id, t["id"]),
            )
        storage.audit(db_path, "trip_confirm_asked", {
            "trip_id": t["id"], "message_id": sent.message_id,
        })


def _ping_text(r) -> str:
    """One instant-ping message. r needs payee/amount_cents/txn_date and
    suggested_name (None when no suggestion)."""
    head = (f"🆕 {_fmt_money(r['amount_cents'])} {r['payee']} "
            f"({r['txn_date']})")
    if r["suggested_name"]:
        return (
            f"{head} — suggest {r['suggested_name']}\n"
            f"Reply “y” to accept, or another category — "
            f"or ignore it, it's in the Inbox."
        )
    return (
        f"{head}\n"
        f"Reply to this message with a category to file it — "
        f"or ignore it, it's in the Inbox."
    )


def post_instant_ping_sync(settings, pt_id: int) -> int:
    """Synchronous instant ping for one pending_txn — the desktop 'Ask in
    chat' button (runs inside the FastAPI route, so raw HTTP rather than
    the PTB application). Returns the Telegram message id.

    Raises RuntimeError when the group isn't configured or the item
    already has an open question in the group.
    """
    import json
    import urllib.parse
    import urllib.request

    db_path = settings.paths.database
    group_id = int(getattr(settings.telegram, "group_chat_id", 0) or 0)
    if not group_id:
        raise RuntimeError("no household group configured")
    with storage.connect(db_path) as con:
        r = con.execute(
            """SELECT pt.id, pt.payee, pt.amount_cents, pt.txn_date,
                      c.name AS suggested_name
               FROM pending_txn pt
               LEFT JOIN category c ON c.id = pt.suggested_category
               WHERE pt.id = ?""", (pt_id,),
        ).fetchone()
    if r is None:
        raise RuntimeError("unknown pending_txn")

    from bot.config import resolve_user_bot_token
    token = resolve_user_bot_token(
        settings, getattr(settings.telegram, "group_bot_user", "steven"))
    text = _ping_text(r)
    data = urllib.parse.urlencode(
        {"chat_id": group_id, "text": text}).encode("utf-8")
    resp = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data),
        ))
    if not resp.get("ok"):
        raise RuntimeError(f"telegram send failed: {resp}")
    message_id = resp["result"]["message_id"]
    with storage.connect(db_path) as con:
        # Re-asking supersedes any earlier open ping for this item — the
        # new message becomes the anchor (unanswered pings are normal;
        # the button's job is to bump the item back into view). Late
        # replies to the OLD message still work via the item-still-open
        # check in _handle_question_reply.
        superseded = [row["id"] for row in con.execute(
            """SELECT id FROM question WHERE item_kind = 'txn'
               AND item_id = ? AND state = 'open'""", (pt_id,),
        ).fetchall()]
        for qid in superseded:
            con.execute(
                "UPDATE question SET state = 'expired', "
                "answer_text = 'superseded by re-ask', resolved_at = ? "
                "WHERE id = ?",
                (storage._utcnow(), qid),
            )
        con.execute(
            """INSERT INTO question
                 (kind, item_kind, item_id, tg_chat_id, asked_message_id)
               VALUES ('instant', 'txn', ?, ?, ?)""",
            (pt_id, group_id, message_id),
        )
    storage.log_chat_message(db_path, tg_chat_id=group_id, direction="out",
                             sender="bot", text=text,
                             tg_message_id=message_id)
    storage.audit(db_path, "group_ping_sent", {
        "pt_id": pt_id, "message_id": message_id, "payee": r["payee"],
        "amount_cents": r["amount_cents"], "via": "desktop",
        "superseded": superseded,
    })
    return message_id


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------

_SKIP_WORDS = {"skip", "not sure", "idk", "dunno", "no idea", "later", "?"}
# "yes" to a ping that carries a suggestion = accept the suggestion.
# Short/ambiguous forms — only honored with a reply anchor (a bare "ok"
# in the group is chatter, but replying "ok" to a suggestion is consent).
_AFFIRM_WORDS = {"yes", "y", "k", "ok", "okay", "1", "+", "yep", "yeah",
                 "sure", "correct", "👍", "✓", "✔"}
# Explicit multi-word acceptances — unambiguous even without an anchor.
_AFFIRM_PHRASES = {"sounds right", "that's right", "thats right",
                   "that works", "sounds good", "looks good",
                   "good suggestion", "suggestion is good",
                   "use the suggestion", "go with that",
                   "go with the suggestion", "yes please"}
_POSITIVE_WORDS = ("good", "great", "right", "fine", "works", "correct",
                   "perfect", "yes", "approve", "accept")


def _is_affirmative(low: str) -> bool:
    if low in _AFFIRM_WORDS or low in _AFFIRM_PHRASES:
        return True
    # "the suggestion looks good to me" — mentions the suggestion + a
    # positive word, in any arrangement.
    return "suggestion" in low and any(p in low for p in _POSITIVE_WORDS)
# Bare-text conversational filler that must never be mistaken for an
# answer attempt when one question happens to be open.
_CHATTER_WORDS = {"ok", "okay", "k", "no", "nope", "thanks", "thank you",
                  "lol", "haha", "nice", "cool", "sounds good", "good",
                  "great", "hi", "hey", "hello", "morning", "night"}


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

    # ── Free-text group message (privacy mode off, 2026-07-08) ──────────
    text = (msg.text or "").strip()
    if not text:
        return
    db_path = settings.paths.database

    # 1. Addressed to the bot → Q&A via the agent (money questions,
    #    "what's pending", etc.). The deterministic queue intercept applies.
    bot_username = (context.bot.username or "").lower()
    if bot_username and f"@{bot_username}" in text.lower():
        stripped = text
        for form in (f"@{context.bot.username}", f"@{bot_username}"):
            stripped = stripped.replace(form, "")
        stripped = stripped.strip() or "help"
        from bot.agent import run_agent_turn
        try:
            reply = await asyncio.to_thread(
                run_agent_turn, db_path,
                user_text=stripped, chat_id=msg.chat_id,
                user_id=user, settings=settings,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("group agent turn failed: %s", e)
            reply = "Something went wrong answering that — try again."
        if reply:
            await msg.reply_text(reply)
        return

    # 1b. "we're traveling until the 26th" — explicit trip declaration
    #     opens a confirmed window immediately (no round-trip).
    from bot import trips as _trips
    travel = _trips.parse_travel_message(text)
    if travel is not None:
        start, end = travel
        trip_id = _trips.create_confirmed_manual(
            db_path, start=start, end=end, by=user)
        with storage.connect(db_path) as con:
            trow = con.execute("SELECT * FROM trip WHERE id = ?",
                               (trip_id,)).fetchone()
        filed = _trips.apply_trip_to_pending(db_path, trow)
        extra = (f" Filed {len(filed)} open charge"
                 f"{'s' if len(filed) != 1 else ''} as Vacation already."
                 if filed else "")
        await msg.reply_text(
            f"🏖 Got it — trip until {end}. Away-from-home charges will "
            f"file as Vacation.{extra}")
        return

    # 2. With exactly ONE open question, short bare text is almost always
    #    an answer attempt — process it even when it isn't an exact
    #    category name, so a near-miss ("Apple") gets a helpful nudge
    #    instead of silence (Steven hit this 2026-07-09). With several
    #    open, only category-shaped text gets the "reply directly" nudge;
    #    with none open, it's humans talking — stay out of it.
    from bot.agent_tools import _resolve_category
    with storage.connect(db_path) as con:
        open_qs = con.execute(
            """SELECT * FROM question
               WHERE tg_chat_id = ? AND state = 'open'
               ORDER BY id DESC LIMIT 2""",
            (msg.chat_id,),
        ).fetchall()
    low_bare = text.lower().strip(".!")
    # A bare letter answers the most recent lettered-choice prompt
    # ("kids" → a) .. c), reply "b") without needing a reply-to anchor.
    if len(low_bare) == 1 and low_bare in _CHOICE_LETTERS:
        with storage.connect(db_path) as con:
            choice = con.execute(
                """SELECT * FROM chat_choice WHERE tg_chat_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (msg.chat_id,),
            ).fetchone()
        if choice is not None:
            await _handle_choice_reply(db_path, msg, user, dict(choice),
                                       settings)
        return
    looks_like_answer = (
        len(text.split()) <= 4
        and "@" not in text
        and not text.endswith("?")
        and low_bare not in _CHATTER_WORDS
        # A bare "yes" with no reply anchor is too ambiguous to act on —
        # affirmatives only accept a suggestion via reply-to.
        and low_bare not in _AFFIRM_WORDS
    )
    if len(open_qs) == 1 and looks_like_answer:
        await _process_answer(db_path, msg, user, dict(open_qs[0]), text,
                              known_users=_known_users(settings))
        return
    if len(open_qs) > 1 and (
        _resolve_category(db_path, text) is not None
        or text.lower().rstrip(".!") in _SKIP_WORDS
    ):
        await msg.reply_text(
            "A few items are open — reply directly to the one you mean.")
        return

    # 3. A bare question with no @-mention ("You there?", "how much is
    #    left in dining?") — nobody else is being addressed, so treat it
    #    as directed at the bot and answer.
    if text.endswith("?") and "@" not in text:
        from bot.agent import run_agent_turn
        try:
            reply = await asyncio.to_thread(
                run_agent_turn, db_path,
                user_text=text, chat_id=msg.chat_id,
                user_id=user, settings=settings,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("group agent turn failed: %s", e)
            reply = "Something went wrong answering that — try again."
        if reply:
            await msg.reply_text(reply)
        return

    # Anything else is human conversation. Referee mode (unprompted
    # evidence) comes later; the restraint rule says stay quiet until then.
    return


async def _handle_question_reply(
    app: Application, settings, msg, user: str, replied_message_id: int,
) -> None:
    db_path = settings.paths.database
    # Trip confirmations anchor to their own table, not `question`.
    with storage.connect(db_path) as con:
        trip = con.execute(
            """SELECT * FROM trip
               WHERE tg_chat_id = ? AND confirm_message_id = ?
               ORDER BY id DESC LIMIT 1""",
            (msg.chat_id, replied_message_id),
        ).fetchone()
    if trip is not None:
        await _handle_trip_reply(db_path, msg, user, dict(trip))
        return
    # Lettered-options messages anchor to chat_choice, not question.
    with storage.connect(db_path) as con:
        choice = con.execute(
            """SELECT * FROM chat_choice
               WHERE tg_chat_id = ? AND message_id = ?
               ORDER BY id DESC LIMIT 1""",
            (msg.chat_id, replied_message_id),
        ).fetchone()
    if choice is not None:
        await _handle_choice_reply(db_path, msg, user, dict(choice), settings)
        return
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
        # Questions are reminders, not locks (Steven, 2026-07-11):
        # unanswered pings are the NORMAL case, so an old ping keeps
        # accepting answers for as long as its item is still open.
        item_status = None
        if q["item_kind"] == "txn":
            with storage.connect(db_path) as con:
                pt = con.execute(
                    "SELECT status FROM pending_txn WHERE id = ?",
                    (q["item_id"],),
                ).fetchone()
            item_status = pt["status"] if pt is not None else None
        if item_status not in ("pending", "skipped"):
            # Answers aren't locks either ("I input the wrong category
            # and it's not letting me update it" — Allison, 2026-07-22).
            # The desktop recategorizes filed txns freely; a reply that
            # names a category (or a member) supersedes the same way.
            # Chatter ("thanks", "ok") on a closed ping stays inert.
            text = (msg.text or "").strip()
            if item_status == "categorized" and _is_category_shaped(
                    db_path, text, _known_users(settings)):
                await _process_answer(db_path, msg, user, dict(q), text,
                                      known_users=_known_users(settings))
                return
            await msg.reply_text("Already handled — nothing to do. ✓")
            return
    await _process_answer(db_path, msg, user, dict(q),
                          (msg.text or "").strip(),
                          known_users=_known_users(settings))


_NEGATIVE_WORDS = {"no", "n", "nope", "nah", "dismiss", "not a trip",
                   "no trip"}


async def _handle_trip_reply(db_path, msg, user: str, trip: dict) -> None:
    from bot import trips
    if trip["state"] != "candidate":
        await msg.reply_text("That trip's already settled. ✓")
        return
    low = (msg.text or "").strip().lower().strip(".!")
    if _is_affirmative(low):
        trips.set_trip_state(db_path, trip["id"], "confirmed", by=user)
        filed = trips.apply_trip_to_pending(db_path, trip)
        extra = (f" Filed {len(filed)} charge"
                 f"{'s' if len(filed) != 1 else ''} from the window as "
                 f"Vacation already." if filed else "")
        await msg.reply_text(
            f"🏖 Trip armed {trip['start_date']} – {trip['end_date']}: "
            f"away-from-home charges will file as Vacation.{extra}")
        return
    if low in _NEGATIVE_WORDS or low in _SKIP_WORDS:
        trips.set_trip_state(db_path, trip["id"], "dismissed", by=user)
        await msg.reply_text("Okay — not a trip. Nothing changes.")
        return
    await msg.reply_text("Reply “y” to arm Vacation filing, or “no”.")


def _known_users(settings) -> frozenset:
    return frozenset(a.user_id for a in settings.gmail_accounts)


_ROUTE_PREFIXES = ("ask ", "for ", "give to ", "send to ", "route to ")


def _routing_target(low: str, known_users: frozenset) -> str | None:
    """'allison', 'ask allison', "allison's" → 'allison' when it names a
    known household member. None when the text isn't a plain routing form."""
    s = low.lstrip("@").strip()
    for p in _ROUTE_PREFIXES:
        if s.startswith(p):
            s = s[len(p):].strip()
            break
    for suffix in ("'s", "’s"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s if s in known_users else None


def _find_user_word(low: str, known_users: frozenset) -> str | None:
    """A known member's name appearing as a word inside longer text
    ('Allison needs to categorize this')."""
    import re
    for u in known_users:
        if re.search(rf"\b{re.escape(u)}\b", low):
            return u
    return None


_CHOICE_LETTERS = "abcd"


def _close_categories(db_path, text: str, n: int = 4) -> list[dict]:
    """Categories plausibly meant by `text`: substring hits first, then
    fuzzy name matches. Returns [{id, name}] with at most n entries."""
    with storage.connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, name FROM category WHERE hidden = 0").fetchall()]
    low = text.lower()
    subs = [r for r in rows if low in r["name"].lower()]
    if subs:
        return subs[:n]
    by_name = {r["name"]: r for r in rows}
    close = difflib.get_close_matches(text, list(by_name), n=n, cutoff=0.5)
    return [by_name[nm] for nm in close]


def _is_category_shaped(db_path, text: str, known_users: frozenset) -> bool:
    """Does this reply plausibly name a category (or a member, which
    files to their personal envelope)? Gates the correction path on
    already-filed items. Affirmatives are excluded: "y" agrees with a
    suggestion, and a filed item has nothing left to agree to."""
    low = text.lower().strip(".!")
    if (not low or low in _SKIP_WORDS or low in _CHATTER_WORDS
            or _is_affirmative(low)):
        return False
    name_word = low[4:].strip() if low.startswith("ask ") else low
    if name_word in known_users:
        return True
    from bot.agent_tools import _resolve_category
    if _resolve_category(db_path, text) is not None:
        return True
    return len(text.strip()) >= 3 and bool(_close_categories(db_path, text))


async def _offer_choices(db_path, msg, q: dict, text: str,
                         options: list[dict]) -> None:
    """“kids” matched several categories → offer a) .. d), remember the
    mapping keyed by the options message so a single letter picks one
    (Steven, 2026-07-19)."""
    import json
    lines = [f"“{text}” matches a few categories:"]
    for letter, o in zip(_CHOICE_LETTERS, options):
        lines.append(f"  {letter}) {o['name']}")
    lines.append("Reply with the letter — or the full name, or “skip”.")
    sent = await msg.reply_text("\n".join(lines))
    with storage.connect(db_path) as con:
        con.execute(
            "INSERT INTO chat_choice "
            "  (tg_chat_id, message_id, question_id, options_json) "
            "VALUES (?, ?, ?, ?)",
            (msg.chat_id, sent.message_id, q["id"],
             json.dumps(options[:len(_CHOICE_LETTERS)])),
        )


async def _handle_choice_reply(db_path, msg, user: str, choice: dict,
                               settings) -> None:
    """A reply to (or bare letter after) a lettered-options message."""
    import json
    options = json.loads(choice["options_json"])
    with storage.connect(db_path) as con:
        q = con.execute("SELECT * FROM question WHERE id = ?",
                        (choice["question_id"],)).fetchone()
    if q is None:
        return
    low = (msg.text or "").strip().lower().strip(".!")
    if len(low) == 1:
        idx = _CHOICE_LETTERS.find(low)
        if 0 <= idx < len(options):
            await _file_and_confirm(db_path, msg, user, dict(q),
                                    options[idx], low)
        else:
            await msg.reply_text(
                "Pick one of the letters above, or type the full "
                "category name.")
        return
    # Anything longer is a fresh answer against the same item.
    await _process_answer(db_path, msg, user, dict(q),
                          (msg.text or "").strip(),
                          known_users=_known_users(settings))


async def _file_and_confirm(db_path, msg, user: str, q: dict, cat: dict,
                            text: str) -> None:
    """Shared tail of every filing path: commit, close, audit, confirm."""
    filed = _file_item(db_path, q, cat["id"], user)
    if not filed:
        await msg.reply_text("That item seems to be gone — nothing filed.")
        _close_question(db_path, q["id"], "resolved_elsewhere", user, text)
        return
    _close_question(db_path, q["id"], "answered", user, text)
    prev_cat_id = (filed["chosen_category"]
                   if filed.get("status") == "categorized" else None)
    storage.audit(db_path, "group_reply_filed", {
        "question_id": q["id"], "item_kind": q["item_kind"],
        "item_id": q["item_id"], "category_id": cat["id"], "by": user,
        "prev_category_id": prev_cat_id,
    })
    if prev_cat_id == cat["id"]:
        await msg.reply_text(
            f"✓ {filed['payee']} is already filed as {cat['name']} — "
            f"all set.")
        return
    if prev_cat_id is not None:
        with storage.connect(db_path) as con:
            old = con.execute("SELECT name FROM category WHERE id = ?",
                              (prev_cat_id,)).fetchone()
        old_name = old["name"] if old else "another category"
        await msg.reply_text(
            f"✓ Changed {filed['payee']} "
            f"({_fmt_money(filed['amount_cents'])}): "
            f"{old_name} → {cat['name']}"
        )
        return
    await msg.reply_text(
        f"✓ Filed {filed['payee']} ({_fmt_money(filed['amount_cents'])}) "
        f"→ {cat['name']}"
    )


async def _process_answer(db_path, msg, user: str, q: dict, text: str,
                          known_users: frozenset = frozenset()) -> None:
    """Apply one human answer to one open question. Shared by the reply-to
    path (deterministic anchor) and the single-open-question fallback."""
    low = text.lower().strip(".!")

    if low in _SKIP_WORDS:
        _close_question(db_path, q["id"], "answered", user, text)
        await msg.reply_text(
            "No problem — it stays in the Inbox for later.")
        return

    # A bare member name files to that member's personal-spending
    # envelope (Steven, 2026-07-19: routing-by-name is retired — typing
    # "Allison" means "this purchase is hers", i.e. Allison Personal
    # Savings, not "forward it to her").
    name_word = low[4:].strip() if low.startswith("ask ") else low
    if name_word in known_users:
        with storage.connect(db_path) as con:
            row = con.execute(
                "SELECT id, name FROM category WHERE hidden = 0 "
                "AND LOWER(name) = ?",
                (f"{name_word} personal savings",),
            ).fetchone()
        if row is not None:
            await _file_and_confirm(db_path, msg, user, q, dict(row), text)
            return

    from bot.agent_tools import _resolve_category
    cat = None
    if _is_affirmative(low):
        # "y" / "ok" / "suggestion is good" = accept the ping's suggestion.
        with storage.connect(db_path) as con:
            row = con.execute(
                """SELECT c.id, c.name FROM pending_txn pt
                   JOIN category c ON c.id = pt.suggested_category
                   WHERE pt.id = ?""", (q["item_id"],),
            ).fetchone()
        if row is not None:
            cat = dict(row)
        else:
            await msg.reply_text(
                "There was no suggestion on that one — "
                "name the category instead.")
            return
    if cat is None:
        cat = _resolve_category(db_path, text)
    if cat is None:
        # "I'm telling you Allison needs to categorize this" — a member's
        # name inside text that isn't a category = route it to them.
        # (Safe ordering: "Allison Personal Savings" already resolved above.)
        # Several plausible categories → lettered options, single-letter
        # pick (Steven, 2026-07-19). Guard len ≥ 3: one- and two-char
        # text substring-matches half the category list.
        options = (_close_categories(db_path, text)
                   if len(text.strip()) >= 3 else [])
        if options:
            await _offer_choices(db_path, msg, q, text, options)
            return
        await msg.reply_text(
            f"Couldn't match “{text}” to a category. "
            f"Try the exact name, or say “skip”."
        )
        return

    await _file_and_confirm(db_path, msg, user, q, cat, text)


async def _reroute_item(db_path, msg, user: str, q: dict,
                        target: str, text: str) -> None:
    """Reassign the question's item to another household member and post a
    fresh question addressed to them (new anchor, so their reply files it)."""
    if q["item_kind"] != "txn":
        await msg.reply_text("Can't route that kind of item yet.")
        return
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, payee, amount_cents, txn_date, status "
            "FROM pending_txn WHERE id = ?", (q["item_id"],),
        ).fetchone()
        if pt is None or pt["status"] not in ("pending", "skipped"):
            _close_question(db_path, q["id"], "resolved_elsewhere", user, text)
            await msg.reply_text("That item's already handled — nothing to route.")
            return
        con.execute(
            "UPDATE pending_txn SET assigned_to_user_id = ? WHERE id = ?",
            (target, pt["id"]),
        )
    _close_question(db_path, q["id"], "answered", user, text)
    name = target.capitalize()
    sent = await msg.reply_text(
        f"👉 Over to {name}: {pt['payee']} "
        f"({_fmt_money(pt['amount_cents'])}, {pt['txn_date']}). "
        f"{name} — reply to this message with a category, or “skip”."
    )
    if sent is not None and getattr(sent, "message_id", None):
        with storage.connect(db_path) as con:
            con.execute(
                """INSERT INTO question
                     (kind, item_kind, item_id, tg_chat_id, asked_message_id)
                   VALUES ('instant', 'txn', ?, ?, ?)""",
                (pt["id"], msg.chat_id, sent.message_id),
            )
    storage.audit(db_path, "group_rerouted", {
        "question_id": q["id"], "pt_id": pt["id"],
        "from": user, "to": target, "text": text[:80],
    })


def _close_category_names(db_path, text: str, n: int = 3) -> list[str]:
    """Best-effort 'did you mean' for an unmatched category answer:
    substring hits first, then difflib similarity."""
    import difflib
    with storage.connect(db_path) as con:
        names = [r["name"] for r in con.execute(
            "SELECT name FROM category WHERE hidden = 0"
        ).fetchall()]
    low = text.lower()
    subs = [nm for nm in names if low in nm.lower()]
    if subs:
        return subs[:n]
    return difflib.get_close_matches(text, names, n=n, cutoff=0.5)


def _file_item(db_path, q, category_id: str, user: str) -> dict | None:
    """Commit a category on the question's item. Mirrors the desktop
    /categorize endpoint: pending_txn + ledger promotion, filed_by stamped."""
    if q["item_kind"] != "txn":
        return None  # order questions arrive with the batch feature
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, payee, amount_cents, ynab_txn_id, status, "
            "chosen_category FROM pending_txn WHERE id = ?",
            (q["item_id"],),
        ).fetchone()
        # 'categorized' is re-fileable: corrections supersede, matching
        # the desktop /categorize endpoint (which has no status guard).
        if pt is None or pt["status"] not in ("pending", "skipped",
                                              "categorized"):
            return None
        con.execute(
            # synced_to_ynab_at = NULL so the writer re-pushes a
            # corrected category instead of considering it done.
            "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
            "status = 'categorized', filed_by = ?, "
            "synced_to_ynab_at = NULL WHERE id = ?",
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
