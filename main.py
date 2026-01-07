import asyncio
import json
import logging
import sys
import os
from typing import Dict, List, Optional
from datetime import datetime
import websockets
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
from contextlib import asynccontextmanager
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('monitoring_service.log')
    ]
)
logger = logging.getLogger(__name__)

# Базовый URL API берём из переменной окружения (для Docker),
# локально по умолчанию используется localhost.
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


class MonitoringService:
    """Служба мониторинга опционов"""

    def __init__(self):
        self.active_monitors: Dict[str, Dict] = {}  # user_id -> monitoring config
        self.websocket_connections: Dict[str, WebSocket] = {}
        self.running = False

    def start_monitoring_for_user(self, user_id: str, options_list: List[Dict], threshold: float = 0.01):
        """Запустить мониторинг для пользователя"""
        if user_id in self.active_monitors:
            logger.info(f"User {user_id} already has active monitoring")
            return

        monitor_config = {
            'user_id': user_id,
            'options': options_list,
            'threshold': threshold,
            'last_check': datetime.now(),
            'signals': []
        }

        self.active_monitors[user_id] = monitor_config
        logger.info(f"Started monitoring for user {user_id} with {len(options_list)} options")

        # Запускаем поток мониторинга
        thread = threading.Thread(
            target=self._monitor_user_options,
            args=(user_id,),
            daemon=True
        )
        thread.start()

    def stop_monitoring_for_user(self, user_id: str):
        """Остановить мониторинг для пользователя"""
        if user_id in self.active_monitors:
            del self.active_monitors[user_id]
            logger.info(f"Stopped monitoring for user {user_id}")

    def _monitor_user_options(self, user_id: str):
        """Фоновый мониторинг опционов пользователя"""
        check_interval = 5  # секунд

        while self.running and user_id in self.active_monitors:
            try:
                monitor_config = self.active_monitors[user_id]
                options = monitor_config['options']
                threshold = monitor_config['threshold']

                # Получаем текущие данные по всем опционам
                current_prices = {}
                for opt in options:
                    data = self._get_option_data(opt['symbol'])
                    if data and 'ask_price' in data:
                        current_prices[opt['symbol']] = data['ask_price']

                # Ищем пары Call/Put
                call_options = [opt for opt in options if opt['type'] == 'C']
                put_options = [opt for opt in options if opt['type'] == 'P']

                for call_opt in call_options:
                    for put_opt in put_options:
                        call_symbol = call_opt['symbol']
                        put_symbol = put_opt['symbol']

                        if call_symbol in current_prices and put_symbol in current_prices:
                            call_price = current_prices[call_symbol]
                            put_price = current_prices[put_symbol]

                            # Проверяем равенство цен
                            if self._check_price_equality(call_price, put_price, threshold):
                                # Отправляем сигнал
                                signal = {
                                    'timestamp': datetime.now().isoformat(),
                                    'call_symbol': call_symbol,
                                    'put_symbol': put_symbol,
                                    'call_price': call_price,
                                    'put_price': put_price,
                                    'price_diff': abs(call_price - put_price)
                                }

                                # Добавляем в историю сигналов
                                monitor_config['signals'].append(signal)

                                # Отправляем уведомление (здесь нужно интегрировать с Telegram)
                                self._send_notification(user_id, signal)

                # Обновляем время последней проверки
                monitor_config['last_check'] = datetime.now()

                time.sleep(check_interval)

            except Exception as e:
                logger.error(f"Error monitoring user {user_id}: {e}")
                time.sleep(check_interval)

    def _get_option_data(self, symbol: str) -> Optional[Dict]:
        """Получить данные опциона с API сервера"""
        try:
            response = requests.get(f"{API_BASE_URL}/data/{symbol}", timeout=5)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"Error getting data for {symbol}: {e}")
        return None

    def _check_price_equality(self, price1: float, price2: float, threshold: float) -> bool:
        """Проверить равенство цен в пределах порога"""
        if price1 <= 0 or price2 <= 0:
            return False

        price_diff = abs(price1 - price2)
        avg_price = (price1 + price2) / 2

        if avg_price > 0:
            relative_diff = price_diff / avg_price
            return relative_diff < threshold

        return False

    def _send_notification(self, user_id: str, signal: Dict):
        """Отправить уведомление о сигнале"""
        # Здесь будет интеграция с Telegram ботом
        message = (
            f"🚨 СИГНАЛ: Цены опционов сравнялись!\n"
            f"Время: {signal['timestamp']}\n"
            f"Call: {signal['call_symbol']} - {signal['call_price']:.2f}\n"
            f"Put: {signal['put_symbol']} - {signal['put_price']:.2f}\n"
            f"Разница: {signal['price_diff']:.4f}"
        )
        logger.info(f"Signal for user {user_id}: {message}")

        # TODO: Реализовать отправку в Telegram через API или WebSocket

    def get_user_status(self, user_id: str) -> Optional[Dict]:
        """Получить статус мониторинга для пользователя"""
        if user_id in self.active_monitors:
            config = self.active_monitors[user_id]
            return {
                'active': True,
                'options_count': len(config['options']),
                'last_check': config['last_check'].isoformat(),
                'signals_count': len(config['signals'])
            }
        return None

    def start(self):
        """Запустить службу мониторинга"""
        self.running = True
        logger.info("Monitoring service started")

    def stop(self):
        """Остановить службу мониторинга"""
        self.running = False
        logger.info("Monitoring service stopped")


# Глобальный экземпляр службы
monitoring_service = MonitoringService()

# FastAPI для управления службой
app = FastAPI(title="Option Monitoring Service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    # Запускаем службу при старте
    monitoring_service.start()
    yield
    # Останавливаем службу при завершении
    monitoring_service.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "Option Monitoring Service", "status": "running"}


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "active_users": len(monitoring_service.active_monitors)
    }


@app.post("/monitoring/start")
async def start_monitoring(user_data: Dict):
    """Запустить мониторинг для пользователя"""
    try:
        user_id = user_data.get('user_id')
        options = user_data.get('options', [])
        threshold = user_data.get('threshold', 0.01)

        if not user_id or not options:
            return {"error": "Missing user_id or options"}

        monitoring_service.start_monitoring_for_user(user_id, options, threshold)

        return {
            "status": "started",
            "user_id": user_id,
            "options_count": len(options),
            "threshold": threshold
        }
    except Exception as e:
        logger.error(f"Error starting monitoring: {e}")
        return {"error": str(e)}


@app.post("/monitoring/stop")
async def stop_monitoring(user_data: Dict):
    """Остановить мониторинг для пользователя"""
    try:
        user_id = user_data.get('user_id')

        if not user_id:
            return {"error": "Missing user_id"}

        monitoring_service.stop_monitoring_for_user(user_id)

        return {
            "status": "stopped",
            "user_id": user_id
        }
    except Exception as e:
        logger.error(f"Error stopping monitoring: {e}")
        return {"error": str(e)}


@app.get("/monitoring/status/{user_id}")
async def get_monitoring_status(user_id: str):
    """Получить статус мониторинга для пользователя"""
    status = monitoring_service.get_user_status(user_id)

    if status:
        return {
            "user_id": user_id,
            **status
        }
    else:
        return {
            "user_id": user_id,
            "active": False,
            "message": "Monitoring not active for this user"
        }


# WebSocket для реального времени
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket соединение для уведомлений"""
    await websocket.accept()
    monitoring_service.websocket_connections[user_id] = websocket

    try:
        while True:
            # Поддерживаем соединение
            await websocket.receive_text()
    except WebSocketDisconnect:
        if user_id in monitoring_service.websocket_connections:
            del monitoring_service.websocket_connections[user_id]


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001  # Другой порт, чтобы не конфликтовать с api_server.py
    )