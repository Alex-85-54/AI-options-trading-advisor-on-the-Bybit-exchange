"""
Интеграционные тесты для trading_agent.py
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
from core.agent.trading_agent import TradingAgent, get_trading_agent


class TestTradingAgent:
    """Интеграционные тесты для TradingAgent"""
    
    @pytest.fixture
    def mock_openai_client(self):
        """Мок OpenAI клиента"""
        mock_client = MagicMock()
        return mock_client
    
    @pytest.fixture
    def trading_agent(self, mock_openai_client):
        """Создает TradingAgent с моком клиента"""
        with patch('core.agent.trading_agent.OpenAI') as mock_openai:
            mock_openai.return_value = mock_openai_client
            agent = TradingAgent(api_key="test-key", model="deepseek-chat")
            agent.client = mock_openai_client
            return agent
    
    def test_agent_initialization(self):
        """Тест инициализации агента"""
        with patch('core.agent.trading_agent.OpenAI') as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            
            agent = TradingAgent(api_key="test-key")
            
            assert agent.api_key == "test-key"
            assert agent.model == "deepseek-chat"
            assert agent.client is not None
    
    def test_agent_initialization_no_api_key(self):
        """Тест инициализации без API ключа"""
        with patch('core.agent.trading_agent.OpenAI'):
            agent = TradingAgent(api_key="")
            # Агент должен инициализироваться, но client может быть None
            assert agent.api_key == ""
    
    def test_market_analysis_success(self, trading_agent, mock_openai_client):
        """Тест успешного анализа рынка"""
        # Мок ответа от LLM
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "summary": "Рынок показывает низкий IVR",
            "recommendations": ["Рассмотреть стрэнгл"]
        })
        
        mock_openai_client.chat.completions.create.return_value = mock_response
        
        data = {
            "underlying": "BTC",
            "underlying_price": 89000.0,
            "options_data": {
                "BTC-4JAN26-89000-C-USDT": {"ask_price": 100.0}
            },
            "ivr_analysis": {"BTC-4JAN26-89000-C-USDT": {"ivr": 15.0}},
            "greeks_analysis": {},
            "anomalies": {},
            "support_resistance": {"support": [], "resistance": []}
        }
        
        result = trading_agent.analyze_market(data)
        
        assert 'summary' in result
        assert result['summary'] != ""
        assert 'ivr_analysis' in result
        mock_openai_client.chat.completions.create.assert_called_once()
    
    def test_market_analysis_api_error(self, trading_agent, mock_openai_client):
        """Тест анализа рынка при ошибке API"""
        # Мок ошибки API
        mock_openai_client.chat.completions.create.side_effect = Exception("API Error")
        
        data = {
            "underlying": "BTC",
            "underlying_price": 89000.0,
            "options_data": {},
            "ivr_analysis": {},
            "greeks_analysis": {},
            "anomalies": {},
            "support_resistance": {}
        }
        
        # При skip_on_error=True должен вернуть skipped результат
        trading_agent.skip_on_error = True
        result = trading_agent.analyze_market(data)
        
        assert result.get('skipped') is True or 'error' in result
    
    def test_make_decision_success(self, trading_agent, mock_openai_client):
        """Тест принятия решения"""
        # Мок ответа с решением
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "signal_type": "strangle",
            "underlying": "BTC",
            "expiration": "4JAN26",
            "strike_call": 90000,
            "strike_put": 88000,
            "reasoning": "Низкий IVR",
            "confidence": 0.75,
            "risk_level": "medium"
        })
        
        mock_openai_client.chat.completions.create.return_value = mock_response
        
        context = {
            "market_analysis": {"summary": "Анализ рынка"},
            "options_data": {
                "BTC-4JAN26-90000-C-USDT": {"ask_price": 100.0},
                "BTC-4JAN26-88000-P-USDT": {"ask_price": 99.0}
            },
            "underlying": "BTC",
            "underlying_price": 89000.0
        }
        
        result = trading_agent.make_decision(context)
        
        assert result is not None
        assert result.get('signal_type') == "strangle"
        assert result.get('underlying') == "BTC"
        assert result.get('confidence') == 0.75
    
    def test_make_decision_no_signal(self, trading_agent, mock_openai_client):
        """Тест принятия решения без сигнала"""
        # Мок ответа без сигнала
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "signal_type": None,
            "reasoning": "Нет подходящих условий"
        })
        
        mock_openai_client.chat.completions.create.return_value = mock_response
        
        context = {
            "market_analysis": {"summary": "Анализ рынка"},
            "options_data": {},
            "underlying": "BTC",
            "underlying_price": 89000.0
        }
        
        result = trading_agent.make_decision(context)
        
        # Может быть None или словарь с signal_type=None
        assert result is None or result.get('signal_type') is None
    
    def test_response_parsing_json(self, trading_agent, mock_openai_client):
        """Тест парсинга JSON ответа"""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"key": "value"}'
        
        mock_openai_client.chat.completions.create.return_value = mock_response
        
        data = {
            "underlying": "BTC",
            "underlying_price": 89000.0,
            "options_data": {},
            "ivr_analysis": {},
            "greeks_analysis": {},
            "anomalies": {},
            "support_resistance": {}
        }
        
        result = trading_agent.analyze_market(data)
        
        # Должен успешно распарсить JSON
        assert result is not None
    
    def test_response_parsing_text(self, trading_agent, mock_openai_client):
        """Тест парсинга текстового ответа"""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Просто текст без JSON"
        
        mock_openai_client.chat.completions.create.return_value = mock_response
        
        data = {
            "underlying": "BTC",
            "underlying_price": 89000.0,
            "options_data": {},
            "ivr_analysis": {},
            "greeks_analysis": {},
            "anomalies": {},
            "support_resistance": {}
        }
        
        result = trading_agent.analyze_market(data)
        
        # Должен обработать текстовый ответ
        assert result is not None
        assert 'summary' in result or 'raw_response' in result
    
    def test_retry_logic(self, trading_agent, mock_openai_client):
        """Тест логики повторов при ошибках"""
        # Первые две попытки - ошибка, третья - успех
        mock_openai_client.chat.completions.create.side_effect = [
            Exception("Error 1"),
            Exception("Error 2"),
            MagicMock(choices=[MagicMock(message=MagicMock(content='{"result": "ok"}'))])
        ]
        
        trading_agent.retry_attempts = 3
        
        data = {
            "underlying": "BTC",
            "underlying_price": 89000.0,
            "options_data": {},
            "ivr_analysis": {},
            "greeks_analysis": {},
            "anomalies": {},
            "support_resistance": {}
        }
        
        result = trading_agent.analyze_market(data)
        
        # Должен успешно выполниться после повторов
        assert mock_openai_client.chat.completions.create.call_count == 3
    
    def test_get_trading_agent_singleton(self):
        """Тест получения singleton экземпляра"""
        with patch('core.agent.trading_agent.OpenAI'):
            agent1 = get_trading_agent()
            agent2 = get_trading_agent()
            
            # Должны быть одним и тем же экземпляром (singleton)
            assert agent1 is agent2