"""
Модуль для работы с SQLite базой данных истории опционов
"""
import sqlite3
import os
import re
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class OptionDatabase:
    """
    Класс для работы с SQLite базой данных опционов
    
    Структура базы данных:
    
    1. option_history - основная таблица истории опционов
       Поля:
       - symbol: TEXT - символ опциона (например, 'BTC-4JAN26-89000-C-USDT')
       - date_data_collection: DATETIME - дата/время сбора данных (округлено до 5 минут)
       - expiration_date: DATE - дата экспирации опциона
       - underlying_ticker: TEXT - базовый актив ('BTC', 'ETH', 'SOL')
       - days_to_expiration: INTEGER - количество дней до экспирации (вычисляется при сохранении)
       - ask_price, bid_price, mark_price: REAL - цены опциона
       - iv, ask_iv, bid_iv, mark_iv: REAL - implied volatility
       - delta, gamma, vega, theta: REAL - греки опциона
       - volume_24h, open_interest: REAL - объем и открытый интерес
       - underlying_price: REAL - цена базового актива
       
       Индексы:
       - idx_option_history_symbol: по symbol
       - idx_option_history_date_data_collection: по date_data_collection
       - idx_option_history_underlying_expiration: по (underlying_ticker, expiration_date)
       - idx_option_history_days_to_expiration: по days_to_expiration
       - idx_option_history_underlying_days: по (underlying_ticker, days_to_expiration)
    
    2. underlying_history - история цен базовых активов
       Поля:
       - symbol: TEXT - символ базового актива ('BTC', 'ETH')
       - timestamp: DATETIME - временная метка
       - price: REAL - цена актива
       
    3. iv_history - история IV (для быстрого доступа)
       Поля:
       - symbol: TEXT - символ опциона
       - timestamp: DATETIME - временная метка
       - iv: REAL - implied volatility
       - ivr: REAL - IV Rank (может быть вычислен позже)
    
    4. support_resistance_levels - уровни поддержки/сопротивления от пользователя
       Поля:
       - underlying: TEXT - базовый актив
       - level_type: TEXT - 'support' или 'resistance'
       - price: REAL - цена уровня
       - created_at: DATETIME - дата создания
       
    5. agent_signals - история сигналов от агента
       Поля:
       - signal_type: TEXT - тип сигнала ('strangle', 'straddle', 'call', 'put')
       - underlying: TEXT - базовый актив
       - expiration: TEXT - дата экспирации
       - strike_call, strike_put, strike: REAL - страйки
       - reasoning: TEXT - обоснование решения
       - confidence: REAL - уверенность (0-1)
       - risk_level: TEXT - уровень риска
       - created_at: DATETIME - дата создания
       - agent_version: TEXT - версия агента
       
    6. signal_results - результаты сигналов (для анализа эффективности)
       Поля:
       - signal_id: INTEGER - ID сигнала (FK к agent_signals)
       - entry_price, exit_price: REAL - цены входа/выхода
       - pnl: REAL - прибыль/убыток
       - entry_timestamp, exit_timestamp: DATETIME
       - status: TEXT - 'pending', 'entered', 'closed', 'expired'
       - notes: TEXT
       
    Важные особенности:
    - В БД сохраняются ТОЛЬКО OTM (Out of The Money) опционы
    - ITM и ATM опционы не сохраняются
    - Данные округляются до 5-минутных интервалов при сохранении
    - days_to_expiration вычисляется автоматически при сохранении
    - Для анализа похожих опционов используется запрос по (underlying_ticker, days_to_expiration)
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Инициализация базы данных
        
        Args:
            db_path: Путь к файлу базы данных. Если None, используется 'data/options.db' в корне проекта
        """
        if db_path is None:
            # Определяем корень проекта (на 2 уровня выше от core/data/)
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / "data" / "options.db"
        
        self.db_path = Path(db_path)
        # Создаем директорию для БД, если её нет
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Получить подключение к базе данных"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row  # Для доступа к колонкам по имени
        return conn
    
    def _log_sql_query(self, query: str, params: Tuple = None):
        """
        Логировать SQL запрос для отладки
        
        Args:
            query: SQL запрос
            params: Параметры запроса
        """
        if params:
            # Форматируем параметры для логирования (безопасно)
            params_str = ", ".join([str(p) for p in params])
            logger.debug(f"SQL Query: {query} | Params: ({params_str})")
        else:
            logger.debug(f"SQL Query: {query}")
    
    def _round_to_5_minutes(self, dt: datetime) -> datetime:
        """
        Округлить datetime до ближайшего 5-минутного интервала (вниз)
        
        Args:
            dt: Временная метка для округления
            
        Returns:
            Округленная временная метка
        """
        # Округляем минуты вниз до ближайшего значения, кратного 5
        rounded_minute = (dt.minute // 5) * 5
        return dt.replace(minute=rounded_minute, second=0, microsecond=0)
    
    def parse_expiration_date(self, expiry_str: str) -> Optional[date]:
        """
        Парсинг даты экспирации из формата Bybit: "4JAN26" -> date(2026, 1, 8)
        
        Args:
            expiry_str: Строка в формате "4JAN26" (день + месяц + год)
            
        Returns:
            date объект или None если не удалось распарсить
        """
        MONTHS = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
            'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        
        try:
            # Парсим формат: день (1-2 цифры) + месяц (3 буквы) + год (2 цифры)
            match = re.match(r'(\d{1,2})([A-Z]{3})(\d{2})', expiry_str.upper())
            if not match:
                return None
                
            day = int(match.group(1))
            month_str = match.group(2)
            year_short = int(match.group(3))
            
            # Преобразуем год: 26 -> 2026
            year = 2000 + year_short
            
            month = MONTHS.get(month_str)
            if month is None:
                logger.error(f"Неизвестный месяц в дате экспирации: {month_str}")
                return None
                
            return date(year, month, day)
        except Exception as e:
            logger.error(f"Ошибка парсинга даты экспирации {expiry_str}: {e}")
            return None
    
    def parse_option_symbol(self, symbol: str) -> Dict[str, any]:
        """
        Парсинг символа опциона для извлечения компонентов
        
        Args:
            symbol: Символ опциона (например, 'BTC-4JAN26-89000-C-USDT')
            
        Returns:
            Словарь с полями: underlying, expiry, strike, option_type, expiration_date
        """
        parts = symbol.split('-')
        if len(parts) < 5:
            return {}
        
        underlying = parts[0]
        expiry_str = parts[1]
        strike = parts[2]
        option_type = parts[3]
        
        expiration_date = self.parse_expiration_date(expiry_str)
        
        return {
            'underlying': underlying,
            'expiry': expiry_str,
            'strike': strike,
            'option_type': option_type,
            'expiration_date': expiration_date
        }
    
    def _init_database(self):
        """Инициализация базы данных: создание таблиц"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # Таблица истории опционов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS option_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    date_data_collection DATETIME NOT NULL,
                    expiration_date DATE NOT NULL,
                    underlying_ticker TEXT NOT NULL,
                    days_to_expiration INTEGER,
                    ask_price REAL,
                    bid_price REAL,
                    mark_price REAL,
                    iv REAL,
                    ask_iv REAL,
                    bid_iv REAL,
                    mark_iv REAL,
                    delta REAL,
                    gamma REAL,
                    vega REAL,
                    theta REAL,
                    volume_24h REAL,
                    open_interest REAL,
                    underlying_price REAL,
                    UNIQUE(symbol, date_data_collection)
                )
            """)
            
            # Индексы для option_history
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_option_history_symbol 
                ON option_history(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_option_history_date_data_collection 
                ON option_history(date_data_collection)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_option_history_underlying_expiration 
                ON option_history(underlying_ticker, expiration_date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_option_history_days_to_expiration 
                ON option_history(days_to_expiration)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_option_history_underlying_days 
                ON option_history(underlying_ticker, days_to_expiration)
            """)
            
            # Таблица истории базовых активов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS underlying_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    price REAL NOT NULL,
                    UNIQUE(symbol, timestamp)
                )
            """)
            
            # Индексы для underlying_history
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_underlying_history_symbol 
                ON underlying_history(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_underlying_history_timestamp 
                ON underlying_history(timestamp)
            """)
            
            # Таблица истории IV (может быть вычислена из option_history, но для быстрого доступа)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS iv_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    iv REAL NOT NULL,
                    ivr REAL,  -- IV Rank (будет вычисляться позже)
                    UNIQUE(symbol, timestamp)
                )
            """)
            
            # Индексы для iv_history
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_iv_history_symbol 
                ON iv_history(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_iv_history_timestamp 
                ON iv_history(timestamp)
            """)
            
            # Таблица уровней поддержки/сопротивления (от пользователя)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS support_resistance_levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    underlying TEXT NOT NULL,
                    level_type TEXT NOT NULL,  -- 'support' или 'resistance'
                    price REAL NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(underlying, level_type, price)
                )
            """)
            
            # Индекс для support_resistance_levels
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_support_resistance_underlying 
                ON support_resistance_levels(underlying)
            """)
            
            # Таблица истории сигналов от агента
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_type TEXT NOT NULL,  -- 'strangle', 'straddle', 'call', 'put'
                    underlying TEXT NOT NULL,
                    expiration TEXT,
                    strike_call REAL,  -- для strangle/straddle
                    strike_put REAL,   -- для strangle/straddle
                    strike REAL,       -- для направленных
                    reasoning TEXT,
                    confidence REAL,  -- 0-1
                    risk_level TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    agent_version TEXT  -- версия агента/промпта
                )
            """)
            
            # Индексы для agent_signals
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_signals_underlying 
                ON agent_signals(underlying)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_signals_created_at 
                ON agent_signals(created_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_signals_signal_type 
                ON agent_signals(signal_type)
            """)
            
            # Таблица результатов сигналов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    entry_price REAL,      -- цена входа (если был вход)
                    exit_price REAL,       -- цена выхода (если был выход)
                    pnl REAL,              -- прибыль/убыток
                    entry_timestamp DATETIME,
                    exit_timestamp DATETIME,
                    status TEXT,           -- 'pending', 'entered', 'closed', 'expired'
                    notes TEXT,
                    FOREIGN KEY (signal_id) REFERENCES agent_signals(id)
                )
            """)
            
            # Индексы для signal_results
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_results_signal_id 
                ON signal_results(signal_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_results_status 
                ON signal_results(status)
            """)
            
            conn.commit()
            logger.info(f"База данных инициализирована: {self.db_path}")
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при инициализации базы данных: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def save_option_data(self, symbol: str, option_data: Dict, timestamp: Optional[datetime] = None):
        """
        Сохранить данные опциона в базу данных
        
        Args:
            symbol: Символ опциона (например, 'BTC-4JAN26-89000-C-USDT')
            option_data: Словарь с данными опциона:
                - ask_price, bid_price, mark_price
                - ask_iv, bid_iv, mark_iv (или iv)
                - delta, gamma, vega, theta
                - volume_24h, open_interest
                - underlying_price
            timestamp: Временная метка. Если None, используется текущее время.
                     Автоматически округляется до минут, кратных 5.
        """
        if timestamp is None:
            timestamp = datetime.now()
        
        # Округляем timestamp до ближайшего 5-минутного интервала
        date_data_collection = self._round_to_5_minutes(timestamp)
        
        # Парсим символ для извлечения компонентов
        parsed = self.parse_option_symbol(symbol)
        if not parsed or not parsed.get('expiration_date'):
            logger.error(f"Не удалось распарсить символ опциона {symbol}")
            return
        
        expiration_date = parsed['expiration_date']
        underlying_ticker = parsed['underlying']
        
        # Вычисляем days_to_expiration
        collection_date = date_data_collection.date()
        days_to_expiration = (expiration_date - collection_date).days
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # Используем mark_iv как основную IV, если она есть, иначе используем iv
            iv = option_data.get('mark_iv') or option_data.get('iv') or option_data.get('ask_iv') or option_data.get('bid_iv')
            
            cursor.execute("""
                INSERT OR REPLACE INTO option_history (
                    symbol, date_data_collection, expiration_date, underlying_ticker, days_to_expiration,
                    ask_price, bid_price, mark_price,
                    iv, ask_iv, bid_iv, mark_iv,
                    delta, gamma, vega, theta,
                    volume_24h, open_interest, underlying_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol,
                date_data_collection.isoformat(),
                expiration_date.isoformat(),
                underlying_ticker,
                days_to_expiration,
                option_data.get('ask_price'),
                option_data.get('bid_price'),
                option_data.get('mark_price'),
                iv,
                option_data.get('ask_iv'),
                option_data.get('bid_iv'),
                option_data.get('mark_iv'),
                option_data.get('delta'),
                option_data.get('gamma'),
                option_data.get('vega'),
                option_data.get('theta'),
                option_data.get('volume_24h'),
                option_data.get('open_interest'),
                option_data.get('underlying_price')
            ))
            
            # Сохраняем IV в отдельную таблицу для быстрого доступа
            if iv is not None:
                cursor.execute("""
                    INSERT OR REPLACE INTO iv_history (symbol, timestamp, iv)
                    VALUES (?, ?, ?)
                """, (symbol, date_data_collection.isoformat(), iv))
            
            # Сохраняем цену базового актива
            underlying_price = option_data.get('underlying_price')
            if underlying_price is not None:
                cursor.execute("""
                    INSERT OR REPLACE INTO underlying_history (symbol, timestamp, price)
                    VALUES (?, ?, ?)
                """, (underlying_ticker, date_data_collection.isoformat(), underlying_price))
            
            conn.commit()
            logger.debug(f"Сохранены данные опциона {symbol} на {date_data_collection}, expiration={expiration_date}, days={days_to_expiration}")
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при сохранении данных опциона {symbol}: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_historical_greeks(self, symbol: str, days: int = 7) -> List[Dict]:
        """
        Получить историю греков для опциона
        
        Args:
            symbol: Символ опциона
            days: Количество дней истории
            
        Returns:
            Список словарей с данными греков
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days)
            
            query = """
                SELECT date_data_collection, delta, gamma, vega, theta, iv, mark_price
                FROM option_history
                WHERE symbol = ? AND date_data_collection >= ?
                ORDER BY date_data_collection ASC
            """
            params = (symbol, since.isoformat())
            
            self._log_sql_query(query, params)
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении истории греков для {symbol}: {e}")
            raise
        finally:
            conn.close()
    
    def get_iv_statistics(self, symbol: str, days: int = 30) -> Dict:
        """
        Получить статистику IV для опциона (по конкретному символу)
        
        Args:
            symbol: Символ опциона
            days: Количество дней истории
            
        Returns:
            Словарь со статистикой: min, max, mean, current, percentiles
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days)
            
            query = """
                SELECT iv FROM option_history
                WHERE symbol = ? AND date_data_collection >= ? AND iv IS NOT NULL
                ORDER BY date_data_collection ASC
            """
            params = (symbol, since.isoformat())
            
            self._log_sql_query(query, params)
            cursor.execute(query, params)
            
            iv_values = [row[0] for row in cursor.fetchall()]
            
            if not iv_values:
                return {
                    'min': None,
                    'max': None,
                    'mean': None,
                    'current': None,
                    'count': 0
                }
            
            # Вычисляем статистику
            import statistics
            
            current_iv = iv_values[-1] if iv_values else None
            
            result = {
                'min': min(iv_values),
                'max': max(iv_values),
                'mean': statistics.mean(iv_values),
                'median': statistics.median(iv_values),
                'current': current_iv,
                'count': len(iv_values)
            }
            
            # Вычисляем процентили, если есть достаточно данных
            if len(iv_values) >= 10:
                sorted_iv = sorted(iv_values)
                result['p25'] = sorted_iv[int(len(sorted_iv) * 0.25)]
                result['p75'] = sorted_iv[int(len(sorted_iv) * 0.75)]
                result['p90'] = sorted_iv[int(len(sorted_iv) * 0.90)]
                result['p95'] = sorted_iv[int(len(sorted_iv) * 0.95)]
            
            return result
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении статистики IV для {symbol}: {e}")
            raise
        finally:
            conn.close()
    
    def get_iv_statistics_by_similar_options(
        self, 
        underlying_ticker: str, 
        days_to_expiration: int, 
        days: int = 30,
        current_iv: Optional[float] = None
    ) -> Dict:
        """
        Получить статистику IV для похожих опционов
        
        Похожие опционы определяются по:
        - underlying_ticker (например, 'BTC')
        - days_to_expiration (например, 2 для опционов, экспирирующихся через 2 дня)
        
        Этот метод используется когда конкретный тикер может быть новым (недавно созданным на бирже),
        и для него недостаточно исторических данных. Вместо этого анализируются похожие опционы
        с теми же параметрами, которые имеют больше истории.
        
        Args:
            underlying_ticker: Базовый актив (например, 'BTC', 'ETH')
            days_to_expiration: Количество дней до экспирации (например, 1, 2, 3)
            days: Количество дней истории для запроса (по умолчанию 30)
            current_iv: Текущее значение IV для опциона (если None, берется последнее из истории похожих)
            
        Returns:
            Словарь со статистикой: min, max, mean, current, percentiles, count
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days)
            
            # Запрашиваем IV всех похожих опционов (с тем же underlying и days_to_expiration)
            query = """
                SELECT iv, symbol, date_data_collection
                FROM option_history
                WHERE underlying_ticker = ? 
                  AND days_to_expiration = ?
                  AND date_data_collection >= ? 
                  AND iv IS NOT NULL
                ORDER BY date_data_collection ASC
            """
            params = (underlying_ticker, days_to_expiration, since.isoformat())
            
            self._log_sql_query(query, params)
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            iv_values = [row[0] for row in rows]
            
            if not iv_values:
                logger.warning(
                    f"Не найдено исторических данных для похожих опционов: "
                    f"underlying={underlying_ticker}, days_to_exp={days_to_expiration}, days={days}"
                )
                return {
                    'min': None,
                    'max': None,
                    'mean': None,
                    'current': current_iv,
                    'count': 0,
                    'similar_symbols_count': 0
                }
            
            # Вычисляем статистику
            import statistics
            
            # Если current_iv не передан, используем последнее значение из истории похожих опционов
            if current_iv is None:
                current_iv = iv_values[-1]
            
            # Получаем количество уникальных символов в выборке
            unique_symbols = len(set(row[1] for row in rows))
            
            result = {
                'min': min(iv_values),
                'max': max(iv_values),
                'mean': statistics.mean(iv_values),
                'median': statistics.median(iv_values),
                'current': current_iv,
                'count': len(iv_values),
                'similar_symbols_count': unique_symbols  # Сколько разных тикеров использовалось
            }
            
            # Вычисляем процентили, если есть достаточно данных
            if len(iv_values) >= 10:
                sorted_iv = sorted(iv_values)
                result['p25'] = sorted_iv[int(len(sorted_iv) * 0.25)]
                result['p75'] = sorted_iv[int(len(sorted_iv) * 0.75)]
                result['p90'] = sorted_iv[int(len(sorted_iv) * 0.90)]
                result['p95'] = sorted_iv[int(len(sorted_iv) * 0.95)]
            
            logger.info(
                f"Статистика IV для похожих опционов: underlying={underlying_ticker}, "
                f"days_to_exp={days_to_expiration}, записей={len(iv_values)}, "
                f"уникальных_тикеров={unique_symbols}, min={result['min']:.2f}, max={result['max']:.2f}"
            )
            
            return result
            
        except sqlite3.Error as e:
            logger.error(
                f"Ошибка при получении статистики IV для похожих опционов "
                f"(underlying={underlying_ticker}, days_to_exp={days_to_expiration}): {e}"
            )
            raise
        finally:
            conn.close()
    
    def get_underlying_history(self, underlying: str, days: int = 30) -> List[Dict]:
        """
        Получить историю цен базового актива
        
        Args:
            underlying: Символ базового актива (например, 'BTC')
            days: Количество дней истории
            
        Returns:
            Список словарей с данными: timestamp, price
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days)
            
            cursor.execute("""
                SELECT timestamp, price
                FROM underlying_history
                WHERE symbol = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (underlying, since.isoformat()))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении истории базового актива {underlying}: {e}")
            raise
        finally:
            conn.close()
    
    def get_options_by_days_to_expiration(
        self, 
        underlying_ticker: str, 
        days_to_expiration: int,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Получить опционы с определенным количеством дней до экспирации
        
        Args:
            underlying_ticker: Базовый актив (BTC, ETH, SOL)
            days_to_expiration: Количество дней до экспирации (например, 1 для завтра)
            limit: Максимальное количество записей (опционально)
            
        Returns:
            Список словарей с данными опционов
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            query = """
                SELECT * FROM option_history
                WHERE underlying_ticker = ? AND days_to_expiration = ?
                ORDER BY date_data_collection DESC
            """
            params = [underlying_ticker, days_to_expiration]
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            self._log_sql_query(query, tuple(params))
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении опционов по days_to_expiration: {e}")
            raise
        finally:
            conn.close()
    
    def get_options_by_expiration_date(
        self,
        underlying_ticker: str,
        expiration_date: date,
        days_back: int = 30
    ) -> List[Dict]:
        """
        Получить историю опционов с определенной датой экспирации
        
        Args:
            underlying_ticker: Базовый актив
            expiration_date: Дата экспирации
            days_back: Количество дней истории для получения
            
        Returns:
            Список словарей с данными опционов
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days_back)
            
            cursor.execute("""
                SELECT * FROM option_history
                WHERE underlying_ticker = ? 
                  AND expiration_date = ?
                  AND date_data_collection >= ?
                ORDER BY date_data_collection ASC
            """, (underlying_ticker, expiration_date.isoformat(), since.isoformat()))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении опционов по expiration_date: {e}")
            raise
        finally:
            conn.close()
    
    def save_signal(self, signal_data: Dict) -> int:
        """
        Сохранить сигнал от агента
        
        Args:
            signal_data: Словарь с данными сигнала:
                - signal_type: 'strangle', 'straddle', 'call', 'put'
                - underlying: базовый актив
                - expiration: дата экспирации
                - strike_call, strike_put: для strangle/straddle
                - strike: для направленных
                - reasoning: обоснование
                - confidence: уверенность (0-1)
                - risk_level: уровень риска
                - agent_version: версия агента
                
        Returns:
            ID созданного сигнала
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO agent_signals (
                    signal_type, underlying, expiration,
                    strike_call, strike_put, strike,
                    reasoning, confidence, risk_level, agent_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_data.get('signal_type'),
                signal_data.get('underlying'),
                signal_data.get('expiration'),
                signal_data.get('strike_call'),
                signal_data.get('strike_put'),
                signal_data.get('strike'),
                signal_data.get('reasoning'),
                signal_data.get('confidence'),
                signal_data.get('risk_level'),
                signal_data.get('agent_version')
            ))
            
            signal_id = cursor.lastrowid
            conn.commit()
            logger.info(f"Сохранен сигнал агента: ID={signal_id}, type={signal_data.get('signal_type')}")
            return signal_id
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при сохранении сигнала: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_signal_history(self, underlying: Optional[str] = None, days: int = 30) -> List[Dict]:
        """
        Получить историю сигналов
        
        Args:
            underlying: Фильтр по базовому активу (опционально)
            days: Количество дней истории
            
        Returns:
            Список словарей с данными сигналов
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            since = datetime.now() - timedelta(days=days)
            
            if underlying:
                cursor.execute("""
                    SELECT * FROM agent_signals
                    WHERE underlying = ? AND created_at >= ?
                    ORDER BY created_at DESC
                """, (underlying, since.isoformat()))
            else:
                cursor.execute("""
                    SELECT * FROM agent_signals
                    WHERE created_at >= ?
                    ORDER BY created_at DESC
                """, (since.isoformat(),))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении истории сигналов: {e}")
            raise
        finally:
            conn.close()
    
    def add_support_resistance_level(self, underlying: str, level_type: str, price: float):
        """
        Добавить уровень поддержки/сопротивления
        
        Args:
            underlying: Базовый актив
            level_type: 'support' или 'resistance'
            price: Цена уровня
        """
        if level_type not in ['support', 'resistance']:
            raise ValueError(f"level_type должен быть 'support' или 'resistance', получено: {level_type}")
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO support_resistance_levels (underlying, level_type, price)
                VALUES (?, ?, ?)
            """, (underlying, level_type, price))
            
            conn.commit()
            logger.info(f"Добавлен уровень {level_type} для {underlying}: {price}")
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при добавлении уровня: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_support_resistance_levels(self, underlying: str) -> Dict[str, List[float]]:
        """
        Получить уровни поддержки/сопротивления для актива
        
        Args:
            underlying: Базовый актив
            
        Returns:
            Словарь с ключами 'support' и 'resistance', содержащий списки цен
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT level_type, price
                FROM support_resistance_levels
                WHERE underlying = ?
                ORDER BY price ASC
            """, (underlying,))
            
            rows = cursor.fetchall()
            
            result = {'support': [], 'resistance': []}
            for row in rows:
                level_type = row['level_type']
                price = row['price']
                result[level_type].append(price)
            
            return result
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении уровней для {underlying}: {e}")
            raise
        finally:
            conn.close()
    
    def remove_support_resistance_level(self, underlying: str, level_type: str, price: float):
        """
        Удалить уровень поддержки/сопротивления
        
        Args:
            underlying: Базовый актив
            level_type: 'support' или 'resistance'
            price: Цена уровня для удаления
        """
        if level_type not in ['support', 'resistance']:
            raise ValueError(f"level_type должен быть 'support' или 'resistance', получено: {level_type}")
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                DELETE FROM support_resistance_levels
                WHERE underlying = ? AND level_type = ? AND price = ?
            """, (underlying, level_type, price))
            
            conn.commit()
            deleted_count = cursor.rowcount
            
            if deleted_count > 0:
                logger.info(f"Удален уровень {level_type} для {underlying}: {price}")
            else:
                logger.warning(f"Уровень {level_type} для {underlying} с ценой {price} не найден")
            
            return deleted_count > 0
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при удалении уровня: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_all_support_resistance_levels(self) -> Dict[str, Dict[str, List[float]]]:
        """
        Получить все уровни поддержки/сопротивления для всех активов
        
        Returns:
            Словарь {underlying: {'support': [...], 'resistance': [...]}}
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT underlying, level_type, price
                FROM support_resistance_levels
                ORDER BY underlying, price ASC
            """)
            
            rows = cursor.fetchall()
            
            result = {}
            for row in rows:
                underlying = row['underlying']
                level_type = row['level_type']
                price = row['price']
                
                if underlying not in result:
                    result[underlying] = {'support': [], 'resistance': []}
                
                result[underlying][level_type].append(price)
            
            return result
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении всех уровней: {e}")
            raise
        finally:
            conn.close()
    
    def get_database_statistics(self) -> Dict[str, any]:
        """
        Получить статистику базы данных (количество записей в каждой таблице)
        
        Returns:
            Словарь со статистикой:
            {
                'option_history': int,
                'underlying_history': int,
                'iv_history': int,
                'support_resistance_levels': int,
                'agent_signals': int,
                'signal_results': int,
                'total': int,
                'db_size_mb': float,  # Размер файла БД в МБ
                'last_update': str  # ISO формат времени последнего обновления
            }
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            stats = {}
            
            # Подсчет записей в каждой таблице
            tables = [
                'option_history',
                'underlying_history',
                'iv_history',
                'support_resistance_levels',
                'agent_signals',
                'signal_results'
            ]
            
            total = 0
            for table in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) as count FROM {table}")
                    row = cursor.fetchone()
                    count = row['count'] if row else 0
                    stats[table] = count
                    total += count
                except sqlite3.Error:
                    # Таблица может не существовать
                    stats[table] = 0
            
            stats['total'] = total
            
            # Размер файла БД в МБ
            db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
            stats['db_size_mb'] = round(db_size_bytes / (1024 * 1024), 2)
            
            # Последняя запись в option_history
            try:
                cursor.execute("""
                    SELECT MAX(date_data_collection) as last_update 
                    FROM option_history
                """)
                row = cursor.fetchone()
                if row and row['last_update']:
                    # SQLite возвращает строку формата ISO (YYYY-MM-DDTHH:MM:SS)
                    stats['last_update'] = row['last_update']
                else:
                    stats['last_update'] = None
            except sqlite3.Error:
                stats['last_update'] = None
            
            conn.close()
            return stats
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении статистики БД: {e}")
            return {
                'error': str(e),
                'option_history': 0,
                'underlying_history': 0,
                'iv_history': 0,
                'support_resistance_levels': 0,
                'agent_signals': 0,
                'signal_results': 0,
                'total': 0,
                'db_size_mb': 0.0,
                'last_update': None
            }


# Глобальный экземпляр базы данных
_db_instance: Optional[OptionDatabase] = None


def get_database(db_path: Optional[str] = None) -> OptionDatabase:
    """
    Получить глобальный экземпляр базы данных (singleton)
    
    Args:
        db_path: Путь к файлу базы данных (используется только при первом вызове)
        
    Returns:
        Экземпляр OptionDatabase
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = OptionDatabase(db_path)
    return _db_instance
