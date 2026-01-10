"""
Интеграционные тесты для decision_engine.py
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import json

from core.agent.decision_engine import DecisionEngine
from services.data_store import OptionDataStore


class TestDecisionEngine:
    """Интеграционные тесты для DecisionEngine"""
    
    @pytest.fixture
    def mock_data_store(self):
        """Мок data_store"""
        store = Mock()
        store.get_by_underlying.return_value = {
            "BTC-4JAN26-89000-C-USDT": {
                "ask_price": 100.0,
                "bid_price": 99.0,
                "underlying_price": 89000.0,
                "delta": 0.5,
                "gamma": 0.01,
                "vega": 0.15,
                "theta": -0.05
            },
            "BTC-4JAN26-89000-P-USDT": {
                "ask_price": 99.0,
                "bid_price": 98.0,
                "underlying_price": 89000.0,
                "delta": -0.5,
                "gamma": 0.01,
                "vega": 0.15,
                "theta": -0.05
            }
        }
        return store
    
    @pytest.fixture
    def mock_database(self, test_database):
        """Мок базы данных"""
        db = test_database
        return db
    
    @pytest.fixture
    def decision_engine(self, mock_data_store, mock_database):
        """Создает DecisionEngine с моками"""
        with patch('core.agent.decision_engine.get_database', return_value=mock_database), \
             patch('core.agent.decision_engine.get_historical_analyzer') as mock_analyzer, \
             patch('core.agent.decision_engine.get_iv_filter') as mock_iv_filter, \
             patch('core.agent.decision_engine.get_greeks_analyzer') as mock_greeks, \
             patch('core.agent.decision_engine.get_anomaly_detector') as mock_anomaly, \
             patch('core.agent.decision_engine.get_trading_agent') as mock_agent:
            
            # Настройка моков
            mock_analyzer_instance = Mock()
            mock_analyzer_instance.calculate_ivr.return_value = 15.0
            mock_analyzer.return_value = mock_analyzer_instance
            
            mock_iv_filter_instance = Mock()
            mock_iv_filter_instance.get_ivr_info.return_value = {"ivr": 15.0, "passes": True}
            mock_iv_filter.return_value = mock_iv_filter_instance
            
            mock_greeks_instance = Mock()
            mock_greeks_instance.analyze_all.return_value = {
                "gamma": {"total_gamma": 0.1, "is_concentrated": False},
                "vega": {"total_vega": 0.2, "is_concentrated": False},
                "skew": {"skew": 0.0, "is_skewed": False}
            }
            mock_greeks.return_value = mock_greeks_instance
            
            mock_anomaly_instance = Mock()
            mock_anomaly_instance.detect_all_anomalies.return_value = {
                "volume_spikes": {"spike_count": 0},
                "delta_imbalance": {"is_imbalanced": False}
            }
            mock_anomaly.return_value = mock_anomaly_instance
            
            mock_agent_instance = Mock()
            mock_agent_instance.analyze_market.return_value = {"summary": "Анализ рынка"}
            mock_agent_instance.make_decision.return_value = {
                "signal_type": "strangle",
                "underlying": "BTC",
                "confidence": 0.75
            }
            mock_agent.return_value = mock_agent_instance
            
            engine = DecisionEngine(data_store=mock_data_store)
            return engine
    
    def test_data_collection(self, decision_engine, mock_data_store):
        """Тест сбора данных для анализа"""
        collected = decision_engine.collect_data("BTC")
        
        assert collected['underlying'] == "BTC"
        assert collected['underlying_price'] == 89000.0
        assert collected['options_count'] > 0
        assert 'options_data' in collected
        assert 'support_resistance' in collected
        mock_data_store.get_by_underlying.assert_called_once_with("BTC")
    
    def test_analysis_pipeline(self, decision_engine, mock_data_store):
        """Тест пайплайна анализа"""
        # Собираем данные
        collected = decision_engine.collect_data("BTC")
        
        # Выполняем анализ
        analysis = decision_engine.analyze_data(collected)
        
        assert 'ivr_analysis' in analysis
        assert 'greeks_analysis' in analysis
        assert 'anomalies' in analysis
        assert 'options_summary' in analysis
    
    def test_llm_integration_mock(self, decision_engine, mock_data_store):
        """Тест интеграции с LLM (моки)"""
        collected = decision_engine.collect_data("BTC")
        analysis = decision_engine.analyze_data(collected)
        
        # Объединяем данные для LLM
        market_data = {
            **collected,
            **analysis
        }
        
        # Вызываем LLM через агента
        result = decision_engine.agent.analyze_market(market_data)
        
        assert result is not None
        assert 'summary' in result
    
    def test_signal_generation(self, decision_engine, mock_data_store):
        """Тест генерации сигналов"""
        # Полный цикл: сбор -> анализ -> решение
        signal = decision_engine.make_decision("BTC")
        
        assert signal is not None
        assert signal.get('signal_type') == "strangle"
        assert signal.get('underlying') == "BTC"
    
    def test_error_handling_no_data(self, decision_engine):
        """Тест обработки ошибок при отсутствии данных"""
        # Создаем engine с пустым data_store
        empty_store = Mock()
        empty_store.get_by_underlying.return_value = {}
        
        engine = DecisionEngine(data_store=empty_store)
        
        collected = engine.collect_data("BTC")
        
        # Должен вернуть структуру с пустыми данными, но без ошибок
        assert collected['underlying'] == "BTC"
        assert collected['options_count'] == 0
    
    def test_full_workflow(self, decision_engine, mock_data_store):
        """Тест полного цикла работы DecisionEngine"""
        # 1. Сбор данных
        collected = decision_engine.collect_data("BTC")
        assert collected['options_count'] > 0
        
        # 2. Анализ данных
        analysis = decision_engine.analyze_data(collected)
        assert 'ivr_analysis' in analysis
        
        # 3. Принятие решения
        signal = decision_engine.make_decision("BTC")
        assert signal is not None
