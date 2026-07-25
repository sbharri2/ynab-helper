import pytest
from fastapi.testclient import TestClient

from bot.http_api import build_app
from bot.storage import init_db
from bot import investments_store as store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("YNABHELPER_API_TOKEN", raising=False)
    db = tmp_path / "t.db"
    init_db(db)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "ui_api_token.txt").write_text("testtoken", encoding="utf-8")
    app = build_app(db_path=db, token_dir=token_dir, webui_dir=tmp_path / "webui")
    c = TestClient(app)
    c.headers.update({"X-API-Token": "testtoken"})
    c.db = db
    return c


def test_snapshot_empty_db_returns_empty_shape(client):
    r = client.get("/investments/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["holdings"] == []
    assert body["as_of"] is None


def test_round_and_values_roundtrip(client):
    hid = store.upsert_holding(client.db, name="Marcus", kind="cash")
    r = client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25",
    })
    assert r.status_code == 200
    rid = r.json()["round_id"]

    r = client.post("/investments/values", json={
        "round_id": rid,
        "values": [{"holding_id": hid, "value_cents": 2566800}],
    })
    assert r.status_code == 200
    assert r.json()["written"] == 1

    snap = client.get("/investments/snapshot").json()
    assert snap["as_of"] == "2026-07-25"
    assert snap["holdings"][0]["values"][0]["cents"] == 2566800


def test_duplicate_as_of_date_returns_500_not_400(client):
    """A UNIQUE constraint violation is a server-side defect (or at least
    not the same class of problem as a bad request body) — it must not be
    silently reclassified as a 400 by a future change."""
    client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    r = client.post("/investments/round", json={
        "label": "Jul 2026 Again", "as_of_date": "2026-07-25"})
    assert r.status_code == 500


def test_values_rejects_unknown_round(client):
    r = client.post("/investments/values", json={
        "round_id": "nope", "values": [{"holding_id": "x", "value_cents": 1}],
    })
    assert r.status_code == 400


def test_snapshot_round_id_truncates_to_that_round(client):
    hid = store.upsert_holding(client.db, name="Marcus", kind="cash")
    r1 = client.post("/investments/round", json={
        "label": "Feb 2026", "as_of_date": "2026-02-15"})
    rid1 = r1.json()["round_id"]
    client.post("/investments/values", json={
        "round_id": rid1,
        "values": [{"holding_id": hid, "value_cents": 1000000}],
    })

    r2 = client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    rid2 = r2.json()["round_id"]
    client.post("/investments/values", json={
        "round_id": rid2,
        "values": [{"holding_id": hid, "value_cents": 2566800}],
    })

    snap = client.get(f"/investments/snapshot?round_id={rid1}").json()
    assert snap["as_of"] == "2026-02-15"
    assert len(snap["holdings"][0]["values"]) == 1
    assert snap["holdings"][0]["values"][0]["cents"] == 1000000


def test_holding_create_then_update(client):
    r = client.post("/investments/holding", json={
        "name": "Roth IRA", "kind": "retirement", "owner": "steven",
    })
    hid = r.json()["holding_id"]
    r = client.post("/investments/holding", json={"id": hid, "closed": True})
    assert r.json()["holding_id"] == hid
    rows = client.get("/investments/holdings").json()["holdings"]
    assert rows[0]["closed"] == 1


def test_holdings_include_closed_false_hides_closed(client):
    open_id = store.upsert_holding(client.db, name="Marcus", kind="cash")
    closed_id = store.upsert_holding(client.db, name="Old 401k", kind="retirement")
    client.post("/investments/holding", json={"id": closed_id, "closed": True})

    rows = client.get("/investments/holdings?include_closed=false").json()["holdings"]
    ids = {r["id"] for r in rows}
    assert open_id in ids
    assert closed_id not in ids


def test_policy_and_premium_routes(client):
    r = client.post("/investments/policy", json={
        "insurance_type": "Homeowners", "provider": "Amica",
        "premium_cents": 210500, "paid_via": "escrow",
    })
    pid = r.json()["policy_id"]
    r = client.post("/investments/policy/premium", json={
        "policy_id": pid, "as_of_date": "2026-07-01",
        "amount_cents": 279800, "source": "escrow",
    })
    assert r.status_code == 200
    rows = client.get("/investments/insurance").json()["policies"]
    assert rows[0]["drift_cents"] == 69300


def test_rounds_route_lists_newest_first(client):
    client.post("/investments/round", json={
        "label": "Feb 2026", "as_of_date": "2026-02-15"})
    client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    rounds = client.get("/investments/rounds").json()["rounds"]
    assert [r["label"] for r in rounds] == ["Jul 2026", "Feb 2026"]


def test_routes_require_token(client):
    r = client.get("/investments/snapshot", headers={"X-API-Token": "wrong"})
    assert r.status_code == 401


def test_mutations_write_audit_rows(client):
    client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    import sqlite3
    con = sqlite3.connect(client.db)
    events = {r[0] for r in con.execute("SELECT event FROM audit_log")}
    assert "ui_investments_round" in events
