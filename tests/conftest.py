"""
Общие фикстуры для тестов
"""
import pytest
import sqlite3
import tempfile
import os
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, List
import json

# Добавляем корень проекта в путь
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.data.database import OptionDatabase
from services.data_store import OptionDataStore
from config import CONFIG, SUBSCRIPTION_CONFIG, AGENT_CONFIG


@pytest.fixture
def temp_db_path():
    """Создает временную БД для тестов"""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def test_database(temp_db_path):
    """Создает экземпляр БД для тестов"""
    db = OptionDatabase(db_path=temp_db_path)
    yield db
    # Очистка после теста
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)


@pytest.fixture
def sample_option_data():
    """Пример данных опциона для тестов"""
    return {
        'ask_price': 100.5,
        'bid_price': 99.5,
        'mark_price': 100.0,
        'ask_iv': 0.25,
        'bid_iv': 0.24,
        'mark_iv': 0.245,
        'iv': 0.245,
        'delta': 0.5,
        'gamma': 0.01,
        'vega': 0.15,
        'theta': -0.05,
        'volume_24h': 1000.0,
        'open_interest': 5000.0,
        'underlying_price': 89000.0
    }


@pytest.fixture
def sample_option_symbol():
    """Пример символа опциона"""
    return "BTC-4JAN26-89000-C-USDT"


@pytest.fixture
def sample_historical_data(test_database, sample_option_symbol, sample_option_data):
    """Создает исторические данные в БД для тестов"""
    # Добавляем данные за последние 30 дней
    base_date = datetime.now() - timedelta(days=30)
    
    for i in range(30):
        timestamp = base_date + timedelta(days=i, hours=12)  # Полдень каждого дня
        # Вариация IV для тестирования
        iv_variation = 0.20 + (i % 10) * 0.01  # IV от 0.20 до 0.29
        option_data = sample_option_data.copy()
        option_data['mark_iv'] = iv_variation
        option_data['iv'] = iv_variation
        
        test_database.save_option_data(sample_option_symbol, option_data, timestamp)
    
    return test_database


@pytest.fixture
def empty_data_store():
    """Создает пустой data_store для тестов"""
    return OptionDataStore()


@pytest.fixture
def populated_data_store(empty_data_store, sample_option_symbol, sample_option_data):
    """Создает data_store с тестовыми данными"""
    empty_data_store.update(sample_option_symbol, sample_option_data)
    return empty_data_store


@pytest.fixture
def mock_bybit_response():
    """Мок ответа от Bybit API для получения опционов"""
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "option",
            "list": [
                {
                    "symbol": "BTC-4JAN26-89000-C-USDT",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "settleCoin": "USDT",
                    "optionsType": "Call",
                    "deliveryTime": "1736006400000",  # 4 января 2026
                    "underlyingPrice": "89000"
                },
                {
                    "symbol": "BTC-4JAN26-89000-P-USDT",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "settleCoin": "USDT",
                    "optionsType": "Put",
                    "deliveryTime": "1736006400000",
                    "underlyingPrice": "89000"
                }
            ]
        }
    }


@pytest.fixture
def mock_deepseek_response():
    """Мок ответа от DeepSeek API"""
    return {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "signal_type": "strangle",
                    "underlying": "BTC",
                    "expiration": "4JAN26",
                    "strike_call": 90000,
                    "strike_put": 88000,
                    "reasoning": "Низкий IVR (15%), сжатие у уровня поддержки",
                    "confidence": 0.75,
                    "risk_level": "medium"
                })
            }
        }]
    }


@pytest.fixture
def test_config():
    """Конфигурация для тестов"""
    return {
        "testnet": True,
        "max_expiration_days": 3,
        "strike_step_3days": 500,
        "strike_steps_count": 7,
        "ivr_threshold": 25.0,
        "min_confidence": 0.6
    }
