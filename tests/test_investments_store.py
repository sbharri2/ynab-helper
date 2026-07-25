import sqlite3
from datetime import date

import pytest

from bot.storage import init_db, connect


INVESTMENT_TABLES = {
    "holding",
    "snapshot_round",
    "holding_value",
    "property_detail",
    "insurance_policy",
    "insurance_premium_observed",
    "savings_target",
}


def test_init_creates_investment_tables(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert INVESTMENT_TABLES.issubset(tables)


def test_holding_value_unique_per_holding_round(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with connect(db) as con:
        con.execute(
            "INSERT INTO holding (id, name, kind) VALUES ('h1', 'Marcus', 'cash')"
        )
        con.execute(
            "INSERT INTO snapshot_round (id, label, as_of_date) "
            "VALUES ('r1', 'Jul 2026', '2026-07-25')"
        )
        con.execute(
            "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
            "VALUES ('h1', 'r1', '2026-07-25', 2566800)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with connect(db) as con:
            con.execute(
                "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
                "VALUES ('h1', 'r1', '2026-07-25', 999)"
            )
