from bot import payee_overrides, storage
from bot.storage import init_db


def _cat(db, cat_id, name):
    with storage.connect(db) as con:
        con.execute("INSERT OR IGNORE INTO category_group (id, name) "
                    "VALUES ('g', 'G')")
        con.execute("INSERT OR IGNORE INTO category (id, group_id, name) "
                    "VALUES (?, 'g', ?)", (cat_id, name))
    return cat_id


def test_match_returns_none_with_no_rules(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    assert payee_overrides.match_rule(db, "HARRIS TEETER 0123") is None


def test_match_is_case_insensitive(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    rid = storage.create_auto_rule(db, pattern=r"\bHARRIS\s*TEETER\b",
                                   category_id=cat, created_by="steven")
    m = payee_overrides.match_rule(db, "harris teeter #0123 APEX USA")
    assert m is not None
    assert m["rule_id"] == rid
    assert m["category_id"] == cat


def test_lowest_id_wins(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a = _cat(db, "c-a", "Groceries")
    b = _cat(db, "c-b", "Dining Out/Entertainment")
    first = storage.create_auto_rule(db, pattern=r"HARRIS", category_id=a,
                                     created_by="seed")
    storage.create_auto_rule(db, pattern=r"HARRIS\s*TEETER", category_id=b,
                             created_by="steven")
    assert payee_overrides.match_rule(db, "HARRIS TEETER")["rule_id"] == first


def test_disabled_rule_never_fires(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    rid = storage.create_auto_rule(db, pattern=r"HARRIS", category_id=cat,
                                   created_by="steven")
    storage.update_auto_rule(db, rid, enabled=False)
    assert payee_overrides.match_rule(db, "HARRIS TEETER") is None


def test_invalid_regex_is_skipped_not_fatal(tmp_path):
    """A rule saved with a bad pattern must not break matching for the
    rules after it — one malformed row cannot take down categorization."""
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    with storage.connect(db) as con:
        con.execute("INSERT INTO auto_rule (pattern, category_id, created_by) "
                    "VALUES ('[unclosed', ?, 'steven')", (cat,))
    storage.create_auto_rule(db, pattern=r"HARRIS", category_id=cat,
                             created_by="steven")
    m = payee_overrides.match_rule(db, "HARRIS TEETER")
    assert m is not None and m["category_id"] == cat


def test_resolve_payee_override_shape_preserved(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-elec", "Electric (24th)")
    storage.create_auto_rule(db, pattern=r"\bDUKE\s*ENERGY\b",
                             category_id=cat, created_by="seed")
    got = payee_overrides.resolve_payee_override(db, "DUKE ENERGY 800-777")
    assert got["category_id"] == cat
    assert got["category_name"] == "Electric (24th)"
    assert got["source"] == "payee_override"


def test_empty_payee(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    assert payee_overrides.match_rule(db, "") is None
    assert payee_overrides.match_rule(db, None) is None
