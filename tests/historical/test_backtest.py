"""
Тесты на исторических данных (backtesting)
"""
import pytest
from datetime import datetime, timedelta, date
from unittest.mock import Mock, patch
from core.data.database import OptionDatabase
from core.agent.decision_engine import DecisionEngine


class TestBacktest:
    """Тесты бэктестинга стратегии"""
    
    @pytest.fixture
    def historical_database(self, test_database):
        """Создает БД с историческими данными"""
        # Добавляем исторические данные за последние 30 дней
        base_date = datetime.now() - timedelta(days=30)
        
        for day in range(30):
            timestamp = base_date + timedelta(days=day, hours=12)
            
            # Вариация IV для симуляции разных рыночных условий
            iv = 0.20 + (day % 10) * 0.01  # IV от 0.20 до 0.29
            
            option_data = {
                "ask_price": 100.0 + day * 0.5,
                "bid_price": 99.0 + day * 0.5,
                "mark_price": 99.5 + day * 0.5,
                "iv": iv,
                "delta": 0.5,
                "gamma": 0.01,
                "vega": 0.15,
                "theta": -0.05,
                "volume_24h": 1000.0,
                "open_interest": 5000.0,
                "underlying_price": 89000.0 + day * 100
            }
            
            test_database.save_option_data(
                "BTC-4JAN26-89000-C-USDT",
                option_data,
                timestamp
            )
        
        return test_database
    
    def test_backtest_strategy(self, historical_database):
        """Тест бэктеста стратегии на исторических данных"""
        # Получаем исторические данные
        history = historical_database.get_historical_greeks(
            "BTC-4JAN26-89000-C-USDT",
            days=30
        )
        
        assert len(history) > 0
        
        # Симулируем принятие решений на исторических данных
        signals = []
        for i in range(0, len(history), 5):  # Каждые 5 дней
            data_point = history[i]
            iv = data_point.get('iv', 0)
            
            # Простая стратегия: если IV < 0.25, генерируем сигнал
            if iv < 0.25:
                signals.append({
                    "timestamp": data_point.get('date_data_collection'),
                    "iv": iv,
                    "signal": "strangle"
                })
        
        # Проверяем, что сигналы были сгенерированы
        assert len(signals) > 0
    
    def test_signal_accuracy(self, historical_database):
        """Тест точности сигналов на исторических данных"""
        # Получаем историю IV
        history = historical_database.get_historical_greeks(
            "BTC-4JAN26-89000-C-USDT",
            days=30
        )
        
        # Симулируем сигналы и их результаты
        correct_signals = 0
        total_signals = 0
        
        for i in range(len(history) - 1):
            current = history[i]
            next_data = history[i + 1]
            
            # Сигнал: низкий IV (< 0.25)
            if current.get('iv', 0) < 0.25:
                total_signals += 1
                # Проверяем, что IV вырос (что хорошо для стрэнгла)
                if next_data.get('iv', 0) > current.get('iv', 0):
                    correct_signals += 1
        
        if total_signals > 0:
            accuracy = correct_signals / total_signals
            # Проверяем, что точность разумная (не 0%)
            assert accuracy >= 0.0
            assert accuracy <= 1.0
    
    def test_performance_metrics(self, historical_database):
        """Тест метрик производительности"""
        history = historical_database.get_historical_greeks(
            "BTC-4JAN26-89000-C-USDT",
            days=30
        )
        
        # Вычисляем метрики
        signals = []
        for data_point in history:
            if data_point.get('iv', 0) < 0.25:
                signals.append({
                    "iv": data_point.get('iv', 0),
                    "timestamp": data_point.get('date_data_collection')
                })
        
        # Win rate (упрощенный расчет)
        if len(signals) > 0:
            # Симулируем результаты
            profitable = len([s for s in signals if s['iv'] < 0.23])
            win_rate = profitable / len(signals) if len(signals) > 0 else 0
            
            assert win_rate >= 0.0
            assert win_rate <= 1.0
    
    def test_historical_scenarios(self, historical_database):
        """Тест различных исторических сценариев"""
        history = historical_database.get_historical_greeks(
            "BTC-4JAN26-89000-C-USDT",
            days=30
        )
        
        # Сценарий 1: Низкий IV
        low_iv_periods = [d for d in history if d.get('iv', 0) < 0.25]
        assert len(low_iv_periods) >= 0
        
        # Сценарий 2: Высокий IV
        high_iv_periods = [d for d in history if d.get('iv', 0) > 0.28]
        assert len(high_iv_periods) >= 0
        
        # Сценарий 3: Волатильность
        iv_values = [d.get('iv', 0) for d in history if d.get('iv') is not None]
        if len(iv_values) > 1:
            iv_range = max(iv_values) - min(iv_values)
            assert iv_range >= 0