from typing import Dict
import os

# Конфигурация
CONFIG = {
    "testnet": False,
    "retries": 3,
    "restart_on_error": True,
    "expiration_year": "26",  # 2026
    "option_types": ["C", "P"],  # Call, Put
    "server_port": 8000,
    # Токен Telegram берём из переменной окружения
    "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),
    # Ключи Bybit (опционально, если понадобятся)
    "bybit_api_key": os.getenv("BYBIT_API_KEY", ""),
    "bybit_api_secret": os.getenv("BYBIT_API_SECRET", ""),
}

# Хранилище активных опционов для отслеживания
ACTIVE_OPTIONS: Dict[str, Dict] = {}