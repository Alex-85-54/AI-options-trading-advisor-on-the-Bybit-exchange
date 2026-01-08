"""
Модуль для работы с доской опционов
Получение списка опционов для подписки через WebSocket
"""
import logging
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Set, Tuple
import re

from pybit.unified_trading import HTTP

from config import CONFIG, SUBSCRIPTION_CONFIG

logger = logging.getLogger(__name__)


def parse_expiration_date(expiry_str: str) -> Optional[date]:
    """
    Парсинг даты экспирации из формата Bybit
    
    Args:
        expiry_str: Строка в формате "4JAN26" (день + месяц + год)
        
    Returns:
        Объект date или None, если не удалось распарсить
        
    Examples:
        "4JAN26" -> date(2026, 1, 4)
        "15FEB26" -> date(2026, 2, 15)
    """
    try:
        # Паттерн: число (день), 3 буквы (месяц), 2 цифры (год)
        match = re.match(r'(\d{1,2})([A-Z]{3})(\d{2})', expiry_str.upper())
        if not match:
            logger.warning(f"Не удалось распарсить дату экспирации: {expiry_str}")
            return None
        
        day_str, month_str, year_str = match.groups()
        day = int(day_str)
        year = 2000 + int(year_str)  # "26" -> 2026
        
        # Маппинг месяцев
        month_map = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        
        month = month_map.get(month_str)
        if month is None:
            logger.warning(f"Неизвестный месяц: {month_str}")
            return None
        
        return date(year, month, day)
        
    except (ValueError, AttributeError) as e:
        logger.error(f"Ошибка при парсинге даты экспирации {expiry_str}: {e}")
        return None


def is_otm(strike: float, underlying_price: float, option_type: str) -> bool:
    """
    Проверка: опцион OTM (Out of The Money)?
    
    Call OTM: strike > underlying_price
    Put OTM: strike < underlying_price
    
    Args:
        strike: Страйк опциона
        underlying_price: Цена базового актива
        option_type: Тип опциона ('C' для Call, 'P' для Put)
        
    Returns:
        True если опцион OTM, False если ITM или ATM
    """
    if option_type.upper() == 'C':
        return strike > underlying_price
    elif option_type.upper() == 'P':
        return strike < underlying_price
    return False


def calculate_days_to_expiration(expiration_date: date, current_date: Optional[date] = None) -> int:
    """
    Вычислить количество дней до экспирации
    
    Args:
        expiration_date: Дата экспирации
        current_date: Текущая дата (если None, используется сегодня)
        
    Returns:
        Количество дней до экспирации (0 если сегодня)
    """
    if current_date is None:
        current_date = date.today()
    
    delta = expiration_date - current_date
    return max(0, delta.days)


class OptionBoard:
    """Класс для работы с доской опционов"""
    
    def __init__(self):
        """Инициализация с HTTP клиентом Bybit"""
        self.http_client = HTTP(
            testnet=CONFIG["testnet"],
            api_key=CONFIG.get("bybit_api_key", ""),
            api_secret=CONFIG.get("bybit_api_secret", ""),
        )
        self.config = SUBSCRIPTION_CONFIG
    
    def get_option_board(self, underlying: str, max_days: int = 3) -> Dict[str, List[str]]:
        """
        Получить доску опционов для подписки
        
        Получает список доступных экспираций с биржи (не более max_days дней),
        определяет страйки для подписки (текущая_цена ± (500 * 7)),
        возвращает список символов опционов для подписки (Call и Put).
        
        Args:
            underlying: Базовый актив (например, 'BTC', 'ETH', 'SOL')
            max_days: Максимальное количество дней до экспирации (по умолчанию 3)
            
        Returns:
            Словарь с ключами:
                - 'symbols': список символов для подписки
                - 'expirations': список экспираций (даты)
                - 'strikes': список страйков
                - 'underlying_price': текущая цена базового актива
        """
        try:
            # Получаем список всех опционов с биржи
            logger.info(f"Получение списка опционов для {underlying}...")
            response = self.http_client.get_instruments_info(category="option")
            
            if response.get("retCode") != 0:
                logger.error(f"Ошибка при получении опционов: {response.get('retMsg')}")
                return {
                    'symbols': [],
                    'expirations': [],
                    'strikes': [],
                    'underlying_price': None
                }
            
            instruments = response.get("result", {}).get("list", [])
            if not instruments:
                logger.warning(f"Не найдено опционов для {underlying}")
                return {
                    'symbols': [],
                    'expirations': [],
                    'strikes': [],
                    'underlying_price': None
                }
            
            # Фильтруем опционы по базовому активу
            underlying_options = [
                inst for inst in instruments
                if inst.get("symbol", "").startswith(underlying + "-")
            ]
            
            if not underlying_options:
                logger.warning(f"Не найдено опционов для базового актива {underlying}")
                return {
                    'symbols': [],
                    'expirations': [],
                    'strikes': [],
                    'underlying_price': None
                }
            
            # Получаем текущую цену базового актива (берем из первого опциона)
            underlying_price = None
            for option in underlying_options:
                price = option.get("underlyingPrice")
                if price and price > 0:
                    underlying_price = float(price)
                    break
            
            if underlying_price is None:
                logger.error(f"Не удалось получить цену базового актива {underlying}")
                return {
                    'symbols': [],
                    'expirations': [],
                    'strikes': [],
                    'underlying_price': None
                }
            
            logger.info(f"Текущая цена {underlying}: {underlying_price}")
            
            # Получаем уникальные экспирации и фильтруем по max_days
            current_date = date.today()
            valid_expirations: Set[str] = set()
            expiration_dates: Dict[str, date] = {}  # expiry_str -> date
            
            for option in underlying_options:
                symbol = option.get("symbol", "")
                # Парсим символ: BTC-4JAN26-89000-C-USDT
                parts = symbol.split("-")
                if len(parts) < 5:
                    continue
                
                expiry_str = parts[1]  # "4JAN26"
                exp_date = parse_expiration_date(expiry_str)
                
                if exp_date is None:
                    continue
                
                days_to_exp = calculate_days_to_expiration(exp_date, current_date)
                
                # Исключаем опционы с экспирацией сегодня (days_to_expiration = 0)
                if self.config.get("skip_today_expiration", True) and days_to_exp == 0:
                    continue
                
                # Фильтруем по max_days
                if days_to_exp > max_days:
                    continue
                
                valid_expirations.add(expiry_str)
                expiration_dates[expiry_str] = exp_date
            
            if not valid_expirations:
                logger.warning(f"Не найдено валидных экспираций для {underlying} (max_days={max_days})")
                return {
                    'symbols': [],
                    'expirations': [],
                    'strikes': [],
                    'underlying_price': underlying_price
                }
            
            logger.info(f"Найдено {len(valid_expirations)} валидных экспираций: {sorted(valid_expirations)}")
            
            # Определяем страйки для подписки: текущая_цена ± (500 * 7)
            strike_step = self.config.get("strike_step_3days", 500)
            strike_steps_count = self.config.get("strike_steps_count", 7)
            
            # Вычисляем диапазон страйков
            min_strike = underlying_price - (strike_step * strike_steps_count)
            max_strike = underlying_price + (strike_step * strike_steps_count)
            
            # Округляем до ближайших страйков
            min_strike = int(min_strike // strike_step) * strike_step
            max_strike = int((max_strike // strike_step) + 1) * strike_step
            
            logger.info(f"Диапазон страйков для подписки: {min_strike} - {max_strike} (шаг: {strike_step})")
            
            # Собираем символы опционов для подписки
            symbols_to_subscribe: List[str] = []
            strikes_set: Set[int] = set()
            
            for option in underlying_options:
                symbol = option.get("symbol", "")
                parts = symbol.split("-")
                
                if len(parts) < 5:
                    continue
                
                expiry_str = parts[1]
                strike_str = parts[2]
                option_type = parts[3]  # 'C' или 'P'
                
                # Проверяем, что экспирация валидна
                if expiry_str not in valid_expirations:
                    continue
                
                try:
                    strike = int(strike_str)
                except ValueError:
                    continue
                
                # Проверяем, что страйк в диапазоне
                if strike < min_strike or strike > max_strike:
                    continue
                
                # Добавляем символ для подписки (Call и Put)
                symbols_to_subscribe.append(symbol)
                strikes_set.add(strike)
            
            # Сортируем страйки
            strikes_list = sorted(strikes_set)
            
            logger.info(
                f"Подготовлено {len(symbols_to_subscribe)} символов для подписки "
                f"({len(valid_expirations)} экспираций, {len(strikes_list)} страйков)"
            )
            
            return {
                'symbols': symbols_to_subscribe,
                'expirations': sorted(valid_expirations),
                'strikes': strikes_list,
                'underlying_price': underlying_price
            }
            
        except Exception as e:
            logger.error(f"Ошибка при получении доски опционов для {underlying}: {e}", exc_info=True)
            return {
                'symbols': [],
                'expirations': [],
                'strikes': [],
                'underlying_price': None
            }
    
    def get_otm_symbols(self, symbols: List[str], underlying_price: float) -> List[str]:
        """
        Фильтровать список символов, оставив только OTM опционы
        
        Args:
            symbols: Список символов опционов
            underlying_price: Цена базового актива
            
        Returns:
            Список символов OTM опционов
        """
        otm_symbols = []
        
        for symbol in symbols:
            parts = symbol.split("-")
            if len(parts) < 5:
                continue
            
            try:
                strike = int(parts[2])
                option_type = parts[3]  # 'C' или 'P'
                
                if is_otm(strike, underlying_price, option_type):
                    otm_symbols.append(symbol)
            except (ValueError, IndexError):
                continue
        
        return otm_symbols


# Глобальный экземпляр
_option_board_instance: Optional[OptionBoard] = None


def get_option_board() -> OptionBoard:
    """
    Получить глобальный экземпляр OptionBoard (singleton)
    
    Returns:
        Экземпляр OptionBoard
    """
    global _option_board_instance
    if _option_board_instance is None:
        _option_board_instance = OptionBoard()
    return _option_board_instance
