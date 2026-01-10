"""
Модуль обнаружения аномалий в данных опционов
Всплески объема, дисбаланс дельты
"""
import logging
from typing import Dict, List, Optional
from collections import defaultdict
import statistics

from config import STRATEGY_CONFIG

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """Класс для обнаружения аномалий в данных опционов"""
    
    def __init__(self):
        """Инициализация детектора"""
        self.volume_spike_multiplier = STRATEGY_CONFIG.get("volume_spike_multiplier", 2.0)
        self.delta_imbalance_threshold = STRATEGY_CONFIG.get("delta_imbalance_threshold", 0.2)
    
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
    
    def detect_volume_spikes(self, options_data: Dict[str, Dict]) -> Dict:
        """
        Обнаружить всплески объема
        
        Аномальным считается опцион, у которого объем превышает
        средний объем в N раз (volume_spike_multiplier).
        
        Args:
            options_data: Словарь с данными опционов {symbol: data}
            
        Returns:
            Словарь с результатами:
                - spikes: список символов с аномальным объемом
                - avg_volume: средний объем
                - max_volume: максимальный объем
                - spike_count: количество всплесков
        """
        try:
            volumes = []
            volume_by_symbol = {}
            
            for symbol, data in options_data.items():
                volume = data.get('volume_24h', 0.0)
                if volume is None or volume <= 0:
                    continue
                
                volumes.append(volume)
                volume_by_symbol[symbol] = volume
            
            if not volumes:
                return {
                    'spikes': [],
                    'avg_volume': 0,
                    'max_volume': 0,
                    'spike_count': 0,
                    'message': 'Нет данных по объему'
                }
            
            avg_volume = statistics.mean(volumes)
            max_volume = max(volumes)
            threshold = avg_volume * self.volume_spike_multiplier
            
            # Находим опционы с аномальным объемом
            spikes = [
                symbol for symbol, volume in volume_by_symbol.items()
                if volume >= threshold
            ]
            
            result = {
                'spikes': spikes,
                'avg_volume': avg_volume,
                'max_volume': max_volume,
                'spike_count': len(spikes),
                'threshold': threshold,
                'multiplier': self.volume_spike_multiplier
            }
            
            if spikes:
                logger.info(
                    f"Обнаружено {len(spikes)} всплесков объема: "
                    f"средний={avg_volume:.2f}, порог={threshold:.2f}"
                )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при обнаружении всплесков объема: {e}", exc_info=True)
            return {
                'spikes': [],
                'avg_volume': 0,
                'max_volume': 0,
                'spike_count': 0,
                'error': str(e)
            }
    
    def detect_delta_imbalance(self, options_data: Dict[str, Dict]) -> Dict:
        """
        Обнаружить дисбаланс дельты между Call и Put опционами
        
        Дисбаланс показывает, есть ли перевес в одну сторону (Call или Put).
        Это может указывать на настроения рынка.
        
        Args:
            options_data: Словарь с данными опционов
            
        Returns:
            Словарь с результатами:
                - imbalance: значение дисбаланса (-1 до 1, где 0 = баланс)
                - call_total_delta: суммарная дельта Call опционов
                - put_total_delta: суммарная дельта Put опционов
                - is_imbalanced: True если дисбаланс превышает порог
                - direction: 'call' если перевес Call, 'put' если Put, 'balanced' если баланс
        """
        try:
            call_total_delta = 0.0
            put_total_delta = 0.0
            call_count = 0
            put_count = 0
            
            for symbol, data in options_data.items():
                delta = data.get('delta', 0.0)
                if delta is None:
                    continue
                
                option_type = self.parse_option_type(symbol)
                if option_type == 'C':
                    call_total_delta += abs(delta)
                    call_count += 1
                elif option_type == 'P':
                    put_total_delta += abs(delta)
                    put_count += 1
            
            total_delta = call_total_delta + put_total_delta
            
            if total_delta == 0:
                return {
                    'imbalance': 0.0,
                    'call_total_delta': 0,
                    'put_total_delta': 0,
                    'call_count': call_count,
                    'put_count': put_count,
                    'is_imbalanced': False,
                    'direction': 'balanced',
                    'message': 'Нет данных по дельте'
                }
            
            # Вычисляем дисбаланс: (Call - Put) / Total
            imbalance = (call_total_delta - put_total_delta) / total_delta
            
            # Определяем направление
            if abs(imbalance) < self.delta_imbalance_threshold:
                direction = 'balanced'
            elif imbalance > 0:
                direction = 'call'
            else:
                direction = 'put'
            
            is_imbalanced = abs(imbalance) >= self.delta_imbalance_threshold
            
            result = {
                'imbalance': imbalance,
                'call_total_delta': call_total_delta,
                'put_total_delta': put_total_delta,
                'call_count': call_count,
                'put_count': put_count,
                'is_imbalanced': is_imbalanced,
                'direction': direction,
                'threshold': self.delta_imbalance_threshold
            }
            
            if is_imbalanced:
                logger.info(
                    f"Обнаружен дисбаланс дельты: {imbalance:.3f} "
                    f"(Call={call_total_delta:.2f}, Put={put_total_delta:.2f}), "
                    f"направление={direction}"
                )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при обнаружении дисбаланса дельты: {e}", exc_info=True)
            return {
                'imbalance': 0.0,
                'call_total_delta': 0,
                'put_total_delta': 0,
                'is_imbalanced': False,
                'direction': 'balanced',
                'error': str(e)
            }
    
    def detect_all_anomalies(self, options_data: Dict[str, Dict]) -> Dict:
        """
        Обнаружить все типы аномалий
        
        Args:
            options_data: Словарь с данными опционов
            
        Returns:
            Словарь со всеми обнаруженными аномалиями
        """
        return {
            'volume_spikes': self.detect_volume_spikes(options_data),
            'delta_imbalance': self.detect_delta_imbalance(options_data)
        }


# Глобальный экземпляр детектора
_detector_instance: Optional[AnomalyDetector] = None


def get_anomaly_detector() -> AnomalyDetector:
    """
    Получить глобальный экземпляр AnomalyDetector (singleton)
    
    Returns:
        Экземпляр AnomalyDetector
    """
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = AnomalyDetector()
    return _detector_instance
