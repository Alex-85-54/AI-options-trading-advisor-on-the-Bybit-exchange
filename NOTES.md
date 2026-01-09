# Option Bot (Bybit + Telegram) — Project Notes

## 1. Общая идея

Бот помогает входить в опционные позиции на бирже Bybit (страты и направленные конструкции) через интерфейс Telegram:

- Пользователь через Telegram добавляет интересующие опционы (Call/Put) по BTC, ETH, SOL.
- Бот подписывается на поток котировок Bybit по этим опционам через WebSocket.
- Бот отслеживает равенство цен Call/Put (для стрэнглов/стрэддлов) и расхождение после равенства.
- При событии (цены сравнялись / разошлись) отправляет уведомления в Telegram.

## 2. Архитектура и сервисы

Проект логически разделён на три сервиса, которые в Docker запускаются как отдельные контейнеры:

1. **Telegram‑бот (`telegram_bot.py`)**
   - Интерфейс пользователя (добавление опционов, запуск/остановка мониторинга, просмотр статуса и цен).
   - Работает через `python-telegram-bot`.
   - Хранит для каждого user_id:
     - список опционов `user_options`,
     - флаг активности мониторинга `user_monitoring`,
     - состояние пар Call/Put `pair_status`.
   - Самостоятельно мониторит пары Call/Put по данным из `data_store` и:
     - шлёт сигнал “цены сравнялись” (вход),
     - шлёт сигнал “цены разошлись” (сброс).
   - В Docker ходит к сервису мониторинга по `MONITORING_SERVICE_URL` (пока интеграция мониторинга из `main.py` не завершена, основная логика сигналов реализована внутри самого Telegram‑бота).

2. **API‑сервис данных опционов (`api_server.py`)**
   - FastAPI‑приложение, отдающее данные по опционам:
     - `GET /data/{symbol}` — текущие данные по конкретному опциону (из `data_store`),
     - `GET /data/underlying/{underlying}` — список опционов по базовому активу,
     - `POST /subscribe` — подписка на список опционов (через `ws_manager`),
     - `GET /active/symbols` / `/subscriptions/*` — информация об активных подписках.
   - Использует:
     - `websocket_manager.ws_manager` — менеджер WebSocket‑соединения и подписок к Bybit,
     - `data_store.data_store` — хранилище последних котировок.
   - Порт по умолчанию: `8000`.

3. **Сервис мониторинга (`main.py`)**
   - FastAPI‑сервис “Option Monitoring Service” с собственной логикой мониторинга:
     - хранит `MonitoringService.active_monitors` (конфиги мониторинга по пользователям),
     - периодически опрашивает API‑сервис (`API_BASE_URL`) и проверяет равенство цен Call/Put.
   - REST‑интерфейс:
     - `POST /monitoring/start` / `stop` / `GET /monitoring/status/{user_id}`.
   - Пока интеграция с Telegram для уведомлений помечена как TODO (логирование в файл и stdout).
   - Порт по умолчанию: `8001`.

## 3. Основные модули

- `config.py`
  - Содержит словарь `CONFIG`:
    - `"testnet"` — флаг тестнета Bybit,
    - `"retries"`, `"restart_on_error"` — настройки WebSocket‑подключения,
    - `"expiration_year"` — суффикс года для тикеров опционов (например `"26"` для 2026),
    - `"option_types"` — `["C", "P"]`,
    - `"server_port"` — порт API‑сервера (обычно `8000`),
    - `"telegram_token"` — читается из `TELEGRAM_TOKEN` (переменная окружения),
    - `"bybit_api_key"`, `"bybit_api_secret"` — читаются из `BYBIT_API_KEY` / `BYBIT_API_SECRET`.
- `websocket_manager.py`
  - Класс `OptionWebSocketManager`:
    - создает тикеры опционов Bybit: `UNDERLYING-dayMONTHYY-strike-type-USDT`,
    - устанавливает WebSocket‑подключение через `pybit.unified_trading.WebSocket` (канал `option`),
    - подписывается на поток `ticker_stream` по списку символов,
    - в `handle_message` принимает сообщения, парсит JSON, извлекает цены (ask/bid, IV, Greeks, пр.),
    - сохраняет обновления в `data_store.update(symbol, option_data)`,
    - `wait_for_data` ждёт появления не нулевых данных по списку символов.
- `data_store.py`
  - Объект-хранилище последних данных по опционам (в памяти процесса) с методами `get`, `update`, `get_by_underlying` и т.п.
- `telegram_bot.py`
  - Класс `TelegramOptionBot`:
    - Структурирован через `ConversationHandler` `python-telegram-bot`.
    - Основной функционал:
      - добавление опционов (через поэтапный выбор underlying → день → месяц → страйк → тип),
      - удаление опционов,
      - просмотр списка и текущих цен,
      - запуск/остановка мониторинга,
      - показ активных сигналов и состояния мониторинга.
    - Мониторинг равенства цен реализован через `JobQueue` (`_monitor_prices_job`):
      - каждые `CHECK_INTERVAL` секунд сравниваются ask‑цены пар Call/Put,
      - если относительная разница \< `THRESHOLD` (по умолчанию 1%) — считается, что цены “сравнялись”,
      - при смене статуса пары (вошли в равенство / вышли из равенства) шлётся уведомление в Telegram.
- `main.py`
  - Дополнительный сервис мониторинга (FastAPI + внутренняя логика) — возможная точка для выноса мониторинга из Telegram‑бота в отдельный сервис (пока уведомления только логируются).

## 4. Запуск через Docker

Используется менеджер пакетов **uv** и общий базовый образ.

### Dockerfile

- Базовый образ: `python:3.12-slim`.
- Установка `uv` через официальный скрипт (`curl https://astral.sh/uv/install.sh | sh`).
- `uv sync --frozen --no-dev` по `pyproject.toml` и `uv.lock`.
- Основной рабочий каталог: `/app`.

### docker-compose.yml (3 сервиса)

- `telegram-bot`:
  - `command: ["uv", "run", "telegram_bot.py"]`
  - env:
    - `TELEGRAM_TOKEN` — обязателен,
    - `BYBIT_API_KEY`, `BYBIT_API_SECRET` — опциональны,
    - `MONITORING_SERVICE_URL: "http://monitoring-service:8001"`.
- `api-server`:
  - `command: ["uv", "run", "api_server.py"]`
  - порт: `8000:8000`.
- `monitoring-service`:
  - `command: ["uv", "run", "main.py"]`
  - env: `API_BASE_URL: "http://api-server:8000"`,
  - порт: `8001:8001`.

`.env` (в корне проекта, подхватывается `docker compose`):

TELEGRAM_TOKEN=твой_реальный_токен_бота
BYBIT_API_KEY=
BYBIT_API_SECRET=

Запуск:
docker compose up --build

5. Логика равенства цен (основа торговых сигналов)
Источник цен: data_store (обновляется через WebSocket‑подписки на Bybit).
Сравнение Call/Put:
Берутся ask‑цены пар опционов Call и Put (одинаковый underlying, дата экспирации, страйк).
Считается:
абсолютная разница: price_diff = |call - put|,
средняя цена: avg_price = (call + put) / 2,
относительная разница: relative_diff = price_diff / avg_price * 100.
Если relative_diff < THRESHOLD * 100 (по умолчанию 1%), считается, что цены “сравнялись”.
События:
Переход пары в состояние “равны” → сигнал к входу в конструкцию (стрэнгл/стрэддл).
Переход пары в состояние “не равны” → сброс сигнала (выход из режима входа).
6. Что важно помнить для будущих доработок
Сейчас основная практическая логика сигналов реализована в Telegram‑боте (через JobQueue и data_store).
Сервис main.py — задел для выноса мониторинга в отдельный микросервис:
можно будет заменить внутренний мониторинг в Telegram‑боте на запросы к monitoring-service.
Все чувствительные данные (токен Telegram, ключи Bybit) берутся из переменных окружения, а не зашиты в код.
Тикеры опционов Bybit конструируются через OptionWebSocketManager.create_option_symbol, важно использовать одну и ту же логику везде (Telegram‑бот, API‑сервис и мониторинг).

6. Разработана архитектура приложения и план разработки в файле ARCHITECTURE.md