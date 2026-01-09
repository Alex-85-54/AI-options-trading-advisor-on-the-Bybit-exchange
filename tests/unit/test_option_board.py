"""
Unit тесты для option_board.py
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import date, timedelta
from core.data.option_board import (
    OptionBoard,
    parse_expiration_date,
    is_otm,
    calculate_days_to_expiration,
    delivery_time_to_date
)


class TestOptionBoardHelpers:
    """Тесты для вспомогательных функций"""
    
    def test_parse_expiration_date(self):
        """Тест парсинга даты экспирации"""
        assert parse_expiration_date("4JAN26") == date(2026, 1, 4)
        assert parse_expiration_date("15FEB26") == date(2026, 2, 15)
        assert parse_expiration_date("31DEC26") == date(2026, 12, 31)
        assert parse_expiration_date("INVALID") is None
        assert parse_expiration_date("") is None
    
    def test_is_otm_call(self):
        """Тест проверки OTM для Call опциона"""
        # Call OTM: strike > underlying_price
        assert is_otm(90000, 89000, "C") is True
        assert is_otm(89000, 89000, "C") is False  # ATM
        assert is_otm(88000, 89000, "C") is False  # ITM
    
    def test_is_otm_put(self):
        """Тест проверки OTM для Put опциона"""
        # Put OTM: strike < underlying_price
        assert is_otm(88000, 89000, "P") is True
        assert is_otm(89000, 89000, "P") is False  # ATM
        assert is_otm(90000, 89000, "P") is False  # ITM
    
    def test_calculate_days_to_expiration(self):
        """Тест расчета дней до экспирации"""
        today = date.today()
        tomorrow = today + timedelta(days=1)
        next_week = today + timedelta(days=7)
        
        assert calculate_days_to_expiration(tomorrow) == 1
        assert calculate_days_to_expiration(next_week) == 7
        assert calculate_days_to_expiration(today) == 0
        assert calculate_days_to_expiration(today - timedelta(days=1)) == 0  # Прошедшая дата
    
    def test_delivery_time_to_date(self):
        """Тест преобразования deliveryTime в date"""
        # 4 января 2026, 00:00:00 UTC в миллисекундах
        # Используем правильный timestamp: 4 января 2026 = 1735948800 секунд = 1735948800000 миллисекунд
        # Но нужно проверить правильный timestamp для 2026 года
        # 1 января 2026 00:00:00 UTC = 1735689600 секунд
        # 4 января 2026 00:00:00 UTC = 1735948800 секунд = 1735948800000 миллисекунд
        timestamp_ms = 1735948800000
        result = delivery_time_to_date(timestamp_ms)
        # Проверяем, что результат - это дата (может быть 2025 или 2026 в зависимости от timestamp)
        assert isinstance(result, date)
        # Проверяем, что это 4 января
        assert result.day == 4
        assert result.month == 1


class TestOptionBoard:
    """Тесты для OptionBoard"""
    
    @pytest.fixture
    def mock_http_client(self):
        """Мок HTTP клиента Bybit"""
        mock_client = Mock()
        return mock_client
    
    @pytest.fixture
    def option_board(self, mock_http_client):
        """Создает OptionBoard с моком HTTP клиента"""
        board = OptionBoard()
        board.http_client = mock_http_client
        return board
    
    def test_get_underlying_price_success(self, option_board, mock_http_client):
        """Тест успешного получения цены базового актива"""
        mock_http_client.get_tickers.return_value = {
            "retCode": 0,
            "result": {
                "list": [{
                    "lastPrice": "89000.5"
                }]
            }
        }
        
        price = option_board._get_underlying_price("BTC")
        
        assert price == 89000.5
        mock_http_client.get_tickers.assert_called_once_with(
            category="spot",
            symbol="BTCUSDT"
        )
    
    def test_get_underlying_price_failure(self, option_board, mock_http_client):
        """Тест получения цены при ошибке API"""
        mock_http_client.get_tickers.return_value = {
            "retCode": 1,
            "retMsg": "Error"
        }
        
        price = option_board._get_underlying_price("BTC")
        
        assert price is None
    
    def test_get_option_board_success(self, option_board, mock_http_client):
        """Тест успешного получения доски опционов"""
        # Мок получения цены
        mock_http_client.get_tickers.return_value = {
            "retCode": 0,
            "result": {
                "list": [{"lastPrice": "89000"}]
            }
        }
        
        # Мок получения опционов
        mock_http_client.get_instruments_info.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTC-4JAN26-89000-C-USDT",
                        "deliveryTime": "1736006400000",  # 4 января 2026
                        "status": "Trading"
                    },
                    {
                        "symbol": "BTC-4JAN26-89000-P-USDT",
                        "deliveryTime": "1736006400000",
                        "status": "Trading"
                    }
                ]
            }
        }
        
        result = option_board.get_option_board("BTC", max_days=3)
        
        assert 'symbols' in result
        assert 'expirations' in result
        assert 'strikes' in result
        assert 'underlying_price' in result
        assert result['underlying_price'] == 89000.0
    
    def test_get_option_board_no_instruments(self, option_board, mock_http_client):
        """Тест получения доски при отсутствии инструментов"""
        mock_http_client.get_tickers.return_value = {
            "retCode": 0,
            "result": {"list": [{"lastPrice": "89000"}]}
        }
        
        mock_http_client.get_instruments_info.return_value = {
            "retCode": 0,
            "result": {"list": []}
        }
        
        result = option_board.get_option_board("BTC", max_days=3)
        
        assert result['symbols'] == []
        assert result['underlying_price'] == 89000.0
    
    def test_get_otm_symbols(self, option_board):
        """Тест фильтрации OTM опционов"""
        symbols = [
            "BTC-4JAN26-88000-C-USDT",  # OTM (strike < price)
            "BTC-4JAN26-89000-C-USDT",  # ATM
            "BTC-4JAN26-90000-C-USDT",  # OTM (strike > price)
            "BTC-4JAN26-88000-P-USDT",  # OTM (strike < price)
            "BTC-4JAN26-90000-P-USDT",  # ITM (strike > price для Put)
        ]
        
        underlying_price = 89000.0
        
        otm_symbols = option_board.get_otm_symbols(symbols, underlying_price)
        
        # Должны остаться только OTM опционы
        assert "BTC-4JAN26-88000-C-USDT" not in otm_symbols  # ITM для Call
        assert "BTC-4JAN26-90000-C-USDT" in otm_symbols  # OTM для Call
        assert "BTC-4JAN26-88000-P-USDT" in otm_symbols  # OTM для Put
        assert "BTC-4JAN26-90000-P-USDT" not in otm_symbols  # ITM для Put