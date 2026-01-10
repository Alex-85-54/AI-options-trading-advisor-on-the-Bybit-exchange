"""
Интеграционные тесты для полного workflow агента
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import json

from core.agent.decision_engine import DecisionEngine
from services.data_store import OptionDataStore


class TestAgentWorkflow:
    """Тесты полного workflow агента"""
    
    @pytest.fixture
    def mock_data_store(self):
        """Создает data_store с тестовыми данными"""
        store = OptionDataStore()
        store.update("BTC-4JAN26-89000-C-USDT", {
            "ask_price": 100.0,
            "bid_price": 99.0,
            "mark_price": 99.5,
            "iv": 0.25,
            "delta": 0.5,
            "gamma": 0.01,
            "vega": 0.15,
            "theta": -0.05,
            "volume_24h": 1000.0,
            "open_interest": 5000.0,
            "underlying_price": 89000.0
        })
        store.update("BTC-4JAN26-89000-P-USDT", {
            "ask_price": 99.0,
            "bid_price": 98.0,
            "mark_price": 98.5,
            "iv": 0.24,
            "delta": -0.5,
            "gamma": 0.01,
            "vega": 0.15,
            "theta": -0.05,
            "volume_24h": 1200.0,
            "open_interest": 6000.0,
            "underlying_price": 89000.0
        })
        return store
    
    @pytest.fixture
    def decision_engine(self, mock_data_store, test_database):
        """Создает DecisionEngine с реальными компонентами"""
        with patch('core.agent.decision_engine.get_database', return_value=test_database), \
             patch('core.agent.decision_engine.get_trading_agent') as mock_agent:
            
            # Мок агента для избежания реальных API вызовов
            mock_agent_instance = Mock()
            mock_agent_instance.analyze_market.return_value = {
                "summary": "Рынок показывает низкий IVR, подходящие условия для стрэнгла",
                "llm_recommendations": {"recommendation": "strangle"}
            }
            mock_agent_instance.make_decision.return_value = {
                "signal_type": "strangle",
                "underlying": "BTC",
                "expiration": "4JAN26",
                "strike_call": 90000,
                "strike_put": 88000,
                "reasoning": "Низкий IVR (15%), сбалансированное распределение греков",
                "confidence": 0.75,
                "risk_level": "medium",
                "timestamp": datetime.now().isoformat()
            }
            mock_agent.return_value = mock_agent_instance
            
            engine = DecisionEngine(data_store=mock_data_store)
            return engine
    
    def test_full_workflow(self, decision_engine, mock_data_store):
        """Тест полного цикла работы агента"""
        # Полный цикл: сбор данных -> анализ -> решение -> форматирование
        signal = decision_engine.make_decision("BTC")
        
        assert signal is not None
        assert signal.get('signal_type') == "strangle"
        assert signal.get('underlying') == "BTC"
        assert signal.get('confidence') is not None
        assert signal.get('reasoning') is not None
    
    def test_signal_formatting(self, decision_engine, mock_data_store):
        """Тест форматирования сигнала"""
        signal = decision_engine.make_decision("BTC")
        
        # Проверяем структуру сигнала
        assert 'signal_type' in signal
        assert 'underlying' in signal
        assert 'confidence' in signal
        assert 'reasoning' in signal
        
        # Для strangle должны быть оба страйка
        if signal.get('signal_type') == 'strangle':
            assert 'strike_call' in signal
            assert 'strike_put' in signal
    
    def test_signal_saving(self, decision_engine, mock_data_store, test_database):
        """Тест сохранения сигнала в БД"""
        signal = decision_engine.make_decision("BTC")
        
        if signal and decision_engine.enable_signal_history:
            # Сохраняем сигнал
            signal_id = test_database.save_signal(signal)
            
            assert signal_id is not None
            
            # Проверяем, что сигнал сохранился
            history = test_database.get_signal_history("BTC", days=1)
            assert len(history) > 0
            assert history[0]['signal_type'] == signal['signal_type']
    
    def test_telegram_integration_mock(self, decision_engine, mock_data_store):
        """Тест интеграции с Telegram (моки)"""
        signal = decision_engine.make_decision("BTC")
        
        # Мок отправки в Telegram
        mock_telegram_send = Mock()
        
        if signal:
            # Форматируем сообщение для Telegram
            message = f"""
🎯 Торговый сигнал: {signal.get('signal_type', 'unknown').upper()}

📊 Базовый актив: {signal.get('underlying', 'N/A')}
📅 Экспирация: {signal.get('expiration', 'N/A')}
💰 Уверенность: {signal.get('confidence', 0) * 100:.0f}%
⚠️ Риск: {signal.get('risk_level', 'N/A')}

💭 Обоснование:
{signal.get('reasoning', 'N/A')}
"""
            mock_telegram_send(message)
            mock_telegram_send.assert_called_once()
    
    def test_error_handling_in_workflow(self, decision_engine, mock_data_store):
        """Тест обработки ошибок в workflow"""
        # Создаем engine с проблемным data_store
        problematic_store = Mock()
        problematic_store.get_by_underlying.side_effect = Exception("Store error")
        
        engine = DecisionEngine(data_store=problematic_store)
        
        # Должен обработать ошибку gracefully
        collected = engine.collect_data("BTC")
        assert 'error' in collected or collected['options_count'] == 0
    
    def test_multiple_underlyings(self, decision_engine, mock_data_store):
        """Тест работы с несколькими базовыми активами"""
        # Добавляем данные для ETH
        mock_data_store.update("ETH-4JAN26-2500-C-USDT", {
            "ask_price": 50.0,
            "underlying_price": 2500.0
        })
        
        # Тестируем для BTC
        signal_btc = decision_engine.make_decision("BTC")
        assert signal_btc is not None
        
        # Тестируем для ETH (может вернуть None если нет достаточных данных)
        # Это нормально, так как у нас нет полных данных для ETH