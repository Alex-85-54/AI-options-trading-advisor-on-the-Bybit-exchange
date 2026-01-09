"""
Модули LLM агента для торговли опционами
"""
from core.agent.trading_agent import TradingAgent, get_trading_agent
from core.agent.decision_engine import DecisionEngine, get_decision_engine
from core.agent.prompt_templates import (
    MARKET_ANALYSIS_PROMPT,
    DECISION_PROMPT,
    SIGNAL_FORMAT_PROMPT
)

__all__ = [
    'TradingAgent',
    'get_trading_agent',
    'DecisionEngine',
    'get_decision_engine',
    'MARKET_ANALYSIS_PROMPT',
    'DECISION_PROMPT',
    'SIGNAL_FORMAT_PROMPT',
]
