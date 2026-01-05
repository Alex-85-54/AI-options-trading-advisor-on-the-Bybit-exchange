from pybit.unified_trading import WebSocket
from fastapi import FastAPI
import uvicorn
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Callable
import pandas as pd
import logging
from time import sleep

DATE_NUMDAY = '4'
DATE_MONTH = 'JAN'
DATE_YEAR = '26'
STRYKE = '89000'
TYPE_OPT = 'C'
app = FastAPI()

@app.get('/get_info')
def handle_message(m):
    result = {}
    result['ticker'] = m['data']['symbol']
    result['ts'] = pd.to_datetime(m['ts'], unit='ms')
    result['askPrice'] = m['data']['askPrice']
    result['bidPrice'] = m['data']['bidPrice']
    result['highPrice24h'] = m['data']['highPrice24h']
    result['lowPrice24h'] = m['data']['lowPrice24h']
    result['underlyingPrice'] = m['data']['underlyingPrice']
    result['delta'] = m['data']['delta']
    result['gamma'] = m['data']['gamma']
    result['vega'] = m['data']['vega']
    result['theta'] = m['data']['theta']
    result['ts'] = pd.to_datetime(m['data']['ts'], unit='ms') + timedelta(hours=7)
    return result


@app.get("/health")
def healt_check():
    return {"status": "ok"}

@app.get("/start_socket")
def get_option_info():
    ws = WebSocket(
        testnet=False,
        channel_type='option',
        retries=2,
        restart_on_error=True,
    )
    ws.ticker_stream(
        symbol=f"BTC-{DATE_NUMDAY}{DATE_MONTH}{DATE_YEAR}-{STRYKE}-{TYPE_OPT}-USDT",
        callback=handle_message
    )

    return ws



if __name__ == "__main__":
    uvicorn.run("get_info_ticker:app", host="0.0.0.0", port = 8000, reload=True)

