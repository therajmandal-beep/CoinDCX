"""
COINDCX FUTURES TRADING BOT + TELEGRAM
INR Margin | USDT Futures Pairs | 23x Leverage
Fixed: line 129 crash, hmac syntax
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
            base = symbol[: -len(quote)]
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
    h = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    )
    return h.hexdigest()

def make_headers(body=""):
    return {
        "X-AUTH-APIKEY"    : os.environ.get("COINDCX_API_KEY", ""),
        "X-AUTH-SIGNATURE" : make_signature(body),
        "Content-Type"     : "application/json",
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
        log.info("market_summary status: " + str(r.status_code) + " " + r.text[:200])
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    mkt = item.get("market", item.get("symbol", "")).upper()
                    if mkt == symbol.upper():
                        for field in ["last_price", "lastPrice", "last", "close", "ltp"]:
                            val = item.get(field)
                            if val:
                                return float(val)
            if isinstance(data, dict):
                inner = data.get("data", [])
                if isinstance(inner, list):
                    for item in inner:
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
            timeout=10
        )
        log.info("trades status: " + str(r2.status_code) + " " + r2.text[:200])
        if r2.status_code == 200:
            data2 = r2.json()
            if isinstance(data2, list) and data2:
                for field in ["price", "last_price", "rate", "p"]:
                    val = data2[0].get(field)
                    if val:
                        return float(val)

        # Fallback: spot BTCUSDT
        log.warning("Using spot BTCUSDT as fallback price")
        r3 = requests.get(BASE_URL + "/exchange/ticker", timeout=10)
        for t in r3.json():
            if t.get("market", "").upper() == "BTCUSDT":
                return float(t["last_price"])

        raise ValueError("All price endpoints failed for " + symbol)
    except Exception as e:
        log.error("get_price error: " + str(e))
        raise


def set_leverage(symbol, leverage):
    try:
        payload = {
            "symbol"    : symbol,
            "leverage"  : str(leverage),
            "timestamp" : int(time.time() * 1000),
        }
        body = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            BASE_URL + "/exchange/v1/derivatives/futures/user/leverage",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info("Leverage: " + str(r.status_code) + " " + r.text)
    except Exception as e:
        log.error("set_leverage error: " + str(e))


def place_order(symbol, side, qty, tp, sl):
    payload = {
        "market"            : symbol,
        "order_type"        : "market_order",
        "side"              : side.lower(),
        "quantity"          : round(qty, 6),
        "leverage"          : LEVERAGE,
        "take_profit_price" : str(round(tp, 2)),
        "stop_loss_price"   : str(round(sl, 2)),
        "client_order_id"   : uuid.uuid4().hex,
        "timestamp"         : int(time.time() * 1000),
    }
    body = json.dumps(payload, separators=(",", ":"))
    log.info("Placing order: " + body)
    r = requests.post(
        BASE_URL + "/exchange/v1/derivatives/futures/orders/create",
        headers=make_headers(body),
        data=body,
        timeout=10
    )
    log.info("Order response: " + str(r.status_code) + " " + r.text)
    r.raise_for_status()
    return r.json()

# ─── DUPLICATE GUARD ─────────────────────────────────────────────────────────
last_signals = {}
signals_lock = threading.Lock()

def is_duplicate(symbol, action):
    key = symbol + "_" + action
    now = time.time()
    with signals_lock:
        if key in last_signals and (now - last_signals[key]) < 60:
            return True
        last_signals[key] = now
    return False

# ─── EXECUTE TRADE ───────────────────────────────────────────────────────────
def execute_trade(symbol, action):
    global BOT_ACTIVE
    action = action.lower()
    if not BOT_ACTIVE:
        return {"status": "blocked", "reason": "Bot stopped"}

    futures_symbol = to_futures_symbol(symbol)
    price          = get_price(futures_symbol)
    balance        = get_balance()
    log.info("Trade: " + action + " " + futures_symbol + " price=" + str(price) + " balance=" + str(balance))

    if balance < 10:
        raise ValueError("Balance too low: " + str(balance))

    qty = round((balance * RISK_PERC * LEVERAGE) / price, 4)
    if qty <= 0:
        raise ValueError("Qty too small: " + str(qty))

    if action == "buy":
        side = "buy"
        sl   = round(price * (1 - SL_PERC), 2)
        tp   = round(price * (1 + SL_PERC * RR), 2)
    else:
        side = "sell"
        sl   = round(price * (1 + SL_PERC), 2)
        tp   = round(price * (1 - SL_PERC * RR), 2)

    set_leverage(futures_symbol, LEVERAGE)
    result = place_order(futures_symbol, side, qty, tp, sl)

    if isinstance(result, dict) and result.get("code") not in (None, 0, 200, "200"):
        error_msg = result.get("message", result.get("msg", "Unknown error"))
        log.error("Order failed: " + str(error_msg))
        s
