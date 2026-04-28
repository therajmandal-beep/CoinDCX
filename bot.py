"""
COINDCX FUTURES TRADING BOT + TELEGRAM
INR Margin | 23x Leverage | Fixed syntax errors
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
def to_futures_symbol(symbol):
    symbol = symbol.upper().replace("-", "").replace("_", "")
    for quote in ["USDT", "INR", "BTC", "ETH"]:
        if symbol.endswith(quote):
            base = symbol[:-len(quote)]
            return "B-" + base + "_USDT"
    return "B-" + symbol + "_USDT"

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── SIGNATURE ───────────────────────────────────────────────────────────────
def make_signature(body):
    secret = os.environ.get("COINDCX_SECRET_KEY", "")
    h = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256)
    return h.hexdigest()

def make_headers(body=""):
    return {
        "X-AUTH-APIKEY"   : os.environ.get("COINDCX_API_KEY", ""),
        "X-AUTH-SIGNATURE": make_signature(body),
        "Content-Type"    : "application/json",
    }

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("Telegram not configured!")
        return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": chat, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if r.ok:
            log.info("Telegram sent!")
            return True
        log.error("Telegram error: " + r.text)
        return False
    except Exception as e:
        log.error("Telegram exception: " + str(e))
        return False

# ─── COINDCX API ─────────────────────────────────────────────────────────────
def get_balance():
    try:
        payload = {"timestamp": int(time.time() * 1000)}
        body    = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            BASE_URL + "/exchange/v1/users/balances",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info("Balance RAW: " + r.text[:300])
        data = r.json()
        if isinstance(data, list):
            for asset in data:
                if asset.get("currency", "").upper() == "INR":
                    bal = float(asset.get("balance", 0))
                    log.info("INR balance: " + str(bal))
                    return bal
        log.error("INR not found in balance")
        return 0.0
    except Exception as e:
        log.error("get_balance error: " + str(e))
        return 0.0


def get_price(symbol):
    try:
        # Try futures market summary
        r = requests.get(
            BASE_URL + "/exchange/v1/derivatives/futures/data/market_summary",
            timeout=10
        )
        log.info("market_summary: " + str(r.status_code) + " " + r.text[:200])
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            for item in items:
                mkt = item.get("market", item.get("symbol", "")).upper()
                if mkt == symbol.upper():
                    for field in ["last_price", "lastPrice", "last", "close", "ltp"]:
                        val = item.get(field)
                        if val:
                            return float(val)

        # Try futures trades
        r2 = requests.get(
            BASE_URL + "/exchange/v1/derivatives/futures/data/trades",
            params={"symbol": symbol, "limit": 1},
            tim
