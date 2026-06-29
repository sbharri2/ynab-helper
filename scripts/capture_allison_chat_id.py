"""One-shot helper: print the chat_id of the first DM to a given bot.

Usage:
    .venv\\Scripts\\python.exe scripts\\capture_allison_chat_id.py
    .venv\\Scripts\\python.exe scripts\\capture_allison_chat_id.py STEVEN_BOT_TOKEN

The optional arg picks which env var holds the bot token. Defaults to
ALLISON_BOT_TOKEN for backward compatibility. The script listens on
that bot's long-poll, captures the chat_id of the first inbound
message, replies to the sender with it, then exits.

Doesn't touch the DB, doesn't touch config.yaml, doesn't interfere
with the production bot's scheduled task. Just one ephemeral long-poll
on the named token.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters


async def _capture(token: str) -> int | None:
    chat_id_holder: dict[str, int] = {}
    stop_event = asyncio.Event()

    async def on_msg(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        user = update.effective_user
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        chat_id_holder["id"] = chat_id
        await update.message.reply_text(
            f"Got it. chat_id={chat_id}. You can close this chat — Claude is "
            f"wiring the rest now."
        )
        print(f"\n>>> chat_id = {chat_id}  (from: {name or user.username})\n")
        stop_event.set()

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.ALL, on_msg))

    print("Listening on HarrisBudgetBot for the first message…")
    print("(Have Allison open the bot and tap Start. Ctrl-C to abort.)")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    return chat_id_holder.get("id")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    env_name = sys.argv[1] if len(sys.argv) > 1 else "ALLISON_BOT_TOKEN"
    token = os.getenv(env_name, "").strip()
    if not token:
        sys.exit(
            f"{env_name} not set in .env. Add a line like:\n"
            f"  {env_name}=<bot-id>:<BotFather-token>"
        )
    print(f"Using env var: {env_name}")
    asyncio.run(_capture(token))


if __name__ == "__main__":
    main()
