"""
COINDCX FUTURES TRADING BOT + TELEGRAM
INR Margin version - Fixed endpoints and symbol format
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
LEVERAGE        = 10
RISK_PERC       = 0.10
BOT_ACTIVE      = True
MARGIN_CURRENCY = "INR"

# ─── SYMBOL FORMAT ───────────────────────────────────────────────────────────
def to_futures_symbol(symbol: str) -> str:
    """
    BTCUSDT  → B-BTC_INR
    BTCINR   → B-BTC_INR
    ETHINR   → B-ETH_INR
    ETHUSDT  → B-ETH_INR
    """
    symbol = symbol.upper().replace("-", "").replace("_", "")
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"B-{base}_INR"
    for quote in ["INR", "BTC", "ETH"]:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"B-{base}_{quote}"
    return f"B-{symbol}_INR"

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
    """
    Uses /exchange/ticker (public endpoint).
    symbol = B-BTC_INR
    """
    try:
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        log.info(f"Ticker status: {r.status_code}")
        tickers = r.json()
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("market", "").upper() == symbol.upper():
                    price = float(t["last_price"])
                    log.info(f"Price {symbol}: ₹{price}")
                    return price
        raise ValueError(f"Symbol {symbol} not found in ticker")
    except Exception as e:
        log.error(f"get_price error: {e}")
        raise


def set_leverage(symbol: str, leverage: int):
    try:
        payload = {
            "symbol"    : symbol,
            "leverage"  : str(leverage),
            "timestamp" : int(time.time() * 1000),
        }
        body = json.dumps(payload, separators=(",", ":"))
        r = requests.post(
            f"{BASE_URL}/exchange/v1/derivatives/futures/user/leverage",
            headers=make_headers(body),
            data=body,
            timeout=10
        )
        log.info(f"Leverage response: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"set_leverage error: {e}")


def place_order(symbol: str, side: str, qty: float, tp: float, sl: float) -> dict:
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
    log.info(f"Placing order: {body}")
    r = requests.post(
        f"{BASE_URL}/exchange/v1/derivatives/futures/orders/create",
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

    futures_symbol = to_futures_symbol(symbol)    # BTCUSDT → B-BTC_INR
    price          = get_price(futures_symbol)
    balance        = get_balance()
    log.info(f"Trade: {action} {futures_symbol} price=₹{price} balance=₹{balance}")

    if balance < 10:
        raise ValueError(f"Balance too low: ₹{balance:.2f}")

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

    set_leverage(futures_symbol, LEVERAGE)
    result = place_order(futures_symbol, side, qty, tp, sl)

    if isinstance(result, dict) and result.get("code") not in (None, 0, 200, "200"):
        error_msg = result.get("message", result.get("msg", "Unknown error"))
        log.error(f"Order failed: {error_msg}")
        send_telegram(
            f"❌ <b>Order Failed</b>\n"
            f"{futures_symbol} {side.upper()}\n"
            f"Error: {error_msg}"
        )
        return result

    emoji = "🟢 LONG" if action == "buy" else "🔴 SHORT"
    send_telegram(
        f"{emoji} <b>{futures_symbol}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Price:    <b>₹{price:,.2f}</b>\n"
        f"📦 Qty:      <b>{qty:.4f}</b>\n"
        f"🎯 TP:       <b>₹{tp:,.2f}</b>\n"
        f"🛑 SL:       <b>₹{sl:,.2f}</b>\n"
        f"⚡ Leverage: <b>{LEVERAGE}x</b>\n"
        f"💼 Balance:  <b>₹{balance:,.2f} INR</b>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    log.info(f"✅ Trade done: {side} {qty} {futures_symbol} @ ₹{price}")
    return result

# ─── TELEGRAM POLLING ────────────────────────────────────────────────────────
def telegram_polling():
    global BOT_ACTIVE
    offset = 0
    log.info("🤖 Telegram polling started!")
    send_telegram(
        "🚀 <b>CoinDCX Futures Bot ONLINE!</b>\n"
        f"💱 Margin: INR\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%\n"
        "Send /help to see commands"
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
                    send_telegram("🟢 <b>Bot STARTED!</b> Ready to trade futures.")
                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED!</b> Send /start to resume.")
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>CoinDCX Futures Bot (INR)</b>\n"
                        "━━━━━━━━━━━━━━\n"
                        "/status  — Bot status\n"
                        "/balance — INR balance\n"
                        "/price   — BTC/INR price\n"
                        "/stop    — Stop trading\n"
                        "/start   — Start trading\n"
                        "/help    — This menu"
                    )
                elif text == "/status":
                    try:
                        bal = get_balance()
                        send_telegram(
                            f"🤖 <b>Futures Bot Status</b>\n"
                            f"━━━━━━━━━━━━\n"
                            f"{'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                            f"Balance:  <b>₹{bal:,.2f} INR</b>\n"
                            f"Leverage: <b>{LEVERAGE}x</b>\n"
                            f"Risk:     <b>{RISK_PERC*100:.0f}%/trade</b>"
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Status error: {str(e)[:100]}")
                elif text == "/balance":
                    try:
                        bal = get_balance()
                        send_telegram(f"💼 INR Balance: <b>₹{bal:,.2f}</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Balance error: {str(e)[:100]}")
                elif text == "/price":
                    try:
                        p = get_price("B-BTC_INR")
                        send_telegram(f"₿ BTC/INR Futures: <b>₹{p:,.2f}</b>")
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
        "status"      : "CoinDCX Futures Bot (INR) 🚀",
        "bot_active"  : BOT_ACTIVE,
        "margin"      : MARGIN_CURRENCY,
        "coindcx_key" : "SET ✅" if os.environ.get("COINDCX_API_KEY")    else "MISSING ❌",
        "tg_token"    : "SET ✅" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING ❌",
        "tg_chat"     : "SET ✅" if os.environ.get("TELEGRAM_CHAT_ID")   else "MISSING ❌",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_tg():
    return jsonify({"sent": send_telegram("🧪 CoinDCX Futures INR Bot Test!")})

@app.route("/test_balance", methods=["GET"])
def test_balance():
    bal = get_balance()
    return jsonify({"INR_balance": bal})

@app.route("/test_price", methods=["GET"])
def test_price():
    try:
        p = get_price("B-BTC_INR")
        return jsonify({"BTC_INR_price": p})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug():
    try:
        # Check B-BTC_INR in ticker
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        tickers = r.json()
        btc_inr = [t for t in tickers if "B-BTC" in t.get("market", "")]

        # Check INR balance
        payload = {"timestamp": int(time.time() * 1000)}
        body    = json.dumps(payload, separators=(",", ":"))
        rb = requests.post(
            f"{BASE_URL}/exchange/v1/users/balances",
            headers=make_headers(body), data=body, timeout=10
        )
        bal_data = rb.json()
        inr_bal  = next(
            (a for a in bal_data if a.get("currency", "").upper() == "INR"), None
        )
        return jsonify({
            "btc_inr_ticker" : btc_inr,
            "inr_balance"    : inr_bal
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("COINDCX FUTURES BOT (INR) STARTING 🚀")
    log.info(f"COINDCX_API_KEY:    {'SET ✅' if os.environ.get('COINDCX_API_KEY')    else 'MISSING ❌'}")
    log.info(f"COINDCX_SECRET_KEY: {'SET ✅' if os.environ.get('COINDCX_SECRET_KEY') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_BOT_TOKEN: {'SET ✅' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_CHAT_ID:   {'SET ✅' if os.environ.get('TELEGRAM_CHAT_ID')   else 'MISSING ❌'}")
    log.info("=" * 50)
    threading.Thread(target=telegram_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
