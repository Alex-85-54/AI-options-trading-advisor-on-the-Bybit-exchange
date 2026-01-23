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
    format_datetime_local,
    DISPLAY_TIMEZONE,
)
from services.websocket_manager import ws_manager
from services.data_store import data_store
from core.data.database import get_database
from core.data.option_board import get_option_board, is_otm
from utils.logging_config import setup_service_logging
from utils.log_buffer import get_log_buffer_handler
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Настройка логирования с ротацией файлов
print("Setting up logging...", flush=True)
logger = setup_service_logging(service_name="api_server", log_level=logging.INFO)
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
logger.info(f"Static directory: {static_dir.absolute()}")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Настройки подписок и времени (UTC+7)
UNDERLYING_ASSETS = ["BTC"]
SUBSCRIPTION_UPDATE_HOUR = 15
SUBSCRIPTION_UPDATE_MINUTE = 5

# Глобальные объекты
option_board = get_option_board()
subscription_scheduler = BackgroundScheduler(timezone=DISPLAY_TIMEZONE)
last_subscription_refresh: Optional[datetime] = None


def refresh_option_subscriptions() -> Dict[str, int]:
    """
    Переподписка на опционы (обновление списка активных символов).
    Использует доску опционов и переподключает WebSocket.
    """
    try:
        global last_subscription_refresh
        logger.info("🔄 Запуск переподписки на опционы (UTC+7)")
        all_symbols: List[str] = []
        max_days = SUBSCRIPTION_CONFIG.get("max_expiration_days", 3)

        for underlying in UNDERLYING_ASSETS:
            board_data = option_board.get_option_board(underlying, max_days=max_days)
            symbols = board_data.get("symbols", [])
            if symbols:
                all_symbols.extend(symbols)
                logger.info(f"✅ {underlying}: найдено {len(symbols)} символов")
            else:
                logger.warning(f"⚠️ {underlying}: символы не найдены")

        if not all_symbols:
            logger.warning("⚠️ Переподписка пропущена: список символов пуст")
            return {"old_count": len(ws_manager.active_symbols), "new_count": 0}

        # Убираем дубликаты
        unique_symbols = list(set(all_symbols))

        # Обновляем подписки WebSocket
        old_count = len(ws_manager.active_symbols)
        ws_manager.update_subscriptions(unique_symbols)
        new_count = len(ws_manager.active_symbols)
        last_subscription_refresh = datetime.now(DISPLAY_TIMEZONE)
        logger.info(f"✅ Подписки обновлены: было {old_count}, стало {new_count}")
        return {"old_count": old_count, "new_count": new_count}
    except Exception as e:
        logger.error(f"Ошибка при переподписке на опционы: {e}", exc_info=True)
        return {"old_count": len(ws_manager.active_symbols), "new_count": len(ws_manager.active_symbols)}


@app.on_event("startup")
async def startup_event():
    """Запуск планировщика переподписки"""
    if not subscription_scheduler.running:
        subscription_scheduler.add_job(
            refresh_option_subscriptions,
            trigger=CronTrigger(
                hour=SUBSCRIPTION_UPDATE_HOUR,
                minute=SUBSCRIPTION_UPDATE_MINUTE,
                timezone=DISPLAY_TIMEZONE,
            ),
            id="daily_subscription_update",
            replace_existing=True,
        )
        subscription_scheduler.start()
        logger.info(
            f"🕒 Планировщик переподписки запущен (ежедневно в {SUBSCRIPTION_UPDATE_HOUR:02d}:{SUBSCRIPTION_UPDATE_MINUTE:02d} UTC+7)"
        )
    # Первичная подписка при старте, чтобы админ-панель сразу показывала актуальные данные
    refresh_option_subscriptions()


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
        if symbol in ws_manager.active_symbols:
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
        "active_symbols": list(ws_manager.active_symbols)
    }


@app.get("/active/symbols")
async def get_active_symbols():
    """Получить список активных символов"""
    return {
        "active_symbols": list(ws_manager.active_symbols),
        "total": len(ws_manager.active_symbols)
    }


@app.post("/subscriptions/update")
async def update_subscriptions(symbols: List[str]):
    """Обновить подписки WebSocket"""
    try:
        # Получаем текущие активные символы
        current_symbols = list(ws_manager.active_symbols)

        # Формируем новый список (объединение старых и новых)
        all_symbols = list(set(current_symbols + symbols))

        # Обновляем подписки
        ws_manager.update_subscriptions(all_symbols)

        return {
            "status": "updated",
            "old_symbols_count": len(current_symbols),
            "new_symbols_count": len(all_symbols),
            "active_symbols": list(ws_manager.active_symbols)
        }
    except Exception as e:
        logger.error(f"Error updating subscriptions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/subscriptions/active")
async def get_active_subscriptions():
    """Получить активные подписки"""
    return {
        "active_symbols": list(ws_manager.active_symbols),
        "count": len(ws_manager.active_symbols)
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
            "subscribed": symbol in ws_manager.active_symbols,
            "message": "No data available" if symbol in ws_manager.active_symbols else "Not subscribed"
        }


# ==================== Admin Panel Endpoints ====================

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Главная страница админ-панели"""
    admin_html_path = static_dir / "admin.html"
    logger.info(f"Trying to load admin.html from: {admin_html_path.absolute()}")
    logger.info(f"File exists: {admin_html_path.exists()}")
    
    if admin_html_path.exists():
        return FileResponse(admin_html_path)
    else:
        # Если файл не найден, возвращаем простое сообщение
        logger.warning(f"Admin HTML file not found at {admin_html_path.absolute()}")
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
        
        # Статус WebSocket менеджера - проверяем не только is_connected, но и наличие ws и активных символов
        # WebSocket считается подключенным, если:
        # 1. is_connected = True, ИЛИ
        # 2. есть ws объект и активные символы, ИЛИ
        # 3. есть данные в data_store (значит WebSocket работает, даже если статус не обновлен)
        has_ws_object = ws_manager.ws is not None
        has_active_symbols = len(ws_manager.active_symbols) > 0
        has_data_in_store = len(all_data) > 0  # Если есть данные, значит WebSocket работал
        
        ws_connected = ws_manager.is_connected or (has_ws_object and has_active_symbols) or has_data_in_store
        
        # Подсчет подписанных OTM опционов (которые сохраняются в БД)
        otm_subscribed_count = 0
        for symbol in ws_manager.active_symbols:
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
            "active_symbols_count": len(ws_manager.active_symbols),
            "otm_subscribed_count": otm_subscribed_count,
            "last_resubscribe": format_datetime_local(last_subscription_refresh) if last_subscription_refresh else None,
            "active_symbols": list(ws_manager.active_symbols)[:50]  # Первые 50 для просмотра
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
        logger.error(f"Error getting application status: {e}", exc_info=True)
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
        logger.error(f"Error getting database stats: {e}", exc_info=True)
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
        logger.error(f"Error getting logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/admin/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket для потоковой передачи логов в реальном времени"""
    await websocket.accept()
    logger.info("WebSocket connection for logs established")
    
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
        logger.info("WebSocket connection for logs disconnected")
    except Exception as e:
        logger.error(f"Error in WebSocket logs: {e}", exc_info=True)
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
    logger.info(f"Starting API server on port {server_port}...")
    logger.info(f"Static directory: {static_dir.absolute()}")
    logger.info(f"Admin HTML exists: {(static_dir / 'admin.html').exists()}")
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
        logger.error(f"Error starting server: {e}", exc_info=True)
        raise