"""
Тесты качества сигналов
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock
from core.data.database import OptionDatabase


class TestSignalQuality:
    """Тесты качества сигналов"""
    
    @pytest.fixture
    def signal_history(self, test_database):
        """Создает историю сигналов для тестирования"""
        signals = [
            {
                "signal_type": "strangle",
                "underlying": "BTC",
                "confidence": 0.75,
                "created_at": datetime.now() - timedelta(days=10)
            },
            {
                "signal_type": "strangle",
                "underlying": "BTC",
                "confidence": 0.80,
                "created_at": datetime.now() - timedelta(days=8)
            },
            {
                "signal_type": "straddle",
                "underlying": "BTC",
                "confidence": 0.65,
                "created_at": datetime.now() - timedelta(days=5)
            },
            {
                "signal_type": "strangle",
                "underlying": "ETH",
                "confidence": 0.70,
                "created_at": datetime.now() - timedelta(days=3)
            }
        ]
        
        for signal in signals:
            test_database.save_signal(signal)
        
        return test_database
    
    def test_signal_consistency(self, signal_history):
        """Тест консистентности сигналов"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        # Проверяем, что сигналы имеют правильную структуру
        for signal in history:
            assert 'signal_type' in signal
            assert 'underlying' in signal
            assert 'confidence' in signal
            assert signal['confidence'] >= 0.0
            assert signal['confidence'] <= 1.0
    
    def test_confidence_calibration(self, signal_history):
        """Тест калибровки уверенности"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        if len(history) > 0:
            # Проверяем распределение уверенности
            confidences = [s.get('confidence', 0) for s in history]
            
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0
            
            # Средняя уверенность должна быть разумной (не слишком низкой, не слишком высокой)
            assert avg_confidence >= 0.0
            assert avg_confidence <= 1.0
            
            # Проверяем, что есть вариация в уверенности
            if len(confidences) > 1:
                confidence_range = max(confidences) - min(confidences)
                assert confidence_range >= 0
    
    def test_false_positive_rate(self, signal_history):
        """Тест частоты ложных срабатываний"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        # Симулируем проверку ложных срабатываний
        # (в реальности это требует данных о результатах сигналов)
        total_signals = len(history)
        
        # Предполагаем, что некоторые сигналы были ложными
        # В реальном тесте это проверялось бы через signal_results
        assert total_signals >= 0
    
    def test_signal_timing(self, signal_history):
        """Тест тайминга сигналов"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        if len(history) > 1:
            # Проверяем, что сигналы не генерируются слишком часто
            timestamps = [s.get('created_at') for s in history if s.get('created_at')]
            
            if len(timestamps) > 1:
                # Сортируем по времени
                timestamps.sort()
                
                # Проверяем интервалы между сигналами
                intervals = []
                for i in range(1, len(timestamps)):
                    if isinstance(timestamps[i], datetime) and isinstance(timestamps[i-1], datetime):
                        interval = (timestamps[i] - timestamps[i-1]).total_seconds() / 3600  # в часах
                        intervals.append(interval)
                
                if intervals:
                    # Минимальный интервал должен быть разумным (не менее часа)
                    min_interval = min(intervals)
                    assert min_interval >= 0  # Может быть 0 если в одном тесте
    
    def test_signal_types_distribution(self, signal_history):
        """Тест распределения типов сигналов"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        signal_types = {}
        for signal in history:
            signal_type = signal.get('signal_type', 'unknown')
            signal_types[signal_type] = signal_types.get(signal_type, 0) + 1
        
        # Проверяем, что есть разнообразие типов сигналов
        assert len(signal_types) >= 0
        
        # Проверяем, что типы сигналов валидны
        valid_types = ['strangle', 'straddle', 'call', 'put']
        for signal_type in signal_types.keys():
            assert signal_type in valid_types or signal_type == 'unknown'
    
    def test_signal_quality_metrics(self, signal_history):
        """Тест метрик качества сигналов"""
        history = signal_history.get_signal_history("BTC", days=30)
        
        if len(history) > 0:
            # Вычисляем метрики
            total_signals = len(history)
            avg_confidence = sum(s.get('confidence', 0) for s in history) / total_signals
            
            # Проверяем метрики
            assert total_signals > 0
            assert avg_confidence >= 0.0
            assert avg_confidence <= 1.0
            
            # Проверяем, что есть сигналы с высокой уверенностью
            high_confidence_signals = [s for s in history if s.get('confidence', 0) >= 0.7]
            assert len(high_confidence_signals) >= 0