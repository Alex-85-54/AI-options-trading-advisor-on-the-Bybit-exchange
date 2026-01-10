"""
Unit тесты для database.py
"""
import pytest
from datetime import datetime, date, timedelta
from core.data.database import OptionDatabase


class TestOptionDatabase:
    """Тесты для OptionDatabase"""
    
    def test_save_option_data(self, test_database, sample_option_symbol, sample_option_data):
        """Тест сохранения данных опциона"""
        timestamp = datetime.now()
        
        test_database.save_option_data(sample_option_symbol, sample_option_data, timestamp)
        
        # Проверяем, что данные сохранились
        history = test_database.get_historical_greeks(sample_option_symbol, days=1)
        assert len(history) > 0
        assert history[0]['delta'] == sample_option_data['delta']
    
    def test_parse_expiration_date(self, test_database):
        """Тест парсинга даты экспирации"""
        # Формат: "4JAN26" -> date(2026, 1, 4)
        expiry_str = "4JAN26"
        result = test_database.parse_expiration_date(expiry_str)
        
        assert result is not None
        assert result == date(2026, 1, 4)
    
    def test_parse_expiration_date_invalid(self, test_database):
        """Тест парсинга невалидной даты экспирации"""
        result = test_database.parse_expiration_date("INVALID")
        assert result is None
    
    def test_parse_option_symbol(self, test_database):
        """Тест парсинга символа опциона"""
        symbol = "BTC-4JAN26-89000-C-USDT"
        result = test_database.parse_option_symbol(symbol)
        
        assert result['underlying'] == "BTC"
        assert result['expiry'] == "4JAN26"
        assert result['strike'] == "89000"
        assert result['option_type'] == "C"
        assert result['expiration_date'] == date(2026, 1, 4)
    
    def test_get_historical_greeks(self, sample_historical_data, sample_option_symbol):
        """Тест получения истории греков"""
        history = sample_historical_data.get_historical_greeks(sample_option_symbol, days=30)
        
        assert len(history) > 0
        assert 'delta' in history[0]
        assert 'gamma' in history[0]
        assert 'vega' in history[0]
        assert 'theta' in history[0]
    
    def test_get_iv_statistics(self, sample_historical_data, sample_option_symbol):
        """Тест получения статистики IV"""
        stats = sample_historical_data.get_iv_statistics(sample_option_symbol, days=30)
        
        assert stats['count'] > 0
        assert stats['min'] is not None
        assert stats['max'] is not None
        assert stats['mean'] is not None
        assert stats['min'] <= stats['max']
    
    def test_days_to_expiration_calculation(self, test_database, sample_option_symbol, sample_option_data):
        """Тест расчета дней до экспирации"""
        # Сохраняем опцион с экспирацией через 3 дня
        expiration_date = date.today() + timedelta(days=3)
        expiry_str = expiration_date.strftime("%-d%b%y").upper()  # Формат "4JAN26"
        
        # Создаем символ с нужной экспирацией
        symbol = f"BTC-{expiry_str}-89000-C-USDT"
        
        timestamp = datetime.now()
        test_database.save_option_data(symbol, sample_option_data, timestamp)
        
        # Проверяем, что days_to_expiration вычислен правильно
        # (должен быть около 3, с учетом округления времени)
        options = test_database.get_options_by_days_to_expiration("BTC", 3, limit=1)
        assert len(options) > 0
        assert options[0]['days_to_expiration'] == 3
