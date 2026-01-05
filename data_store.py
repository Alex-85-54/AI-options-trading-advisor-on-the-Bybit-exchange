from typing import Dict, List, Optional
import pandas as pd
from datetime import datetime
import threading


class OptionDataStore:
    """Хранилище данных об опционах"""

    def __init__(self):
        self._data: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._subscribers: List[callable] = []

    def update(self, symbol: str, data: Dict):
        """Обновить данные по опциону"""
        with self._lock:
            self._data[symbol] = {
                **data,
                'timestamp': datetime.now(),
                'symbol': symbol
            }

            # Уведомляем подписчиков
            for callback in self._subscribers:
                callback(symbol, self._data[symbol])

    def get(self, symbol: str) -> Optional[Dict]:
        """Получить данные по конкретному опциону"""
        with self._lock:
            return self._data.get(symbol)

    def get_all(self) -> Dict[str, Dict]:
        """Получить все данные"""
        with self._lock:
            return self._data.copy()

    def get_by_underlying(self, underlying: str) -> Dict[str, Dict]:
        """Получить опционы по базовому активу"""
        with self._lock:
            return {
                symbol: data for symbol, data in self._data.items()
                if symbol.startswith(underlying)
            }

    def subscribe(self, callback: callable):
        """Подписаться на обновления"""
        with self._lock:
            self._subscribers.append(callback)


data_store = OptionDataStore()