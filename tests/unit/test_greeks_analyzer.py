"""
Unit тесты для greeks_analyzer.py
"""
import pytest
from core.strategy.greeks_analyzer import GreeksAnalyzer


class TestGreeksAnalyzer:
    """Тесты для GreeksAnalyzer"""
    
    def test_parse_strike(self):
        """Тест парсинга страйка из символа"""
        analyzer = GreeksAnalyzer()
        
        assert analyzer.parse_strike("BTC-4JAN26-89000-C-USDT") == 89000.0
        assert analyzer.parse_strike("ETH-15FEB26-2500-P-USDT") == 2500.0
        assert analyzer.parse_strike("INVALID") is None
        assert analyzer.parse_strike("BTC-4JAN26-INVALID-C-USDT") is None
    
    def test_parse_option_type(self):
        """Тест парсинга типа опциона"""
        analyzer = GreeksAnalyzer()
        
        assert analyzer.parse_option_type("BTC-4JAN26-89000-C-USDT") == "C"
        assert analyzer.parse_option_type("BTC-4JAN26-89000-P-USDT") == "P"
        assert analyzer.parse_option_type("INVALID") is None
    
    def test_analyze_gamma_distribution(self):
        """Тест анализа распределения гаммы"""
        analyzer = GreeksAnalyzer()
        
        # Тестовые данные с концентрацией гаммы на страйке 90000
        options_data = {
            "BTC-4JAN26-88000-C-USDT": {"gamma": 0.01},
            "BTC-4JAN26-89000-C-USDT": {"gamma": 0.05},
            "BTC-4JAN26-90000-C-USDT": {"gamma": 0.10},  # Максимум
            "BTC-4JAN26-91000-C-USDT": {"gamma": 0.06},
            "BTC-4JAN26-92000-C-USDT": {"gamma": 0.02},
        }
        
        result = analyzer.analyze_gamma_distribution(options_data)
        
        assert result['total_gamma'] > 0
        assert result['max_gamma_strike'] == 90000.0
        assert 'concentration' in result
        assert 'is_concentrated' in result
    
    def test_analyze_gamma_distribution_empty(self):
        """Тест анализа гаммы при отсутствии данных"""
        analyzer = GreeksAnalyzer()
        
        result = analyzer.analyze_gamma_distribution({})
        
        assert result['total_gamma'] == 0
        assert result['max_gamma_strike'] is None
        assert result['is_concentrated'] is False
    
    def test_analyze_vega_distribution(self):
        """Тест анализа распределения веги"""
        analyzer = GreeksAnalyzer()
        
        options_data = {
            "BTC-4JAN26-88000-C-USDT": {"vega": 0.15},
            "BTC-4JAN26-89000-C-USDT": {"vega": 0.20},
            "BTC-4JAN26-90000-C-USDT": {"vega": 0.25},  # Максимум
            "BTC-4JAN26-91000-C-USDT": {"vega": 0.18},
        }
        
        result = analyzer.analyze_vega_distribution(options_data)
        
        assert result['total_vega'] > 0
        assert result['max_vega_strike'] == 90000.0
        assert 'concentration' in result
        assert 'is_concentrated' in result
    
    def test_analyze_vega_distribution_empty(self):
        """Тест анализа веги при отсутствии данных"""
        analyzer = GreeksAnalyzer()
        
        result = analyzer.analyze_vega_distribution({})
        
        assert result['total_vega'] == 0
        assert result['max_vega_strike'] is None
        assert result['is_concentrated'] is False
    
    def test_calculate_skew_balanced(self):
        """Тест расчета скью при балансе Call/Put"""
        analyzer = GreeksAnalyzer()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.5},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.5},
        }
        
        result = analyzer.calculate_skew(options_data)
        
        assert result['skew'] == 0.0  # Баланс
        assert result['call_count'] == 1
        assert result['put_count'] == 1
        assert result['is_skewed'] is False
    
    def test_calculate_skew_call_heavy(self):
        """Тест расчета скью при перевесе Call"""
        analyzer = GreeksAnalyzer()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.7},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.3},
        }
        
        result = analyzer.calculate_skew(options_data)
        
        assert result['skew'] > 0  # Перевес Call
        assert result['call_count'] == 1
        assert result['put_count'] == 1
    
    def test_calculate_skew_put_heavy(self):
        """Тест расчета скью при перевесе Put"""
        analyzer = GreeksAnalyzer()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {"delta": 0.3},
            "BTC-4JAN26-89000-P-USDT": {"delta": 0.7},
        }
        
        result = analyzer.calculate_skew(options_data)
        
        assert result['skew'] < 0  # Перевес Put
        assert result['call_count'] == 1
        assert result['put_count'] == 1
    
    def test_calculate_skew_empty(self):
        """Тест расчета скью при отсутствии данных"""
        analyzer = GreeksAnalyzer()
        
        result = analyzer.calculate_skew({})
        
        assert result['skew'] == 0.0
        assert result['call_count'] == 0
        assert result['put_count'] == 0
        assert result['is_skewed'] is False
    
    def test_analyze_all(self):
        """Тест комплексного анализа"""
        analyzer = GreeksAnalyzer()
        
        options_data = {
            "BTC-4JAN26-89000-C-USDT": {
                "gamma": 0.05,
                "vega": 0.20,
                "delta": 0.5
            },
            "BTC-4JAN26-89000-P-USDT": {
                "gamma": 0.05,
                "vega": 0.20,
                "delta": 0.5
            },
        }
        
        result = analyzer.analyze_all(options_data)
        
        assert 'gamma' in result
        assert 'vega' in result
        assert 'skew' in result
        assert result['gamma']['total_gamma'] > 0
        assert result['vega']['total_vega'] > 0
        assert 'skew' in result['skew']