from pybit.unified_trading import WebSocket
from config import CONFIG
from data_store import data_store
import logging
import json
from typing import List
from datetime import datetime, timedelta
import pandas as pd
import time
from typing import Set

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # Установите DEBUG вместо INFO

# Добавьте handler если еще нет
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

class OptionWebSocketManager:
    """Менеджер WebSocket подключений"""

    def __init__(self):
        self.ws = None
        self.active_symbols = set()
        self.is_connected = False

    def create_option_symbol(
            self,
            underlying: str,
            day: str,
            month: str,
            strike: str,
            option_type: str
    ) -> str:
        """Создать символ опциона в формате Bybit"""
        year = CONFIG["expiration_year"]
        return f"{underlying}-{day}{month}{year}-{strike}-{option_type}-USDT"

    def parse_option_symbol(self, symbol: str) -> dict:
        """Парсинг символа опциона"""
        # Пример: BTC-4JAN26-89000-C-USDT
        parts = symbol.split('-')
        if len(parts) >= 5:
            return {
                'underlying': parts[0],
                'expiry': parts[1],
                'strike': parts[2],
                'option_type': parts[3],
                'settlement': parts[4]
            }
        return {}

    def handle_message(self, message):
        """Обработчик сообщений от WebSocket"""
        try:
            logger.info(f"📨 Received WebSocket message")

            # Если это dict, используем как есть
            if isinstance(message, dict):
                data = message
                logger.debug(f"Message is dict with keys: {data.keys()}")
            # Если это строка, пытаемся распарсить JSON
            elif isinstance(message, (str, bytes, bytearray)):
                try:
                    data = json.loads(message)
                    logger.debug(f"Parsed JSON string to dict with keys: {data.keys()}")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON: {e}")
                    logger.error(f"Message content: {message[:200]}")
                    return
            else:
                logger.error(f"Unknown message type: {type(message)}")
                return

            # Теперь обрабатываем data
            if 'topic' in data and 'data' in data:
                symbol = data['topic'].split('.')[-1]
                logger.info(f"Processing data for symbol: {symbol}")

                # data['data'] - это словарь, а не список!
                data_item = data['data']

                if isinstance(data_item, dict):
                    self._process_single_item(data_item, symbol)
                else:
                    logger.error(f"Unexpected data type in 'data' field: {type(data_item)}")
                    logger.error(f"Data content: {data_item}")

            else:
                logger.warning(f"Unexpected message format. Keys: {data.keys()}")

        except Exception as e:
            logger.error(f"❌ Error in handle_message: {e}", exc_info=True)

    def _process_single_item(self, item, symbol):
        """Обработать один элемент данных"""
        try:
            logger.debug(f"Processing item for {symbol}")

            # Проверяем, что item - словарь
            if not isinstance(item, dict):
                logger.error(f"Item is not a dict: {type(item)} - {item}")
                return

            logger.debug(f"Item keys: {item.keys()}")

            # Преобразуем timestamp (используем ts из корневого объекта, а не из item)
            # ts может быть в корневом объекте, проверим
            ts_value = item.get('ts')  # Пробуем получить из item

            if ts_value:
                ts = pd.to_datetime(ts_value, unit='ms') + timedelta(hours=7)
            else:
                # Если нет в item, используем текущее время
                ts = datetime.now()

            # Извлекаем данные с преобразованием типов
            option_data = {
                'symbol': symbol,
                'timestamp': ts,
                'ask_price': float(item.get('askPrice', 0)),
                'bid_price': float(item.get('bidPrice', 0)),
                'ask_iv': float(item.get('askIv', 0)),
                'bid_iv': float(item.get('bidIv', 0)),
                'mark_price': float(item.get('markPrice', 0)),
                'mark_iv': float(item.get('markPriceIv', 0)),  # Обратите внимание: markPriceIv, а не markIv
                'underlying_price': float(item.get('underlyingPrice', 0)),
                'delta': float(item.get('delta', 0)),
                'gamma': float(item.get('gamma', 0)),
                'vega': float(item.get('vega', 0)),
                'theta': float(item.get('theta', 0)),
                'open_interest': float(item.get('openInterest', 0)),
                'volume_24h': float(item.get('volume24h', 0)),
                'turnover_24h': float(item.get('turnover24h', 0)),
                'bid_size': float(item.get('bidSize', 0)),
                'ask_size': float(item.get('askSize', 0)),
                'high_price_24h': float(item.get('highPrice24h', 0)),
                'low_price_24h': float(item.get('lowPrice24h', 0)),
                'last_price': float(item.get('lastPrice', 0)),
                'index_price': float(item.get('indexPrice', 0))
            }

            # Логируем полученные данные
            logger.info(f"Data for {symbol}: ask={option_data['ask_price']}, bid={option_data['bid_price']}")

            # Сохраняем в хранилище
            data_store.update(symbol, option_data)

            logger.info(
                f"✅ Updated data for {symbol}: ask={option_data['ask_price']:.2f}, bid={option_data['bid_price']:.2f}")

        except KeyError as e:
            logger.error(f"Missing key {e} in item for {symbol}")
            logger.error(f"Available keys: {item.keys()}")
        except ValueError as e:
            logger.error(f"Value error for {symbol}: {e}")
            logger.error(f"Problematic value in item: {item}")
        except Exception as e:
            logger.error(f"Error processing item for {symbol}: {e}", exc_info=True)

    def connect(self, symbols: List[str], wait_for_data: bool = True):
        """Подключиться к WebSocket с указанными символами"""
        if self.ws and self.is_connected:
            # Если уже подключены, добавляем новые символы
            self.add_symbols(symbols)
        else:
            try:
                self.ws = WebSocket(
                    testnet=CONFIG["testnet"],
                    channel_type='option',
                    retries=CONFIG["retries"],
                    restart_on_error=CONFIG["restart_on_error"],
                )

                # Включим логирование pybit для отладки
                import logging as pybit_logging
                pybit_logging.getLogger("pybit").setLevel(logging.DEBUG)

                logger.info(f"Subscribing to symbols: {symbols}")

                self.ws.ticker_stream(
                    symbol=symbols,
                    callback=self.handle_message
                )

                self.active_symbols.update(symbols)
                self.is_connected = True
                logger.info(f"✅ WebSocket connected for {len(symbols)} symbols")

            except Exception as e:
                logger.error(f"❌ Failed to connect WebSocket: {e}")
                raise

        # Ждем получения данных если нужно
        if wait_for_data:
            return self.wait_for_data(symbols)
        return True

    def add_symbols(self, symbols: List[str]):
        """Добавить новые символы для отслеживания"""
        if not self.is_connected:
            return

        new_symbols = [s for s in symbols if s not in self.active_symbols]
        if new_symbols:
            self.ws.ticker_stream(
                symbol=new_symbols,
                callback=self.handle_message
            )
            self.active_symbols.update(new_symbols)
            logger.info(f"Added symbols: {new_symbols}")

    def remove_symbols(self, symbols: List[str]):
        """Убрать символы из отслеживания"""
        # Note: PyBit не поддерживает отписку от отдельных символов
        # Нужно переподключаться с обновленным списком
        pass

    def disconnect(self):
        """Отключить WebSocket"""
        if self.ws:
            self.ws.close()
            self.is_connected = False
            logger.info("WebSocket disconnected")

    def update_subscriptions(self, symbols: List[str]):
        """Обновить список отслеживаемых символов"""
        if not self.is_connected:
            self.connect(symbols)
            return

        # Сравниваем текущие и новые символы
        symbols_set = set(symbols)

        # Если списки одинаковые, ничего не делаем
        if symbols_set == self.active_symbols:
            return

        # Закрываем текущее соединение
        self.disconnect()

        # Переподключаемся с новым списком символов
        time.sleep(1)  # Небольшая пауза
        self.connect(list(symbols_set))
        logger.info(f"WebSocket subscriptions updated. Now tracking: {len(symbols_set)} symbols")

    def wait_for_data(self, symbols: List[str], timeout: int = 30) -> bool:
        """Ожидать получения данных по символам"""
        start_time = time.time()
        missing_symbols = set(symbols)

        logger.info(f"Waiting for data for {len(symbols)} symbols: {symbols}")

        while time.time() - start_time < timeout:
            # Проверяем, какие символы уже есть в data_store
            current_missing = list(missing_symbols)
            for symbol in current_missing:
                data = data_store.get(symbol)
                if data and 'ask_price' in data:
                    # Проверяем, что данные не нулевые
                    if data['ask_price'] > 0 or data['bid_price'] > 0:
                        missing_symbols.remove(symbol)
                        logger.info(
                            f"✓ Data received for {symbol}: ask={data.get('ask_price', 'N/A')}, bid={data.get('bid_price', 'N/A')}")

            if not missing_symbols:
                logger.info("✅ All data received!")
                return True

            # Обновляем лог каждые 5 секунд
            if int(time.time() - start_time) % 5 == 0:
                logger.info(f"⏳ Still waiting for {len(missing_symbols)} symbols")

            time.sleep(1)

        logger.warning(f"⚠️ Timeout waiting for data. Missing: {list(missing_symbols)}")

        # Проверим, что хотя бы у некоторых есть данные
        for symbol in symbols:
            data = data_store.get(symbol)
            if data:
                logger.info(f"Symbol {symbol} has data: {data.get('ask_price', 'N/A')}")

        return False




    # В конце файла добавить функцию для получения всех активных символов
    def get_all_active_symbols():
        """Получить все активные символы от всех пользователей"""
        # Здесь нужно получать символы из Telegram бота
        # Временно возвращаем пустой список
        return []


# Глобальный экземпляр менеджера
ws_manager = OptionWebSocketManager()