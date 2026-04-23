"""
COINDCX AI TRADING BOT + TELEGRAM
Adapted from Bitunix bot — uses HMAC-SHA256 auth, CoinDCX REST API
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
BASE_URL   = "https://api.coindcx.com"
SL_PERC    = 0.007
RR         = 1.8
LEVERAGE   = 10
RISK_PERC  = 0.10
BOT_ACTIVE = True

# ─── SYMBOL FORMAT ───────────────────────────────────────────────────────────
# CoinDCX futures symbol format: "B-BTC_USDT"
# TradingView sends "BTCUSDT" → we convert
def to_coindcx_symbol(symbol: str) -> str:
    """
    Convert 'BTCUSDT' → 'B-BTC_USDT' (CoinDCX futures format).
    Handles most common USDT pairs.
    For INR pairs: 'BTCINR' → 'B-BTC_INR'
    Adjust the logic if you trade other pairs.
    """
    symbol = symbol.upper()
    for quote in ["USDT", "INR", "BTC", "ETH", "BNB"]:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"B-{base}_{quote}"
    return symbol  # fallback: return as-is

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── COINDCX SIGNATURE (HMAC-SHA256) ─────────────────────────────────────────
def make_signature(body: str) -> str:
    """CoinDCX: HMAC-SHA256(secret_key, request_body_as_string)"""
    secret = os.environ.get("COINDCX_SECRET_KEY", "")
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def make_headers(body: str = "") -> dict:
    api_key = os.environ.get("COINDCX_API_KEY", "")
    return {
        "X-AUTH-APIKEY"     : api_key,
        "X-AUTH-SIGNATURE"  : make_signature(body),
        "Content-Type"      : "application/json",
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
    """
    POST /exchange/v1/users/balances
    Returns available USDT balance.
    """
    try:
        payload = {"timestamp": int(time.time() * 1000)}
        body    = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            f"{BASE_URL}/exchange/v1/users/balances",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info(f"Balance response: {r.status_code} {r.text[:300]}")
        data = r.json()
        # data is a list of balance objects
        if isinstance(data, list):
            for asset in data:
                if asset.get("currency_short_name", "").upper() == "USDT":
                    return float(asset.get("balance", 0))
        # Some accounts may return a dict with 'balances' key
        if isinstance(data, dict):
            for asset in data.get("balances", []):
                if asset.get("currency_short_name", "").upper() == "USDT":
                    return float(asset.get("balance", 0))
        log.error(f"USDT balance not found in: {data}")
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0


def get_price(symbol: str) -> float:
    """
    GET /exchange/ticker  (public, no auth needed)
    CoinDCX returns a list; match on 'market' field.
    symbol should be CoinDCX format e.g. 'B-BTC_USDT'
    """
    try:
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        log.info(f"Price response: {r.status_code}")
        tickers = r.json()
        for t in tickers:
            if t.get("market", "").upper() == symbol.upper():
                return float(t["last_price"])
        raise ValueError(f"No ticker found for {symbol}")
    except Exception as e:
        log.error(f"get_price error: {e}")
        raise


def set_leverage(symbol: str, leverage: int):
    """
    POST /exchange/v1/margin/settings
    Sets leverage for a margin/futures pair.
    ⚠️ Verify exact endpoint & fields with CoinDCX docs for your account type.
    """
    try:
        payload = {
            "symbol"    : symbol,
            "leverage"  : leverage,
            "timestamp" : int(time.time() * 1000),
        }
        body = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            f"{BASE_URL}/exchange/v1/margin/settings",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info(f"Leverage response: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"set_leverage error: {e}")


def place_order(symbol: str, side: str, qty: float, tp: float, sl: float) -> dict:
    """
    POST /exchange/v1/orders/create
    side = 'buy' or 'sell'

    For futures/margin accounts CoinDCX supports:
      - take_profit_price
      - stop_loss_price
      - leverage

    ⚠️ If you're on a SPOT account, remove leverage/tp/sl fields.
    ⚠️ Verify field names against your CoinDCX account tier docs.
    """
    payload = {
        "market"            : symbol,
        "order_type"        : "market_order",
        "side"              : side.lower(),     # "buy" or "sell"
        "total_quantity"    : round(qty, 6),
        "leverage"          : LEVERAGE,
        "take_profit_price" : round(tp, 2),
        "stop_loss_price"   : round(sl, 2),
        "client_order_id"   : uuid.uuid4().hex,
        "timestamp"         : int(time.time() * 1000),
    }
    body = json.dumps(payload, separators=(",", ":"))
    log.info(f"Placing order: {body}")
    r = requests.post(
        f"{BASE_URL}/exchange/v1/orders/create",
        headers=make_headers(body),
        data=body,
        timeout=10
    )
    log.info(f"Order response: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

# ─── DUPLICATE GUARD ─────────────────────────────────────────────────────────
last_signals = {}
signals_lock = threading.Lock()

def is_duplicate(symbol: str, action: str) -> bool:
    key = f"{symbol}_{action}"
    now = time.time()
    with signals_lock:
        if key in last_signals and (now - last_signals[key]) < 60:
            return True
        last_signals[key] = now
    return False

# ─── EXECUTE TRADE ────────────────────────────────────────────────────────────
def execute_trade(symbol: str, action: str) -> dict:
    global BOT_ACTIVE
    action = action.lower()

    if not BOT_ACTIVE:
        return {"status": "blocked", "reason": "Bot stopped"}

    cdx_symbol = to_coindcx_symbol(symbol)   # e.g. "B-BTC_USDT"
    price      = get_price(cdx_symbol)
    balance    = get_balance()
    log.info(f"Trade: {action} {cdx_symbol} price={price} balance={balance}")

    if balance < 1:
        raise ValueError(f"Balance too low: ${balance:.2f}")

    qty = round((balance * RISK_PERC * LEVERAGE) / price, 4)
    if qty <= 0:
        raise ValueError(f"Qty too small: {qty}")

    if action == "buy":
        side = "buy"
        sl   = round(price * (1 - SL_PERC), 2)
        tp   = round(price * (1 + SL_PERC * RR), 2)
    else:
        side = "sell"
        sl   = round(price * (1 + SL_PERC), 2)
        tp   = round(price * (1 - SL_PERC * RR), 2)

    set_leverage(cdx_symbol, LEVERAGE)
    result = place_order(cdx_symbol, side, qty, tp, sl)

    # CoinDCX success: result has 'orders' list or 'id'
    if result.get("code") and result["code"] != 200:
        error_msg = result.get("message", "Unknown error")
        log.error(f"Order failed: {error_msg}")
        send_telegram(
            f"❌ <b>Order Failed</b>\n"
            f"{cdx_symbol} {side.upper()}\n"
            f"Error: {error_msg}"
        )
        return result

    emoji = "🟢 BUY" if action == "buy" else "🔴 SELL"
    send_telegram(
        f"{emoji} <b>{cdx_symbol}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Price:   <b>₹{price:,.2f}</b>\n"
        f"📦 Qty:     <b>{qty:.4f}</b>\n"
        f"🎯 TP:      <b>₹{tp:,.2f}</b>\n"
        f"🛑 SL:      <b>₹{sl:,.2f}</b>\n"
        f"💼 Balance: <b>${balance:,.2f} USDT</b>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    log.info(f"✅ Trade done: {side} {qty} {cdx_symbol} @ {price}")
    return result

# ─── TELEGRAM POLLING ────────────────────────────────────────────────────────
def telegram_polling():
    global BOT_ACTIVE
    offset = 0
    log.info("🤖 Telegram polling started!")
    send_telegram(
        "🚀 <b>CoinDCX Bot is ONLINE!</b>\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%\n"
        "Send /help to see all commands"
    )
    while True:
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
            if not token:
                time.sleep(10)
                continue
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 10},
                timeout=15
            )
            if not r.ok:
                time.sleep(5)
                continue
            for update in r.json().get("result", []):
                offset    = update["update_id"] + 1
                msg       = update.get("message", {})
                text      = msg.get("text", "").strip().lower()
                from_chat = str(msg.get("chat", {}).get("id", ""))
                if from_chat != str(chat):
                    continue
                if text in ("/start", "start"):
                    BOT_ACTIVE = True
                    send_telegram("🟢 <b>Bot STARTED!</b> Ready to trade.")
                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED!</b> Send /start to resume.")
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>CoinDCX Bot Commands</b>\n"
                        "━━━━━━━━━━━━━━\n"
                        "/status  — Bot status\n"
                        "/balance — USDT balance\n"
                        "/price   — BTC price\n"
                        "/stop    — Stop trading\n"
                        "/start   — Start trading\n"
                        "/help    — This menu"
                    )
                elif text == "/status":
                    try:
                        bal = get_balance()
                        send_telegram(
                            f"🤖 <b>Bot Status</b>\n"
                            f"━━━━━━━━━━━━\n"
                            f"{'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                            f"Balance:  <b>${bal:,.2f} USDT</b>\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Risk:     {RISK_PERC*100:.0f}%/trade"
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Status error: {str(e)[:100]}")
                elif text == "/balance":
                    try:
                        bal = get_balance()
                        send_telegram(f"💼 Balance: <b>${bal:,.2f} USDT</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Balance error: {str(e)[:100]}")
                elif text == "/price":
                    try:
                        p = get_price("B-BTC_USDT")
                        send_telegram(f"₿ BTC/USDT: <b>₹{p:,.2f}</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Price error: {str(e)[:100]}")
        except Exception as e:
            log.error(f"Polling error: {e}")
        time.sleep(2)

# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT").upper()
        log.info(f"📡 Webhook: {action.upper()} {symbol}")
        if action not in ("buy", "sell"):
            return jsonify({"error": "unknown action"}), 400
        if is_duplicate(symbol, action):
            return jsonify({"status": "duplicate"}), 200
        result = execute_trade(symbol, action)
        return jsonify({"status": "ok", "result": result}), 200
    except requests.HTTPError as e:
        err = e.response.text
        send_telegram(f"❌ <b>Trade Failed</b>\n{err[:200]}")
        return jsonify({"error": err}), 500
    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{str(e)[:200]}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"        : "CoinDCX Bot Running 🚀",
        "bot_active"    : BOT_ACTIVE,
        "coindcx_key"   : "SET ✅" if os.environ.get("COINDCX_API_KEY")    else "MISSING ❌",
        "tg_token"      : "SET ✅" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING ❌",
        "tg_chat"       : "SET ✅" if os.environ.get("TELEGRAM_CHAT_ID")   else "MISSING ❌",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_tg():
    ok = send_telegram("🧪 Test from CoinDCX Bot!")
    return jsonify({"sent": ok})

@app.route("/test_balance", methods=["GET"])
def test_balance():
    bal = get_balance()
    return jsonify({"balance": bal})

@app.route("/test_price", methods=["GET"])
def test_price():
    try:
        p = get_price("B-BTC_USDT")
        return jsonify({"price": p})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("COINDCX BOT STARTING 🚀")
    log.info(f"COINDCX_API_KEY:    {'SET ✅' if os.environ.get('COINDCX_API_KEY')    else 'MISSING ❌'}")
    log.info(f"COINDCX_SECRET_KEY: {'SET ✅' if os.environ.get('COINDCX_SECRET_KEY') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_BOT_TOKEN: {'SET ✅' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_CHAT_ID:   {'SET ✅' if os.environ.get('TELEGRAM_CHAT_ID')   else 'MISSING ❌'}")
    log.info("=" * 50)

    threading.Thread(target=telegram_polling, daemon=True).start()
    log.info("Telegram thread started!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
          
