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
    
    def check_ivr(
        self,
        symbol: str,
        option_data: Optional[Dict] = None,
        threshold: Optional[float] = None
    ) -> Optional[bool]:
        """
        Проверить, проходит ли опцион фильтр по IVR
        
        Args:
            symbol: Символ опциона
            option_data: Опциональные данные опциона для получения текущей IV
                        (может содержать 'mark_iv', 'iv', 'ask_iv', 'bid_iv')
            
        Returns:
            True если IVR < threshold (подходит для продажи), False если нет, None если недостаточно данных
        """
        try:
            # Извлекаем текущую IV из данных опциона, если они переданы
            current_iv = None
            if option_data:
                current_iv = (
                    option_data.get('mark_iv') or 
                    option_data.get('iv') or 
                    option_data.get('ask_iv') or 
                    option_data.get('bid_iv')
                )
            
            ivr = self.analyzer.calculate_ivr(symbol, current_iv=current_iv)
            
            if ivr is None:
                logger.debug(f"Не удалось вычислить IVR для {symbol}")
                return None
            
            threshold_value = self.ivr_threshold if threshold is None else threshold
            passes = ivr < threshold_value
            
            logger.debug(
                f"IVR фильтр для {symbol}: IVR={ivr:.2f}%, "
                f"threshold={threshold_value}%, passes={passes}"
            )
            
            return passes
            
        except Exception as e:
            logger.error(f"Ошибка при проверке IVR для {symbol}: {e}", exc_info=True)
            return None
    
    def filter_options(self, options_data: Dict[str, Dict], threshold: Optional[float] = None) -> Dict[str, Dict]:
        """
        Отфильтровать опционы по IVR
        
        Args:
            options_data: Словарь с данными опционов {symbol: data}
            
        Returns:
            Словарь с отфильтрованными опционами (только те, у которых IVR < threshold)
        """
        filtered = {}
        
        for symbol, data in options_data.items():
            # Передаем данные опциона для получения текущей IV
            if self.check_ivr(symbol, option_data=data, threshold=threshold):
                filtered[symbol] = data
        
        logger.info(
            f"IVR фильтр: из {len(options_data)} опционов прошло {len(filtered)} "
            f"(threshold={self.ivr_threshold}%)"
        )
        
        return filtered
    
    def get_ivr_info(
        self,
        symbol: str,
        option_data: Optional[Dict] = None,
        threshold: Optional[float] = None
    ) -> Dict:
        """
        Получить информацию об IVR для опциона
        
        Args:
            symbol: Символ опциона
            option_data: Опциональные данные опциона для получения текущей IV
            
        Returns:
            Словарь с информацией: ivr, threshold, passes, message
        """
        # Извлекаем текущую IV из данных опциона, если они переданы
        current_iv = None
        if option_data:
            current_iv = (
                option_data.get('mark_iv') or 
                option_data.get('iv') or 
                option_data.get('ask_iv') or 
                option_data.get('bid_iv')
            )
        
        ivr = self.analyzer.calculate_ivr(symbol, current_iv=current_iv)
        
        if ivr is None:
            return {
                'ivr': None,
                'threshold': self.ivr_threshold,
                'passes': None,
                'message': 'Недостаточно данных для расчета IVR'
            }
        
        threshold_value = self.ivr_threshold if threshold is None else threshold
        passes = ivr < threshold_value
        
        return {
            'ivr': ivr,
            'threshold': threshold_value,
            'passes': passes,
            'message': f"IVR {ivr:.2f}% {'<' if passes else '>='} {threshold_value}%"
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
