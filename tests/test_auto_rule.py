import sqlite3

import pytest

from bot import storage
from bot.storage import init_db


def _seed_category(db, cat_id="cat-groceries", name="Groceries"):
    with storage.connect(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO category_group (id, name) VALUES (?, ?)",
            ("grp-1", "Day to Day Expenses"),
        )
        con.execute(
            "INSERT OR IGNORE INTO category (id, group_id, name) VALUES (?, ?, ?)",
            (cat_id, "grp-1", name),
        )
    return cat_id


def test_auto_rule_table_exists(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "auto_rule" in tables


def test_create_and_list(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    rid = storage.create_auto_rule(
        db, pattern=r"\bHARRIS\s*TEETER\b", category_id=cat,
        created_by="steven", note="weekly groceries",
    )
    assert rid > 0
    rules = storage.list_auto_rules(db)
    assert len(rules) == 1
    assert rules[0]["pattern"] == r"\bHARRIS\s*TEETER\b"
    assert rules[0]["category_id"] == cat
    assert rules[0]["enabled"] == 1
    assert rules[0]["fire_count"] == 0
    assert rules[0]["created_by"] == "steven"


def test_duplicate_pattern_rejected(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    storage.create_auto_rule(db, pattern=r"\bAPPLE\b", category_id=cat,
                             created_by="steven")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_auto_rule(db, pattern=r"\bAPPLE\b", category_id=cat,
                                 created_by="steven")


def test_enabled_only_filter(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    a = storage.create_auto_rule(db, pattern=r"\bA\b", category_id=cat,
                                 created_by="seed")
    storage.create_auto_rule(db, pattern=r"\bB\b", category_id=cat,
                             created_by="seed")
    storage.update_auto_rule(db, a, enabled=False)
    assert len(storage.list_auto_rules(db)) == 2
    assert len(storage.list_auto_rules(db, enabled_only=True)) == 1


def test_update_and_delete(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    other = _seed_category(db, "cat-dining", "Dining Out/Entertainment")
    rid = storage.create_auto_rule(db, pattern=r"\bX\b", category_id=cat,
                                   created_by="steven")
    assert storage.update_auto_rule(db, rid, category_id=other) is True
    assert storage.list_auto_rules(db)[0]["category_id"] == other
    assert storage.delete_auto_rule(db, rid) is True
    assert storage.list_auto_rules(db) == []
    assert storage.delete_auto_rule(db, rid) is False


def test_bump_fire_count(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    rid = storage.create_auto_rule(db, pattern=r"\bY\b", category_id=cat,
                                   created_by="steven")
    storage.bump_auto_rule_fire(db, rid)
    storage.bump_auto_rule_fire(db, rid)
    row = storage.list_auto_rules(db)[0]
    assert row["fire_count"] == 2
    assert row["last_fired_at"] is not None
