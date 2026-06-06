"""Strip inline keyboards from a range of recent bot messages.

Best-effort cleanup after a bot restart leaves orphaned questions with
live keyboards in a chat. Reads bot_conversation.last_asked_message_id
as the high watermark and walks backward N messages, calling
editMessageReplyMarkup on each. Telegram will fail silently for any
message_id that isn't a bot message or has already been touched.

Usage:
    python -m scripts.strip_stale_keyboards            # default 10 back
    python -m scripts.strip_stale_keyboards --count 20
"""
from __future__ import annotations

import argparse
import logging

import httpx

from bot import storage
from bot.config import load_settings

log = logging.getLogger("strip_stale")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=10,
                   help="how many message_ids back from current to strip")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()

    token = settings.telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"

    with storage.connect(settings.paths.database) as con:
        rows = con.execute(
            "SELECT chat_id, last_asked_message_id FROM bot_conversation "
            "WHERE last_asked_message_id IS NOT NULL"
        ).fetchall()

    for r in rows:
        chat_id = r["chat_id"]
        head = int(r["last_asked_message_id"])
        log.info("chat %s: stripping keyboards on msg_ids %d..%d (excluding %d)",
                 chat_id, head - args.count, head - 1, head)
        stripped = 0
        for mid in range(head - 1, head - args.count - 1, -1):
            try:
                resp = httpx.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "message_id": mid,
                        "reply_markup": {"inline_keyboard": []},
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    stripped += 1
                    log.info("  stripped msg %d", mid)
                else:
                    desc = resp.json().get("description", "")[:80]
                    log.debug("  skip msg %d: %s", mid, desc)
            except Exception as e:  # noqa: BLE001
                log.debug("  skip msg %d: %s", mid, e)
        log.info("chat %s: stripped %d keyboards", chat_id, stripped)


if __name__ == "__main__":
    main()
