"""
Unit тесты для historical_analyzer.py
"""
import pytest
from datetime import datetime, timedelta
from core.data.historical_analyzer import HistoricalAnalyzer


class TestHistoricalAnalyzer:
    """Тесты для HistoricalAnalyzer"""
    
    def test_get_iv_percentiles_with_data(self, sample_historical_data, sample_option_symbol):
        """Тест получения процентилей IV при наличии данных"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = sample_historical_data
        
        result = analyzer.get_iv_percentiles(sample_option_symbol, days=30)
        
        assert result['count'] > 0
        assert result['min'] is not None
        assert result['max'] is not None
        assert result['mean'] is not None
        assert result['p50'] is not None
        assert result['current'] is not None
        assert result['min'] <= result['max']
        assert result['min'] <= result['current'] <= result['max']
    
    def test_get_iv_percentiles_no_data(self, test_database):
        """Тест получения процентилей IV при отсутствии данных"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = test_database
        
        result = analyzer.get_iv_percentiles("NONEXISTENT-SYMBOL", days=30)
        
        assert result['count'] == 0
        assert result['min'] is None
        assert result['max'] is None
        assert result['current'] is None
    
    def test_get_greeks_trend(self, sample_historical_data, sample_option_symbol):
        """Тест получения тренда греков"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = sample_historical_data
        
        result = analyzer.get_greeks_trend(sample_option_symbol, days=30)
        
        assert 'delta' in result
        assert 'gamma' in result
        assert 'vega' in result
        assert 'theta' in result
        assert 'iv' in result
        
        # Проверяем структуру тренда
        for greek in ['delta', 'gamma', 'vega', 'theta', 'iv']:
            assert 'trend' in result[greek]
            assert result[greek]['trend'] in ['up', 'down', 'stable', 'unknown']
    
    def test_get_greeks_trend_insufficient_data(self, test_database):
        """Тест получения тренда греков при недостаточных данных"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = test_database
        
        result = analyzer.get_greeks_trend("NONEXISTENT-SYMBOL", days=30)
        
        for greek in ['delta', 'gamma', 'vega', 'theta', 'iv']:
            assert result[greek]['trend'] == 'unknown'
            assert result[greek]['change'] is None
    
    def test_calculate_ivr_with_data(self, sample_historical_data, sample_option_symbol):
        """Тест расчета IVR при наличии данных"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = sample_historical_data
        
        ivr = analyzer.calculate_ivr(sample_option_symbol, days=30)
        
        assert ivr is not None
        assert 0 <= ivr <= 100  # IVR должен быть в диапазоне 0-100
    
    def test_calculate_ivr_no_data(self, test_database):
        """Тест расчета IVR при отсутствии данных"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = test_database
        
        ivr = analyzer.calculate_ivr("NONEXISTENT-SYMBOL", days=30)
        
        assert ivr is None
    
    def test_calculate_ivr_zero_range(self, test_database, sample_option_symbol, sample_option_data):
        """Тест расчета IVR при нулевом диапазоне (min == max)"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = test_database
        
        # Сохраняем одинаковые значения IV
        timestamp = datetime.now()
        for i in range(10):
            option_data = sample_option_data.copy()
            option_data['iv'] = 0.25  # Всегда одно и то же значение
            test_database.save_option_data(sample_option_symbol, option_data, timestamp + timedelta(hours=i))
        
        ivr = analyzer.calculate_ivr(sample_option_symbol, days=30)
        
        # При нулевом диапазоне IVR должен быть None
        assert ivr is None
    
    def test_get_comprehensive_analysis(self, sample_historical_data, sample_option_symbol):
        """Тест комплексного анализа"""
        analyzer = HistoricalAnalyzer()
        analyzer.db = sample_historical_data
        
        result = analyzer.get_comprehensive_analysis(sample_option_symbol)
        
        assert result['symbol'] == sample_option_symbol
        assert 'iv_percentiles' in result
        assert 'ivr' in result
        assert 'greeks_trend' in result
        
        assert result['iv_percentiles']['count'] > 0
        assert result['ivr'] is not None or result['iv_percentiles']['count'] == 0
