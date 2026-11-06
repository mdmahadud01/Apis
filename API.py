import asyncio
import threading
import json
from collections import deque
from datetime import datetime, timezone
import pytz
from flask import Flask, request, jsonify
from pyquotex.stable_api import Quotex

# --------------------- User Settings ---------------------
# Manual Token + CSRF (email/password login removed)
SSID_TOKEN = ""        # window.settings.token er value
CSRF_TOKEN = ""        # XSRF-TOKEN cookie er value
USE_DEMO = True
HOST = "0.0.0.0"
PORT = 5000
MAX_CANDLES = 5000

UTC_PLUS_6 = pytz.timezone('Asia/Dhaka')
# ---------------------------------------------------------

app = Flask(__name__)

# Global Storage: { "PAIR_TIMEFRAME": deque(maxlen=5000) }
MARKET_DATA = {}
ALL_INSTRUMENTS = []
q_instance = None
# Pay‑out tracking per candle time
PAYOUT_HISTORY = {}  # { "PAIR_TIMEFRAME": { "timestamp": payout } }


def ask_manual_credentials():
    """Run-er somoy manually Token (SSID) ar CSRF input ney."""
    print("=" * 80)
    print("🔐 MANUAL TOKEN + CSRF LOGIN (Email/Password লাগবে না)")
    print("=" * 80)
    print("Token পাবেন: qxbroker.com এ login → F12 → Console → window.settings.token")
    print("CSRF পাবেন: F12 → Application → Cookies → XSRF-TOKEN")
    print("-" * 80)

    ssid = SSID_TOKEN.strip() or input("👉 Enter SSID TOKEN: ").strip()
    csrf = CSRF_TOKEN.strip() or input("👉 Enter CSRF TOKEN (XSRF-TOKEN, optional): ").strip()

    if not ssid:
        print("❌ SSID TOKEN খালি রাখা যাবে না!")
        raise SystemExit(1)

    return ssid, csrf or None


def ts_to_dt(ts):
    """Convert timestamp to UTC+6 datetime."""
    if ts is None:
        return None
    try:
        ts = int(ts)
        if ts > 1_000_000_000_000:
            ts = ts / 1000
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt_utc.astimezone(UTC_PLUS_6)
    except:
        return None


def parse_timeframe(tf_str: str) -> int:
    """Convert time format (e.g., '1m', '5m', '15s') to seconds."""
    if not tf_str:
        return 60
    tf_str = tf_str.lower().strip()
    try:
        if tf_str.endswith('s'):
            return int(tf_str[:-1])
        elif tf_str.endswith('m'):
            return int(tf_str[:-1]) * 60
        elif tf_str.endswith('h'):
            return int(tf_str[:-1]) * 3600
        return int(tf_str)
    except:
        return 60


def get_payout(q, pair_name):
    """Retrieve current payout for a given pair from instrument list."""
    if not q or not hasattr(q, "api") or not q.api or not hasattr(q.api, "instruments"):
        return 0.0

    for inst in q.api.instruments:
        if len(inst) > 1 and inst[1] == pair_name:
            if len(inst) > 18 and inst[18] is not None:
                return float(inst[18])
            if len(inst) > 5 and inst[5] is not None:
                return float(inst[5])
            # Fallback: scan for any numeric value between 0 and 100
            for value in reversed(inst):
                if isinstance(value, (int, float)) and 0 <= value <= 100:
                    return float(value)
            return 0.0
    return 0.0


def get_payout_for_time(pair, timeframe, time_str):
    """
    Return the payout that was active at the given candle time.
    If not found, return the current payout.
    """
    storage_key = f"{pair}_{timeframe}"
    if storage_key in PAYOUT_HISTORY:
        payout_times = list(PAYOUT_HISTORY[storage_key].keys())
        if payout_times:
            if time_str in PAYOUT_HISTORY[storage_key]:
                return PAYOUT_HISTORY[storage_key][time_str]
            else:
                # Find closest previous time
                sorted_times = sorted(payout_times)
                for t in reversed(sorted_times):
                    if t <= time_str:
                        return PAYOUT_HISTORY[storage_key][t]
    return 0.0


def update_payout_history(pair, timeframe, time_str, payout):
    """Store payout for a specific candle time."""
    storage_key = f"{pair}_{timeframe}"
    if storage_key not in PAYOUT_HISTORY:
        PAYOUT_HISTORY[storage_key] = {}
    PAYOUT_HISTORY[storage_key][time_str] = payout


def detect_markets(q):
    """Auto‑detect all available trading pairs from Quotex."""
    if not q or not hasattr(q, "api") or not q.api:
        return []

    markets = []
    seen = set()

    try:
        for inst in q.api.instruments:
            if len(inst) < 2:
                continue
            pair = str(inst[1]).strip()
            if not pair or pair in seen:
                continue
            seen.add(pair)
            payout = get_payout(q, pair)
            pair_upper = pair.upper()

            # Categorise
            if pair.endswith("_otc") or pair.endswith("_OTC"):
                category = "OTC"
            elif any(x in pair_upper for x in ["BTC","ETH","LTC","BNB","SOL","DOGE","XRP","ADA","DOT","TRX","AVAX"]):
                category = "Crypto"
            elif any(x in pair_upper for x in ["XAU","XAG","WTI","BRENT","NGAS"]):
                category = "Commodity"
            elif any(x in pair_upper for x in ["US30","NAS100","SPX","GER40","UK100","JP225","AUS200","FR40","EU50"]):
                category = "Index"
            else:
                category = "Forex"

            markets.append({
                "pair": pair,
                "display_name": pair.replace("_otc", "").replace("_OTC", ""),
                "payout": payout,
                "category": category,
            })
    except Exception as e:
        print(f"❌ Market Detection Error: {e}")

    return sorted(markets, key=lambda x: x["pair"])


async def safe_fetch(q, pair, period):
    """Safely fetch candles, trying both v1 and v2."""
    try:
        raw = await q.get_candles(pair, period)
        if raw:
            return raw
    except:
        pass
    try:
        raw = await q.get_candle_v2(pair, period)
        if raw:
            return raw
    except:
        pass
    return None


def update_market_data(storage_key, candle_data):
    """
    Insert a candle into the deque with deduplication and sorting.
    The deque stores candles in **descending** time order (latest first).
    """
    if storage_key not in MARKET_DATA:
        MARKET_DATA[storage_key] = deque(maxlen=MAX_CANDLES)

    existing = list(MARKET_DATA[storage_key])
    found = False
    for i, c in enumerate(existing):
        if c["time"] == candle_data["time"]:
            existing[i] = candle_data
            found = True
            break
    if not found:
        existing.append(candle_data)

    # Remove duplicates by time (keep last occurrence)
    unique = {}
    for c in existing:
        unique[c["time"]] = c
    # Sort by time descending (latest first)
    sorted_data = sorted(unique.values(), key=lambda x: x["time"], reverse=True)

    new_deque = deque(maxlen=MAX_CANDLES)
    for c in sorted_data:
        new_deque.append(c)
    MARKET_DATA[storage_key] = new_deque


async def background_market_loop():
    """Main background loop: fetch candles and payouts continuously."""
    global q_instance, ALL_INSTRUMENTS

    # --- Manual token/csrf input (login system removed) ---
    ssid, csrf = ask_manual_credentials()

    q = Quotex()
    q.set_token_csrf(ssid_token=ssid, csrf_token=csrf)

    print("\n🔌 Connecting to Quotex (token mode, no login)...")
    check, reason = await q.connect()

    if not check or not await q.check_connect():
        print(f"❌ Connection failed! Reason: {reason}")
        print("💡 Token expire hote pare — browser theke notun token niye abar try korun.")
        return

    print("✅ Connected successfully (token mode — login ছাড়াই)")
    q.set_account_mode("practice" if USE_DEMO else "real")
    q_instance = q

    print(f"💼 Mode: {'DEMO' if USE_DEMO else 'REAL'} Account")
    print("✅ Starting data collection...")

    period = 60  # 1‑minute candles

    while True:
        try:
            # Auto‑detect available markets
            instruments = detect_markets(q)
            if instruments:
                ALL_INSTRUMENTS = instruments

            pairs = [m["pair"] for m in ALL_INSTRUMENTS] if ALL_INSTRUMENTS else ["USDPHP_otc", "USDARS_otc", "USDBRL_otc"]

            for pair in pairs:
                current_payout = get_payout(q, pair)
                raw_candles = await safe_fetch(q, pair, period)
                if not raw_candles:
                    continue

                storage_key = f"{pair}_1m"

                for r in raw_candles:
                    ts = r.get("time") or r.get("timestamp") or r.get("t") or r.get("date")
                    dt = ts_to_dt(ts)
                    if dt is None:
                        continue
                    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")

                    # Retrieve or store payout for this specific time
                    if storage_key in PAYOUT_HISTORY and time_str in PAYOUT_HISTORY[storage_key]:
                        payout_for_time = PAYOUT_HISTORY[storage_key][time_str]
                    else:
                        payout_for_time = current_payout
                        update_payout_history(pair, "1m", time_str, payout_for_time)

                    open_p = float(r.get("open") or r.get("o", 0))
                    close_p = float(r.get("close") or r.get("c", 0))
                    signal = "CALL" if close_p > open_p else "PUT"

                    candle_data = {
                        "close": close_p,
                        "high": float(r.get("high") or r.get("h", 0)),
                        "low": float(r.get("low") or r.get("l", 0)),
                        "open": open_p,
                        "pair": pair.replace('_otc', '-OTC').upper(),
                        "signal": signal,
                        "time": time_str,
                        "payout": payout_for_time
                    }

                    update_market_data(storage_key, candle_data)

                await asyncio.sleep(0.1)  # yield control

            print(f"🔄 [Cycle Complete] Updated {len(pairs)} markets. Active storage keys: {len(MARKET_DATA)}")
            await asyncio.sleep(5)  # update frequency

        except Exception as e:
            print(f"⚠️ Error in background loop: {e}")
            await asyncio.sleep(5)


def start_async_loop():
    """Run the async loop in a separate daemon thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(background_market_loop())


# --------------------- Flask API Routes ---------------------

@app.route('/', methods=['GET'])
def get_candle_data():
    """
    Return historical candle data for a given pair and timeframe.
    Query parameters:
      - Pair   : e.g. USDPHP_otc
      - Candle : number of latest candles to return (default 5000)
      - Time   : timeframe (currently only '1m' is collected)
    Example: http://103.191.50.90:5000/?Pair=USDPHP_otc&Candle=5000&Time=1m
    """
    pair = request.args.get('Pair') or request.args.get('pair') or "USDPHP_otc"
    candle_limit = request.args.get('Candle') or request.args.get('candle') or 5000
    timeframe = request.args.get('Time') or request.args.get('time') or "1m"

    try:
        candle_limit = int(candle_limit)
    except:
        candle_limit = 5000

    storage_key = f"{pair}_{timeframe}"
    if storage_key in MARKET_DATA:
        data_list = list(MARKET_DATA[storage_key])[:candle_limit]
    else:
        fallback_key = f"{pair}_1m"
        data_list = list(MARKET_DATA.get(fallback_key, []))[:candle_limit]

    response_payload = {
        "api_pair": pair,
        "timeframe": timeframe,
        "count": len(data_list),
        "data": data_list
    }
    return jsonify(response_payload)


@app.route('/markets', methods=['GET'])
def get_active_markets():
    """Return a list of all auto‑detected trading pairs with current payouts."""
    return jsonify({
        "status": "success",
        "total_markets": len(ALL_INSTRUMENTS),
        "markets": ALL_INSTRUMENTS
    })


@app.route('/latest', methods=['GET'])
def get_latest_candle():
    """Return the most recent candle for a given pair."""
    pair = request.args.get('Pair') or request.args.get('pair') or "USDPHP_otc"
    timeframe = request.args.get('Time') or request.args.get('time') or "1m"
    storage_key = f"{pair}_{timeframe}"
    if storage_key in MARKET_DATA and MARKET_DATA[storage_key]:
        latest = list(MARKET_DATA[storage_key])[0]
        return jsonify({"status": "success", "data": latest})
    else:
        return jsonify({
            "status": "error",
            "message": f"No data found for {pair} with timeframe {timeframe}"
        }), 404


@app.route('/payout-history', methods=['GET'])
def get_payout_history():
    """Return the payout history for a given pair (per candle time)."""
    pair = request.args.get('Pair') or request.args.get('pair') or "USDPHP_otc"
    timeframe = request.args.get('Time') or request.args.get('time') or "1m"
    storage_key = f"{pair}_{timeframe}"
    if storage_key in PAYOUT_HISTORY:
        return jsonify({
            "status": "success",
            "pair": pair,
            "timeframe": timeframe,
            "payout_history": PAYOUT_HISTORY[storage_key]
        })
    else:
        return jsonify({
            "status": "error",
            "message": f"No payout history found for {pair}"
        }), 404


# --------------------- Main Execution ---------------------

if __name__ == "__main__":
    # Start Quotex data fetcher in background thread
    t = threading.Thread(target=start_async_loop, daemon=True)
    t.start()

    # Start Flask server
    print(f"🚀 Flask API Server running on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)