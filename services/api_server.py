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

from config import CONFIG, STRATEGY_CONFIG, AGENT_CONFIG, DATA_CONFIG, SUBSCRIPTION_CONFIG, ANALYSIS_CONFIG
from services.websocket_manager import ws_manager
from services.data_store import data_store
from core.data.database import get_database
from utils.logging_config import setup_service_logging
from utils.log_buffer import get_log_buffer_handler
import logging

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
        "timestamp": datetime.now().isoformat(),
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
                "timestamp": datetime.now().isoformat()
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
            age = (datetime.now() - timestamp).total_seconds()
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
        # Статус WebSocket менеджера
        ws_status = {
            "connected": ws_manager.is_connected,
            "active_symbols_count": len(ws_manager.active_symbols),
            "active_symbols": list(ws_manager.active_symbols)[:50]  # Первые 50 для просмотра
        }
        
        # Статус data_store
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
            "last_update": last_update.isoformat() if last_update else None
        }
        
        # Общий статус
        return {
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "websocket": ws_status,
            "data_store": store_stats,
            "services": {
                "api_server": "running",
                "websocket_manager": "connected" if ws_manager.is_connected else "disconnected"
            }
        }
    except Exception as e:
        logger.error(f"Error getting application status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


@app.get("/admin/api/stats")
async def get_database_stats():
    """Получить статистику базы данных (количество записей)"""
    try:
        db = get_database()
        stats = db.get_database_statistics()
        return stats
    except Exception as e:
        logger.error(f"Error getting database stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/api/services")
async def get_services_status():
    """Получить статус всех сервисов"""
    import requests
    import os
    
    services = {
        "api_server": {
            "status": "running",
            "port": CONFIG["server_port"],
            "url": f"http://localhost:{CONFIG['server_port']}"
        },
        "monitoring_service": {
            "status": "unknown",
            "port": 8001,
            "url": "http://localhost:8001"
        }
    }
    
    # Проверяем monitoring service
    try:
        monitoring_url = os.getenv("MONITORING_SERVICE_URL", "http://localhost:8001")
        response = requests.get(f"{monitoring_url}/health", timeout=2)
        if response.status_code == 200:
            services["monitoring_service"]["status"] = "running"
        else:
            services["monitoring_service"]["status"] = "error"
    except Exception as e:
        services["monitoring_service"]["status"] = "unavailable"
        services["monitoring_service"]["error"] = str(e)
    
    return services


@app.get("/admin/api/logs")
async def get_logs(limit: int = 100, level: Optional[str] = None, logger_name: Optional[str] = None):
    """Получить логи из буфера"""
    try:
        logs = log_buffer.get_logs(limit=limit, level=level, logger_name=logger_name)
        counts = log_buffer.count_by_level()
        return {
            "logs": logs,
            "total": len(logs),
            "counts_by_level": counts
        }
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/admin/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket для потоковой передачи логов в реальном времени"""
    await websocket.accept()
    logger.info("WebSocket connection for logs established")
    
    try:
        # Отправляем последние 100 логов при подключении
        initial_logs = log_buffer.get_logs(limit=100)
        await websocket.send_text(json.dumps({
            "type": "initial",
            "logs": initial_logs
        }))
        
        # Отслеживаем новые логи (простое решение - опрос каждые 0.5 секунды)
        last_count = len(log_buffer.logs)
        while True:
            await asyncio.sleep(0.5)  # Проверяем каждые 0.5 секунды
            
            current_count = len(log_buffer.logs)
            if current_count > last_count:
                # Есть новые логи - отправляем последние
                new_logs = log_buffer.get_logs(limit=current_count - last_count)
                await websocket.send_text(json.dumps({
                    "type": "update",
                    "logs": new_logs
                }))
                last_count = current_count
            
            # Периодически отправляем heartbeat
            await websocket.send_text(json.dumps({
                "type": "heartbeat",
                "timestamp": datetime.now().isoformat()
            }))
            
    except WebSocketDisconnect:
        logger.info("WebSocket connection for logs disconnected")
    except Exception as e:
        logger.error(f"Error in WebSocket logs: {e}")
        try:
            await websocket.close()
        except:
            pass


if __name__ == "__main__":
    print(f"✓ Starting API server on port {CONFIG['server_port']}...", flush=True)
    print(f"✓ Static directory: {static_dir.absolute()}", flush=True)
    print(f"✓ Admin HTML exists: {(static_dir / 'admin.html').exists()}", flush=True)
    logger.info(f"Starting API server on {CONFIG['server_port']}...")
    logger.info(f"Static directory: {static_dir.absolute()}")
    logger.info(f"Admin HTML exists: {(static_dir / 'admin.html').exists()}")
    try:
        print(f"✓ Starting uvicorn...", flush=True)
        uvicorn.run(
            "api_server:app",
            host="0.0.0.0",
            port=7000,
            log_level="info"
        )
    except Exception as e:
        print(f"✗ Error starting server: {e}", flush=True)
        logger.error(f"Error starting server: {e}", exc_info=True)
        raise