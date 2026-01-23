"""
Модуль LLM агента для торговли опционами
Интеграция с DeepSeek API для анализа рынка и принятия решений
"""
import logging
import json
import time
import os
import re
from typing import Dict, List, Optional, Any
from datetime import datetime

try:
    from openai import OpenAI
    from openai import APIError, APIConnectionError, APITimeoutError, RateLimitError
except ImportError:
    OpenAI = None
    APIError = Exception
    APIConnectionError = Exception
    APITimeoutError = Exception
    RateLimitError = Exception
    logging.warning("openai library not installed. Install with: pip install openai")

from config import AGENT_CONFIG, STRATEGY_CONFIG
from core.agent.prompt_templates import (
    MARKET_ANALYSIS_PROMPT,
    DECISION_PROMPT,
    SIGNAL_FORMAT_PROMPT
)

logger = logging.getLogger(__name__)


def _clean_json_response(response: str) -> str:
    """
    Очистить ответ LLM от markdown форматирования и извлечь JSON
    
    Args:
        response: Сырой ответ от LLM
        
    Returns:
        Очищенная строка с JSON
    """
    if not response or not response.strip():
        return ""
    
    cleaned = response.strip()
    
    # Удаляем markdown блоки кода, если они есть
    # Паттерн: ```json ... ``` или ``` ... ```
    json_pattern = r'```(?:json)?\s*(.*?)\s*```'
    match = re.search(json_pattern, cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
        logger.debug("Извлечен JSON из markdown блока")
    
    # Пытаемся найти JSON объект в тексте (на случай если есть пояснительный текст)
    # Ищем первую открывающую скобку { и последнюю закрывающую }
    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        cleaned = cleaned[first_brace:last_brace + 1]
        logger.debug("Извлечен JSON из текста")
    
    return cleaned


class TradingAgent:
    """Класс LLM агента для анализа рынка и принятия торговых решений"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "deepseek-chat"):
        """
        Инициализация торгового агента
        
        Args:
            api_key: API ключ DeepSeek (если None, берется из AGENT_CONFIG)
            model: Модель для использования (по умолчанию "deepseek-chat")
        """
        if OpenAI is None:
            raise ImportError(
                "openai library is required. Install it with: pip install openai"
            )
        
        self.api_key = api_key or AGENT_CONFIG.get("deepseek_api_key", "")
        
        # Убираем кавычки, если они есть (на случай если переменная окружения была в кавычках)
        if self.api_key:
            self.api_key = self.api_key.strip().strip('"').strip("'")
        
        if not self.api_key:
            logger.warning(
                "DeepSeek API key not provided. Agent will not be able to make API calls.\n"
                f"AGENT_CONFIG['deepseek_api_key'] = '{AGENT_CONFIG.get('deepseek_api_key', '')}'\n"
                f"DEEPSEEK_API_KEY env var = '{os.getenv('DEEPSEEK_API_KEY', 'NOT_SET')}'"
            )
        else:
            logger.info(f"DeepSeek API key loaded (length: {len(self.api_key)}, starts with: {self.api_key[:7]}...)")
        
        self.model = model or AGENT_CONFIG.get("deepseek_model", "deepseek-chat")
        self.base_url = AGENT_CONFIG.get("deepseek_base_url", "https://api.deepseek.com")
        
        # Параметры retry и обработки ошибок
        self.retry_attempts = AGENT_CONFIG.get("api_retry_attempts", 3)
        self.retry_delay = AGENT_CONFIG.get("api_retry_delay_seconds", 2)
        self.api_timeout = AGENT_CONFIG.get("api_timeout_seconds", 30)
        self.skip_on_error = AGENT_CONFIG.get("skip_on_api_error", True)
        
        # Инициализируем клиент OpenAI (совместимый с DeepSeek)
        if self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.api_timeout
                )
                logger.info(f"TradingAgent инициализирован с моделью {self.model}")
            except Exception as e:
                logger.error(f"Ошибка при инициализации DeepSeek клиента: {e}")
                self.client = None
        else:
            self.client = None
    
    def _call_llm(self, messages: List[Dict[str, str]], temperature: float = 0.7) -> Optional[str]:
        """
        Вызвать LLM API с retry логикой
        
        Args:
            messages: Список сообщений для промпта
            temperature: Температура генерации (0-1)
            
        Returns:
            Ответ от LLM или None при ошибке
        """
        if not self.client:
            logger.error("DeepSeek клиент не инициализирован. Проверьте API ключ.")
            return None
        
        last_exception = None
        
        # Retry логика с экспоненциальной задержкой
        for attempt in range(1, self.retry_attempts + 1):
            try:
                logger.debug(f"Попытка {attempt}/{self.retry_attempts} вызова DeepSeek API")
                
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    stream=False,
                    timeout=self.api_timeout
                )
                
                if response.choices and len(response.choices) > 0:
                    content = response.choices[0].message.content
                    logger.debug(f"Получен ответ от LLM: {content[:100]}...")
                    if attempt > 1:
                        logger.info(f"✅ Успешный ответ после {attempt} попыток")
                    return content
                else:
                    logger.warning("LLM вернул пустой ответ")
                    return None
                    
            except RateLimitError as e:
                last_exception = e
                # Rate limit - увеличиваем задержку
                wait_time = self.retry_delay * (2 ** (attempt - 1)) * 2  # Удваиваем для rate limit
                logger.warning(
                    f"⚠️ Rate limit при вызове DeepSeek API (попытка {attempt}/{self.retry_attempts}). "
                    f"Ожидание {wait_time} секунд..."
                )
                if attempt < self.retry_attempts:
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Превышен лимит запросов к DeepSeek API после {attempt} попыток")
                    
            except APITimeoutError as e:
                last_exception = e
                wait_time = self.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"⚠️ Таймаут при вызове DeepSeek API (попытка {attempt}/{self.retry_attempts}). "
                    f"Ожидание {wait_time} секунд..."
                )
                if attempt < self.retry_attempts:
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Таймаут DeepSeek API после {attempt} попыток")
                    
            except APIConnectionError as e:
                last_exception = e
                wait_time = self.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"⚠️ Ошибка подключения к DeepSeek API (попытка {attempt}/{self.retry_attempts}). "
                    f"Ожидание {wait_time} секунд..."
                )
                if attempt < self.retry_attempts:
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Не удалось подключиться к DeepSeek API после {attempt} попыток")
                    
            except APIError as e:
                last_exception = e
                # Проверяем код ошибки
                error_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)
                
                # Некоторые ошибки не стоит повторять (например, 400 Bad Request)
                if error_code and error_code in [400, 401, 403]:
                    logger.error(f"❌ Ошибка API (код {error_code}): {e}. Повтор не имеет смысла.")
                    return None
                
                wait_time = self.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"⚠️ Ошибка DeepSeek API (код {error_code}, попытка {attempt}/{self.retry_attempts}): {e}. "
                    f"Ожидание {wait_time} секунд..."
                )
                if attempt < self.retry_attempts:
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Ошибка DeepSeek API после {attempt} попыток: {e}")
                    
            except Exception as e:
                last_exception = e
                wait_time = self.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"⚠️ Неожиданная ошибка при вызове DeepSeek API (попытка {attempt}/{self.retry_attempts}): {e}. "
                    f"Ожидание {wait_time} секунд..."
                )
                if attempt < self.retry_attempts:
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"❌ Критическая ошибка при вызове DeepSeek API после {attempt} попыток: {e}",
                        exc_info=True
                    )
        
        # Все попытки исчерпаны
        logger.error(
            f"❌ Не удалось получить ответ от DeepSeek API после {self.retry_attempts} попыток. "
            f"Последняя ошибка: {last_exception}"
        )
        return None
    
    def analyze_market(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Анализ рынка опционов
        
        Анализирует текущее состояние рынка опционов на основе предоставленных данных:
        - Доска опционов
        - Исторические данные (IV, греки)
        - Анализ стратегий (IVR, распределение греков, аномалии)
        
        Args:
            data: Словарь с данными для анализа:
                - options_data: данные опционов
                - underlying_price: цена базового актива
                - analysis: результаты анализа (IVR, греки, аномалии)
                - support_resistance: уровни поддержки/сопротивления
                
        Returns:
            Словарь с результатами анализа:
                - summary: краткое резюме
                - ivr_analysis: анализ IVR
                - greeks_analysis: анализ греков
                - anomalies: обнаруженные аномалии
                - recommendations: рекомендации от LLM
        """
        try:
            # Формируем промпт для анализа рынка
            prompt = MARKET_ANALYSIS_PROMPT.format(
                underlying=data.get('underlying', 'BTC'),
                underlying_price=data.get('underlying_price', 0),
                options_count=len(data.get('options_data', {})),
                ivr_info=json.dumps(data.get('ivr_analysis', {}), indent=2, ensure_ascii=False),
                greeks_info=json.dumps(data.get('greeks_analysis', {}), indent=2, ensure_ascii=False),
                anomalies_info=json.dumps(data.get('anomalies', {}), indent=2, ensure_ascii=False),
                support_resistance=json.dumps(data.get('support_resistance', {}), indent=2, ensure_ascii=False)
            )
            
            messages = [
                {"role": "system", "content": "Ты - эксперт по торговле опционами. Анализируй данные и давай профессиональные рекомендации."},
                {"role": "user", "content": prompt}
            ]
            
            # Вызываем LLM
            response = self._call_llm(messages, temperature=0.7)
            
            if not response:
                if self.skip_on_error:
                    logger.warning("⚠️ Пропускаем анализ рынка из-за ошибки API (skip_on_api_error=True)")
                    return {
                        'summary': 'Анализ рынка пропущен: DeepSeek API недоступен',
                        'error': 'API unavailable',
                        'skipped': True
                    }
                else:
                    return {
                        'summary': 'Не удалось получить анализ от LLM',
                        'error': 'API call failed'
                    }
            
            # Парсим ответ (может быть JSON или текст)
            try:
                # Пытаемся очистить и распарсить как JSON
                cleaned_response = _clean_json_response(response)
                if cleaned_response:
                    analysis_result = json.loads(cleaned_response)
                else:
                    # Если не удалось извлечь JSON, используем исходный ответ
                    raise json.JSONDecodeError("No JSON found", response, 0)
            except json.JSONDecodeError:
                # Если не JSON, возвращаем как текст
                logger.debug("Ответ LLM не является JSON, возвращаем как текст")
                analysis_result = {
                    'summary': response,
                    'raw_response': response
                }
            
            logger.info(f"Анализ рынка завершен для {data.get('underlying', 'unknown')}")
            
            return {
                'summary': analysis_result.get('summary', response),
                'ivr_analysis': data.get('ivr_analysis', {}),
                'greeks_analysis': data.get('greeks_analysis', {}),
                'anomalies': data.get('anomalies', {}),
                'llm_recommendations': analysis_result,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Ошибка при анализе рынка: {e}", exc_info=True)
            return {
                'summary': f'Ошибка анализа: {str(e)}',
                'error': str(e)
            }
    
    def make_decision(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Принятие решения о торговой позиции
        
        На основе анализа рынка принимает решение о входе в позицию:
        - Стрэнгл (strangle)
        - Стрэддл (straddle)
        - Направленная позиция (call или put)
        
        Args:
            context: Контекст для принятия решения:
                - market_analysis: результаты анализа рынка
                - options_data: данные опционов
                - underlying: базовый актив
                - underlying_price: цена базового актива
                
        Returns:
            Словарь с решением или None:
                - signal_type: 'strangle', 'straddle', 'call', 'put' или None
                - underlying: базовый актив
                - expiration: дата экспирации
                - strike_call, strike_put: страйки для strangle/straddle
                - strike: страйк для направленных
                - reasoning: обоснование решения
                - confidence: уверенность (0-1)
                - risk_level: уровень риска
        """
        try:
            # Получаем порог IVR из конфигурации
            ivr_threshold = STRATEGY_CONFIG.get("ivr_threshold", 50.0)
            
            # Формируем промпт для принятия решения
            prompt = DECISION_PROMPT.format(
                market_analysis=json.dumps(context.get('market_analysis', {}), indent=2, ensure_ascii=False),
                underlying=context.get('underlying', 'BTC'),
                underlying_price=context.get('underlying_price', 0),
                options_summary=json.dumps(context.get('options_summary', {}), indent=2, ensure_ascii=False),
                ivr_threshold=ivr_threshold
            )
            
            messages = [
                {"role": "system", "content": "Ты - эксперт по торговле опционами. Принимай решения на основе анализа данных. Всегда возвращай ответ в формате JSON."},
                {"role": "user", "content": prompt}
            ]
            
            # Вызываем LLM
            response = self._call_llm(messages, temperature=0.5)  # Ниже температура для более детерминированных решений
            
            if not response:
                if self.skip_on_error:
                    logger.warning("⚠️ Пропускаем принятие решения из-за ошибки API (skip_on_api_error=True)")
                    return None
                else:
                    logger.warning("Не удалось получить решение от LLM")
                    return None
            
            # Очищаем и парсим JSON ответ
            try:
                # Проверяем, что ответ не пустой
                if not response or not response.strip():
                    logger.error("LLM вернул пустой ответ")
                    return None
                
                # Очищаем ответ от возможного markdown форматирования
                cleaned_response = _clean_json_response(response)
                
                if not cleaned_response:
                    logger.error("Не удалось извлечь JSON из ответа LLM")
                    logger.error(f"Исходный ответ (первые 500 символов): {response[:500]}")
                    return None
                
                # Логируем очищенный ответ для отладки
                logger.debug(f"Очищенный ответ LLM (первые 500 символов): {cleaned_response[:500]}")
                
                # Парсим JSON
                decision = json.loads(cleaned_response)
                
                # Валидация решения
                signal_type = decision.get('signal_type')
                if signal_type not in ['strangle', 'straddle', 'call', 'put', None]:
                    logger.warning(f"Неизвестный тип сигнала: {signal_type}")
                    return None
                
                # Если signal_type None, значит нет подходящих условий
                if signal_type is None:
                    logger.info("LLM решил, что условий для входа нет")
                    return None
                
                # Формируем полное решение
                result = {
                    'signal_type': signal_type,
                    'underlying': decision.get('underlying', context.get('underlying', 'BTC')),
                    'expiration': decision.get('expiration'),
                    'strike_call': decision.get('strike_call'),
                    'strike_put': decision.get('strike_put'),
                    'strike': decision.get('strike'),
                    'reasoning': decision.get('reasoning', ''),
                    'confidence': decision.get('confidence', 0.5),
                    'risk_level': decision.get('risk_level', 'medium'),
                    'timestamp': datetime.now().isoformat()
                }
                
                # Проверяем минимальную уверенность
                min_confidence = AGENT_CONFIG.get("min_confidence", 0.6)
                if result['confidence'] < min_confidence:
                    logger.info(
                        f"Уверенность {result['confidence']:.2f} ниже порога {min_confidence}, "
                        f"сигнал отклонен"
                    )
                    return None
                
                logger.info(
                    f"Принято решение: {signal_type} для {result['underlying']}, "
                    f"уверенность={result['confidence']:.2f}"
                )
                
                return result
                
            except json.JSONDecodeError as e:
                logger.error(f"Ошибка парсинга JSON ответа от LLM: {e}")
                logger.error(f"Полный ответ LLM (первые 1000 символов): {response[:1000] if response else 'ПУСТОЙ ОТВЕТ'}")
                logger.error(f"Очищенный ответ (первые 1000 символов): {cleaned_response[:1000] if 'cleaned_response' in locals() else 'НЕ ОЧИЩЕН'}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при принятии решения: {e}", exc_info=True)
            return None


# Глобальный экземпляр агента
_agent_instance: Optional[TradingAgent] = None


def get_trading_agent(api_key: Optional[str] = None, model: Optional[str] = None) -> TradingAgent:
    """
    Получить глобальный экземпляр TradingAgent (singleton)
    
    Args:
        api_key: API ключ DeepSeek (опционально, используется только при первом вызове)
        model: Модель для использования (опционально)
        
    Returns:
        Экземпляр TradingAgent
    """
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = TradingAgent(api_key=api_key, model=model)
    return _agent_instance
