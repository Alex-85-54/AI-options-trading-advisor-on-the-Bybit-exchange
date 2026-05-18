"""Фильтр экспираций агента по AGENT_CONFIG.max_expiration_days."""
from datetime import date
from unittest.mock import Mock, patch

import pytest

from core.agent.decision_engine import DecisionEngine


@pytest.fixture
def engine():
    with patch("core.agent.decision_engine.get_database") as mock_db, \
         patch("core.agent.decision_engine.get_historical_analyzer"), \
         patch("core.agent.decision_engine.get_iv_filter"), \
         patch("core.agent.decision_engine.get_greeks_analyzer"), \
         patch("core.agent.decision_engine.get_anomaly_detector"), \
         patch("core.agent.decision_engine.get_trading_agent"):
        mock_db.return_value.get_support_resistance_levels.return_value = {
            "support": [],
            "resistance": [],
        }
        eng = DecisionEngine(data_store=Mock())
        eng.max_expiration_days = 3
        return eng


@patch("core.agent.decision_engine.date")
def test_collect_data_filters_by_agent_max_dte(mock_date_module, engine):
    mock_date_module.today.return_value = date(2026, 1, 1)

    store = Mock()
    store.get_by_underlying.return_value = {
        "BTC-4JAN26-90000-C-USDT": {"underlying_price": 90000.0},   # DTE 3
        "BTC-10JAN26-90000-C-USDT": {"underlying_price": 90000.0},  # DTE 9
        "BTC-2JAN26-90000-C-USDT": {"underlying_price": 90000.0},   # DTE 1
    }
    engine.data_store = store

    collected = engine.collect_data("BTC")

    symbols = set(collected["options_data"].keys())
    assert symbols == {
        "BTC-4JAN26-90000-C-USDT",
        "BTC-2JAN26-90000-C-USDT",
    }
    assert collected["options_count"] == 2


@patch("core.agent.decision_engine.date")
def test_group_options_respects_max_dte(mock_date_module, engine):
    mock_date_module.today.return_value = date(2026, 1, 1)

    options = {
        "BTC-4JAN26-90000-C-USDT": {},
        "BTC-4JAN26-90000-P-USDT": {},
        "BTC-20JAN26-90000-C-USDT": {},
    }
    grouped = engine._group_options_by_expiration(options)

    assert len(grouped) == 1
    assert grouped[0]["expiration"] == "4JAN26"
    assert grouped[0]["days_to_expiration"] == 3
    assert len(grouped[0]["options_data"]) == 2
