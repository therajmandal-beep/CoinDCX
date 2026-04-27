"""
COINDCX FUTURES TRADING BOT + TELEGRAM
INR Margin | USDT Futures Pairs | 23x Leverage
"""
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify

# ─── BOT SETTINGS ────────────────────────────────────────────────────────────
BASE_URL        = "https://api.coindcx.com"
SL_PERC         = 0.007
RR              = 1.8
LEVERAGE        = 23
RISK_PERC       = 0.10
BOT_ACTIVE      = True
MARGIN_CURRENCY = "INR"

# ─── SYMBOL FORMAT ───────────────────────────────────────────────────────────
def to_futures_symbol(symbol: str) -> str:
    """
    BTCUSDT → B-BTC_USDT
    BTCINR  → B-BTC_USDT
    ETHINR  → B-ETH_USDT
    ETHUSDT → B-ETH_USDT
    """
    symbol = symbol.upper().replace("-", "").replace("_", "")
    for quote in ["USDT", "INR", "BTC", "ETH"]:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"B-{base}_USDT"
    return f"B-{symbol}_USDT"

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── SIGNATURE ───────────────────────────────────────────────────────────────
def make_signature(body: str) -> str:
    secret = os.environ.get("COINDCX_SECRET_KEY", "")
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def make_headers(body: str = "") -> dict:
    return {
        "X-AUTH-APIKEY"    : os.environ.get("COINDCX_API_KEY", ""),
        "X-AUTH-SIGNATURE" : make_signature(body),
        "Content-Type"     : "application/json",
    }

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("Telegram not configured!")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if r.ok:
            log.info("✅ Telegram sent!")
            return True
        log.error(f"Telegram error: {r.text}")
        return False
    except Exception as e:
        log.error(f"Telegram exception: {e}")
        return False

# ─── COINDCX API ─────────────────────────────────────────────────────────────
def get_balance() -> float:
    try:
        payload = {"timestamp": int(time.time() * 1000)}
        body    = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            f"{BASE_URL}/exchange/v1/users/balances",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info(f"Balance RAW: {r.text[:300]}")
        data = r.json()
        if isinstance(data, list):
            for asset in data:
                if asset.get("currency", "").upper() == "INR":
                    bal = float(asset.get("balance", 0))
                    log.info(f"INR balance: ₹{bal}")
                    return bal
        log.error(f"INR not found in balance: {data}")
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0


def get_price(symbol: str) -> float:
    try:
        # Try all known CoinDCX futures price endpoints
        endpoints = [
            f"/exchange/v1/derivatives/futures/data/market_summary?symbol={symbol}",
            f"/exchange/v1/derivatives/futures/data/ticker?symbol={symbol}",
            f"/exchange/v1/derivatives/futures/data/trades?symbol={symbol}&limit=1",
            f"/exchange/v1/derivatives/futures/data/candles?symbol={symbol}&resolution=1&from={int(time.time())-120}&to={int(time.time())}",
            f"/exchange/v1/derivatives/futures/data/market_summary",
            f"/derivatives/api/v1/tickers",
        ]
        for ep in endpoints:
            try:
                r = requests.get(f"{BASE_URL}{ep}", timeout=5)
                log.info(f"Price try {ep}: {r.status_code} {r.text[:200]}")
                if r.status_code != 200:
                    continue
