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
    s = match_score(_order(4723, days_ago=2), _txn(4500, days_ago=4))
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
