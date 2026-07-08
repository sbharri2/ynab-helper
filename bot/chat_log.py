"""Redesign-v2 Phase 1: log every Telegram message, both directions.

The conversation becomes data; Telegram is just a client (docs/redesign-v2.md).
Rows land in ``chat_message`` and feed the desktop Chat portal timeline plus,
later, the per-job eval sets for the narrow-job qwen pipeline.

Interception strategy — no call sites touched:

* **Outbound** — ``ChatLogRateLimiter`` implements PTB's ``BaseRateLimiter``
  hook, which wraps every Bot API request the library makes (``get_updates``
  excluded by PTB itself). One choke point regardless of which of the ~40
  ``send_message``/``reply_text`` call sites produced the send. It performs
  no rate limiting; it only observes.

* **Inbound** — ``log_inbound_update`` is registered as a ``TypeHandler`` in
  group -100 so it sees every ``Update`` before the real handlers and never
  blocks them (plain return; no ``ApplicationHandlerStop``).

Logging must never break the bot's ability to talk: every failure path here
is swallowed at WARNING level (``storage.log_chat_message`` already does the
same for DB errors).
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import BaseRateLimiter, ContextTypes

from bot import storage

log = logging.getLogger(__name__)


def _resolve_user(settings, tg_user_id: int | None) -> str:
    """Map a Telegram user id to 'steven'/'allison'; raw id string otherwise.

    chat_id == Telegram user_id across all our bots (DMs), so the
    gmail_accounts chat_id mapping doubles as the sender map. In a future
    group chat the sender still arrives as from_user.id, so this keeps
    working unchanged.
    """
    if tg_user_id is None:
        return "?"
    for acct in getattr(settings, "gmail_accounts", []) or []:
        try:
            if int(acct.chat_id) == int(tg_user_id):
                return acct.user_id
        except (TypeError, ValueError):
            continue
    return str(tg_user_id)


class ChatLogRateLimiter(BaseRateLimiter[None]):
    """Observe-only 'rate limiter': logs sendMessage traffic to chat_message."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:  # noqa: D102 — required by ABC
        return None

    async def shutdown(self) -> None:  # noqa: D102 — required by ABC
        return None

    async def process_request(
        self,
        callback,
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: None,
    ):
        result = await callback(*args, **kwargs)
        if endpoint == "sendMessage":
            try:
                self._log_send(data, result)
            except Exception as e:  # noqa: BLE001 — never break a send
                log.warning("outbound chat log failed: %s", e)
        return result

    def _log_send(self, data: dict[str, Any], result: Any) -> None:
        chat_id = data.get("chat_id")
        if chat_id is None:
            return
        # PTB may pass reply linkage either as the legacy flat field or as a
        # ReplyParameters object/dict under 'reply_parameters'.
        reply_to = data.get("reply_to_message_id")
        if reply_to is None:
            rp = data.get("reply_parameters")
            if rp is not None:
                reply_to = (
                    rp.get("message_id") if isinstance(rp, dict)
                    else getattr(rp, "message_id", None)
                )
        # The wrapped callback returns the raw API JSON dict; be tolerant of
        # object results too.
        if isinstance(result, dict):
            message_id = result.get("message_id")
        else:
            message_id = getattr(result, "message_id", None)
        storage.log_chat_message(
            self._db_path,
            tg_chat_id=int(chat_id),
            direction="out",
            sender="bot",
            text=data.get("text"),
            tg_message_id=message_id,
            reply_to_tg_message_id=reply_to,
        )


async def log_inbound_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """TypeHandler(group=-100) callback: log every inbound message/tap."""
    try:
        settings = context.application.bot_data.get("settings")
        if settings is None:
            return
        db_path = settings.paths.database

        if update.callback_query is not None:
            cq = update.callback_query
            msg = cq.message
            storage.log_chat_message(
                db_path,
                tg_chat_id=(msg.chat_id if msg else cq.from_user.id),
                direction="in",
                sender=_resolve_user(settings, cq.from_user.id),
                text=f"[button] {cq.data}",
                # anchor the tap to the message whose keyboard was tapped
                reply_to_tg_message_id=(msg.message_id if msg else None),
            )
            return

        msg = update.effective_message
        if msg is None or msg.from_user is None:
            return
        text = msg.text or msg.caption
        if text is None:
            # photo/document/etc. — record that *something* arrived so the
            # portal timeline has no silent gaps.
            text = "[non-text message]"
        storage.log_chat_message(
            db_path,
            tg_chat_id=msg.chat_id,
            direction="in",
            sender=_resolve_user(settings, msg.from_user.id),
            text=text,
            tg_message_id=msg.message_id,
            reply_to_tg_message_id=(
                msg.reply_to_message.message_id if msg.reply_to_message else None
            ),
        )
    except Exception as e:  # noqa: BLE001 — never block real handlers
        log.warning("inbound chat log failed: %s", e)
