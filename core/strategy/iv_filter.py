"""
Модуль фильтрации опционов по IV Rank (IVR)
Фильтрует опционы с низким IVR для стратегий продажи опционов
"""
import logging
from typing import Dict, List, Optional

from core.data.historical_analyzer import get_historical_analyzer
from config import STRATEGY_CONFIG

logger = logging.getLogger(__name__)


class IVFilter:
    """Класс для фильтрации опционов по IV Rank"""
    
    def __init__(self):
        """Инициализация фильтра"""
        self.analyzer = get_historical_analyzer()
        self.ivr_threshold = STRATEGY_CONFIG.get("ivr_threshold", 25.0)
    
    def check_ivr(self, symbol: str) -> Optional[bool]:
        """
        Проверить, проходит ли опцион фильтр по IVR
        
        Args:
            symbol: Символ опциона
            
        Returns:
            True если IVR < threshold (подходит для продажи), False если нет, None если недостаточно данных
        """
        try:
            ivr = self.analyzer.calculate_ivr(symbol)
            
            if ivr is None:
                logger.debug(f"Не удалось вычислить IVR для {symbol}")
                return None
            
            passes = ivr < self.ivr_threshold
            
            logger.debug(
                f"IVR фильтр для {symbol}: IVR={ivr:.2f}%, "
                f"threshold={self.ivr_threshold}%, passes={passes}"
            )
            
            return passes
            
        except Exception as e:
            logger.error(f"Ошибка при проверке IVR для {symbol}: {e}", exc_info=True)
            return None
    
    def filter_options(self, options_data: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Отфильтровать опционы по IVR
        
        Args:
            options_data: Словарь с данными опционов {symbol: data}
            
        Returns:
            Словарь с отфильтрованными опционами (только те, у которых IVR < threshold)
        """
        filtered = {}
        
        for symbol, data in options_data.items():
            if self.check_ivr(symbol):
                filtered[symbol] = data
        
        logger.info(
            f"IVR фильтр: из {len(options_data)} опционов прошло {len(filtered)} "
            f"(threshold={self.ivr_threshold}%)"
        )
        
        return filtered
    
    def get_ivr_info(self, symbol: str) -> Dict:
        """
        Получить информацию об IVR для опциона
        
        Args:
            symbol: Символ опциона
            
        Returns:
            Словарь с информацией: ivr, threshold, passes
        """
        ivr = self.analyzer.calculate_ivr(symbol)
        
        if ivr is None:
            return {
                'ivr': None,
                'threshold': self.ivr_threshold,
                'passes': None,
                'message': 'Недостаточно данных для расчета IVR'
            }
        
        passes = ivr < self.ivr_threshold
        
        return {
            'ivr': ivr,
            'threshold': self.ivr_threshold,
            'passes': passes,
            'message': f"IVR {ivr:.2f}% {'<' if passes else '>='} {self.ivr_threshold}%"
        }


# Глобальный экземпляр фильтра
_filter_instance: Optional[IVFilter] = None


def get_iv_filter() -> IVFilter:
    """
    Получить глобальный экземпляр IVFilter (singleton)
    
    Returns:
        Экземпляр IVFilter
    """
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = IVFilter()
    return _filter_instance
