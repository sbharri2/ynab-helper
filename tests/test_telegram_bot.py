"""Smoke tests for the Telegram bot module.

The bot loop itself (long-polling, async handlers, push loop) is hard to
fully unit-test without a live Telegram server. These tests cover the
pure reply-parser, which is where most of the logic that *could* go
quietly wrong lives.
"""
from datetime import datetime

from bot.telegram_bot import _in_digest_window, _parse_user_reply


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


# ---------------------------------------------------------------------------
# _in_digest_window — gates non-Amazon/Venmo txns to a window after digest_time
# ---------------------------------------------------------------------------

def test_in_digest_window_inside():
    """A time 30 min after digest_time is inside the default 60-min window."""
    now = datetime(2026, 5, 16, 9, 30)
    assert _in_digest_window(now, "09:00") is True


def test_in_digest_window_at_start():
    """Boundary: exactly digest_time is inside the window."""
    now = datetime(2026, 5, 16, 9, 0)
    assert _in_digest_window(now, "09:00") is True


def test_in_digest_window_outside_before():
    """Before digest_time the window is closed."""
    now = datetime(2026, 5, 16, 8, 59)
    assert _in_digest_window(now, "09:00") is False


def test_in_digest_window_outside_after():
    """More than window_minutes past digest_time the window is closed."""
    now = datetime(2026, 5, 16, 10, 30)
    assert _in_digest_window(now, "09:00") is False


def test_in_digest_window_malformed_returns_false():
    """Bad config must not crash — return False (skip txns) instead."""
    now = datetime(2026, 5, 16, 9, 30)
    assert _in_digest_window(now, "not-a-time") is False
    assert _in_digest_window(now, "") is False
    assert _in_digest_window(now, None) is False  # type: ignore[arg-type]


def test_in_digest_window_custom_minutes():
    """window_minutes parameter widens or narrows the window."""
    now = datetime(2026, 5, 16, 11, 0)
    # 2 hours after 09:00 — outside default 60min, inside 180min
    assert _in_digest_window(now, "09:00", window_minutes=60) is False
    assert _in_digest_window(now, "09:00", window_minutes=180) is True
