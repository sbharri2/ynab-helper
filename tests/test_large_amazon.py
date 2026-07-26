"""Large Amazon charges bypass the auto-bucket and ask instead.

Spec: docs/superpowers/specs/2026-07-25-large-amazon-attention-design.md
"""
import sqlite3
from datetime import date

from bot import ingest, storage


def test_threshold_defaults_to_15000_cents():
    assert ingest.LARGE_AMAZON_DEFAULT_CENTS == 15000


def test_charge_at_threshold_is_large():
    assert ingest._is_large_amazon_charge(-15000, None) is True


def test_charge_below_threshold_is_not_large():
    assert ingest._is_large_amazon_charge(-14999, None) is False


def test_refund_is_never_large():
    # Signed test, not abs() — an $815 refund must not raise a question.
    assert ingest._is_large_amazon_charge(81509, None) is False


def test_settings_override_threshold():
    from bot.config import AmazonConfig, Settings

    s = Settings.model_construct(amazon=AmazonConfig(large_charge_cents=50000))
    assert ingest._is_large_amazon_charge(-20000, s) is False
    assert ingest._is_large_amazon_charge(-50000, s) is True
