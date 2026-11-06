import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from pyquotex.stable_api import Quotex

# Railway variables: BOT_TOKEN (required), USE_DEMO (optional), DATA_DIR (optional)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "5000"))
HOST = "0.0.0.0"
USE_DEMO = os.getenv("USE_DEMO", "true").lower() in {"1", "true", "yes"}
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "candles.sqlite3"
MAX_CANDLES = int(os.getenv("MAX_CANDLES", "5000"))
POLL_SECONDS = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quotex-service")
app = Flask(__name__)

q_instance = None
collector_task = None
owner_chat_id = None
all_instruments = []
cloudflared_process = None
cloudflared_url = None
stop_event = asyncio.Event()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS candles (
            pair TEXT NOT NULL, timeframe TEXT NOT NULL, candle_time INTEGER NOT NULL,
            close REAL, high REAL, low REAL, open REAL, signal TEXT, payout REAL,
            payload TEXT NOT NULL, PRIMARY KEY(pair,timeframe,candle_time))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_candles_pair_time ON candles(pair,timeframe,candle_time DESC)")
        con.execute("CREATE TABLE IF NOT EXISTS payouts (pair TEXT NOT NULL,timeframe TEXT NOT NULL,candle_time INTEGER NOT NULL,payout REAL,PRIMARY KEY(pair,timeframe,candle_time))")
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def setting(key, value=None):
    with db() as con:
        if value is not None:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def save_candle(pair, timeframe, item):
    ts = int(item["timestamp"])
    payload = json.dumps(item, separators=(",", ":"))
    with db() as con:
        con.execute("""INSERT INTO candles(pair,timeframe,candle_time,close,high,low,open,signal,payout,payload)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pair,timeframe,candle_time) DO UPDATE SET
            close=excluded.close,high=excluded.high,low=excluded.low,open=excluded.open,
            signal=excluded.signal,payout=excluded.payout,payload=excluded.payload""",
            (pair, timeframe, ts, item["close"], item["high"], item["low"], item["open"], item["signal"], item["payout"], payload))
        con.execute("INSERT INTO payouts(pair,timeframe,candle_time,payout) VALUES(?,?,?,?) ON CONFLICT(pair,timeframe,candle_time) DO UPDATE SET payout=excluded.payout", (pair,timeframe,ts,item["payout"]))


def latest_timestamp(pair, timeframe="1m"):
    with db() as con:
        row = con.execute("SELECT MAX(candle_time) FROM candles WHERE pair=? AND timeframe=?", (pair,timeframe)).fetchone()
        return row[0] if row and row[0] else None


def ts_seconds(value):
    if value is None:
        return None
    try:
        value = float(value)
        if value > 1_000_000_000_000:
            value /= 1000
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_candle(raw, pair, payout):
    ts = ts_seconds(raw.get("time") or raw.get("timestamp") or raw.get("t") or raw.get("date"))
    if ts is None:
        return None
    op = float(raw.get("open", raw.get("o", 0)) or 0)
    cl = float(raw.get("close", raw.get("c", 0)) or 0)
    return {
        "close": cl, "high": float(raw.get("high", raw.get("h", 0)) or 0),
        "low": float(raw.get("low", raw.get("l", 0)) or 0), "open": op,
        "pair": pair.replace("_otc", "-OTC").replace("_OTC", "-OTC").upper(),
        "signal": "CALL" if cl > op else "PUT", "time": datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": ts, "payout": float(payout or 0)
    }


def detect_markets(q):
    markets, seen = [], set()
    try:
        for inst in (getattr(getattr(q, "api", None), "instruments", None) or []):
            if len(inst) < 2:
                continue
            pair = str(inst[1]).strip()
            if not pair or pair in seen:
                continue
            seen.add(pair)
            payout = 0.0
            if len(inst) > 18 and isinstance(inst[18], (int, float)):
                payout = float(inst[18])
            elif len(inst) > 5 and isinstance(inst[5], (int, float)):
                payout = float(inst[5])
            upper = pair.upper()
            category = "OTC" if "_OTC" in upper else "Crypto" if any(x in upper for x in ("BTC","ETH","LTC","BNB","SOL","DOGE","XRP")) else "Commodity" if any(x in upper for x in ("XAU","XAG","WTI","BRENT")) else "Index" if any(x in upper for x in ("US30","NAS100","SPX","GER40")) else "Forex"
            markets.append({"pair": pair, "display_name": pair.replace("_otc", "").replace("_OTC", ""), "payout": payout, "category": category})
    except Exception:
        log.exception("Market detection failed")
    return sorted(markets, key=lambda x: x["pair"])


async def notify(text):
    if application and owner_chat_id:
        try:
            await application.bot.send_message(chat_id=owner_chat_id, text=text[:4000])
        except Exception:
            log.exception("Telegram notification failed")


async def close_quotex():
    global q_instance, collector_task
    task = collector_task
    collector_task = None
    if task and task is not asyncio.current_task():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    q, q_instance = q_instance, None
    if q:
        try:
            await q.close()
        except Exception:
            log.exception("Quotex close error")


async def connect_quotex(ssid, csrf):
    global q_instance, collector_task
    await close_quotex()
    q = Quotex(ssid_token=ssid, csrf_token=csrf or None)
    await notify("🔌 Quotex connection হচ্ছে...")
    try:
        ok, reason = await asyncio.wait_for(q.connect(), timeout=45)
        if not ok or not await q.check_connect():
            await q.close()
            await notify(f"❌ Login failed: {reason or 'Connection rejected'}\nনতুন Token/CSRF দিয়ে আবার /start দিন।")
            return False
    except Exception as exc:
        try:
            await q.close()
        except Exception:
            pass
        await notify(f"❌ Connection error: {type(exc).__name__}: {exc}")
        return False
    q.set_account_mode("practice" if USE_DEMO else "real")
    q_instance = q
    collector_task = asyncio.create_task(collector_loop(q), name="candle-collector")
    await notify(f"✅ Quotex login successful\nAccount: {'DEMO' if USE_DEMO else 'REAL'}\n💾 SQLite candle database ready. পুরোনো candle অক্ষত থাকবে।")
    return True


async def fetch_pair(q, pair, period=60):
    stored_before_fetch = latest_timestamp(pair)
    payout = 0.0
    try:
        for inst in (getattr(getattr(q, "api", None), "instruments", None) or []):
            if len(inst) > 1 and inst[1] == pair:
                if len(inst) > 18 and isinstance(inst[18], (int, float)):
                    payout = float(inst[18])
                elif len(inst) > 5 and isinstance(inst[5], (int, float)):
                    payout = float(inst[5])
                break
    except Exception:
        pass

    # First fetch the recent candle window. The bundled library's get_candles
    # signature is (asset, end_from_time, offset, period), not (asset, period).
    raw = None
    try:
        raw = await asyncio.wait_for(q.get_candle_v2(pair, period), timeout=25)
    except Exception as exc:
        log.warning("Recent candle fetch failed for %s: %s", pair, exc)
    if not raw:
        try:
            raw = await asyncio.wait_for(q.get_candles(pair, int(time.time()), 5000 * period, period), timeout=25)
        except Exception as exc:
            log.warning("Historical candle fetch failed for %s: %s", pair, exc)
            return 0

    saved = 0
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        item = normalize_candle(row, pair, payout)
        if item:
            save_candle(pair, "1m", item)
            saved += 1

    # If a stored gap exists, ask the history endpoint for a window reaching back
    # to the earliest known missing region. Provider limits may restrict recovery.
    last = stored_before_fetch
    if last is not None and last < int(time.time()) - 120:
        gap = int(time.time()) - last
        offset = min(max(gap + 600, 3600), 30 * 86400)
        try:
            history = await asyncio.wait_for(q.get_candles(pair, int(time.time()), offset, period), timeout=30)
            for row in history or []:
                if isinstance(row, dict):
                    item = normalize_candle(row, pair, payout)
                    if item:
                        save_candle(pair, "1m", item)
                        saved += 1
        except Exception as exc:
            log.info("Gap backfill unavailable for %s: %s", pair, exc)
    return saved


async def collector_loop(q):
    global all_instruments
    await notify("📡 Candle collector started.")
    while q_instance is q:
        try:
            markets = detect_markets(q)
            if markets:
                all_instruments = markets
                pairs = [m["pair"] for m in markets]
            else:
                pairs = ["USDPHP_otc", "USDARS_otc", "USDBRL_otc"]
            total = 0
            for pair in pairs:
                if q_instance is not q:
                    return
                try:
                    total += await fetch_pair(q, pair)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Pair collection failed: %s", pair)
                await asyncio.sleep(0.15)
            log.info("Collection cycle done: markets=%s saved_or_updated=%s", len(pairs), total)
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Collector cycle failed")
            await asyncio.sleep(5)


application = None


def is_owner(update: Update):
    global owner_chat_id
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return False
    stored = setting("owner_chat_id")
    if stored is None:
        setting("owner_chat_id", chat_id)
        owner_chat_id = chat_id
        return True
    owner_chat_id = int(stored)
    return chat_id == owner_chat_id


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ এই bot-এর owner access শুধু নির্ধারিত Telegram account-এর জন্য।")
        return
    context.user_data.clear()
    context.user_data["auth_stage"] = "ssid"
    await update.message.reply_text("🔐 Quotex Login\n\nআপনার SSID Token পাঠান।\nএটি Telegram chat-এ private রাখুন।")


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await close_quotex()
    await update.message.reply_text("🚪 Quotex logout সম্পন্ন। SQLite candle, payout ও historical data সংরক্ষিত আছে। নতুন করে login করতে /start দিন।")


async def server_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global cloudflared_process, cloudflared_url
    if not is_owner(update):
        return
    if cloudflared_url and cloudflared_process and cloudflared_process.returncode is None:
        await update.message.reply_text(f"🌍 Public API URL:\n{cloudflared_url}")
        return
    binary = shutil.which("cloudflared")
    if not binary:
        await update.message.reply_text("❌ cloudflared binary পাওয়া যায়নি। Docker image-এ এটি install থাকা প্রয়োজন।")
        return
    await update.message.reply_text("⏳ Cloudflare quick tunnel চালু করছি; URL তৈরি হতে কিছু সময় লাগতে পারে...")
    try:
        cloudflared_process = await asyncio.create_subprocess_exec(binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        cloudflared_url = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and cloudflared_process.returncode is None:
            try:
                line = await asyncio.wait_for(cloudflared_process.stdout.readline(), timeout=3)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            match = re.search(rb"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                cloudflared_url = match.group(0).decode()
                break
        if cloudflared_url:
            await update.message.reply_text(f"🎉 Public server live!\n{cloudflared_url}\n\nAPI: {cloudflared_url}/markets")
        else:
            await update.message.reply_text("❌ ৪০ সেকেন্ডের মধ্যে Cloudflare URL পাওয়া যায়নি। Railway logs দেখুন।")
    except Exception as exc:
        await update.message.reply_text(f"❌ Tunnel error: {exc}")


async def skip_csrf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or context.user_data.get("auth_stage") != "csrf":
        return
    context.user_data["csrf"] = ""
    await finish_login(update, context)


async def finish_login(update, context):
    ssid = context.user_data.pop("ssid", "")
    csrf = context.user_data.pop("csrf", "")
    context.user_data.pop("auth_stage", None)
    if not ssid:
        await update.message.reply_text("❌ SSID token খালি। আবার /start দিন।")
        return
    await update.message.reply_text("⏳ Token যাচাই করছি...")
    await connect_quotex(ssid, csrf)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    stage = context.user_data.get("auth_stage")
    value = (update.message.text or "").strip()
    if stage == "ssid":
        if value.startswith("/") or len(value) < 10:
            await update.message.reply_text("এটি SSID token মনে হচ্ছে না। সঠিক token পাঠান অথবা /cancel দিন।")
            return
        context.user_data["ssid"] = value
        context.user_data["auth_stage"] = "csrf"
        await update.message.reply_text("এবার CSRF Token (XSRF-TOKEN cookie) পাঠান। না থাকলে /skipcsrf দিন।")
    elif stage == "csrf":
        if value.startswith("/"):
            await update.message.reply_text("CSRF পাঠান অথবা /skipcsrf দিন।")
            return
        context.user_data["csrf"] = value
        await finish_login(update, context)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        context.user_data.clear()
        await update.message.reply_text("Login process বাতিল।")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    with db() as con:
        count = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        pairs = con.execute("SELECT COUNT(DISTINCT pair) FROM candles").fetchone()[0]
    await update.message.reply_text(f"Status: {'🟢 Connected' if q_instance else '🔴 Logged out'}\nMarkets cached: {len(all_instruments)}\nStored candles: {count}\nStored pairs: {pairs}\nDatabase: {DB_PATH}")


@app.get("/")
def candle_api():
    pair = request.args.get("Pair", request.args.get("pair", "USDPHP_otc"))
    timeframe = request.args.get("Time", request.args.get("time", "1m"))
    try:
        limit = max(1, min(int(request.args.get("Candle", request.args.get("candle", MAX_CANDLES))), 50000))
    except ValueError:
        limit = MAX_CANDLES
    with db() as con:
        rows = con.execute("SELECT payload FROM candles WHERE pair=? AND timeframe=? ORDER BY candle_time DESC LIMIT ?", (pair,timeframe,limit)).fetchall()
    data = [json.loads(r[0]) for r in rows]
    return jsonify({"api_pair": pair, "timeframe": timeframe, "count": len(data), "data": data})


@app.get("/markets")
def markets_api():
    return jsonify({"status": "success", "total_markets": len(all_instruments), "markets": all_instruments})


@app.get("/latest")
def latest_api():
    pair = request.args.get("Pair", request.args.get("pair", "USDPHP_otc"))
    timeframe = request.args.get("Time", request.args.get("time", "1m"))
    with db() as con:
        row = con.execute("SELECT payload FROM candles WHERE pair=? AND timeframe=? ORDER BY candle_time DESC LIMIT 1", (pair,timeframe)).fetchone()
    if not row:
        return jsonify({"status": "error", "message": f"No data found for {pair} with timeframe {timeframe}"}), 404
    return jsonify({"status": "success", "data": json.loads(row[0])})


@app.get("/payout-history")
def payout_api():
    pair = request.args.get("Pair", request.args.get("pair", "USDPHP_otc"))
    timeframe = request.args.get("Time", request.args.get("time", "1m"))
    with db() as con:
        rows = con.execute("SELECT candle_time,payout FROM payouts WHERE pair=? AND timeframe=? ORDER BY candle_time", (pair,timeframe)).fetchall()
    if not rows:
        return jsonify({"status": "error", "message": f"No payout history found for {pair}"}), 404
    return jsonify({"status": "success", "pair": pair, "timeframe": timeframe, "payout_history": {datetime.fromtimestamp(r[0], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"): r[1] for r in rows}})


@app.get("/health")
def health_api():
    return jsonify({"status": "ok", "connected": q_instance is not None})


def run_flask():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


async def post_init(application_obj):
    global owner_chat_id
    init_db()
    stored = setting("owner_chat_id")
    owner_chat_id = int(stored) if stored else None
    log.info("Telegram bot started; API port=%s database=%s", PORT, DB_PATH)


async def post_shutdown(application_obj):
    await close_quotex()
    if cloudflared_process and cloudflared_process.returncode is None:
        cloudflared_process.terminate()


def main():
    global application
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    init_db()
    threading.Thread(target=run_flask, daemon=True, name="flask-api").start()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("logout", logout_cmd))
    application.add_handler(CommandHandler("server", server_cmd))
    application.add_handler(CommandHandler("skipcsrf", skip_csrf))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
