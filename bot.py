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
            timeout=10
        )
        log.info("trades: " + str(r2.status_code) + " " + r2.text[:200])
        if r2.status_code == 200:
            data2 = r2.json()
            if isinstance(data2, list) and data2:
                for field in ["price", "last_price", "rate", "p"]:
                    val = data2[0].get(field)
                    if val:
                        return float(val)

        # Fallback: spot BTCUSDT
        log.warning("Falling back to spot BTCUSDT price")
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
            "symbol"   : symbol,
            "leverage" : str(leverage),
            "timestamp": int(time.time() * 1000),
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
        "market"           : symbol,
        "order_type"       : "market_order",
        "side"             : side.lower(),
        "quantity"         : round(qty, 6),
        "leverage"         : LEVERAGE,
        "take_profit_price": str(round(tp, 2)),
        "stop_loss_price"  : str(round(sl, 2)),
        "client_order_id"  : uuid.uuid4().hex,
        "timestamp"        : int(time.time() * 1000),
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
        error_msg = str(result.get("message", result.get("msg", "Unknown error")))
        log.error("Order failed: " + error_msg)
        send_telegram("❌ <b>Order Failed</b>\n" + futures_symbol + " " + side.upper() + "\nError: " + error_msg)
        return result

    emoji  = "🟢 LONG" if action == "buy" else "🔴 SHORT"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        emoji + " <b>" + futures_symbol + "</b>\n"
        + "━━━━━━━━━━━━━━\n"
        + "💰 Price:    <b>₹" + "{:,.2f}".format(price) + "</b>\n"
        + "📦 Qty:      <b>" + str(qty) + "</b>\n"
        + "🎯 TP:       <b>₹" + "{:,.2f}".format(tp) + "</b>\n"
        + "🛑 SL:       <b>₹" + "{:,.2f}".format(sl) + "</b>\n"
        + "⚡ Leverage: <b>" + str(LEVERAGE) + "x</b>\n"
        + "💼 Balance:  <b>₹" + "{:,.2f}".format(balance) + " INR</b>\n"
        + "⏰ " + now_str
    )
    send_telegram(msg)
    log.info("Trade done: " + side + " " + str(qty) + " " + futures_symbol)
    return result

# ─── TELEGRAM POLLING ────────────────────────────────────────────────────────
def telegram_polling():
    global BOT_ACTIVE
    offset = 0
    log.info("Telegram polling started!")

    sl_pct = str(round(SL_PERC * 100, 2))
    tp_pct = str(round(SL_PERC * RR * 100, 2))
    lev    = str(LEVERAGE)
    risk   = str(int(RISK_PERC * 100))

    startup_msg = (
        "🚀 <b>CoinDCX Futures Bot ONLINE!</b>\n"
        + "💱 Margin: INR\n"
        + "SL: " + sl_pct + "% | TP: " + tp_pct + "%\n"
        + "Leverage: " + lev + "x | Risk: " + risk + "%\n"
        + "Send /help to see commands"
    )
    send_telegram(startup_msg)

    while True:
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
            if not token:
                time.sleep(10)
                continue
            r = requests.get(
                "https://api.telegram.org/bot" + token + "/getUpdates",
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
                        + "━━━━━━━━━━━━━━\n"
                        + "/status  — Bot status\n"
                        + "/balance — INR balance\n"
                        + "/price   — BTC futures price\n"
                        + "/stop    — Stop trading\n"
                        + "/start   — Start trading\n"
                        + "/help    — This menu"
                    )
                elif text == "/status":
                    try:
                        bal     = get_balance()
                        running = "🟢 RUNNING" if BOT_ACTIVE else "🔴 STOPPED"
                        send_telegram(
                            "🤖 <b>Futures Bot Status</b>\n"
                            + "━━━━━━━━━━━━\n"
                            + running + "\n"
                            + "Balance:  <b>₹" + "{:,.2f}".format(bal) + " INR</b>\n"
                            + "Leverage: <b>" + str(LEVERAGE) + "x</b>\n"
                            + "Risk:     <b>" + str(int(RISK_PERC * 100)) + "%/trade</b>"
                        )
                    except Exception as e:
                        send_telegram("⚠️ Status error: " + str(e)[:100])
                elif text == "/balance":
                    try:
                        bal = get_balance()
                        send_telegram("💼 INR Balance: <b>₹" + "{:,.2f}".format(bal) + "</b>")
                    except Exception as e:
                        send_telegram("⚠️ Balance error: " + str(e)[:100])
                elif text == "/price":
                    try:
                        p = get_price("B-BTC_USDT")
                        send_telegram("₿ BTC Futures: <b>₹" + "{:,.2f}".format(p) + "</b>")
                    except Exception as e:
                        send_telegram("⚠️ Price error: " + str(e)[:100])
        except Exception as e:
            log.error("Polling error: " + str(e))
        time.sleep(2)

# ─── FLASK APP ───────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT").upper()
        log.info("Webhook: " + action.upper() + " " + symbol)
        if action not in ("buy", "sell"):
            return jsonify({"error": "unknown action"}), 400
        if is_duplicate(symbol, action):
            return jsonify({"status": "duplicate"}), 200
        result = execute_trade(symbol, action)
        return jsonify({"status": "ok", "result": result}), 200
    except requests.HTTPError as e:
        err = e.response.text
        send_telegram("❌ <b>Trade Failed</b>\n" + err[:200])
        return jsonify({"error": err}), 500
    except Exception as e:
        log.error("Webhook error: " + str(e))
        send_telegram("❌ <b>Error</b>\n" + str(e)[:200])
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"     : "CoinDCX Futures Bot Running",
        "bot_active" : BOT_ACTIVE,
        "leverage"   : LEVERAGE,
        "risk"       : str(int(RISK_PERC * 100)) + "%",
        "margin"     : MARGIN_CURRENCY,
        "coindcx_key": "SET" if os.environ.get("COINDCX_API_KEY")    else "MISSING",
        "tg_token"   : "SET" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING",
        "tg_chat"    : "SET" if os.environ.get("TELEGRAM_CHAT_ID")   else "MISSING",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_tg():
    return jsonify({"sent": send_telegram("CoinDCX Futures INR Bot Test!")})

@app.route("/test_balance", methods=["GET"])
def test_balance():
    return jsonify({"INR_balance": get_balance()})

@app.route("/test_price", methods=["GET"])
def test_price():
    try:
        p = get_price("B-BTC_USDT")
        return jsonify({"BTC_USDT_price": p})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug():
    results = {}
    for ep in [
        "/exchange/v1/derivatives/futures/data/market_summary",
        "/exchange/v1/derivatives/futures/data/trades?symbol=B-BTC_USDT&limit=1",
        "/exchange/v1/derivatives/futures/data/ticker?symbol=B-BTC_USDT",
    ]:
        try:
            r = requests.get(BASE_URL + ep, timeout=5)
            results[ep] = {"status": r.status_code, "sample": r.text[:300]}
        except Exception as e:
            results[ep] = {"error": str(e)}

    payload = {"timestamp": int(time.time() * 1000)}
    body    = json.dumps(payload, separators=(",", ":"))
    rb      = requests.post(
        BASE_URL + "/exchange/v1/users/balances",
        headers=make_headers(body), data=body, timeout=10
    )
    inr = next((a for a in rb.json() if a.get("currency", "").upper() == "INR"), None)
    results["inr_balance"] = inr
    return jsonify(results)

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("COINDCX FUTURES BOT (INR) STARTING")
    log.info("LEVERAGE: " + str(LEVERAGE) + "x")
    log.info("RISK:     " + str(int(RISK_PERC * 100)) + "%")
    log.info("API KEY:  " + ("SET" if os.environ.get("COINDCX_API_KEY")    else "MISSING"))
    log.info("TG TOKEN: " + ("SET" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING"))
    log.info("TG CHAT:  " + ("SET" if os.environ.get("TELEGRAM_CHAT_ID")   else "MISSING"))
    log.info("=" * 50)
    threading.Thread(target=telegram_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
