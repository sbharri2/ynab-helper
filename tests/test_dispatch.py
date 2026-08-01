from datetime import date

from bot import dispatch, storage
from bot.storage import init_db, insert_pending_txn


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-elec','g','Electric (24th)')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Joint Checking','checking',1)")
    return db


def _ledger(db, payee, cents=-18400):
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split) VALUES ('a1', '2026-08-01', ?, ?, 0)",
            (cents, payee),
        )
        return cur.lastrowid


def _pending(db, payee, lid, cents=-18400):
    return insert_pending_txn(
        db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
        ynab_account_id="a1", payee=payee, amount_cents=cents,
        txn_date=date(2026, 8, 1), memo="",
    )


def test_no_rule_means_ask(tmp_path):
    db = _setup(tmp_path)
    lid = _ledger(db, "SOME NEW MERCHANT")
    pt = _pending(db, "SOME NEW MERCHANT", lid)
    got = dispatch.classify(db, pt)
    assert got["action"] == "ask"
    assert got["rule_id"] is None


def test_rule_hit_means_fyi(tmp_path):
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\s*ENERGY\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY 800-777-9898")
    pt = _pending(db, "DUKE ENERGY 800-777-9898", lid)
    got = dispatch.classify(db, pt)
    assert got["action"] == "fyi"
    assert got["rule_id"] == rid
    assert got["category_id"] == "c-elec"


def test_file_by_rule_writes_pending_and_ledger(tmp_path):
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY")
    pt = _pending(db, "DUKE ENERGY", lid)
    assert dispatch.file_by_rule(db, pt, rid, "c-elec") is True
    with storage.connect(db) as con:
        row = con.execute("SELECT status, chosen_category, filed_by "
                          "FROM pending_txn WHERE id = ?", (pt,)).fetchone()
        assert row["status"] == "categorized"
        assert row["chosen_category"] == "c-elec"
        assert row["filed_by"] == f"rule:{rid}"
        lrow = con.execute("SELECT category_id FROM ledger_txn WHERE id = ?",
                           (lid,)).fetchone()
        assert lrow["category_id"] == "c-elec"
        rule = con.execute("SELECT fire_count, last_fired_at FROM auto_rule "
                           "WHERE id = ?", (rid,)).fetchone()
        assert rule["fire_count"] == 1
        assert rule["last_fired_at"] is not None


def test_dispatch_pending_counts(tmp_path):
    db = _setup(tmp_path)
    storage.create_auto_rule(db, pattern=r"\bDUKE\b", category_id="c-elec",
                             created_by="steven")
    for payee in ("DUKE ENERGY", "DUKE ENERGY", "MYSTERY SHOP"):
        lid = _ledger(db, payee)
        _pending(db, payee, lid)
    got = dispatch.dispatch_pending(db)
    assert got == {"filed": 2, "asked": 1}


def test_dispatch_is_idempotent(tmp_path):
    """Running twice must not double-file or double-bump fire_count."""
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY")
    _pending(db, "DUKE ENERGY", lid)
    dispatch.dispatch_pending(db)
    second = dispatch.dispatch_pending(db)
    assert second["filed"] == 0
    with storage.connect(db) as con:
        n = con.execute("SELECT fire_count FROM auto_rule WHERE id = ?",
                        (rid,)).fetchone()["fire_count"]
    assert n == 1


def test_ynab_uuid_pending_updates_ledger_by_uuid(tmp_path):
    """Rows whose ynab_txn_id is a real UUID (not 'ledger:N') must still
    promote the category onto the ledger row."""
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split, ynab_txn_id) "
            "VALUES ('a1','2026-08-01',-18400,'DUKE ENERGY',0,'uuid-1')")
    pt = insert_pending_txn(
        db, user_id="steven", ynab_txn_id="uuid-1", ynab_account_id="a1",
        payee="DUKE ENERGY", amount_cents=-18400,
        txn_date=date(2026, 8, 1), memo="")
    assert dispatch.file_by_rule(db, pt, rid, "c-elec") is True
    with storage.connect(db) as con:
        got = con.execute("SELECT category_id FROM ledger_txn "
                          "WHERE ynab_txn_id = 'uuid-1'").fetchone()
    assert got["category_id"] == "c-elec"
