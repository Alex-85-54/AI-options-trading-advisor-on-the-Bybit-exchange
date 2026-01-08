from typing import Dict, List, Optional
import pandas as pd
from datetime import datetime, timedelta
import threading
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from core.data.database import get_database
from core.data.option_board import is_otm

logger = logging.getLogger(__name__)


class OptionDataStore:
    """Хранилище данных об опционах"""

    def __init__(self):
        self._data: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._subscribers: List[callable] = []
        self._scheduler: Optional[BackgroundScheduler] = None
        self._db = None

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

    def _calculate_next_save_time(self, current_time: Optional[datetime] = None) -> datetime:
        """
        Вычислить следующий момент времени, кратный 5 минутам
        
        Args:
            current_time: Текущее время. Если None, используется datetime.now()
            
        Returns:
            Следующий момент времени, кратный 5 минутам
        """
        if current_time is None:
            current_time = datetime.now()
        
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
                try:
                    return current_time.replace(day=next_day, hour=next_hour, minute=next_minute, second=0, microsecond=0)
                except ValueError:
                    # Если день не существует (например, 32 января), переходим на следующий месяц
                    return (current_time + timedelta(days=1)).replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
            return current_time.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
        
        return current_time.replace(minute=next_minute, second=0, microsecond=0)

    def save_to_database(self):
        """
        Сохранить текущее состояние data_store в БД
        
        Сохраняет только последние актуальные данные для каждого символа.
        Обрабатывает ошибки БД без остановки работы.
        """
        if self._db is None:
            self._db = get_database()
        
        # Получаем все данные (копия для безопасности)
        all_data = self.get_all()
        
        if not all_data:
            logger.debug("Нет данных для сохранения в БД")
            return
        
        saved_count = 0
        error_count = 0
        
        try:
            for symbol, data in all_data.items():
                try:
                    # Фильтрация OTM опционов: сохраняем только OTM опционы
                    underlying_price = data.get('underlying_price')
                    if underlying_price is None or underlying_price <= 0:
                        logger.debug(f"Пропущен {symbol}: нет цены базового актива")
                        continue
                    
                    # Парсим символ для получения страйка и типа опциона
                    parts = symbol.split('-')
                    if len(parts) < 5:
                        logger.debug(f"Пропущен {symbol}: неверный формат символа")
                        continue
                    
                    try:
                        strike = int(parts[2])
                        option_type = parts[3]  # 'C' или 'P'
                    except (ValueError, IndexError):
                        logger.debug(f"Пропущен {symbol}: не удалось извлечь страйк или тип")
                        continue
                    
                    # Проверяем, является ли опцион OTM
                    if not is_otm(strike, underlying_price, option_type):
                        logger.debug(f"Пропущен {symbol}: опцион не OTM (strike={strike}, underlying={underlying_price}, type={option_type})")
                        continue
                    
                    # Подготавливаем данные для сохранения
                    # Убираем служебные поля, которые не нужны в БД
                    option_data = {
                        'ask_price': data.get('ask_price'),
                        'bid_price': data.get('bid_price'),
                        'mark_price': data.get('mark_price'),
                        'ask_iv': data.get('ask_iv'),
                        'bid_iv': data.get('bid_iv'),
                        'mark_iv': data.get('mark_iv'),
                        'iv': data.get('iv'),
                        'delta': data.get('delta'),
                        'gamma': data.get('gamma'),
                        'vega': data.get('vega'),
                        'theta': data.get('theta'),
                        'volume_24h': data.get('volume_24h'),
                        'open_interest': data.get('open_interest'),
                        'underlying_price': underlying_price,
                    }
                    
                    # Используем timestamp из данных или текущее время
                    timestamp = data.get('timestamp')
                    if timestamp is None:
                        timestamp = datetime.now()
                    
                    # Сохраняем в БД (timestamp будет округлен внутри save_option_data)
                    self._db.save_option_data(symbol, option_data, timestamp)
                    saved_count += 1
                    
                except Exception as e:
                    logger.error(f"Ошибка при сохранении данных опциона {symbol} в БД: {e}", exc_info=True)
                    error_count += 1
                    # Продолжаем сохранение других символов
            
            if saved_count > 0:
                logger.info(f"Сохранено {saved_count} опционов в БД" + (f", ошибок: {error_count}" if error_count > 0 else ""))
            elif error_count > 0:
                logger.warning(f"Не удалось сохранить данные в БД: {error_count} ошибок")
                
        except Exception as e:
            logger.error(f"Критическая ошибка при сохранении данных в БД: {e}", exc_info=True)

    def start_periodic_save(self, interval_minutes: int = 5, align_to_interval: bool = True):
        """
        Запустить периодическое сохранение данных в БД
        
        Args:
            interval_minutes: Интервал сохранения в минутах (по умолчанию 5)
            align_to_interval: Выравнивать время сохранения по интервалам (по умолчанию True)
        """
        if self._scheduler is not None and self._scheduler.running:
            logger.warning("Периодическое сохранение уже запущено")
            return
        
        if self._db is None:
            self._db = get_database()
        
        self._scheduler = BackgroundScheduler()
        
        if align_to_interval:
            # Вычисляем время первого сохранения
            next_save_time = self._calculate_next_save_time()
            logger.info(f"Периодическое сохранение запущено. Первое сохранение в {next_save_time}")
            
            # Планируем первое сохранение на вычисленное время
            self._scheduler.add_job(
                self._schedule_periodic_save,
                trigger=DateTrigger(run_date=next_save_time),
                id='first_save',
                replace_existing=True
            )
        else:
            # Простое периодическое сохранение без выравнивания
            self._scheduler.add_job(
                self.save_to_database,
                trigger='interval',
                minutes=interval_minutes,
                id='periodic_save',
                replace_existing=True
            )
            logger.info(f"Периодическое сохранение запущено с интервалом {interval_minutes} минут")
        
        self._scheduler.start()

    def _schedule_periodic_save(self):
        """
        Выполнить сохранение и запланировать следующее
        Используется при выравнивании по интервалам
        """
        # Выполняем сохранение
        self.save_to_database()
        
        # Вычисляем время следующего сохранения
        next_save_time = self._calculate_next_save_time()
        
        # Планируем следующее сохранение
        if self._scheduler is not None:
            self._scheduler.add_job(
                self._schedule_periodic_save,
                trigger=DateTrigger(run_date=next_save_time),
                id='periodic_save',
                replace_existing=True
            )
            logger.debug(f"Следующее сохранение запланировано на {next_save_time}")

    def stop_periodic_save(self):
        """Остановить периодическое сохранение"""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown()
            self._scheduler = None
            logger.info("Периодическое сохранение остановлено")


data_store = OptionDataStore()