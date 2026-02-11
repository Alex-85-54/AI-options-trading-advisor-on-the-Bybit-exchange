from typing import Dict, Optional
import os
from datetime import datetime, timedelta, timezone

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
    "max_expiration_days": 365,              # Максимум дней до экспирации для подписки
    "strike_step_3days": 500,              # Шаг страйка для опционов до 3 дней
    "strike_steps_count": 7,               # ±7 шагов от текущей цены
    "daily_update_time_utc": "15:05",      # Время обновления подписок
    "skip_today_expiration": False,        # Собираем данные до экспирации (days_to_expiration = 0)
    "new_options_time_utc": "15:05",       # Время добавления новых опционов на бирже
    "save_only_otm": True,                 # Сохранять только OTM опционы
    "refresh_job_timeout_sec": 120,         # Таймаут задачи переподписки (сек); после него задача считается завершённой
    "http_request_timeout_sec": 30,       # Таймаут HTTP-запросов к бирже (доска опционов)
}

# Конфигурация анализа исторических данных
ANALYSIS_CONFIG = {
    "iv_analysis_days": 7,                # Количество дней истории для анализа IV (процентили, IVR)
    "greeks_analysis_days": 7,             # Количество дней истории для анализа тренда греков
}

# Конфигурация стратегий анализа
STRATEGY_CONFIG = {
    # IV Filter
    "ivr_threshold": 40.0,                 # Порог IVR для фильтрации (опционы с IVR < threshold считаются подходящими)
    
    # Greeks Analyzer
    "gamma_concentration_threshold": 0.1,  # Порог концентрации гаммы (доля гаммы в узком диапазоне страйков)
    "vega_concentration_threshold": 0.1,  # Порог концентрации веги
    "skew_threshold": 0.1,                 # Порог скью для обнаружения асимметрии (абсолютное значение)
    
    # Anomaly Detector
    "volume_spike_multiplier": 2.0,        # Множитель для обнаружения всплеска объема (средний объем * multiplier)
    "delta_imbalance_threshold": 0.1,      # Порог дисбаланса дельты (разница между Call и Put дельтами)
}

# Конфигурация динамических порогов (рассчитываются по истории в БД)
DYNAMIC_THRESHOLD_CONFIG = {
    "enabled": True,
    "lookback_days": 7,                  # Окно истории для расчета
    "recalc_interval_hours": 24,          # Переcчет не чаще чем раз в N часов
    "min_sample_size": 50,                # Минимум точек для расчета (иначе fallback на STRATEGY_CONFIG)
    "percentiles": {
        "ivr_threshold": 85,
        "delta_imbalance": 85,
        "skew": 85,
        "volume_spike": 95,
        "gamma_concentration": 85,
        "vega_concentration": 85
    }
}

# Бины DTE (days_to_expiration) для динамических порогов
DTE_BINS = [
    {"label": "0-1", "min": 0, "max": 1},
    {"label": "2-3", "min": 2, "max": 3},
    {"label": "4-7", "min": 4, "max": 7},
    {"label": "8-14", "min": 8, "max": 14},
    {"label": "15-30", "min": 15, "max": 30},
    {"label": "31-60", "min": 31, "max": 60},
    {"label": "61-120", "min": 61, "max": 120},
    {"label": "121-200", "min": 121, "max": 200},
    {"label": "201-365", "min": 201, "max": 365},
    {"label": "366+", "min": 366, "max": None}
]

# Конфигурация часового пояса для отображения времени
# Часовой пояс для отображения времени пользователю (по умолчанию UTC+7)
TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "7"))  # UTC+7

# Получаем объект часового пояса (используем фиксированный offset для простоты и надежности)
DISPLAY_TIMEZONE = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Конфигурация LLM агента
# Загружаем API ключ из переменной окружения
# Убираем кавычки и пробелы, если они есть
_deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip().strip('"').strip("'")

AGENT_CONFIG = {
    "run_interval_minutes": 60,            # Частота запуска агента (каждый час)
    "run_at_hour_start": True,             # Запускать в начале каждого часа (10:00, 11:00, ...)
    "max_expiration_days": 3,              # Максимальная экспирация для анализа 
    "min_confidence": 0.5,                 # Минимальная уверенность для сигнала
    "deepseek_api_key": _deepseek_api_key,
    "deepseek_model": "deepseek-chat",
    "deepseek_base_url": "https://api.deepseek.com",
    "enable_signal_history": True,         # Сохранение истории сигналов
    # Обработка ошибок и retry
    "api_retry_attempts": 2,               # Количество попыток повтора при ошибке API
    "api_retry_delay_seconds": 2,          # Начальная задержка между попытками (секунды)
    "api_timeout_seconds": 30,            # Таймаут для API запросов (секунды)
    "skip_on_api_error": True,            # Пропускать цикл при недоступности API (не падать)
}


def format_datetime_local(dt: Optional[datetime], format_str: str = '%Y-%m-%d %H:%M:%S') -> str:
    """
    Форматировать datetime в локальный часовой пояс пользователя
    
    Args:
        dt: Объект datetime (если None, возвращает 'никогда')
        format_str: Строка формата для strftime (по умолчанию '%Y-%m-%d %H:%M:%S')
        
    Returns:
        Отформатированная строка времени в локальном часовом поясе
    """
    if dt is None:
        return 'никогда'
    
    # Если datetime наивен (без timezone info), предполагаем что это UTC
    if dt.tzinfo is None:
        # Создаем UTC timezone
        from datetime import timezone as tz
        dt = dt.replace(tzinfo=tz.utc)
    
    # Конвертируем в локальный часовой пояс
    local_dt = dt.astimezone(DISPLAY_TIMEZONE)
    
    # Форматируем
    return local_dt.strftime(format_str)