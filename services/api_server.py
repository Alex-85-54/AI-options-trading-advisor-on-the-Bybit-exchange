from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from typing import List, Dict, Optional
from pydantic import BaseModel
from datetime import datetime
import sys
import json
import asyncio
import os
import requests
from pathlib import Path

print("=" * 50, flush=True)
print("API Server starting...", flush=True)
print("=" * 50, flush=True)

# Добавляем корень проекта в путь для импортов
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config import (
    CONFIG,
    STRATEGY_CONFIG,
    AGENT_CONFIG,
    DATA_CONFIG,
    SUBSCRIPTION_CONFIG,
    ANALYSIS_CONFIG,
    DYNAMIC_THRESHOLD_CONFIG,
    DTE_BINS,
    format_datetime_local,
    DISPLAY_TIMEZONE,
)
from services.websocket_manager import ws_manager
from services.data_store import data_store
from core.data.database import get_database
from core.data.option_board import get_option_board, is_otm
from core.strategy.dynamic_thresholds import DynamicThresholds
from utils.logging_config import setup_logging as _setup_logging_base
from utils.log_buffer import get_log_buffer_handler
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import threading

# Настройка логирования: читаемый формат с временем и описанием на русском
print("Setting up logging...", flush=True)
_log_format = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_log_datefmt = "%Y-%m-%d %H:%M:%S"
logger = _setup_logging_base(
    service_name="api_server",
    log_level=logging.INFO,
    format_string=_log_format,
)
# Устанавливаем формат даты/времени для всех хендлеров api_server
for h in logger.handlers:
    if hasattr(h, "setFormatter") and isinstance(h.formatter, logging.Formatter):
        h.setFormatter(logging.Formatter(_log_format, datefmt=_log_datefmt))
print("✓ Logger initialized", flush=True)

# Инициализация log buffer handler для админ-панели
print("Initializing log buffer...", flush=True)
log_buffer = get_log_buffer_handler(max_logs=1000)
root_logger = logging.getLogger()
# Добавляем buffer handler к root logger, чтобы получать все логи
if log_buffer not in root_logger.handlers:
    root_logger.addHandler(log_buffer)
print("✓ Log buffer initialized", flush=True)

print("Creating FastAPI app...", flush=True)
app = FastAPI(title="Option Data Service", version="1.0.0")
print("✓ FastAPI app created", flush=True)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Монтирование статики для админ-панели
# Проверяем путь к static директории (в Docker это /app/static, локально - project_root/static)
static_dir = Path("/app/static") if Path("/app/static").exists() else project_root / "static"
static_dir.mkdir(exist_ok=True, parents=True)
logger.info("Каталог статики: %s", static_dir.absolute())
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Настройки подписок и времени (UTC+7)
UNDERLYING_ASSETS = ["BTC"]

# Время обновления подписок берется из конфига (формат "HH:MM")
_subscription_time = SUBSCRIPTION_CONFIG.get("daily_update_time_utc", "08:05")
try:
    _subscription_parts = _subscription_time.split(":")
    SUBSCRIPTION_UPDATE_HOUR = int(_subscription_parts[0])
    SUBSCRIPTION_UPDATE_MINUTE = int(_subscription_parts[1])
except (ValueError, IndexError):
    logger.warning(
        f"Некорректный формат daily_update_time_utc='{_subscription_time}', используем 08:05"
    )
    SUBSCRIPTION_UPDATE_HOUR = 8
    SUBSCRIPTION_UPDATE_MINUTE = 5

# Глобальные объекты
option_board = get_option_board()
dynamic_thresholds = DynamicThresholds()
subscription_scheduler = AsyncIOScheduler(timezone=DISPLAY_TIMEZONE)
last_subscription_refresh: Optional[datetime] = None
next_subscription_run: Optional[datetime] = None
subscription_lock = threading.Lock()


def refresh_option_subscriptions() -> Dict[str, int]:
    """
    Переподписка на опционы (обновление списка активных символов).
    Использует доску опционов и переподключает WebSocket.
    """
    logger.info("Переподписка на опционы: начало выполнения задачи")
    if not subscription_lock.acquire(blocking=False):
        logger.warning("Переподписка уже выполняется в другом потоке; новый запуск пропущен")
        active = ws_manager.get_active_symbols_copy()
        return {"old_count": len(active), "new_count": len(active)}
    try:
        global last_subscription_refresh
        last_subscription_refresh = datetime.now(DISPLAY_TIMEZONE)
        logger.info("Переподписка: получение списка символов с биржи (UTC+7)")
        all_symbols: List[str] = []
        max_days = SUBSCRIPTION_CONFIG.get("max_expiration_days", 3)

        for underlying in UNDERLYING_ASSETS:
            board_data = option_board.get_option_board(underlying, max_days=max_days)
            symbols = board_data.get("symbols", [])
            if symbols:
                all_symbols.extend(symbols)
                logger.info("Переподписка: по активу %s найдено символов — %s", underlying, len(symbols))
            else:
                logger.warning("Переподписка: по активу %s символы не найдены", underlying)

        if not all_symbols:
            logger.warning("Переподписка: список символов пуст, обновление отменено")
            return {"old_count": len(ws_manager.get_active_symbols_copy()), "new_count": 0}

        unique_symbols = list(set(all_symbols))

        old_count = len(ws_manager.get_active_symbols_copy())
        ws_manager.update_subscriptions(unique_symbols)
        new_count = len(ws_manager.get_active_symbols_copy())
        try:
            job = subscription_scheduler.get_job("daily_subscription_update")
            if job:
                global next_subscription_run
                next_subscription_run = job.next_run_time
        except Exception:
            pass

        logger.info("Переподписка завершена успешно: было подписок %s, стало %s", old_count, new_count)
        return {"old_count": old_count, "new_count": new_count}
    except Exception as e:
        logger.error("Переподписка завершена с ошибкой: %s", e, exc_info=True)
        active = ws_manager.get_active_symbols_copy()
        return {"old_count": len(active), "new_count": len(active)}
    finally:
        subscription_lock.release()
        logger.info("Переподписка на опционы: задача завершена (освобождён lock)")


REFRESH_JOB_TIMEOUT_SEC = SUBSCRIPTION_CONFIG.get("refresh_job_timeout_sec", 120)


async def refresh_option_subscriptions_async() -> Dict[str, int]:
    """
    Асинхронный запуск переподписки в отдельном потоке с таймаутом.
    После таймаута задача считается завершённой, чтобы следующий запуск по расписанию не пропускался.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(refresh_option_subscriptions),
            timeout=REFRESH_JOB_TIMEOUT_SEC,
        )
        return result
    except asyncio.TimeoutError:
        logger.error(
            "Переподписка не завершилась за %s с (таймаут). Следующая попытка — по расписанию.",
            REFRESH_JOB_TIMEOUT_SEC,
        )
        return {"old_count": 0, "new_count": 0, "timeout": True}


@app.on_event("startup")
async def startup_event():
    """Запуск планировщика переподписки"""
    if not subscription_scheduler.running:
        job = subscription_scheduler.add_job(
            refresh_option_subscriptions_async,
            trigger=CronTrigger(
                hour=SUBSCRIPTION_UPDATE_HOUR,
                minute=SUBSCRIPTION_UPDATE_MINUTE,
                timezone=DISPLAY_TIMEZONE,
            ),
            id="daily_subscription_update",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        subscription_scheduler.start()
        logger.info(
            "Планировщик переподписки запущен: ежедневно в %s UTC+7, таймаут задачи %s с",
            f"{SUBSCRIPTION_UPDATE_HOUR:02d}:{SUBSCRIPTION_UPDATE_MINUTE:02d}",
            REFRESH_JOB_TIMEOUT_SEC,
        )
        global next_subscription_run
        next_subscription_run = job.next_run_time
        logger.info("Следующая переподписка по расписанию: %s", next_subscription_run)
    # Первичная подписка при старте, чтобы админ-панель сразу показывала актуальные данные
    await refresh_option_subscriptions_async()


@app.on_event("shutdown")
async def shutdown_event():
    """Остановка планировщика"""
    if subscription_scheduler.running:
        subscription_scheduler.shutdown()


# Pydantic модели
class OptionSymbol(BaseModel):
    underlying: str
    day: str
    month: str
    strike: str
    option_type: str


class StraddleConfig(BaseModel):
    underlying: str
    current_price: float
    distance_percent: float = 5.0  # Процент удаления от текущей цены


@app.get("/")
async def root():
    return {"service": "Option Data Service", "status": "running"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Отдаём пустой ответ 204 — убирает 404 в логах при открытии админки в браузере."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Запрет индексации для ботов — уменьшает запросы к несуществующим путям."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return {
        "status": "ok",
        "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE)),
        "static_dir": str(static_dir.absolute()),
        "static_exists": static_dir.exists(),
        "admin_html_exists": (static_dir / "admin.html").exists()
    }


@app.get("/data/{symbol}")
async def get_option_data(symbol: str):
    """Получить данные по конкретному опциону"""
    data = data_store.get(symbol)
    if not data:
        # Проверяем, подписан ли символ
        if symbol in ws_manager.get_active_symbols_copy():
            return {
                "symbol": symbol,
                "status": "subscribed",
                "message": "Waiting for data from exchange",
                "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE))
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol {symbol} not found or not subscribed"
            )
    return data


@app.get("/data/underlying/{underlying}")
async def get_underlying_options(underlying: str):
    """Получить все опционы по базовому активу"""
    data = data_store.get_by_underlying(underlying)
    return {"underlying": underlying, "options": data}


@app.post("/subscribe")
async def subscribe_to_options(symbols: List[OptionSymbol]):
    """Подписаться на обновления опционов"""
    symbol_strings = [
        ws_manager.create_option_symbol(
            s.underlying, s.day, s.month, s.strike, s.option_type
        )
        for s in symbols
    ]

    ws_manager.connect(symbol_strings)

    return {
        "status": "subscribed",
        "symbols": symbol_strings,
        "active_symbols": ws_manager.get_active_symbols_copy()
    }


@app.get("/active/symbols")
async def get_active_symbols():
    """Получить список активных символов"""
    return {
        "active_symbols": ws_manager.get_active_symbols_copy(),
        "total": len(ws_manager.get_active_symbols_copy())
    }


@app.post("/subscriptions/update")
async def update_subscriptions(symbols: List[str]):
    """Обновить подписки WebSocket"""
    try:
        # Получаем текущие активные символы
        current_symbols = ws_manager.get_active_symbols_copy()

        # Формируем новый список (объединение старых и новых)
        all_symbols = list(set(current_symbols + symbols))

        # Обновляем подписки
        ws_manager.update_subscriptions(all_symbols)

        return {
            "status": "updated",
            "old_symbols_count": len(current_symbols),
            "new_symbols_count": len(all_symbols),
            "active_symbols": ws_manager.get_active_symbols_copy()
        }
    except Exception as e:
        logger.error("Ошибка обновления подписок: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/subscriptions/active")
async def get_active_subscriptions():
    """Получить активные подписки"""
    return {
        "active_symbols": ws_manager.get_active_symbols_copy(),
        "count": len(ws_manager.get_active_symbols_copy())
    }


@app.get("/data/check/{symbol}")
async def check_option_data(symbol: str):
    """Проверить наличие и актуальность данных"""
    data = data_store.get(symbol)

    if data:
        # Проверяем актуальность данных (не старше 30 секунд)
        timestamp = data.get('timestamp')
        if isinstance(timestamp, datetime):
            now = datetime.now(DISPLAY_TIMEZONE)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=DISPLAY_TIMEZONE)
            age = (now - timestamp).total_seconds()
            is_fresh = age < 30
        else:
            is_fresh = False

        return {
            "symbol": symbol,
            "available": True,
            "fresh": is_fresh,
            "age_seconds": age if 'age' in locals() else None,
            "has_price": 'ask_price' in data and data['ask_price'] > 0,
            "data": data if is_fresh else None
        }
    else:
        return {
            "symbol": symbol,
            "available": False,
            "subscribed": symbol in ws_manager.get_active_symbols_copy(),
            "message": "Нет данных" if symbol in ws_manager.get_active_symbols_copy() else "Нет подписки"
        }


# ==================== Admin Panel Endpoints ====================

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Главная страница админ-панели"""
    admin_html_path = static_dir / "admin.html"
    logger.info("Загрузка админ-панели: %s", admin_html_path.absolute())
    logger.info("Файл админ-панели существует: %s", admin_html_path.exists())
    
    if admin_html_path.exists():
        return FileResponse(admin_html_path)
    else:
        # Если файл не найден, возвращаем простое сообщение
        logger.warning("Файл админ-панели не найден: %s", admin_html_path.absolute())
        return HTMLResponse(f"""
        <html>
            <head><title>Admin Panel - File Not Found</title></head>
            <body>
                <h1>Admin Panel</h1>
                <p>Admin panel HTML file not found.</p>
                <p>Expected path: {admin_html_path.absolute()}</p>
                <p>Static dir: {static_dir.absolute()}</p>
                <p>Please create static/admin.html</p>
            </body>
        </html>
        """)


@app.get("/admin/api/config")
async def get_strategy_config():
    """Получить все параметры стратегий"""
    return {
        "strategy_config": STRATEGY_CONFIG,
        "agent_config": {
            k: v for k, v in AGENT_CONFIG.items() 
            if k != "deepseek_api_key"  # Не показываем API ключ
        },
        "data_config": DATA_CONFIG,
        "subscription_config": SUBSCRIPTION_CONFIG,
        "analysis_config": ANALYSIS_CONFIG
    }


@app.get("/admin/api/thresholds")
async def get_dynamic_thresholds():
    """Получить динамические пороги стратегии"""
    try:
        db = get_database()
        thresholds = db.get_all_strategy_thresholds()
        static_thresholds = {
            "ivr_threshold": STRATEGY_CONFIG.get("ivr_threshold"),
            "gamma_concentration_threshold": STRATEGY_CONFIG.get("gamma_concentration_threshold"),
            "vega_concentration_threshold": STRATEGY_CONFIG.get("vega_concentration_threshold"),
            "skew_threshold": STRATEGY_CONFIG.get("skew_threshold"),
            "volume_spike_multiplier": STRATEGY_CONFIG.get("volume_spike_multiplier"),
            "delta_imbalance_threshold": STRATEGY_CONFIG.get("delta_imbalance_threshold"),
        }
        return {
            "dynamic_enabled": DYNAMIC_THRESHOLD_CONFIG.get("enabled", True),
            "dynamic_thresholds": thresholds,
            "static_thresholds": static_thresholds,
            "dte_bins": DTE_BINS,
            "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE)),
        }
    except Exception as e:
        logger.error("Ошибка получения динамических порогов: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/api/thresholds/recalculate")
async def recalculate_thresholds(underlying: Optional[str] = None, dte_bucket: Optional[str] = None):
    """
    Ручной пересчет динамических порогов.
    Если underlying не задан - пересчитывает по всем активам из data_store.
    """
    try:
        targets = []
        if underlying:
            targets = [underlying]
        else:
            targets = list(data_store.get_all().keys())
            # Преобразуем список символов в уникальные underlying
            underlyings = set()
            for symbol in targets:
                parts = symbol.split("-")
                if parts:
                    underlyings.add(parts[0])
            targets = sorted(list(underlyings))
        if not targets:
            return {"status": "skipped", "message": "Нет активных underlying для пересчета"}
        insufficient = []
        for item in targets:
            result = await asyncio.to_thread(
                dynamic_thresholds.recalculate_for_underlying,
                item,
                dte_bucket,
            )
            if result.get("insufficient_bins"):
                insufficient.append(result)
        return {
            "status": "ok",
            "underlyings": targets,
            "dte_bucket": dte_bucket,
            "insufficient_bins": insufficient,
            "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE)),
        }
    except Exception as e:
        logger.error("Ошибка пересчёта порогов: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/api/thresholds/active")
async def get_active_thresholds(underlying: str):
    """
    Получить активные пороги для underlying + актуальный DTE-бин из data_store.
    """
    try:
        options = data_store.get_by_underlying(underlying)
        if not options:
            return {
                "status": "not_found",
                "underlying": underlying,
                "message": "Нет данных в data_store"
            }
        dte_bucket = dynamic_thresholds._primary_bucket_from_options(options)
        thresholds = dynamic_thresholds.get_thresholds_for_options(underlying, options)
        dynamic = get_database().get_strategy_thresholds(underlying, dte_bucket) if dte_bucket else {}
        active_type = "dynamic" if dynamic else "static"
        return {
            "status": "ok",
            "underlying": underlying,
            "dte_bucket": dte_bucket,
            "active_type": active_type,
            "thresholds": thresholds,
            "dynamic_thresholds": dynamic,
            "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE)),
        }
    except Exception as e:
        logger.error("Ошибка получения активных порогов: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/api/status")
async def get_application_status():
    """Получить текущее состояние приложения"""
    try:
        # Статус data_store - получаем данные сначала
        all_data = data_store.get_all()
        last_update = None
        if all_data:
            # Находим самый поздний timestamp
            timestamps = [
                data.get('timestamp') 
                for data in all_data.values() 
                if isinstance(data.get('timestamp'), datetime)
            ]
            if timestamps:
                last_update = max(timestamps)
        
        store_stats = {
            "total_options": len(all_data),
            "last_update": format_datetime_local(last_update) if last_update else None
        }
        
        # Статус WebSocket: is_connected, наличие ws и активных символов или данные в store
        ws_ref, is_conn = ws_manager._get_connection_status()
        active_symbols_list = ws_manager.get_active_symbols_copy()
        has_ws_object = ws_ref is not None
        has_active_symbols = len(active_symbols_list) > 0
        has_data_in_store = len(all_data) > 0
        ws_connected = is_conn or (has_ws_object and has_active_symbols) or has_data_in_store

        otm_subscribed_count = 0
        for symbol in active_symbols_list:
            data = all_data.get(symbol)
            if not data:
                continue
            underlying_price = data.get("underlying_price")
            if underlying_price is None or underlying_price <= 0:
                continue
            parts = symbol.split("-")
            if len(parts) < 5:
                continue
            try:
                strike = float(parts[2])
                option_type = parts[3]
            except (ValueError, IndexError):
                continue
            if is_otm(strike, underlying_price, option_type):
                otm_subscribed_count += 1

        ws_status = {
            "connected": ws_connected,
            "active_symbols_count": len(active_symbols_list),
            "otm_subscribed_count": otm_subscribed_count,
            "last_resubscribe": format_datetime_local(last_subscription_refresh) if last_subscription_refresh else None,
            "next_resubscribe": format_datetime_local(next_subscription_run) if next_subscription_run else None,
            "active_symbols": active_symbols_list[:50]
        }
        
        # Общий статус
        return {
            "status": "running",
            "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE)),
            "websocket": ws_status,
            "data_store": store_stats,
            "services": {
                "api_server": "running",
                "websocket_manager": "connected" if ws_connected else "disconnected"
            }
        }
    except Exception as e:
        logger.error("Ошибка получения статуса приложения: %s", e, exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE))
        }


@app.get("/admin/api/stats")
async def get_database_stats():
    """Получить статистику базы данных (количество записей)"""
    try:
        db = get_database()
        stats = db.get_database_statistics()
        # Убеждаемся, что все ожидаемые поля присутствуют
        result = {
            'option_history': stats.get('option_history', 0),
            'underlying_history': stats.get('underlying_history', 0),
            'iv_history': stats.get('iv_history', 0),
            'support_resistance_levels': stats.get('support_resistance_levels', 0),
            'agent_signals': stats.get('agent_signals', 0),  # Используем правильное имя
            'signal_results': stats.get('signal_results', 0),
            'total': stats.get('total', 0),
            'db_size_mb': stats.get('db_size_mb', 0.0),
            'last_update': stats.get('last_update')
        }
        return result
    except Exception as e:
        logger.error("Ошибка получения статистики БД: %s", e, exc_info=True)
        # Возвращаем пустую статистику вместо ошибки, чтобы админ-панель не падала
        return {
            'option_history': 0,
            'underlying_history': 0,
            'iv_history': 0,
            'support_resistance_levels': 0,
            'agent_signals': 0,
            'signal_results': 0,
            'total': 0,
            'db_size_mb': 0.0,
            'last_update': None,
            'error': str(e)
        }


@app.get("/admin/api/services")
async def get_services_status():
    """Получить статус всех сервисов"""
    import requests
    import os
    
    services = {
        "api_server": {
            "status": "running",
            "port": 7000,  # Порт внутри контейнера
            "url": f"http://localhost:8000"  # Порт на хосте
        },
        "monitoring_service": {
            "status": "unknown",
            "port": 8001,
            "url": "http://localhost:8001"
        }
    }
    
    # Проверяем monitoring service - внутри Docker используем имя сервиса
    try:
        # Сначала пробуем через имя сервиса (внутри Docker сети)
        monitoring_url = os.getenv("MONITORING_SERVICE_URL", "http://monitoring-service:8001")
        try:
            response = requests.get(f"{monitoring_url}/health", timeout=2)
            if response.status_code == 200:
                services["monitoring_service"]["status"] = "running"
            else:
                services["monitoring_service"]["status"] = "error"
        except requests.exceptions.RequestException:
            # Если не получилось через имя сервиса, пробуем localhost (для внешнего доступа)
            try:
                response = requests.get("http://localhost:8001/health", timeout=2)
                if response.status_code == 200:
                    services["monitoring_service"]["status"] = "running"
                else:
                    services["monitoring_service"]["status"] = "error"
            except requests.exceptions.RequestException as e:
                services["monitoring_service"]["status"] = "unavailable"
                services["monitoring_service"]["error"] = str(e)
    except Exception as e:
        services["monitoring_service"]["status"] = "unavailable"
        services["monitoring_service"]["error"] = str(e)
    
    return services


@app.get("/admin/api/logs")
async def get_logs(limit: int = 100, level: Optional[str] = None, logger_name: Optional[str] = None):
    """Получить логи из буфера"""
    try:
        # Исключаем логи websocket_manager (слишком много)
        logs = log_buffer.get_logs(limit=limit * 2, level=level, logger_name=logger_name)
        # Фильтруем логи websocket_manager (кроме ошибок)
        filtered_logs = [
            log for log in logs 
            if 'websocket_manager' not in log.get('logger', '').lower() 
            or log.get('level') == 'ERROR'
        ]
        # Берем только нужное количество
        filtered_logs = filtered_logs[-limit:] if len(filtered_logs) > limit else filtered_logs
        
        counts = log_buffer.count_by_level()
        return {
            "logs": filtered_logs,
            "total": len(filtered_logs),
            "counts_by_level": counts
        }
    except Exception as e:
        logger.error("Ошибка получения логов: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/admin/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket для потоковой передачи логов в реальном времени"""
    await websocket.accept()
    logger.info("WebSocket для логов: подключение установлено")
    
    try:
        # Отправляем последние 100 логов при подключении (исключая websocket_manager)
        all_logs = log_buffer.get_logs(limit=200)  # Берем больше, чтобы после фильтрации было достаточно
        filtered_logs = [
            log for log in all_logs 
            if 'websocket_manager' not in log.get('logger', '').lower() 
            or log.get('level') == 'ERROR'
        ]
        initial_logs = filtered_logs[-100:] if len(filtered_logs) > 100 else filtered_logs
        
        await websocket.send_text(json.dumps({
            "type": "initial",
            "logs": initial_logs
        }, default=str))
        
        # Отслеживаем новые логи (простое решение - опрос каждые 0.5 секунды)
        last_logs_count = len(log_buffer.logs)
        heartbeat_counter = 0  # Счетчик для heartbeat (каждые 10 итераций = 5 секунд)
        while True:
            await asyncio.sleep(0.5)  # Проверяем каждые 0.5 секунды
            
            current_logs_count = len(log_buffer.logs)
            if current_logs_count > last_logs_count:
                # Есть новые логи - получаем последние и фильтруем
                new_logs = log_buffer.get_logs(limit=current_logs_count - last_logs_count)
                filtered_new_logs = [
                    log for log in new_logs 
                    if 'websocket_manager' not in log.get('logger', '').lower() 
                    or log.get('level') == 'ERROR'
                ]
                
                if filtered_new_logs:
                    await websocket.send_text(json.dumps({
                        "type": "update",
                        "logs": filtered_new_logs
                    }, default=str))
                
                last_logs_count = current_logs_count
            
            # Периодически отправляем heartbeat (каждые 5 секунд = 10 итераций)
            heartbeat_counter += 1
            if heartbeat_counter >= 10:
                heartbeat_counter = 0
                await websocket.send_text(json.dumps({
                    "type": "heartbeat",
                    "timestamp": format_datetime_local(datetime.now(DISPLAY_TIMEZONE))
                }, default=str))
            
    except WebSocketDisconnect:
        logger.info("WebSocket для логов: подключение закрыто")
    except Exception as e:
        logger.error("Ошибка в WebSocket логов: %s", e, exc_info=True)
        try:
            await websocket.close()
        except:
            pass


if __name__ == "__main__":
    # Используем порт из конфига или дефолтный 7000
    server_port = 7000  # Хардкод порта
    print(f"✓ Starting API server on port {server_port}...", flush=True)
    print(f"✓ Static directory: {static_dir.absolute()}", flush=True)
    print(f"✓ Admin HTML exists: {(static_dir / 'admin.html').exists()}", flush=True)
    logger.info("Запуск API-сервера на порту %s", server_port)
    logger.info("Каталог статики: %s", static_dir.absolute())
    logger.info("Файл админ-панели присутствует: %s", (static_dir / "admin.html").exists())
    try:
        print(f"✓ Starting uvicorn on port {server_port}...", flush=True)
        print(f"✓ Host: 0.0.0.0, Port: {server_port}", flush=True)
        # Используем объект app напрямую - это надежнее чем строка с путем
        uvicorn.run(
            app,  # Используем объект app напрямую
            host="0.0.0.0",
            port=server_port,
            log_level="info",
            access_log=True,
            loop="asyncio"
        )
        # Эта строка не выполнится, так как uvicorn.run() блокирует выполнение
        print("✓ Uvicorn server started successfully", flush=True)
    except Exception as e:
        print(f"✗ Error starting server: {e}", flush=True)
        import traceback
        traceback.print_exc()
        logger.error("Ошибка запуска сервера: %s", e, exc_info=True)
        raise