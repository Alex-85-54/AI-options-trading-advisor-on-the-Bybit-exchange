"""
Unit тесты для anomaly_detector.py
"""
import pytest
from core.strategy.anomaly_detector import AnomalyDetector


class TestAnomalyDetector:
    """Тесты для AnomalyDetector"""
    
    def test_parse_option_type(self):
        """Тест парсинга типа опциона"""
        detector = AnomalyDetector()
        
        assert detector.parse_option_type("BTC-4JAN26-89000-C-USDT") == "C"
        assert detector.parse_option_type("BTC-4JAN26-89000-P-USDT") == "P"
        assert detector.parse_option_type("INVALID") is None
    
    def test_detect_volume_spikes(self):
        """Тест обнаружения всплесков объема"""
        detector = AnomalyDetector()
        
        # Нормальные объемы
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"volume_24h": 1000.0},
            "BTC-4JAN26-89000-P-USDT": {"volume_24h": 1200.0},
            "BTC-4JAN26-90000-C-USDT": {"volume_24h": 1100.0},
            # Аномальный объем (в 3 раза больше среднего)
            "BTC-4JAN26-91000-C-USDT": {"volume_24h": 5000.0},
        }
        
        result = detector.detect_volume_spikes(options_data)
        
        assert result['avg_volume'] > 0
        assert result['max_volume'] == 5000.0
        assert len(result['spikes']) > 0
        assert "BTC-4JAN26-91000-C-USDT" in result['spikes']
    
    def test_detect_volume_spikes_no_spikes(self):
        """Тест обнаружения всплесков при их отсутствии"""
        detector = AnomalyDetector()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"volume_24h": 1000.0},
            "BTC-4JAN26-89000-P-USDT": {"volume_24h": 1200.0},
            "BTC-4JAN26-90000-C-USDT": {"volume_24h": 1100.0},
        }
        
        result = detector.detect_volume_spikes(options_data)
        
        assert result['avg_volume'] > 0
        assert len(result['spikes']) == 0
        assert result['spike_count'] == 0
    
    def test_detect_volume_spikes_empty(self):
        """Тест обнаружения всплесков при отсутствии данных"""
        detector = AnomalyDetector()
        
        result = detector.detect_volume_spikes({})
        
        assert result['avg_volume'] == 0
        assert result['max_volume'] == 0
        assert len(result['spikes']) == 0
        assert result['spike_count'] == 0
    
    def test_detect_delta_imbalance_balanced(self):
        """Тест обнаружения дисбаланса дельты при балансе"""
        detector = AnomalyDetector()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.5},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.5},
            "BTC-4JAN26-90000-C-USDT": {"delta": 0.4},
            "BTC-4JAN26-90000-P-USDT": {"delta": 0.4},
        }
        
        result = detector.detect_delta_imbalance(options_data)
        
        assert abs(result['imbalance']) < detector.delta_imbalance_threshold
        assert result['direction'] == 'balanced'
        assert result['is_imbalanced'] is False
    
    def test_detect_delta_imbalance_call_heavy(self):
        """Тест обнаружения дисбаланса при перевесе Call"""
        detector = AnomalyDetector()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.7},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.2},
            "BTC-4JAN26-90000-C-USDT": {"delta": 0.6},
            "BTC-4JAN26-90000-P-USDT": {"delta": 0.1},
        }
        
        result = detector.detect_delta_imbalance(options_data)
        
        assert result['imbalance'] > 0  # Перевес Call
        assert result['direction'] == 'call'
        assert result['is_imbalanced'] is True
    
    def test_detect_delta_imbalance_put_heavy(self):
        """Тест обнаружения дисбаланса при перевесе Put"""
        detector = AnomalyDetector()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.2},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.7},
            "BTC-4JAN26-90000-C-USDT": {"delta": 0.1},
            "BTC-4JAN26-90000-P-USDT": {"delta": 0.6},
        }
        
        result = detector.detect_delta_imbalance(options_data)
        
        assert result['imbalance'] < 0  # Перевес Put
        assert result['direction'] == 'put'
        assert result['is_imbalanced'] is True
    
    def test_detect_delta_imbalance_empty(self):
        """Тест обнаружения дисбаланса при отсутствии данных"""
        detector = AnomalyDetector()
        
        result = detector.detect_delta_imbalance({})
        
        assert result['imbalance'] == 0.0
        assert result['call_total_delta'] == 0
        assert result['put_total_delta'] == 0
        assert result['is_imbalanced'] is False
        assert result['direction'] == 'balanced'
    
    def test_detect_all_anomalies(self):
        """Тест обнаружения всех типов аномалий"""
        detector = AnomalyDetector()
        
        # Средний объем ~1500, всплеск должен быть > 3000 (2x multiplier)
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {
                "volume_24h": 1000.0,
                "delta": 0.5
            },
            "BTC-4JAN26-89000-P-USDT": {
                "volume_24h": 1000.0,
                "delta": 0.2  # Дисбаланс (Call 0.5, Put 0.2)
            },
            "BTC-4JAN26-90000-C-USDT": {
                "volume_24h": 5000.0,  # Всплеск (средний ~1667, порог ~3334)
                "delta": 0.3
            },
        }
        
        result = detector.detect_all_anomalies(options_data)
        
        assert 'volume_spikes' in result
        assert 'delta_imbalance' in result
        assert result['volume_spikes']['spike_count'] > 0
        assert result['delta_imbalance']['is_imbalanced'] is True