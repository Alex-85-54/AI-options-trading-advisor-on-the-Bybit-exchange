from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from typing import List, Dict, Optional
from pydantic import BaseModel
from datetime import datetime

from websocket_manager import ws_manager
from data_store import data_store
from config import CONFIG

app = FastAPI(title="Option Data Service", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic модели
class OptionSymbol(BaseModel):
    underlying: str
    day: str
    month: str
    strike: str
    option_type: str


class StraddleConfig(BaseModel):
    underlying: str
    current_price: float
    distance_percent: float = 5.0  # Процент удаления от текущей цены


@app.get("/")
async def root():
    return {"service": "Option Data Service", "status": "running"}


@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/data/{symbol}")
async def get_option_data(symbol: str):
    """Получить данные по конкретному опциону"""
    data = data_store.get(symbol)
    if not data:
        # Проверяем, подписан ли символ
        if symbol in ws_manager.active_symbols:
            return {
                "symbol": symbol,
                "status": "subscribed",
                "message": "Waiting for data from exchange",
                "timestamp": datetime.now().isoformat()
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol {symbol} not found or not subscribed"
            )
    return data


@app.get("/data/underlying/{underlying}")
async def get_underlying_options(underlying: str):
    """Получить все опционы по базовому активу"""
    data = data_store.get_by_underlying(underlying)
    return {"underlying": underlying, "options": data}


@app.post("/subscribe")
async def subscribe_to_options(symbols: List[OptionSymbol]):
    """Подписаться на обновления опционов"""
    symbol_strings = [
        ws_manager.create_option_symbol(
            s.underlying, s.day, s.month, s.strike, s.option_type
        )
        for s in symbols
    ]

    ws_manager.connect(symbol_strings)

    return {
        "status": "subscribed",
        "symbols": symbol_strings,
        "active_symbols": list(ws_manager.active_symbols)
    }


@app.get("/active/symbols")
async def get_active_symbols():
    """Получить список активных символов"""
    return {
        "active_symbols": list(ws_manager.active_symbols),
        "total": len(ws_manager.active_symbols)
    }


@app.post("/subscriptions/update")
async def update_subscriptions(symbols: List[str]):
    """Обновить подписки WebSocket"""
    try:
        # Получаем текущие активные символы
        current_symbols = list(ws_manager.active_symbols)

        # Формируем новый список (объединение старых и новых)
        all_symbols = list(set(current_symbols + symbols))

        # Обновляем подписки
        ws_manager.update_subscriptions(all_symbols)

        return {
            "status": "updated",
            "old_symbols_count": len(current_symbols),
            "new_symbols_count": len(all_symbols),
            "active_symbols": list(ws_manager.active_symbols)
        }
    except Exception as e:
        logger.error(f"Error updating subscriptions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/subscriptions/active")
async def get_active_subscriptions():
    """Получить активные подписки"""
    return {
        "active_symbols": list(ws_manager.active_symbols),
        "count": len(ws_manager.active_symbols)
    }


@app.get("/data/check/{symbol}")
async def check_option_data(symbol: str):
    """Проверить наличие и актуальность данных"""
    data = data_store.get(symbol)

    if data:
        # Проверяем актуальность данных (не старше 30 секунд)
        timestamp = data.get('timestamp')
        if isinstance(timestamp, datetime):
            age = (datetime.now() - timestamp).total_seconds()
            is_fresh = age < 30
        else:
            is_fresh = False

        return {
            "symbol": symbol,
            "available": True,
            "fresh": is_fresh,
            "age_seconds": age if 'age' in locals() else None,
            "has_price": 'ask_price' in data and data['ask_price'] > 0,
            "data": data if is_fresh else None
        }
    else:
        return {
            "symbol": symbol,
            "available": False,
            "subscribed": symbol in ws_manager.active_symbols,
            "message": "No data available" if symbol in ws_manager.active_symbols else "Not subscribed"
        }



if __name__ == "__main__":
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=CONFIG["server_port"]
    )