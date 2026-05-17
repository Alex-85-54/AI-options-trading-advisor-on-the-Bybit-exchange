Архитектура торгового советника по опционам с LLM агентом
1. Общая концепция
Торговый советник с LLM агентом (DeepSeek), который:
Периодически анализирует рынок опционов
Собирает данные: доска опционов, история греков/IV
Принимает решения о входе в позиции (Стрэнгл, Стрэддл, направленные)
Отправляет сигналы в Telegram бот
2. Структура проекта
Bot_Option_cursor/├── core/                          # Основные модули│   ├── __init__.py│   ├── agent/                     # LLM агент│   │   ├── __init__.py│   │   ├── trading_agent.py       # Основной класс агента│   │   ├── prompt_templates.py   # Шаблоны промптов для LLM│   │   └── decision_engine.py    # Логика принятия решений│   ├── data/                      # Работа с данными│   │   ├── __init__.py│   │   ├── database.py           # SQLite для истории│   │   ├── data_collector.py     # Сбор данных с Bybit│   │   ├── option_board.py       # Доска опционов│   │   └── historical_analyzer.py # Анализ истории│   ├── strategy/                  # Торговые стратегии│   │   ├── __init__.py│   │   ├── iv_filter.py          # Фильтр по IVR│   │   ├── greeks_analyzer.py    # Анализ греков│   │   ├── anomaly_detector.py   # Поиск аномалий│   │   └── position_builder.py   # Построение позиций│ services/               # Сервисы (существующие)│   ├── telegram_bot.py           # Telegram бот (расширить)│   ├── api_server.py             # API сервер (расширить)│   ├── websocket_manager.py      # WebSocket менеджер│   └── data_store.py             # In-memory хранилище├── config.py                      # Конфигурация (расширить)├── utils.py                       # Утилиты└── main_agent.py                  # Точка входа для агента
3. Блок-схема архитектуры
┌─────────────────────────────────────────────────────────────┐│                    LLM Trading Agent                         ││                  (trading_agent.py)                          ││                                                              ││  ┌──────────────────────────────────────────────────────┐  ││  │  Decision Engine (decision_engine.py)                │  ││  │  - Анализ данных                                       │  ││  │  - Принятие решений                                    │  ││  │  - Формирование сигналов                              │  ││  └──────────────────────────────────────────────────────┘  ││                          │                                   ││                          ▼                                   ││  ┌──────────────────────────────────────────────────────┐  ││  │  DeepSeek API Client                                  │  ││  │  - Отправка промптов                                   │  ││  │  - Получение ответов                                  │  ││  └──────────────────────────────────────────────────────┘  │└──────────────────────────┬──────────────────────────────────┘                           │        ┌──────────────────┼──────────────────┐        │                  │                  │        ▼                  ▼                  ▼┌──────────────┐  ┌──────────────┐  ┌──────────────┐│ Data         │  │ Strategy     │  │ News         ││ Collector    │  │ Analyzer     │  │ Parser       ││              │  │              │  │              ││ - Доска      │  │ - IVR фильтр │  │ - Telegram   ││   опционов   │  │ - Греки      │  │   каналы     ││ - Греки/IV   │  │ - Аномалии   │  │              ││ - История    │  │ - Позиции    │  │              │└──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │                 │                 │       └─────────────────┼─────────────────┘                         │        ┌────────────────┼────────────────┐        │                │                │        ▼                ▼                ▼┌──────────────┐  ┌──────────────┐  ┌──────────────┐│ SQLite DB    │  │ Bybit API    │  │ Telegram Bot ││ (История)    │  │ WebSocket    │  │ (Сигналы)    │└──────────────┘  └──────────────┘  └──────────────┘
4. Последовательные шаги реализации
Этап 1: Инфраструктура данных (1-2 недели)
Шаг 1.1: SQLite база данных
Создать core/data/database.py
Таблицы:
option_history (symbol, timestamp, price, iv, delta, gamma, vega, theta, volume, oi)
underlying_history (symbol, timestamp, price)
Методы: save_option_data(), get_historical_greeks(), get_iv_statistics()
Шаг 1.2: Расширение data_store
Интеграция с SQLite: периодическое сохранение данных из WebSocket в БД

Механизм сохранения:
- Данные из WebSocket накапливаются в data_store (in-memory)
- Периодическое сохранение в БД: каждые 5 минут
- Время сохранения: моменты времени, кратные 5 минутам (13:00, 13:05, 13:10, 13:15 и т.д.)
- Реализация: использовать schedule/APScheduler с выравниванием по 5-минутным интервалам

Методы:
- save_to_database() - сохранить текущее состояние data_store в БД
- start_periodic_save() - запустить периодическое сохранение (в отдельном потоке)
- _calculate_next_save_time() - вычислить следующий момент времени, кратный 5 минутам

Важно:
- Сохранять только последние актуальные данные для каждого символа (не дублировать)
- Логировать успешное/неуспешное сохранение
- Обрабатывать ошибки БД без остановки работы WebSocket
Шаг 1.3: Сборщик доски опционов
Создать core/data/option_board.py
Метод get_option_board(underlying, max_days=3) — получить опционы на ближайшие 3 дня

Особенности:
- Получение списка доступных экспираций с биржи (не более 3 дней)
- Определение страйков для подписки: текущая_цена ± (500 * 7)
- Подписка на все опционы в диапазоне (Call и Put)
- При сохранении в БД: фильтрация только OTM опционов (ITM не сохраняются)
- Исключение опционов с экспирацией сегодня (days_to_expiration = 0)
Этап 2: Анализ данных (1-2 недели)
Шаг 2.1: Анализатор истории
Создать core/data/historical_analyzer.py
Методы:
get_iv_percentiles(symbol, days=30) — процентили IV
get_greeks_trend(symbol, days=7) — тренд греков
calculate_ivr(symbol) — IV Rank (текущая IV относительно диапазона)
Шаг 2.2: Стратегии анализа
core/strategy/iv_filter.py: фильтр по IVR < 25%
core/strategy/greeks_analyzer.py: анализ распределения гаммы/веги, скью
core/strategy/anomaly_detector.py: всплески объема, дисбаланс дельты
Этап 3: Новости (1 неделя)
Шаг 3.1: Парсер Telegram новостей
Не реализуем в текущей версии.
Этап 4: LLM агент (2-3 недели)
Шаг 4.1: Интеграция с DeepSeek
Создать core/agent/trading_agent.py
Класс TradingAgent:
__init__(api_key, model="deepseek-chat")
analyze_market(data) — анализ рынка
make_decision(context) — принятие решения
Шаг 4.2: Промпты
Создать core/agent/prompt_templates.py
Шаблоны:
MARKET_ANALYSIS_PROMPT — анализ рынка
DECISION_PROMPT — принятие решения
SIGNAL_FORMAT_PROMPT — форматирование сигнала
Шаг 4.3: Decision Engine
Создать core/agent/decision_engine.py
Логика:
Сбор данных (доска, история)
Анализ (IVR, греки, аномалии)
Запрос к LLM с контекстом
Парсинг ответа
Формирование сигнала
Этап 5: Интеграция с Telegram (1 неделя)
Шаг 5.1: Расширение telegram_bot.py
Добавить команду /agent_status — статус агента
Добавить команду /agent_start / /agent_stop — управление агентом
Обработчик сигналов от агента:
Форматирование сигнала
Отправка в Telegram с деталями (тип позиции, опционы, обоснование)
Шаг 5.2: Управление уровнями поддержки/сопротивления
Команда /set_levels_support и /set_levels_resistance — установка уровней для актива
Уровни передаются от пользователя боту в виде значений цены базового актива, с помощью кнопки.
Так же имеется кнопка для просмотра внесенных уровней,
и кнопка для удаления уровней пользователем.
Хранение в БД
Передача в контекст агента
Этап 6: Периодический запуск агента (1 неделя)
Шаг 6.1: Scheduler
Создать main_agent.py
Использовать schedule или APScheduler
Запуск агента каждые N минут (настраиваемо)
Логирование работы
Шаг 6.2: Обработка ошибок
Retry логика для API запросов
Обработка недоступности DeepSeek API
Логирование ошибок
Этап 7: Тестирование и оптимизация (1-2 недели)
Шаг 7.1: Тестирование
Unit тесты для анализаторов
Интеграционные тесты для агента
Тестирование на исторических данных
Шаг 7.2: Оптимизация
Кэширование данных
Оптимизация запросов к БД
Настройка промптов
5. Детали реализации
5.1. Схема базы данных SQLite

**Важно:** Режим сохранения опционов задаётся в `SUBSCRIPTION_CONFIG.save_only_otm`. При `save_only_otm = False` в БД сохраняются все опционы (OTM+ITM+ATM) с полем `is_otm` (1 = OTM, 0 = ITM/ATM). Для расчётов, не связанных с GEX, во всех запросах используется фильтр `is_otm = 1`. Для расчёта GEX используются все опционы (OTM+ITM+ATM), только с days_to_expiration ≤ 3.

-- История опционов
```sql
CREATE TABLE option_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date_data_collection DATETIME NOT NULL,
    expiration_date DATE NOT NULL,
    underlying_ticker TEXT NOT NULL,
    days_to_expiration INTEGER,
    ask_price REAL, bid_price REAL, mark_price REAL,
    iv REAL, ask_iv REAL, bid_iv REAL, mark_iv REAL,
    delta REAL, gamma REAL, vega REAL, theta REAL,
    volume_24h REAL, open_interest REAL, underlying_price REAL,
    is_otm INTEGER NOT NULL DEFAULT 1,
    option_type TEXT,
    strike REAL,
    UNIQUE(symbol, date_data_collection)
);

-- Индексы для быстрого поиска
CREATE INDEX idx_option_history_symbol ON option_history(symbol);
CREATE INDEX idx_option_history_date_data_collection ON option_history(date_data_collection);
CREATE INDEX idx_option_history_underlying_expiration ON option_history(underlying_ticker, expiration_date);
CREATE INDEX idx_option_history_days_to_expiration ON option_history(days_to_expiration);
CREATE INDEX idx_option_history_underlying_days ON option_history(underlying_ticker, days_to_expiration);
CREATE INDEX idx_option_history_is_otm ON option_history(is_otm);
```

-- Пресеты GEX (тикер + дата экспирации по пользователю Telegram)
```sql
CREATE TABLE gex_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    underlying TEXT NOT NULL,
    expiration_str TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, underlying, expiration_str)
);
```

-- Настройки мониторинга GEX по пользователю (порог и частота проверки)
```sql
CREATE TABLE gex_monitor_settings (
    user_id INTEGER PRIMARY KEY,
    threshold REAL,
    interval_minutes INTEGER,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```
Методы: `get_gex_monitor_settings(user_id)`, `set_gex_monitor_threshold(user_id, value)`, `set_gex_monitor_interval(user_id, interval_minutes)`.

-- История базовых активов
```sql
CREATE TABLE underlying_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    price REAL,
    UNIQUE(symbol, timestamp)
);
```

-- Уровни поддержки/сопротивления (от пользователя)
```sql
CREATE TABLE support_resistance_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying TEXT NOT NULL,
    level_type TEXT NOT NULL, -- 'support' или 'resistance'
    price REAL NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(underlying, level_type, price)
);
```

**Парсинг даты экспирации:**
- Формат Bybit: "4JAN26" (день + месяц + год)
- Преобразование: "4JAN26" → DATE "2026-01-08"
- Функция `parse_expiration_date(expiry_str)` в `core/data/database.py`

**Фильтрация OTM опционов:**
- Call OTM: strike > underlying_price
- Put OTM: strike < underlying_price
- Проверка выполняется перед сохранением в БД
- ITM и ATM опционы не сохраняются

**Примеры запросов:**
```sql
-- Получить опционы с экспирацией завтра (days_to_expiration = 1)
SELECT * FROM option_history 
WHERE underlying_ticker = 'BTC' 
  AND days_to_expiration = 1
ORDER BY date_data_collection DESC;

-- Получить историю опционов для анализа IVR
SELECT * FROM option_history 
WHERE underlying_ticker = 'BTC' 
  AND expiration_date = '2026-01-08'
  AND date_data_collection >= date('now', '-30 days');
```

5.2. Логика подписок на WebSocket и фильтрация опционов

**Параметры подписок:**
- Максимальный срок до экспирации: 3 дня
- Шаг страйка для опционов до 3 дней: 500
- Количество шагов от текущей цены: ±7 шагов
- Фильтрация: подписываемся на все опционы в диапазоне, но сохраняем в БД только OTM

**Сценарий первого запуска:**
1. Получить список доступных экспираций с биржи (не более 3 дней от текущей даты)
2. Для каждой экспирации:
   - Получить текущую цену базового актива
   - Вычислить страйки для подписки: текущая_цена ± (500 * 7)
   - Подписаться на все опционы (Call и Put) в этом диапазоне страйков
3. При сохранении в БД (каждые 5 минут):
   - Проверить каждый опцион: является ли он OTM?
   - Сохранить только OTM опционы
   - ITM и ATM опционы игнорируются

**Пример расчета страйков:**
- Текущая цена BTC: 89,500
- Шаг страйка: 500
- Количество шагов: 7
- Диапазон страйков: от 89,500 - (500 * 7) = 86,000 до 89,500 + (500 * 7) = 93,000
- Подписка на опционы со страйками: 86000, 86500, 87000, ..., 92500, 93000

**Ежедневное обновление подписок:**
- Время обновления: 8:05 UTC (через 5 минут после добавления новых опционов на бирже в 8:00 UTC)
- Переход суток: 8:00 UTC
- Действия при обновлении:
  1. Остановить сбор данных для опционов с `days_to_expiration = 0` (экспирируются сегодня)
  2. Удалить эти опционы из активных подписок WebSocket
  3. Получить новые опционы с биржи (если появились)
  4. Добавить новые опционы в подписки (если соответствуют критериям: до 3 дней, в диапазоне страйков)
  5. Пересчитать подписки для опционов с `days_to_expiration = 1, 2, 3`

**Алгоритм определения OTM:**
```python
def is_otm(strike: float, underlying_price: float, option_type: str) -> bool:
    """
    Проверка: опцион OTM (Out of The Money)?
    
    Call OTM: strike > underlying_price
    Put OTM: strike < underlying_price
    
    Returns:
        True если опцион OTM, False если ITM или ATM
    """
    if option_type == 'C':
        return strike > underlying_price
    elif option_type == 'P':
        return strike < underlying_price
    return False
```

**Конфигурация подписок:**
```python
SUBSCRIPTION_CONFIG = {
    "max_expiration_days": 3,              # Максимум дней до экспирации
    "strike_step_3days": 500,              # Шаг страйка для опционов до 3 дней
    "strike_steps_count": 7,               # ±7 шагов от текущей цены
    "daily_update_time_utc": "15:05",      # Время обновления подписок
    "skip_today_expiration": False,        # Собирать до экспирации (days_to_expiration = 0)
    "save_only_otm": True,                 # Сохранять только OTM опционы
}
```

5.4. Формат сигнала от агента
{    "signal_type": "strangle" | "straddle" | "call" | "put",    "underlying": "BTC",    "expiration": "4JAN26",    "strike_call": 89000,  # для strangle/straddle    "strike_put": 88000,   # для strangle/straddle    "strike": 89000,       # для направленных    "reasoning": "Низкий IVR (15%), сжатие у уровня поддержки...",    "confidence": 0.75,    # 0-1    "risk_level": "medium",    "timestamp": "2026-01-04T10:30:00"}
5.5. Пример промпта для LLM
Ты - эксперт по торговле опционами. Проанализируй следующие данные:Доска опционов BTC (ближайшие 1-2 недели):- Текущая цена BTC: $89,500- IVR: 15% (низкий)- Распределение греков: гамма сконцентрирована на 90k- Объем: всплеск в OTM Call на 92k- Уровень поддержки: $88,500 (от пользователя)История IV за 30 дней:- Минимум: 20%- Максимум: 85%- Текущая: 25%Оцени возможность входа в позицию:1. Стрэнгл (если нет дисбаланса, но есть сжатие)2. Стрэддл (если событие и IV не взлетела)3. Направленная позиция Call/Put (если явный дисбаланс)Верни JSON с решением или null если условий нет.


7. Уточнения и дополнения (на основе ответов)
7.1. Частота запуска агента
Частота анализа: 1 раз в час
В main_agent.py использовать schedule или APScheduler с интервалом 60 минут
Рекомендуется запускать в начале каждого часа (например, 10:00, 11:00, 12:00)
Добавить в конфигурацию AGENT_RUN_INTERVAL_MINUTES = 60
7.2. История торговли и сигналов
Дополнительная таблица для истории сигналов:
-- История сигналов от агента
```CREATE TABLE agent_signals (    id INTEGER PRIMARY KEY AUTOINCREMENT,    signal_type TEXT NOT NULL, -- 'strangle', 'straddle', 'call', 'put'    underlying TEXT NOT NULL,    expiration TEXT,    strike_call REAL,  -- для strangle/straddle    strike_put REAL,   -- для strangle/straddle    strike REAL,       -- для направленных    reasoning TEXT,    confidence REAL,  -- 0-1    risk_level TEXT,    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,    agent_version TEXT  -- версия агента/промпта для отслеживания изменений);CREATE INDEX idx_agent_signals_underlying ON agent_signals(underlying);CREATE INDEX idx_agent_signals_created_at ON agent_signals(created_at);CREATE INDEX idx_agent_signals_signal_type ON agent_signals(signal_type);-- История результатов сигналов (для анализа эффективности)CREATE TABLE signal_results (    id INTEGER PRIMARY KEY AUTOINCREMENT,    signal_id INTEGER NOT NULL,    entry_price REAL,      -- цена входа (если был вход)    exit_price REAL,       -- цена выхода (если был выход)    pnl REAL,              -- прибыль/убыток    entry_timestamp DATETIME,    exit_timestamp DATETIME,    status TEXT,           -- 'pending', 'entered', 'closed', 'expired'    notes TEXT,    FOREIGN KEY (signal_id) REFERENCES agent_signals(id));CREATE INDEX idx_signal_results_signal_id ON signal_results(signal_id);CREATE INDEX idx_signal_results_status ON signal_results(status);```
Методы для работы с историей:
save_signal(signal_data) — сохранить сигнал
get_signal_history(underlying, days=30) — получить историю сигналов
get_signal_statistics() — статистика эффективности (win rate, avg PnL)
update_signal_result(signal_id, result_data) — обновить результат сигнала
7.3. Конфигурация агента, данных и подписок
Добавить в config.py:
AGENT_CONFIG = {    "run_interval_minutes": 60,  # Частота запуска агента    "max_expiration_days": 3,     # Максимальная экспирация для анализа (3 дня)    "ivr_threshold": 25,          # Порог IVR для фильтрации    "min_confidence": 0.6,        # Минимальная уверенность для сигнала    "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", ""),    "deepseek_model": "deepseek-chat",    "deepseek_base_url": "https://api.deepseek.com",    "enable_signal_history": True,  # Сохранение истории сигналов}

DATA_CONFIG = {    "save_interval_minutes": 5,      # Интервал сохранения данных из WebSocket в БД    "align_to_interval": True,       # Выравнивание времени сохранения по 5-минутным интервалам    "save_on_startup": True,         # Сохранить данные при старте сервиса    "batch_save": True,              # Батчинг запросов к БД (сохранять все символы за один запрос)    "save_only_otm": True,           # Сохранять только OTM опционы (ITM не сохраняются)}

SUBSCRIPTION_CONFIG = {    "max_expiration_days": 3,              # Максимум дней до экспирации для подписки    "strike_step_3days": 500,              # Шаг страйка для опционов до 3 дней    "strike_steps_count": 7,               # ±7 шагов от текущей цены    "daily_update_time_utc": "15:05",      # Время обновления подписок (UTC)    "skip_today_expiration": False,        # Собирать до экспирации (days_to_expiration = 0)    "new_options_time_utc": "15:05",      # Время добавления новых опционов на бирже (UTC)}
7.4. Метрики эффективности
Для анализа работы агента:
Win Rate — процент прибыльных сигналов
Average PnL — средняя прибыль/убыток на сигнал
Sharpe Ratio — соотношение доходности к риску
Signal Frequency — частота генерации сигналов
Confidence Distribution — распределение уверенности агента
Модуль для анализа:
core/analytics/performance_analyzer.py
Методы: calculate_win_rate(), calculate_avg_pnl(), generate_report()
7.5. Рекомендации по реализации
Поэтапная интеграция DeepSeek:
Сначала реализовать сбор данных и анализ
Затем добавить заглушку агента (возвращает тестовые сигналы)
После проверки инфраструктуры интегрировать DeepSeek API
Логирование:
Логировать все действия агента
Отдельный лог-файл для сигналов: agent_signals.log
Логировать промпты и ответы LLM (для отладки и улучшения)
Обработка ошибок:
Если DeepSeek API недоступен — пропустить цикл, не падать
Если нет данных по опционам — логировать предупреждение
Retry логика с экспоненциальной задержкой
Производительность:
Кэшировать результаты анализа доски опционов (обновлять раз в 5-10 минут)
Батчинг запросов к БД
Асинхронные запросы к DeepSeek API
Безопасность:
API ключи только в переменных окружения
Не логировать чувствительные данные
Валидация входных данных от LLM перед сохранением
Мониторинг:
Health check endpoint для агента
Метрики: время выполнения анализа, количество запросов к API
Алерты при длительных простоях или ошибках
7.6. Будущие улучшения (не в текущей версии)
Анализ новостей из Telegram каналов
Автоматическое отслеживание результатов сигналов (если будет интеграция с биржей)
A/B тестирование разных версий промптов
Машинное обучение для оптимизации параметров стратегии
Мульти-активные анализ (несколько активов одновременно)

7.7. Периодичность сохранения данных из WebSocket в БД
Частота сохранения: каждые 5 минут
Время сохранения: моменты времени, кратные 5 минутам (13:00, 13:05, 13:10, 13:15 и т.д.)

Реализация:
- Использовать schedule или APScheduler
- Выравнивание времени: вычислять следующий момент времени, кратный 5 минутам
- Пример: если текущее время 13:03, следующее сохранение в 13:05
- Если текущее время 13:07, следующее сохранение в 13:10

Алгоритм выравнивания времени:
```python
def get_next_save_time(current_time):
    # Получаем минуты текущего времени
    current_minute = current_time.minute
    # Округляем вверх до ближайшего значения, кратного 5
    next_minute = ((current_minute // 5) + 1) * 5
    # Если перевалили за час, переходим на следующий час
    if next_minute >= 60:
        next_hour = current_time.hour + 1
        next_minute = 0
        # Если перевалили за день, переходим на следующий день
        if next_hour >= 24:
            next_day = current_time.day + 1
            next_hour = 0
            return current_time.replace(day=next_day, hour=next_hour, minute=next_minute, second=0, microsecond=0)
        return current_time.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
    return current_time.replace(minute=next_minute, second=0, microsecond=0)
```

Особенности:
- Сохранение происходит асинхронно, не блокирует работу WebSocket
- При сохранении берутся последние актуальные данные из data_store для каждого символа
- **Фильтрация OTM:** перед сохранением проверяется, является ли опцион OTM (Out of The Money)
  - Call OTM: strike > underlying_price
  - Put OTM: strike < underlying_price
  - Только OTM опционы сохраняются в БД, ITM и ATM опционы игнорируются
- При сохранении вычисляются и сохраняются дополнительные поля:
  - `expiration_date` - извлекается из symbol (формат "4JAN26" → DATE "2026-01-08")
  - `underlying_ticker` - извлекается из symbol (например, "BTC")
  - `days_to_expiration` - вычисляется как expiration_date - date_data_collection
- При ошибке сохранения - логировать, но продолжать работу
- Первое сохранение происходит при старте сервиса (сразу или в ближайший 5-минутный интервал)

7.8. Динамические пороги стратегии
Добавлена таблица `strategy_thresholds` для хранения динамически рассчитанных порогов по каждому активу и DTE-бину.

```sql
CREATE TABLE strategy_thresholds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying TEXT NOT NULL,
    dte_bucket TEXT NOT NULL,      -- бин по days_to_expiration (например, '0-1', '2-3', '31-60')
    metric TEXT NOT NULL,          -- имя порога (например, 'skew_threshold')
    value REAL NOT NULL,           -- рассчитанное значение порога
    sample_size INTEGER NOT NULL,  -- размер выборки
    method TEXT NOT NULL,          -- метод расчета (например, 'pct_85')
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(underlying, dte_bucket, metric)
);
CREATE INDEX idx_strategy_thresholds_underlying ON strategy_thresholds(underlying);
CREATE INDEX idx_strategy_thresholds_bucket ON strategy_thresholds(dte_bucket);
```

Порог пересчитывается периодически (по конфигурации) и используется вместо статических значений, с fallback на `STRATEGY_CONFIG` при недостатке данных.

**Важно:** Существующие данные в БД не мигрируются. Новая структура применяется для новых данных, начиная с момента обновления схемы БД.