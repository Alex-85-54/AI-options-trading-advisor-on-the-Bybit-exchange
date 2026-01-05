import asyncio
import requests
from typing import Dict, List, Tuple
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PriceMonitor:
    def __init__(self, api_url: str = "http://localhost:8000"):
        self.api_url = api_url
        self.active_monitors: Dict[str, asyncio.Task] = {}

    async def monitor_pair(self, call_symbol: str, put_symbol: str, chat_id: int, threshold: float = 0.01):
        """Мониторинг пары опционов на равенство цен"""
        while True:
            try:
                # Получаем данные по опционам
                call_data = requests.get(f"{self.api_url}/data/{call_symbol}").json()
                put_data = requests.get(f"{self.api_url}/data/{put_symbol}").json()

                if call_data and put_data and 'ask_price' in call_data and 'ask_price' in put_data:
                    call_ask = call_data['ask_price']
                    put_ask = put_data['ask_price']

                    if call_ask > 0 and put_ask > 0:
                        price_diff = abs(call_ask - put_ask)
                        avg_price = (call_ask + put_ask) / 2

                        if avg_price > 0 and (price_diff / avg_price) < threshold:
                            message = (
                                f"🚨 СИГНАЛ ДЛЯ СТРЭДДЛА!\n\n"
                                f"Call {call_symbol}: {call_ask:.2f}\n"
                                f"Put {put_symbol}: {put_ask:.2f}\n\n"
                                f"Разница: {price_diff:.4f} ({price_diff / avg_price * 100:.2f}%)\n"
                                f"Время: {datetime.now().strftime('%H:%M:%S')}"
                            )

                            # Здесь отправляем сообщение в Telegram
                            # await telegram_bot.send_message(chat_id, message)
                            logger.info(f"Signal detected: {call_symbol} / {put_symbol}")

                            # После сигнала можно сделать паузу
                            await asyncio.sleep(60)  # 1 минута паузы

                await asyncio.sleep(5)  # Проверка каждые 5 секунд

            except Exception as e:
                logger.error(f"Error monitoring pair: {e}")
                await asyncio.sleep(5)

    def start_monitoring(self, call_symbol: str, put_symbol: str, chat_id: int):
        """Запустить мониторинг пары"""
        monitor_id = f"{call_symbol}_{put_symbol}"

        if monitor_id in self.active_monitors:
            logger.info(f"Monitor {monitor_id} already running")
            return

        task = asyncio.create_task(self.monitor_pair(call_symbol, put_symbol, chat_id))
        self.active_monitors[monitor_id] = task
        logger.info(f"Started monitoring {monitor_id}")

    def stop_monitoring(self, call_symbol: str, put_symbol: str):
        """Остановить мониторинг пары"""
        monitor_id = f"{call_symbol}_{put_symbol}"

        if monitor_id in self.active_monitors:
            self.active_monitors[monitor_id].cancel()
            del self.active_monitors[monitor_id]
            logger.info(f"Stopped monitoring {monitor_id}")


# Глобальный экземпляр монитора
price_monitor = PriceMonitor()