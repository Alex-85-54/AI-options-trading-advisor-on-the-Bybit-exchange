"""
Модуль для анализа исторических данных опционов
Анализ IV, греков, трендов для принятия торговых решений
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import statistics

from core.data.database import get_database
from config import ANALYSIS_CONFIG

logger = logging.getLogger(__name__)


class HistoricalAnalyzer:
    """Класс для анализа исторических данных опционов"""
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Инициализация анализатора
        
        Args:
            db_path: Путь к базе данных (опционально)
        """
        self.db = get_database(db_path)
    
    def get_iv_percentiles(self, symbol: str, days: Optional[int] = None) -> Dict:
        """
        Получить процентили IV для опциона за указанный период
        
        Args:
            symbol: Символ опциона (например, 'BTC-4JAN26-89000-C-USDT')
            days: Количество дней истории (по умолчанию из ANALYSIS_CONFIG["iv_analysis_days"])
            
        Returns:
            Словарь с процентилями и статистикой:
                - p25, p50 (median), p75, p90, p95: процентили
                - min, max, mean: минимальное, максимальное, среднее значение
                - current: текущее значение IV
                - count: количество записей
        """
        if days is None:
            days = ANALYSIS_CONFIG.get("iv_analysis_days", 30)
        
        try:
            # Получаем статистику IV из базы данных
            stats = self.db.get_iv_statistics(symbol, days)
            
            if stats.get('count', 0) == 0:
                logger.warning(f"Нет данных по IV для {symbol} за последние {days} дней")
                return {
                    'p25': None,
                    'p50': None,
                    'p75': None,
                    'p90': None,
                    'p95': None,
                    'min': None,
                    'max': None,
                    'mean': None,
                    'current': None,
                    'count': 0
                }
            
            # Формируем результат с процентилями
            result = {
                'min': stats.get('min'),
                'max': stats.get('max'),
                'mean': stats.get('mean'),
                'p50': stats.get('median'),  # Медиана = p50
                'current': stats.get('current'),
                'count': stats.get('count', 0)
            }
            
            # Добавляем процентили, если они были вычислены
            if 'p25' in stats:
                result['p25'] = stats['p25']
            if 'p75' in stats:
                result['p75'] = stats['p75']
            if 'p90' in stats:
                result['p90'] = stats['p90']
            if 'p95' in stats:
                result['p95'] = stats['p95']
            
            logger.debug(
                f"IV процентили для {symbol}: "
                f"min={result.get('min'):.2f}, p25={result.get('p25'):.2f}, "
                f"p50={result.get('p50'):.2f}, p75={result.get('p75'):.2f}, "
                f"max={result.get('max'):.2f}, current={result.get('current'):.2f}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при получении процентилей IV для {symbol}: {e}", exc_info=True)
            return {
                'p25': None,
                'p50': None,
                'p75': None,
                'p90': None,
                'p95': None,
                'min': None,
                'max': None,
                'mean': None,
                'current': None,
                'count': 0
            }
    
    def get_greeks_trend(self, symbol: str, days: Optional[int] = None) -> Dict:
        """
        Получить тренд греков для опциона за указанный период
        
        Анализирует направление изменения греков (delta, gamma, vega, theta)
        за последние N дней и вычисляет тренд (рост/падение/стабильность).
        
        Args:
            symbol: Символ опциона
            days: Количество дней истории (по умолчанию из ANALYSIS_CONFIG["greeks_analysis_days"])
            
        Returns:
            Словарь с трендами для каждого грека:
                - delta: {'trend': 'up'/'down'/'stable', 'change': float, 'change_pct': float}
                - gamma: аналогично
                - vega: аналогично
                - theta: аналогично
                - iv: аналогично
        """
        if days is None:
            days = ANALYSIS_CONFIG.get("greeks_analysis_days", 3)
        
        try:
            # Получаем историю греков из базы данных
            history = self.db.get_historical_greeks(symbol, days)
            
            if not history or len(history) < 2:
                logger.warning(f"Недостаточно данных для анализа тренда греков для {symbol}")
                return {
                    'delta': {'trend': 'unknown', 'change': None, 'change_pct': None},
                    'gamma': {'trend': 'unknown', 'change': None, 'change_pct': None},
                    'vega': {'trend': 'unknown', 'change': None, 'change_pct': None},
                    'theta': {'trend': 'unknown', 'change': None, 'change_pct': None},
                    'iv': {'trend': 'unknown', 'change': None, 'change_pct': None}
                }
            
            # Берем первое и последнее значение для каждого грека
            first = history[0]
            last = history[-1]
            
            def calculate_trend(first_val: Optional[float], last_val: Optional[float], name: str) -> Dict:
                """Вычислить тренд для одного грека"""
                if first_val is None or last_val is None:
                    return {'trend': 'unknown', 'change': None, 'change_pct': None}
                
                change = last_val - first_val
                change_pct = (change / abs(first_val) * 100) if first_val != 0 else 0
                
                # Определяем тренд: если изменение > 5% - значимое, иначе стабильное
                threshold = 0.05  # 5%
                if abs(change_pct) < threshold:
                    trend = 'stable'
                elif change > 0:
                    trend = 'up'
                else:
                    trend = 'down'
                
                return {
                    'trend': trend,
                    'change': change,
                    'change_pct': change_pct,
                    'first_value': first_val,
                    'last_value': last_val
                }
            
            result = {
                'delta': calculate_trend(first.get('delta'), last.get('delta'), 'delta'),
                'gamma': calculate_trend(first.get('gamma'), last.get('gamma'), 'gamma'),
                'vega': calculate_trend(first.get('vega'), last.get('vega'), 'vega'),
                'theta': calculate_trend(first.get('theta'), last.get('theta'), 'theta'),
                'iv': calculate_trend(first.get('iv'), last.get('iv'), 'iv')
            }
            
            logger.debug(
                f"Тренд греков для {symbol} за {days} дней: "
                f"delta={result['delta']['trend']}, gamma={result['gamma']['trend']}, "
                f"vega={result['vega']['trend']}, theta={result['theta']['trend']}, "
                f"iv={result['iv']['trend']}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при получении тренда греков для {symbol}: {e}", exc_info=True)
            return {
                'delta': {'trend': 'unknown', 'change': None, 'change_pct': None},
                'gamma': {'trend': 'unknown', 'change': None, 'change_pct': None},
                'vega': {'trend': 'unknown', 'change': None, 'change_pct': None},
                'theta': {'trend': 'unknown', 'change': None, 'change_pct': None},
                'iv': {'trend': 'unknown', 'change': None, 'change_pct': None}
            }
    
    def calculate_ivr(self, symbol: str, days: Optional[int] = None) -> Optional[float]:
        """
        Вычислить IV Rank (IVR) для опциона
        
        IV Rank показывает, где находится текущая IV относительно исторического диапазона.
        Формула: IVR = (Текущая IV - Минимальная IV) / (Максимальная IV - Минимальная IV) * 100
        
        Args:
            symbol: Символ опциона
            days: Количество дней истории для расчета диапазона (по умолчанию из ANALYSIS_CONFIG["iv_analysis_days"])
            
        Returns:
            IV Rank в процентах (0-100) или None, если недостаточно данных
        """
        if days is None:
            days = ANALYSIS_CONFIG.get("iv_analysis_days", 30)
        
        try:
            # Получаем статистику IV
            stats = self.db.get_iv_statistics(symbol, days)
            
            min_iv = stats.get('min')
            max_iv = stats.get('max')
            current_iv = stats.get('current')
            
            # Проверяем наличие всех необходимых данных
            if min_iv is None or max_iv is None or current_iv is None:
                logger.warning(
                    f"Недостаточно данных для расчета IVR для {symbol}: "
                    f"min={min_iv}, max={max_iv}, current={current_iv}"
                )
                return None
            
            # Проверяем, что диапазон не нулевой
            iv_range = max_iv - min_iv
            if iv_range == 0:
                logger.warning(f"Диапазон IV равен нулю для {symbol}, IVR не может быть вычислен")
                return None
            
            # Вычисляем IV Rank
            ivr = ((current_iv - min_iv) / iv_range) * 100
            
            # Ограничиваем значение в диапазоне 0-100
            ivr = max(0, min(100, ivr))
            
            logger.debug(
                f"IVR для {symbol}: {ivr:.2f}% "
                f"(current={current_iv:.2f}, min={min_iv:.2f}, max={max_iv:.2f})"
            )
            
            return ivr
            
        except Exception as e:
            logger.error(f"Ошибка при расчете IVR для {symbol}: {e}", exc_info=True)
            return None
    
    def get_comprehensive_analysis(self, symbol: str, iv_days: Optional[int] = None, greeks_days: Optional[int] = None) -> Dict:
        """
        Получить комплексный анализ опциона
        
        Объединяет все методы анализа в один результат.
        
        Args:
            symbol: Символ опциона
            iv_days: Количество дней для анализа IV (по умолчанию из ANALYSIS_CONFIG["iv_analysis_days"])
            greeks_days: Количество дней для анализа греков (по умолчанию из ANALYSIS_CONFIG["greeks_analysis_days"])
            
        Returns:
            Словарь с полным анализом:
                - iv_percentiles: процентили IV
                - ivr: IV Rank
                - greeks_trend: тренд греков
        """
        if iv_days is None:
            iv_days = ANALYSIS_CONFIG.get("iv_analysis_days", 30)
        if greeks_days is None:
            greeks_days = ANALYSIS_CONFIG.get("greeks_analysis_days", 3)
        
        return {
            'symbol': symbol,
            'iv_percentiles': self.get_iv_percentiles(symbol, iv_days),
            'ivr': self.calculate_ivr(symbol, iv_days),
            'greeks_trend': self.get_greeks_trend(symbol, greeks_days)
        }


# Глобальный экземпляр анализатора
_analyzer_instance: Optional[HistoricalAnalyzer] = None


def get_historical_analyzer(db_path: Optional[str] = None) -> HistoricalAnalyzer:
    """
    Получить глобальный экземпляр HistoricalAnalyzer (singleton)
    
    Args:
        db_path: Путь к базе данных (используется только при первом вызове)
        
    Returns:
        Экземпляр HistoricalAnalyzer
    """
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = HistoricalAnalyzer(db_path)
    return _analyzer_instance
