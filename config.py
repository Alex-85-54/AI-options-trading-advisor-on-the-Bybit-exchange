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

# Конфигурация сохранения данных в БД
DATA_CONFIG = {
    "save_interval_minutes": 5,      # Интервал сохранения данных из WebSocket в БД
    "align_to_interval": True,       # Выравнивание времени сохранения по 5-минутным интервалам
    "save_on_startup": True,         # Сохранить данные при старте сервиса
    "batch_save": True,              # Батчинг запросов к БД (сохранять все символы за один запрос)
}

DATA_SAVE_INTERVAL_MINUTES = 5  # Интервал сохранения данных
DATA_SAVE_ALIGN_TO_INTERVAL = True  # Выравнивание по 5-минутным интервалам

# Конфигурация подписок на опционы
SUBSCRIPTION_CONFIG = {
    "max_expiration_days": 3,              # Максимум дней до экспирации для подписки
    "strike_step_3days": 500,              # Шаг страйка для опционов до 3 дней
    "strike_steps_count": 7,               # ±7 шагов от текущей цены
    "daily_update_time_utc": "08:05",      # Время обновления подписок (UTC)
    "skip_today_expiration": True,         # Пропускать опционы с экспирацией сегодня
    "new_options_time_utc": "08:00",       # Время добавления новых опционов на бирже (UTC)
    "save_only_otm": True,                 # Сохранять только OTM опционы
}