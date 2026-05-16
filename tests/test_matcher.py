from datetime import date, timedelta
from bot.matcher import match_score, find_best_match


def _order(total, days_ago=2):
    return {
        "id": 1,
        "total_cents": total,
        "order_date": date.today() - timedelta(days=days_ago),
        "external_id": "123-4567890-1234567",
    }


def _txn(amount, days_ago=0, payee="AMAZON.COM*ABC123", memo=""):
    return {
        "ynab_txn_id": "tx-1",
        "amount_cents": -amount,
        "txn_date": date.today() - timedelta(days=days_ago),
        "payee": payee,
        "memo": memo,
    }


def test_exact_amount_same_day_high_score():
    s = match_score(_order(4723, days_ago=0), _txn(4723, days_ago=0))
    assert s >= 0.85


def test_drift_amount_lower_score():
    # Order placed 4 days ago, txn cleared 2 days ago (realistic: txn AFTER order)
    s = match_score(_order(4723, days_ago=4), _txn(4500, days_ago=2))
    assert 0.5 <= s < 0.85


def test_too_old_zero_score():
    s = match_score(_order(4723, days_ago=20), _txn(4723, days_ago=0))
    assert s < 0.3


def test_memo_bonus_when_order_id_present():
    s_no = match_score(_order(4723), _txn(4723))
    s_with = match_score(_order(4723), _txn(4723, memo="AMAZON 123-4567890-1234567"))
    assert s_with > s_no


def test_find_best_match_returns_none_when_no_candidates_above_threshold():
    pending = [_order(4723, days_ago=2)]
    txn = _txn(99999, days_ago=2)
    result = find_best_match(pending, txn, threshold=0.85)
    assert result is None


def test_find_best_match_returns_unambiguous_winner():
    pending = [_order(4723, days_ago=2), _order(9999, days_ago=2)]
    txn = _txn(4723, days_ago=2)
    result = find_best_match(pending, txn, threshold=0.85)
    assert result is not None
    assert result["id"] == 1


def test_find_best_match_returns_none_when_ambiguous():
    pending = [_order(4723, days_ago=2), _order(4723, days_ago=3)]
    txn = _txn(4723, days_ago=2)
    result = find_best_match(pending, txn, threshold=0.85, ambiguity_gap=0.10)
    assert result is None


def test_amount_tolerance_uses_percent_for_large_orders():
    """A $2.23 drift on a $47.23 order eats most of the 10% tolerance (=$4.72),
    landing well below 0.85 — this catches the bug where a flat $10 cap was used."""
    s = match_score(_order(4723, days_ago=0), _txn(4500, days_ago=0))
    # diff=223 cents, tolerance=min(4723*0.10, 1000)=472 cents
    # amount_score = 1 - 223/472 ≈ 0.528 → weighted 0.264
    # date=1.0*0.30=0.30, payee=1.0*0.20=0.20, no memo bonus
    # total ≈ 0.764, definitely < 0.85
    assert 0.6 <= s < 0.85


def test_venmo_source_path():
    """The Venmo branch was uncovered — verify it scores correctly."""
    order = {"id": 7, "total_cents": 4200, "order_date": date.today(),
             "external_id": "venmo-txn-id"}
    txn = {"ynab_txn_id": "tx-v", "amount_cents": -4200,
           "txn_date": date.today(), "payee": "VENMO PAYMENT", "memo": ""}
    s = match_score(order, txn, source="venmo")
    assert s >= 0.85


def test_future_dated_txn_does_not_match():
    """Txn dated BEFORE the order date is impossible — should score 0."""
    s = match_score(_order(4723, days_ago=0), _txn(4723, days_ago=5))
    # _txn with days_ago=5 means txn was 5 days ago; order is TODAY.
    # So txn (5 days ago) is BEFORE order (today) → delta_days = -5 → score 0
    assert s == 0.0


def test_unknown_source_fails_closed():
    """Unknown source should score 0 on payee (fail closed), not 0.5 (foot-gun)."""
    order = {"id": 1, "total_cents": 100, "order_date": date.today(),
             "external_id": "x"}
    txn = {"ynab_txn_id": "x", "amount_cents": -100,
           "txn_date": date.today(), "payee": "RANDOM MERCHANT", "memo": ""}
    s = match_score(order, txn, source="unknown")
    # amount=1.0*0.5=0.5, date=1.0*0.3=0.3, payee=0*0.2=0, no memo
    # total = 0.8 < 0.85 threshold — won't auto-match
    assert s < 0.85
