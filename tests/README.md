# План тестирования торгового бота опционов

## Обзор

Этот документ описывает стратегию тестирования для торгового советника по опционам с LLM агентом.

## Структура тестов

```
tests/
├── unit/                    # Unit тесты для отдельных модулей
│   ├── test_historical_analyzer.py
│   ├── test_iv_filter.py
│   ├── test_greeks_analyzer.py
│   ├── test_anomaly_detector.py
│   ├── test_position_builder.py
│   ├── test_option_board.py
│   ├── test_database.py
│   └── test_data_store.py
├── integration/            # Интеграционные тесты
│   ├── test_decision_engine.py
│   ├── test_trading_agent.py
│   └── test_agent_workflow.py
├── historical/             # Тестирование на исторических данных
│   ├── test_backtest.py
│   └── test_signal_quality.py
├── fixtures/               # Тестовые данные
│   ├── sample_options_data.json
│   ├── sample_historical_data.db
│   └── mock_responses.py
└── conftest.py            # Общие фикстуры для pytest
```

## Типы тестов

### 1. Unit тесты для анализаторов

**Цель:** Проверить корректность работы отдельных модулей анализа данных.

#### 1.1. `test_historical_analyzer.py`
- ✅ `test_get_iv_percentiles()` - проверка расчета процентилей IV
- ✅ `test_get_greeks_trend()` - проверка определения тренда греков
- ✅ `test_calculate_ivr()` - проверка расчета IV Rank
- ✅ `test_get_comprehensive_analysis()` - комплексный анализ
- ✅ `test_edge_cases()` - граничные случаи (нет данных, нулевые значения)

#### 1.2. `test_iv_filter.py`
- ✅ `test_check_ivr()` - проверка фильтрации по IVR
- ✅ `test_filter_options_list()` - фильтрация списка опционов
- ✅ `test_threshold_configuration()` - проверка настройки порога

#### 1.3. `test_greeks_analyzer.py`
- ✅ `test_gamma_concentration()` - концентрация гаммы
- ✅ `test_vega_concentration()` - концентрация веги
- ✅ `test_skew_calculation()` - расчет скью
- ✅ `test_distribution_analysis()` - анализ распределения

#### 1.4. `test_anomaly_detector.py`
- ✅ `test_volume_spike_detection()` - обнаружение всплесков объема
- ✅ `test_delta_imbalance()` - дисбаланс дельты
- ✅ `test_anomaly_scoring()` - оценка аномалий

#### 1.5. `test_position_builder.py`
- ✅ `test_strangle_building()` - построение стрэнгла
- ✅ `test_straddle_building()` - построение стрэддла
- ✅ `test_directional_position()` - направленные позиции

#### 1.6. `test_option_board.py`
- ✅ `test_get_option_board()` - получение доски опционов
- ✅ `test_expiration_filtering()` - фильтрация по экспирации
- ✅ `test_strike_range_calculation()` - расчет диапазона страйков
- ✅ `test_otm_filtering()` - фильтрация OTM опционов

#### 1.7. `test_database.py`
- ✅ `test_save_option_data()` - сохранение данных опциона
- ✅ `test_get_historical_greeks()` - получение истории греков
- ✅ `test_get_iv_statistics()` - статистика IV
- ✅ `test_expiration_date_parsing()` - парсинг даты экспирации
- ✅ `test_days_to_expiration_calculation()` - расчет дней до экспирации

#### 1.8. `test_data_store.py`
- ✅ `test_update_and_get()` - обновление и получение данных
- ✅ `test_periodic_save()` - периодическое сохранение
- ✅ `test_otm_filtering_on_save()` - фильтрация OTM при сохранении
- ✅ `test_subscribers()` - система подписчиков

### 2. Интеграционные тесты для агента

**Цель:** Проверить взаимодействие компонентов агента и корректность принятия решений.

#### 2.1. `test_decision_engine.py`
- ✅ `test_data_collection()` - сбор данных для анализа
- ✅ `test_analysis_pipeline()` - пайплайн анализа (IVR, греки, аномалии)
- ✅ `test_llm_integration()` - интеграция с DeepSeek API (моки)
- ✅ `test_signal_generation()` - генерация сигналов
- ✅ `test_error_handling()` - обработка ошибок

#### 2.2. `test_trading_agent.py`
- ✅ `test_agent_initialization()` - инициализация агента
- ✅ `test_market_analysis()` - анализ рынка
- ✅ `test_decision_making()` - принятие решений
- ✅ `test_response_parsing()` - парсинг ответов LLM
- ✅ `test_retry_logic()` - логика повторов при ошибках API

#### 2.3. `test_agent_workflow.py`
- ✅ `test_full_workflow()` - полный цикл работы агента
- ✅ `test_signal_formatting()` - форматирование сигналов
- ✅ `test_signal_saving()` - сохранение сигналов в БД
- ✅ `test_telegram_integration()` - интеграция с Telegram (моки)

### 3. Тестирование на исторических данных

**Цель:** Проверить качество сигналов на исторических данных (backtesting).

#### 3.1. `test_backtest.py`
- ✅ `test_backtest_strategy()` - бэктест стратегии
- ✅ `test_signal_accuracy()` - точность сигналов
- ✅ `test_performance_metrics()` - метрики производительности (win rate, PnL)
- ✅ `test_historical_scenarios()` - различные исторические сценарии

#### 3.2. `test_signal_quality.py`
- ✅ `test_signal_consistency()` - консистентность сигналов
- ✅ `test_confidence_calibration()` - калибровка уверенности
- ✅ `test_false_positive_rate()` - частота ложных срабатываний
- ✅ `test_signal_timing()` - тайминг сигналов

## Инструменты тестирования

- **pytest** - основной фреймворк для тестирования
- **pytest-mock** - для мокирования внешних зависимостей
- **pytest-asyncio** - для асинхронных тестов
- **pytest-cov** - для покрытия кода тестами
- **faker** - для генерации тестовых данных

## Запуск тестов

```bash
# Все тесты
uv run pytest tests/

# Только unit тесты
uv run pytest tests/unit/

# Только интеграционные тесты
uv run pytest tests/integration/

# С покрытием кода
uv run pytest tests/ --cov=core --cov=services --cov-report=html

# Конкретный тест
uv run pytest tests/unit/test_historical_analyzer.py::test_calculate_ivr

# С verbose выводом
uv run pytest tests/ -v
```

## Моки и фикстуры

### Внешние зависимости для мокирования:
- **Bybit API** - HTTP и WebSocket запросы
- **DeepSeek API** - LLM запросы
- **Telegram Bot API** - отправка сообщений
- **SQLite БД** - использование тестовой БД в памяти

### Фикстуры:
- Тестовые данные опционов
- Исторические данные в БД
- Моки ответов API
- Конфигурация для тестов

## Критерии успешности

- ✅ Покрытие кода тестами: минимум 80%
- ✅ Все unit тесты проходят
- ✅ Все интеграционные тесты проходят
- ✅ Backtesting показывает разумные результаты
- ✅ Нет критических багов в логике анализа

## Следующие шаги

После успешного прохождения тестов:
1. Оптимизация производительности (шаг 7.2)
2. Настройка CI/CD для автоматического запуска тестов
3. Мониторинг качества сигналов в продакшене
