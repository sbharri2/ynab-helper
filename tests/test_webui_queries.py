"""Tests for the Tauri/web UI's localhost HTTP API.

``fixture_db`` copies the live database once per test module so tests read
real data without ever touching the live file (bot stays the single
writer — see feedback_bulk_db_ops_on_gdrive / project_db_on_gdrive memory).
"""
import datetime as _dt
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

LIVE_DB = Path(r"C:\Users\Steven\ynabhelper\ynab_helper.db")


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory) -> str:
    tmp_dir = tmp_path_factory.mktemp("webui_db")
    dest = tmp_dir / "test.db"
    shutil.copy(LIVE_DB, dest)
    return str(dest)


def make_app(tmp_path, db_path):
    (tmp_path / "ui_api_token.txt").write_text("legacy-tok")
    (tmp_path / "ui_api_tokens.json").write_text(
        json.dumps({"allison-tok": "allison"}))
    from bot import http_api
    return http_api.build_app(db_path=db_path, token_dir=tmp_path,
                               webui_dir=tmp_path / "webui")


def test_token_identity(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    assert c.get("/categories",
                 headers={"x-api-token": "legacy-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "allison-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "wrong"}).status_code == 401


def test_registry_q_categories(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_categories"](fixture_db)
    assert rows and {"id", "group_id", "group_name", "name",
                     "is_spending", "hidden"} <= set(rows[0])


def test_q_route(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    h = {"x-api-token": "legacy-tok"}
    assert c.post("/q/q_categories", json={}, headers=h).status_code == 200
    assert c.post("/q/nope", json={}, headers=h).status_code == 404
    assert c.post("/q/q_categories", json={}).status_code in (401, 422)


def test_q_transactions_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_transactions"](fixture_db, since="2026-07-01",
                                       until="2026-07-31", limit=50)
    assert rows
    need = {"id", "account_name", "posted_date", "amount_cents", "payee",
            "category_name", "pending_id", "decision_state",
            "asked_in_chat", "account_on_budget", "is_split"}
    assert need <= set(rows[0])
    assert isinstance(rows[0]["asked_in_chat"], bool)
    assert isinstance(rows[0]["account_on_budget"], bool)
    assert isinstance(rows[0]["is_split"], bool)


def test_q_transactions_limit_offset(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_transactions"](fixture_db, limit=3, offset=0)
    assert len(rows) == 3


def test_q_month_categories_available_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_month_categories"](fixture_db, month="2026-07")
    r = next(x for x in rows if x["category_name"] == "Groceries")
    assert isinstance(r["available_cents"], int)


def test_q_category_avg_activity_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_category_avg_activity"](fixture_db, months=6)
    assert rows
    need = {"category_id", "months_observed", "avg_cents"}
    assert need <= set(rows[0])
    # avg_cents is a magnitude (outflows negated in the SQL), never negative.
    assert all(r["avg_cents"] >= 0 for r in rows)


def test_q_cash_trace_shape_and_walk(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_cash_trace"](fixture_db, months=6)
    assert len(rows) == 6
    need = {"month", "inflow_cents", "outflow_cents", "net_cents",
            "end_cash_cents"}
    assert need <= set(rows[0])
    for r in rows:
        assert r["net_cents"] == r["inflow_cents"] + r["outflow_cents"]
    # months clamp to 1..=24 (matches the Rust `.clamp(1, 24)`).
    assert len(REGISTRY["q_cash_trace"](fixture_db, months=999)) == 24
    assert len(REGISTRY["q_cash_trace"](fixture_db, months=0)) == 1


def test_q_cc_balance_summary_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    row = REGISTRY["q_cc_balance_summary"](fixture_db)
    need = {"cc_total_owed_cents", "checking_total_cents",
            "after_payoff_cents", "cards"}
    assert need <= set(row)
    assert row["after_payoff_cents"] == (
        row["checking_total_cents"] - row["cc_total_owed_cents"])
    if row["cards"]:
        assert {"account_id", "name", "balance_cents"} <= set(row["cards"][0])


def test_q_inbox_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_inbox"](fixture_db)
    if rows:
        need = {"id", "txn_date", "payee", "amount_cents", "memo",
                "raw_summary", "status", "queue_lane", "assigned_to_user_id",
                "suggested_category_id", "suggested_category_name"}
        assert need <= set(rows[0])
        assert all(r["status"] in ("pending", "skipped") for r in rows)


def test_income_sources_golden(fixture_db):
    """Structural, not date-pinned: `last_seen` moves forward with every
    real paycheck, and even a genuinely-biweekly payee can classify as
    "semi-monthly" some months depending on where its dates land in the
    calendar (see the cadence heuristic in q_income_sources) — so this
    only asserts shape-stable properties, not values that age out."""
    from bot.webui_queries import REGISTRY
    srcs = {s["payee_key"]: s for s in REGISTRY["q_income_sources"](fixture_db)}

    obrien_key = "ACH O'BRIEN/ATKINS A"
    assert obrien_key in srcs
    assert srcs[obrien_key]["cadence"] in ("biweekly", "semi-monthly")
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", srcs[obrien_key]["last_seen"])
    assert srcs[obrien_key]["last_seen"] <= _dt.date.today().isoformat()

    # Actalent is a manual override, not auto-detected — derive the
    # expectation from the fixture's own override table rather than
    # hardcoding a cadence that a future override edit could change.
    con = sqlite3.connect(fixture_db)
    con.row_factory = sqlite3.Row
    override = con.execute(
        "SELECT cadence FROM income_source_override "
        "WHERE payee_key = ? AND status = 'active'",
        ("ACH ACTALENT, INC.",),
    ).fetchone()
    con.close()
    if override is not None:
        assert "ACH ACTALENT, INC." in srcs
        assert srcs["ACH ACTALENT, INC."]["cadence"] == override["cadence"]


def test_rta_matches_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rta = REGISTRY["q_ready_to_assign"](fixture_db, month="2026-07")
    assert rta["ready_to_assign_cents"] == rta["cash_cents"] - rta["available_cents"]
    _ = [p["expected_date"] for p in rta["paychecks"]
         if p["source_payee"] == "ACH O'BRIEN/ATKINS A"]
    assert "2026-07-14" in " ".join(
        (p.get("actual_date") or p["expected_date"]) for p in rta["paychecks"])


def test_q_seasonal_funds_stub(fixture_db):
    from bot.webui_queries import REGISTRY
    assert REGISTRY["q_seasonal_funds"](fixture_db) == []


def test_spa_serving(tmp_path, fixture_db):
    from fastapi.testclient import TestClient
    app = make_app(tmp_path, fixture_db)
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<html>hb</html>")
    c = TestClient(app)
    assert c.get("/").text == "<html>hb</html>"
    assert c.get("/budget").text == "<html>hb</html>"      # SPA fallback
    assert c.post("/q/q_seasonal_funds", json={},
                  headers={"x-api-token": "legacy-tok"}).json() == []


def test_spa_serving_missing_bundle_404s(tmp_path, fixture_db):
    """No webui/index.html at all (bundle never deployed) -> a helpful 404,
    not a crash."""
    from fastapi.testclient import TestClient
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    resp = c.get("/")
    assert resp.status_code == 404
    assert "webui bundle not deployed" in resp.json()["detail"]


def test_spa_fallback_404s_for_unknown_api_prefix(tmp_path, fixture_db):
    """A path under a real API prefix that isn't a real route (e.g. a typo)
    must 404, never fall through to index.html."""
    from fastapi.testclient import TestClient
    app = make_app(tmp_path, fixture_db)
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<html>hb</html>")
    c = TestClient(app)
    resp = c.get("/q/nonexistent-path")
    assert resp.status_code == 404
    assert resp.text != "<html>hb</html>"


def test_q_transactions_camelcase_body_filters(tmp_path, fixture_db):
    """DEBT from Task 2's review: prove POST /q/{name} converts a camelCase
    body key (accountId) to the snake_case kwarg (account_id) the query
    function expects, and that the filter actually took effect."""
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    h = {"x-api-token": "legacy-tok"}

    unfiltered = c.post("/q/q_transactions", json={"limit": 25},
                         headers=h)
    assert unfiltered.status_code == 200
    all_rows = unfiltered.json()
    assert all_rows
    target_account_id = all_rows[0]["account_id"]

    resp = c.post("/q/q_transactions",
                   json={"accountId": target_account_id, "limit": 25},
                   headers=h)
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert all(r["account_id"] == target_account_id for r in rows)


def test_categorize_stamps_filed_by_per_token(tmp_path, fixture_db):
    """POST /categorize must stamp pending_txn.filed_by with the username
    the AUTHENTICATING TOKEN maps to (per-user attribution), not a
    hardcoded "steven" — proves the token->user wiring added for the
    mobile web UI actually reaches the write path."""
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)

    con = sqlite3.connect(fixture_db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id FROM pending_txn WHERE status IN ('pending', 'skipped') "
        "LIMIT 1"
    ).fetchone()
    if row is None:
        # Fixture had nothing pending/skipped (unlikely but possible on a
        # freshly-cleared inbox) — insert a minimal synthetic row into the
        # FIXTURE db (never the live one) so the test still exercises the
        # real write path.
        con.execute(
            "INSERT INTO pending_txn "
            "  (user_id, ynab_txn_id, payee, amount_cents, txn_date, status) "
            "VALUES ('steven', 'synthetic-filed-by-test', 'Test Payee', "
            "        -1234, '2026-07-01', 'pending')"
        )
        con.commit()
        row = con.execute(
            "SELECT id FROM pending_txn WHERE ynab_txn_id = "
            "'synthetic-filed-by-test'"
        ).fetchone()
    pt_id = row["id"]

    cat = con.execute(
        "SELECT id FROM category WHERE hidden = 0 LIMIT 1"
    ).fetchone()
    cat_id = cat["id"]
    con.close()

    resp = c.post(
        "/categorize",
        json={"pt_id": pt_id, "category_id": cat_id},
        headers={"x-api-token": "allison-tok"},
    )
    assert resp.status_code == 200

    con2 = sqlite3.connect(fixture_db)
    filed_by = con2.execute(
        "SELECT filed_by FROM pending_txn WHERE id = ?", (pt_id,)
    ).fetchone()[0]
    con2.close()
    assert filed_by == "allison"
