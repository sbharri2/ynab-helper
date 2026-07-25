import json
from datetime import date

from bot.storage import init_db
from bot import investments_insurance as ins


def test_annualize_by_frequency():
    assert ins.annualize(10000, "monthly") == 120000
    assert ins.annualize(10000, "quarterly") == 40000
    assert ins.annualize(10000, "semiannual") == 20000
    assert ins.annualize(10000, "annual") == 10000
    assert ins.annualize(None, "annual") is None


def test_upsert_policy_creates_then_updates(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Homeowners", provider="Amica",
        premium_cents=210500, premium_frequency="annual", paid_via="escrow",
    )
    same = ins.upsert_policy(db, id=pid, premium_cents=279800)
    assert same == pid
    rows = ins.list_policies(db)
    assert len(rows) == 1
    assert rows[0]["premium_cents"] == 279800


def test_drift_uses_latest_observation(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Homeowners", provider="Amica",
        premium_cents=210500, premium_frequency="annual", paid_via="escrow",
    )
    ins.record_premium(
        db, policy_id=pid, as_of_date="2026-07-01",
        amount_cents=279800, source="escrow",
    )
    ins.record_premium(
        db, policy_id=pid, as_of_date="2025-07-01",
        amount_cents=210500, source="escrow",
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["observed_cents"] == 279800          # latest, not the older one
    assert row["observed_source"] == "escrow"
    assert row["drift_cents"] == 69300
    assert row["unverified"] is False


def test_policy_without_observation_is_unverified_not_zero_drift(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    ins.upsert_policy(
        db, insurance_type="Umbrella", provider="Amica", premium_cents=50000,
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["observed_cents"] is None
    assert row["drift_cents"] is None
    assert row["unverified"] is True


def test_observation_older_than_12_months_is_unverified(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(db, insurance_type="Flood", premium_cents=79802)
    ins.record_premium(
        db, policy_id=pid, as_of_date="2025-01-01", amount_cents=79802,
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["unverified"] is True
    assert row["drift_cents"] == 0


def test_snapshot_shape_annualizes_premium(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    ins.upsert_policy(
        db, insurance_type="Auto", provider="Amica", premium_cents=10000,
        premium_frequency="monthly", coverage="100/300", deductible="500",
        sales_contact="Amica agent", renewal_date="2027-01-01",
        comments="bundled", through_employer=0,
    )
    rows = ins.list_policies_for_snapshot(db)
    assert set(rows[0]) == {
        "insurance_type", "through_employer", "provider", "sales_contact",
        "coverage", "deductible", "annual_premium_cents", "comments",
        "renewal_date",
    }
    assert rows[0]["annual_premium_cents"] == 120000
    assert rows[0]["through_employer"] is False


def test_inactive_policies_excluded_from_snapshot(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(db, insurance_type="Old Term Life")
    ins.upsert_policy(db, id=pid, active=0)
    assert ins.list_policies_for_snapshot(db) == []
    assert len(ins.list_policies(db)) == 1      # registry still shows it


def test_list_policies_is_json_serializable(tmp_path):
    """Verify returned dicts have no datetime objects (created_at excluded)."""
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Auto", provider="Amica", premium_cents=100000,
    )
    ins.record_premium(
        db, policy_id=pid, as_of_date="2026-07-01", amount_cents=100000,
    )
    rows = ins.list_policies(db)
    # This should not raise: TypeError: Object of type datetime is not JSON serializable
    json.dumps(rows)
    # Verify created_at is not in the dict
    assert "created_at" not in rows[0]


def test_same_day_observations_use_later_inserted(tmp_path):
    """With two observations on the same date, the later-inserted one (higher id) wins."""
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Home", provider="Amica", premium_cents=100000,
    )
    # Record first observation
    ins.record_premium(
        db, policy_id=pid, as_of_date="2026-07-01", amount_cents=100000,
    )
    # Record second observation on the same date with different amount
    ins.record_premium(
        db, policy_id=pid, as_of_date="2026-07-01", amount_cents=125000,
    )
    row = ins.list_policies(db)[0]
    # Should use the second one (higher id, later inserted)
    assert row["observed_cents"] == 125000
