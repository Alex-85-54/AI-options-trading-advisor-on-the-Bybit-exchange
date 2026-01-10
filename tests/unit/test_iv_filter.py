"""
Unit тесты для iv_filter.py
"""
import pytest
from unittest.mock import Mock, patch
from core.strategy.iv_filter import IVFilter


class TestIVFilter:
    """Тесты для IVFilter"""
    
    def test_check_ivr_below_threshold(self):
        """Тест проверки IVR ниже порога (должен пройти фильтр)"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            mock_analyzer.calculate_ivr.return_value = 15.0  # IVR < 25
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            result = filter_obj.check_ivr("BTC-4JAN26-89000-C-USDT")
            
            assert result is True
            mock_analyzer.calculate_ivr.assert_called_once()
    
    def test_check_ivr_above_threshold(self):
        """Тест проверки IVR выше порога (не должен пройти фильтр)"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            mock_analyzer.calculate_ivr.return_value = 50.0  # IVR > 25
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            result = filter_obj.check_ivr("BTC-4JAN26-89000-C-USDT")
            
            assert result is False
    
    def test_check_ivr_no_data(self):
        """Тест проверки IVR при отсутствии данных"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            mock_analyzer.calculate_ivr.return_value = None
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            result = filter_obj.check_ivr("BTC-4JAN26-89000-C-USDT")
            
            assert result is None
    
    def test_check_ivr_exactly_at_threshold(self):
        """Тест проверки IVR точно на пороге"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            mock_analyzer.calculate_ivr.return_value = 25.0  # IVR == threshold
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            result = filter_obj.check_ivr("BTC-4JAN26-89000-C-USDT")
            
            # IVR должен быть < threshold, поэтому 25.0 не проходит
            assert result is False
    
    def test_filter_options_list(self):
        """Тест фильтрации словаря опционов"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            # Первый опцион проходит (IVR < 25), второй нет
            mock_analyzer.calculate_ivr.side_effect = [15.0, 30.0]
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            options_data = {
                "BTC-4JAN26-89000-C-USDT": {"ask_price": 100.0},
                "BTC-4JAN26-89000-P-USDT": {"ask_price": 99.0}
            }
            
            result = filter_obj.filter_options(options_data)
            
            assert len(result) == 1
            assert "BTC-4JAN26-89000-C-USDT" in result
            assert "BTC-4JAN26-89000-P-USDT" not in result
    
    def test_custom_threshold(self):
        """Тест с кастомным порогом IVR"""
        with patch('core.strategy.iv_filter.get_historical_analyzer') as mock_get_analyzer:
            mock_analyzer = Mock()
            mock_analyzer.calculate_ivr.return_value = 20.0
            mock_get_analyzer.return_value = mock_analyzer
            
            filter_obj = IVFilter()
            filter_obj.ivr_threshold = 15.0  # Кастомный порог
            
            result = filter_obj.check_ivr("BTC-4JAN26-89000-C-USDT")
            
            # IVR=20 > threshold=15, не проходит
            assert result is False
