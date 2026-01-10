"""
Модуль анализа распределения греков
Анализ концентрации гаммы/веги, скью распределения
"""
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import statistics

from config import STRATEGY_CONFIG

logger = logging.getLogger(__name__)


class GreeksAnalyzer:
    """Класс для анализа распределения греков"""
    
    def __init__(self):
        """Инициализация анализатора"""
        self.gamma_threshold = STRATEGY_CONFIG.get("gamma_concentration_threshold", 0.3)
        self.vega_threshold = STRATEGY_CONFIG.get("vega_concentration_threshold", 0.3)
        self.skew_threshold = STRATEGY_CONFIG.get("skew_threshold", 0.1)
    
    def parse_strike(self, symbol: str) -> Optional[float]:
        """
        Извлечь страйк из символа опциона
        
        Args:
            symbol: Символ опциона (например, 'BTC-4JAN26-89000-C-USDT')
            
        Returns:
            Страйк или None
        """
        try:
            parts = symbol.split('-')
            if len(parts) >= 3:
                return float(parts[2])
        except (ValueError, IndexError):
            pass
        return None
    
    def parse_option_type(self, symbol: str) -> Optional[str]:
        """
        Извлечь тип опциона из символа
        
        Args:
            symbol: Символ опциона
            
        Returns:
            'C' для Call, 'P' для Put, или None
        """
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3].upper()
        except IndexError:
            pass
        return None
    
    def analyze_gamma_distribution(self, options_data: Dict[str, Dict]) -> Dict:
        """
        Анализ распределения гаммы по страйкам
        
        Args:
            options_data: Словарь с данными опционов {symbol: data}
            
        Returns:
            Словарь с анализом:
                - total_gamma: общая гамма
                - concentration: концентрация гаммы (доля в центральных страйках)
                - max_gamma_strike: страйк с максимальной гаммой
                - is_concentrated: True если концентрация выше порога
        """
        try:
            # Группируем гамму по страйкам
            gamma_by_strike: Dict[float, float] = defaultdict(float)
            total_gamma = 0.0
            
            for symbol, data in options_data.items():
                gamma = data.get('gamma', 0.0)
                if gamma is None or gamma <= 0:
                    continue
                
                strike = self.parse_strike(symbol)
                if strike is None:
                    continue
                
                # Суммируем гамму по страйкам (Call и Put вместе)
                gamma_by_strike[strike] += abs(gamma)
                total_gamma += abs(gamma)
            
            if total_gamma == 0:
                return {
                    'total_gamma': 0,
                    'concentration': 0,
                    'max_gamma_strike': None,
                    'is_concentrated': False,
                    'message': 'Нет данных по гамме'
                }
            
            # Находим страйк с максимальной гаммой
            max_gamma_strike = max(gamma_by_strike.items(), key=lambda x: x[1])[0] if gamma_by_strike else None
            
            # Вычисляем концентрацию: доля гаммы в ±2 страйках от максимального
            if max_gamma_strike is not None:
                # Получаем все страйки
                strikes = sorted(gamma_by_strike.keys())
                
                # Находим индекс максимального страйка
                try:
                    max_idx = strikes.index(max_gamma_strike)
                    # Берем ±2 страйка вокруг максимума
                    start_idx = max(0, max_idx - 2)
                    end_idx = min(len(strikes), max_idx + 3)
                    concentrated_strikes = strikes[start_idx:end_idx]
                    
                    # Суммируем гамму в этом диапазоне
                    concentrated_gamma = sum(gamma_by_strike[s] for s in concentrated_strikes)
                    concentration = concentrated_gamma / total_gamma if total_gamma > 0 else 0
                except ValueError:
                    concentration = 0
            else:
                concentration = 0
            
            is_concentrated = concentration >= self.gamma_threshold
            
            result = {
                'total_gamma': total_gamma,
                'concentration': concentration,
                'max_gamma_strike': max_gamma_strike,
                'is_concentrated': is_concentrated,
                'threshold': self.gamma_threshold
            }
            
            logger.debug(
                f"Гамма распределение: концентрация={concentration:.2%}, "
                f"макс страйк={max_gamma_strike}, сконцентрирована={is_concentrated}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при анализе распределения гаммы: {e}", exc_info=True)
            return {
                'total_gamma': 0,
                'concentration': 0,
                'max_gamma_strike': None,
                'is_concentrated': False,
                'error': str(e)
            }
    
    def analyze_vega_distribution(self, options_data: Dict[str, Dict]) -> Dict:
        """
        Анализ распределения веги по страйкам
        
        Args:
            options_data: Словарь с данными опционов
            
        Returns:
            Словарь с анализом распределения веги (аналогично гамме)
        """
        try:
            vega_by_strike: Dict[float, float] = defaultdict(float)
            total_vega = 0.0
            
            for symbol, data in options_data.items():
                vega = data.get('vega', 0.0)
                if vega is None or vega <= 0:
                    continue
                
                strike = self.parse_strike(symbol)
                if strike is None:
                    continue
                
                vega_by_strike[strike] += abs(vega)
                total_vega += abs(vega)
            
            if total_vega == 0:
                return {
                    'total_vega': 0,
                    'concentration': 0,
                    'max_vega_strike': None,
                    'is_concentrated': False,
                    'message': 'Нет данных по веге'
                }
            
            max_vega_strike = max(vega_by_strike.items(), key=lambda x: x[1])[0] if vega_by_strike else None
            
            if max_vega_strike is not None:
                strikes = sorted(vega_by_strike.keys())
                try:
                    max_idx = strikes.index(max_vega_strike)
                    start_idx = max(0, max_idx - 2)
                    end_idx = min(len(strikes), max_idx + 3)
                    concentrated_strikes = strikes[start_idx:end_idx]
                    
                    concentrated_vega = sum(vega_by_strike[s] for s in concentrated_strikes)
                    concentration = concentrated_vega / total_vega if total_vega > 0 else 0
                except ValueError:
                    concentration = 0
            else:
                concentration = 0
            
            is_concentrated = concentration >= self.vega_threshold
            
            result = {
                'total_vega': total_vega,
                'concentration': concentration,
                'max_vega_strike': max_vega_strike,
                'is_concentrated': is_concentrated,
                'threshold': self.vega_threshold
            }
            
            logger.debug(
                f"Вега распределение: концентрация={concentration:.2%}, "
                f"макс страйк={max_vega_strike}, сконцентрирована={is_concentrated}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при анализе распределения веги: {e}", exc_info=True)
            return {
                'total_vega': 0,
                'concentration': 0,
                'max_vega_strike': None,
                'is_concentrated': False,
                'error': str(e)
            }
    
    def calculate_skew(self, options_data: Dict[str, Dict], underlying_price: Optional[float] = None) -> Dict:
        """
        Вычислить скью (асимметрию) распределения опционов
        
        Скью показывает, есть ли дисбаланс между Call и Put опционами.
        Положительный скью = больше Call опционов, отрицательный = больше Put.
        
        Args:
            options_data: Словарь с данными опционов
            underlying_price: Цена базового актива (для нормализации страйков)
            
        Returns:
            Словарь с анализом скью:
                - skew: значение скью
                - call_count: количество Call опционов
                - put_count: количество Put опционов
                - is_skewed: True если скью превышает порог
        """
        try:
            call_deltas = []
            put_deltas = []
            call_count = 0
            put_count = 0
            
            for symbol, data in options_data.items():
                delta = data.get('delta', 0.0)
                if delta is None:
                    continue
                
                option_type = self.parse_option_type(symbol)
                if option_type == 'C':
                    call_deltas.append(abs(delta))
                    call_count += 1
                elif option_type == 'P':
                    put_deltas.append(abs(delta))
                    put_count += 1
            
            # Вычисляем средние дельты
            avg_call_delta = statistics.mean(call_deltas) if call_deltas else 0
            avg_put_delta = statistics.mean(put_deltas) if put_deltas else 0
            
            # Скью = разница между Call и Put дельтами, нормализованная
            total_delta = avg_call_delta + avg_put_delta
            if total_delta > 0:
                skew = (avg_call_delta - avg_put_delta) / total_delta
            else:
                skew = 0.0
            
            is_skewed = abs(skew) >= self.skew_threshold
            
            result = {
                'skew': skew,
                'call_count': call_count,
                'put_count': put_count,
                'avg_call_delta': avg_call_delta,
                'avg_put_delta': avg_put_delta,
                'is_skewed': is_skewed,
                'threshold': self.skew_threshold
            }
            
            logger.debug(
                f"Скью: {skew:.3f} (Call={call_count}, Put={put_count}), "
                f"асимметрия={'есть' if is_skewed else 'нет'}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при вычислении скью: {e}", exc_info=True)
            return {
                'skew': 0.0,
                'call_count': 0,
                'put_count': 0,
                'is_skewed': False,
                'error': str(e)
            }
    
    def analyze_all(self, options_data: Dict[str, Dict], underlying_price: Optional[float] = None) -> Dict:
        """
        Комплексный анализ распределения греков
        
        Args:
            options_data: Словарь с данными опционов
            underlying_price: Цена базового актива
            
        Returns:
            Словарь со всеми анализами
        """
        return {
            'gamma': self.analyze_gamma_distribution(options_data),
            'vega': self.analyze_vega_distribution(options_data),
            'skew': self.calculate_skew(options_data, underlying_price)
        }


# Глобальный экземпляр анализатора
_analyzer_instance: Optional[GreeksAnalyzer] = None


def get_greeks_analyzer() -> GreeksAnalyzer:
    """
    Получить глобальный экземпляр GreeksAnalyzer (singleton)
    
    Returns:
        Экземпляр GreeksAnalyzer
    """
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = GreeksAnalyzer()
    return _analyzer_instance
