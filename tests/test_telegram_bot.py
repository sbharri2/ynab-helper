"""Smoke tests for the Telegram bot module.

The bot loop itself (long-polling, async handlers, push loop) is hard to
fully unit-test without a live Telegram server. These tests cover the
pure reply-parser, which is where most of the logic that *could* go
quietly wrong lives.
"""
from bot.telegram_bot import _parse_user_reply


def test_parse_reply_confirmation():
    assert _parse_user_reply("y") == ("confirm", None)
    assert _parse_user_reply("YES") == ("confirm", None)
    assert _parse_user_reply("✅") == ("confirm", None)  # ✅


def test_parse_reply_skip():
    assert _parse_user_reply("skip") == ("skip", None)
    assert _parse_user_reply("/skip") == ("skip", None)


def test_parse_reply_undo():
    assert _parse_user_reply("/undo") == ("undo", None)


def test_parse_reply_freetext():
    assert _parse_user_reply("for the trip") == ("text", "for the trip")
    assert _parse_user_reply("Groceries") == ("text", "Groceries")
