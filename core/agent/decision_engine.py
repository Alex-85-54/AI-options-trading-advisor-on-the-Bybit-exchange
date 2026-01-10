"""
Decision Engine - движок принятия решений для торгового агента
Собирает данные, выполняет анализ и формирует торговые сигналы
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from config import AGENT_CONFIG
from core.data.database import get_database
from core.data.historical_analyzer import get_historical_analyzer
from core.strategy.iv_filter import get_iv_filter
from core.strategy.greeks_analyzer import get_greeks_analyzer
from core.strategy.anomaly_detector import get_anomaly_detector
from core.agent.trading_agent import get_trading_agent

logger = logging.getLogger(__name__)


class DecisionEngine:
    """Движок принятия решений для торгового агента"""
    
    def __init__(self, data_store=None):
        """
        Инициализация Decision Engine
        
        Args:
            data_store: Экземпляр OptionDataStore (если None, нужно передавать данные явно)
        """
        self.data_store = data_store
        self.db = get_database()
        self.analyzer = get_historical_analyzer()
        self.iv_filter = get_iv_filter()
        self.greeks_analyzer = get_greeks_analyzer()
        self.anomaly_detector = get_anomaly_detector()
        self.agent = get_trading_agent()
        
        self.max_expiration_days = AGENT_CONFIG.get("max_expiration_days", 3)
        self.enable_signal_history = AGENT_CONFIG.get("enable_signal_history", True)
    
    def collect_data(self, underlying: str) -> Dict[str, Any]:
        """
        Сбор данных для анализа
        
        Собирает:
        - Текущие данные опционов из data_store
        - Исторические данные из БД
        - Уровни поддержки/сопротивления
        
        Args:
            underlying: Базовый актив (например, 'BTC')
            
        Returns:
            Словарь с собранными данными
        """
        try:
            # Получаем текущие данные опционов
            if self.data_store:
                options_data = self.data_store.get_by_underlying(underlying)
            else:
                options_data = {}
                logger.warning("data_store не предоставлен, используем только исторические данные")
            
            # Фильтруем опционы по максимальной экспирации (если есть данные)
            filtered_options = {}
            underlying_price = None
            
            for symbol, data in options_data.items():
                # Извлекаем цену базового актива
                if underlying_price is None:
                    underlying_price = data.get('underlying_price')
                
                # Парсим символ для проверки экспирации
                parts = symbol.split('-')
                if len(parts) >= 2:
                    # Можно добавить проверку days_to_expiration, но для этого нужна дата экспирации
                    filtered_options[symbol] = data
            
            # Если нет данных в data_store, пытаемся получить из БД
            if not filtered_options:
                logger.info(f"Нет текущих данных для {underlying}, используем только исторические данные")
            
            # Получаем уровни поддержки/сопротивления
            support_resistance = self.db.get_support_resistance_levels(underlying)
            
            # Получаем цену базового актива (из первого опциона или из БД)
            if underlying_price is None and filtered_options:
                first_option = next(iter(filtered_options.values()))
                underlying_price = first_option.get('underlying_price', 0)
            
            result = {
                'underlying': underlying,
                'underlying_price': underlying_price or 0,
                'options_data': filtered_options,
                'options_count': len(filtered_options),
                'support_resistance': support_resistance,
                'timestamp': datetime.now().isoformat()
            }
            
            logger.info(
                f"Собраны данные для {underlying}: {len(filtered_options)} опционов, "
                f"цена={underlying_price}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при сборе данных для {underlying}: {e}", exc_info=True)
            return {
                'underlying': underlying,
                'underlying_price': 0,
                'options_data': {},
                'options_count': 0,
                'support_resistance': {'support': [], 'resistance': []},
                'error': str(e)
            }
    
    def analyze_data(self, collected_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Анализ собранных данных
        
        Выполняет:
        - Анализ IVR для опционов
        - Анализ распределения греков
        - Обнаружение аномалий
        
        Args:
            collected_data: Данные, собранные методом collect_data
            
        Returns:
            Словарь с результатами анализа
        """
        try:
            underlying = collected_data.get('underlying', 'BTC')
            options_data = collected_data.get('options_data', {})
            
            if not options_data:
                logger.warning(f"Нет данных опционов для анализа {underlying}")
                return {
                    'ivr_analysis': {},
                    'greeks_analysis': {},
                    'anomalies': {},
                    'error': 'No options data'
                }
            
            # Анализ IVR для каждого опциона
            # Используем новый подход с похожими опционами
            ivr_analysis = {}
            for symbol, data in options_data.items():
                # Передаем данные опциона для получения текущей IV
                ivr_info = self.iv_filter.get_ivr_info(symbol, option_data=data)
                if ivr_info.get('ivr') is not None:
                    ivr_analysis[symbol] = ivr_info
            
            # Анализ распределения греков
            greeks_analysis = self.greeks_analyzer.analyze_all(
                options_data,
                collected_data.get('underlying_price')
            )
            
            # Обнаружение аномалий
            anomalies = self.anomaly_detector.detect_all_anomalies(options_data)
            
            # Формируем сводку для опционов
            options_summary = self._create_options_summary(options_data, ivr_analysis)
            
            result = {
                'ivr_analysis': ivr_analysis,
                'greeks_analysis': greeks_analysis,
                'anomalies': anomalies,
                'options_summary': options_summary
            }
            
            logger.info(
                f"Анализ завершен для {underlying}: "
                f"IVR для {len(ivr_analysis)} опционов, "
                f"аномалий={anomalies.get('volume_spikes', {}).get('spike_count', 0)}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при анализе данных: {e}", exc_info=True)
            return {
                'ivr_analysis': {},
                'greeks_analysis': {},
                'anomalies': {},
                'error': str(e)
            }
    
    def _create_options_summary(self, options_data: Dict[str, Dict], ivr_analysis: Dict) -> Dict:
        """
        Создать сводку по опционам для LLM
        
        Args:
            options_data: Данные опционов
            ivr_analysis: Анализ IVR
            
        Returns:
            Сводка опционов
        """
        summary = {
            'total_count': len(options_data),
            'call_count': 0,
            'put_count': 0,
            'low_ivr_count': 0,  # IVR < threshold
            'expirations': set(),
            'strikes_range': {'min': None, 'max': None}
        }
        
        strikes = []
        
        for symbol, data in options_data.items():
            # Подсчет Call/Put
            parts = symbol.split('-')
            if len(parts) >= 4:
                option_type = parts[3].upper()
                if option_type == 'C':
                    summary['call_count'] += 1
                elif option_type == 'P':
                    summary['put_count'] += 1
                
                # Экспирация
                if len(parts) >= 2:
                    summary['expirations'].add(parts[1])
                
                # Страйк
                if len(parts) >= 3:
                    try:
                        strike = float(parts[2])
                        strikes.append(strike)
                    except ValueError:
                        pass
            
            # Проверка IVR
            ivr_info = ivr_analysis.get(symbol, {})
            if ivr_info.get('passes'):
                summary['low_ivr_count'] += 1
        
        if strikes:
            summary['strikes_range'] = {
                'min': min(strikes),
                'max': max(strikes)
            }
        
        summary['expirations'] = list(summary['expirations'])
        
        return summary
    
    def make_decision(self, underlying: str) -> Optional[Dict[str, Any]]:
        """
        Принятие решения о торговой позиции
        
        Полный цикл:
        1. Сбор данных
        2. Анализ данных
        3. Запрос к LLM с контекстом
        4. Парсинг ответа
        5. Формирование сигнала
        
        Args:
            underlying: Базовый актив для анализа
            
        Returns:
            Словарь с решением (сигналом) или None, если решение не принято
        """
        try:
            logger.info(f"🤖 Начало принятия решения для {underlying}")
            
            # Проверяем, что агент инициализирован
            if not self.agent:
                logger.error("TradingAgent не инициализирован")
                return None
            
            if not self.agent.client:
                logger.warning("DeepSeek клиент не инициализирован. Проверьте API ключ.")
            
            # Шаг 1: Сбор данных
            logger.debug(f"Сбор данных для {underlying}")
            collected_data = self.collect_data(underlying)
            
            if collected_data.get('error'):
                logger.error(f"Ошибка при сборе данных: {collected_data.get('error')}")
                return None
            
            # Шаг 2: Анализ данных
            analysis_results = self.analyze_data(collected_data)
            
            if analysis_results.get('error'):
                logger.error(f"Ошибка при анализе данных: {analysis_results.get('error')}")
                return None
            
            # Шаг 3: Формируем данные для анализа рынка
            market_data = {
                'underlying': underlying,
                'underlying_price': collected_data.get('underlying_price', 0),
                'options_data': collected_data.get('options_data', {}),
                'ivr_analysis': analysis_results.get('ivr_analysis', {}),
                'greeks_analysis': analysis_results.get('greeks_analysis', {}),
                'anomalies': analysis_results.get('anomalies', {}),
                'support_resistance': collected_data.get('support_resistance', {})
            }
            
            # Шаг 4: Анализ рынка через LLM
            market_analysis = self.agent.analyze_market(market_data)
            
            if market_analysis.get('error'):
                if market_analysis.get('skipped'):
                    logger.info("Анализ рынка пропущен из-за недоступности API. Продолжаем без LLM анализа.")
                else:
                    logger.warning(f"Ошибка при анализе рынка: {market_analysis.get('error')}")
                # Продолжаем, даже если анализ рынка не удался
            
            # Шаг 5: Формируем контекст для принятия решения
            decision_context = {
                'underlying': underlying,
                'underlying_price': collected_data.get('underlying_price', 0),
                'market_analysis': market_analysis,
                'options_summary': analysis_results.get('options_summary', {})
            }
            
            # Шаг 6: Принятие решения через LLM
            decision = self.agent.make_decision(decision_context)
            
            if not decision:
                logger.info(f"Решение не принято для {underlying} (нет подходящих условий)")
                return None
            
            # Шаг 7: Сохранение сигнала в БД (если включено)
            if self.enable_signal_history:
                try:
                    signal_data = {
                        'signal_type': decision.get('signal_type'),
                        'underlying': decision.get('underlying'),
                        'expiration': decision.get('expiration'),
                        'strike_call': decision.get('strike_call'),
                        'strike_put': decision.get('strike_put'),
                        'strike': decision.get('strike'),
                        'reasoning': decision.get('reasoning', ''),
                        'confidence': decision.get('confidence', 0.5),
                        'risk_level': decision.get('risk_level', 'medium'),
                        'agent_version': '1.0'  # Можно сделать настраиваемым
                    }
                    signal_id = self.db.save_signal(signal_data)
                    decision['signal_id'] = signal_id
                    logger.info(f"Сигнал сохранен в БД с ID={signal_id}")
                except Exception as e:
                    logger.error(f"Ошибка при сохранении сигнала в БД: {e}", exc_info=True)
            
            logger.info(
                f"Решение принято для {underlying}: {decision.get('signal_type')}, "
                f"уверенность={decision.get('confidence', 0):.2f}"
            )
            
            return decision
            
        except Exception as e:
            logger.error(f"Ошибка при принятии решения для {underlying}: {e}", exc_info=True)
            return None


# Глобальный экземпляр Decision Engine
_engine_instance: Optional[DecisionEngine] = None


def get_decision_engine(data_store=None) -> DecisionEngine:
    """
    Получить глобальный экземпляр DecisionEngine (singleton)
    
    Args:
        data_store: Экземпляр OptionDataStore (опционально)
        
    Returns:
        Экземпляр DecisionEngine
    """
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = DecisionEngine(data_store=data_store)
    return _engine_instance
