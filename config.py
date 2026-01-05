from typing import Dict, List

# Конфигурация
CONFIG = {
    "testnet": False,
    "retries": 3,
    "restart_on_error": True,
    "expiration_year": "26",  # 2026
    "option_types": ["C", "P"],  # Call, Put
    "server_port": 8000,
    "telegram_token": "6179178203:AAGV-zAG4Z3uKU1NTM8Jwcs-ILEXNI9xLOo",
}

# Хранилище активных опционов для отслеживания
ACTIVE_OPTIONS: Dict[str, Dict] = {}