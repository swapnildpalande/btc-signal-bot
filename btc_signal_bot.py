"""
BTC Weekly Options — Fully Automated Signal Bot v2
====================================================
Fixed: Deribit API error handling + retry + fallback APIs
"""

import urllib.request
import urllib.error
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# ╔══════════════════════════════════════════════════╗
# ║  CONFIG                                          ║
# ╚══════════════════════════════════════════════════╝

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

VRP_STRONG_SELL = 15
VRP_MILD_SELL = 5
VRP_MILD_BUY = -3
VRP_STRONG_BUY = -10
ZSCORE_STRONG = 1.5
ZSCORE_MILD = 0.75
TREND_THRESHOLD = 8
SIGNAL_THRESHOLD = 0.5

STATE_FILE = "signal_state.json"
LOG_FILE = "trade_log.json"

# ╔══════════════════════════════════════════════════╗
# ║  MATH                                           ║
# ╚══════════════════════════════════════════════════╝

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_straddle(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return abs(S - K) * 2
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    call = S * norm_cdf(d1) - K * norm_cdf(d2)
    put = K * norm_cdf(-d2) - S * norm_cdf(-d1)
    return call + put

# ╔══════════════════════════════════════════════════╗
# ║  HTTP — Retry + Error Handling                   ║
# ╚══════════════════════════════════════════════════╝

def fetch_json(url, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.reason}"
            print(f"   ⚠️ Attempt {attempt+1}/{retries}: {last_error}")
            try:
                body = e.read().decode()
                print(f"   Response: {body[:200]}")
            except:
                pass
        except Exception as e:
            last_error = str(e)
            print(f"   ⚠️ Attempt {attempt+1}/{retries}: {last_error}")
        if attempt < retries - 1:
            time.sleep((attempt + 1) * 5)
    raise Exception(f"API failed after {retries} attempts: {last_error}")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print("✅ Telegram message sent!")
                return True
    except Exception as e:
        print(f"❌ Telegram failed: {e}")
    return False

# ╔══════════════════════════════════════════════════╗
# ║  DATA — Multiple Sources with Fallback           ║
# ╚══════════════════════════════════════════════════╝

def get_btc_4h_deribit(days=30):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    url = (f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
           f"?instrument_name=BTC-PERPETUAL&start_timestamp={start_ms}"
           f"&end_timestamp={now_ms}&resolution=240")
    data = fetch_json(url)
    r = data.get("result", {})
    if not r or not r.get("close"):
        raise Exception("Empty Deribit BTC data")
    return [{"time": r["ticks"][i], "open": r["open"][i], "high": r["high"][i],
             "low": r["low"][i], "close": r["close"][i]} for i in range(len(r["close"]))]

def get_btc_4h_binance(days=30):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol=BTCUSDT&interval=4h&startTime={start_ms}&endTime={now_ms}&limit=1000")
    data = fetch_json(url)
    return [{"time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4])} for k in data]

def get_btc_4h_candles(days=30):
    for name, func in [("Deribit", get_btc_4h_deribit), ("Binance", get_btc_4h_binance)]:
        print(f"   Trying {name} BTC 4H...")
        try:
            c = func(days)
            print(f"   ✅ {name}: {len(c)} candles")
            return c
        except Exception as e:
            print(f"   ❌ {name}: {e}")
    raise Exception("All BTC sources failed")

def get_dvol_daily(days=60):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000

    # Try instrument names: BTC-DVOL, DVOL, btc_dvol
    for instrument in ["BTC-DVOL", "DVOL", "btc_dvol"]:
        print(f"   Trying Deribit DVOL ({instrument})...")
        try:
            url = (f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
                   f"?instrument_name={instrument}&start_timestamp={start_ms}"
                   f"&end_timestamp={now_ms}&resolution=1D")
            data = fetch_json(url, retries=2)
            r = data.get("result", {})
            if r and r.get("close") and len(r["close"]) > 5:
                candles = [{"time": r["ticks"][i], "close": r["close"][i]}
                          for i in range(len(r["close"]))]
                print(f"   ✅ DVOL ({instrument}): {len(candles)} candles")
                return candles
        except Exception as e:
            print(f"   ❌ {instrument}: {e}")

    # Fallback: volatility index endpoint
    print("   Trying Deribit get_volatility_index_data...")
    try:
        url = (f"https://www.deribit.com/api/v2/public/get_volatility_index_data"
               f"?currency=BTC&resolution=24"
               f"&start_timestamp={start_ms}&end_timestamp={now_ms}")
        data = fetch_json(url, retries=2)
        points = data.get("result", {}).get("data", [])
        if points and len(points) > 5:
            candles = [{"time": p[0], "close": p[4]} for p in points]
            print(f"   ✅ DVOL (index): {len(candles)} candles")
            return candles
    except Exception as e:
        print(f"   ❌ Index API: {e}")

    raise Exception("All DVOL sources failed")

def get_btc_price():
    for name, url_key in [
        ("Deribit", "https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL"),
        ("Binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"),
    ]:
        try:
            data = fetch_json(url_key, retries=2)
            if "result" in data:
                price = data["result"]["last_price"]
            elif "price" in data:
                price = float(data["price"])
            elif "bitcoin" in data:
                price = data["bitcoin"]["usd"]
            else:
                continue
            print(f"   💰 BTC ({name}): ${price:,.0f}")
            return price
        except:
            pass
    raise Exception("Cannot get BTC price")

# ╔══════════════════════════════════════════════════╗
# ║  SIGNAL                                          ║
# ╚══════════════════════════════════════════════════╝

def compute_rv(candles_4h, window=42):
    if len(candles_4h) < window + 1:
        return None
    closes = [c["close"] for c in candles_4h]
    log_ret = [math.log(closes[i]/closes[i-1]) for i in range(1, len(closes))
               if closes[i] > 0 and closes[i-1] > 0]
    if len(log_ret) < window:
        return None
    recent = log_ret[-window:]
    mean = sum(recent) / len(recent)
    var = sum((r - mean)**2 for r in recent) / (len(recent) - 1)
    return math.sqrt(var) * math.sqrt(2190) * 100

def compute_dvol_z(dvol_candles, window=30):
    if len(dvol_candles) < window:
        return None, None
    closes = [c["close"] for c in dvol_candles[-window:]]
    mean = sum(closes) / len(closes)
    var = sum((c - mean)**2 for c in closes) / (len(closes) - 1)
    std = math.sqrt(var) if var > 0 else 0.01
    return dvol_candles[-1]["close"], (dvol_candles[-1]["close"] - mean) / std

def compute_trend(candles_4h, sma_days=20):
    day_map = {}
    for c in candles_4h:
        day = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        day_map[day] = c["close"]
    days_sorted = sorted(day_map.keys())
    if len(days_sorted) < sma_days:
        return None
    recent = [day_map[d] for d in days_sorted[-sma_days:]]
    sma = sum(recent) / len(recent)
    return (recent[-1] - sma) / sma * 100

def generate_signal():
    print("📡 Fetching live data...")
    btc_4h = get_btc_4h_candles(30)
    dvol_daily = get_dvol_daily(60)
    btc_price = get_btc_price()

    rv = compute_rv(btc_4h)
    dvol, dvol_z = compute_dvol_z(dvol_daily)
    trend = compute_trend(btc_4h)

    if any(v is None for v in [rv, dvol, dvol_z, trend]):
        return None, f"Data insufficient: rv={rv}, dvol={dvol}, z={dvol_z}, trend={trend}"

    vrp = dvol - rv
    score = 0
    reasons = []

    if vrp > VRP_STRONG_SELL:    score += 1.5;  reasons.append(f"📈 VRP {vrp:+.1f} → Strong SELL")
    elif vrp > VRP_MILD_SELL:    score += 0.75; reasons.append(f"📈 VRP {vrp:+.1f} → Mild sell")
    elif vrp < VRP_STRONG_BUY:   score -= 1.5;  reasons.append(f"📉 VRP {vrp:+.1f} → Strong BUY")
    elif vrp < VRP_MILD_BUY:     score -= 0.75; reasons.append(f"📉 VRP {vrp:+.1f} → Mild buy")
    else:                         reasons.append(f"↔️ VRP {vrp:+.1f} → Neutral")

    if dvol_z > ZSCORE_STRONG:    score += 0.75; reasons.append(f"🔺 Z {dvol_z:+.2f} → Elevated")
    elif dvol_z > ZSCORE_MILD:    score += 0.35; reasons.append(f"🔺 Z {dvol_z:+.2f} → Slightly high")
    elif dvol_z < -ZSCORE_STRONG: score -= 0.75; reasons.append(f"🔻 Z {dvol_z:+.2f} → Depressed")
    elif dvol_z < -ZSCORE_MILD:   score -= 0.35; reasons.append(f"🔻 Z {dvol_z:+.2f} → Slightly low")

    if abs(trend) > TREND_THRESHOLD:
        if score > 0:
            score *= 0.5; reasons.append(f"⚠️ Trend {trend:+.1f}% → Short vol halved")
        else:
            reasons.append(f"🌊 Trend {trend:+.1f}% → Supports long vol")

    strike = round(btc_price / 100) * 100
    premium = bs_straddle(btc_price, strike, 5/365, dvol/100)

    if score > SIGNAL_THRESHOLD:
        position, action = "SHORT", "🔴 SELL ATM STRADDLE (Premium विका)"
    elif score < -SIGNAL_THRESHOLD:
        position, action = "LONG", "🟢 BUY ATM STRADDLE (Premium घ्या)"
    else:
        position, action = "FLAT", "⚪ NO TRADE (या आठवड्यात trade नाही)"

    size = min(1.0, abs(score)/2) if position != "FLAT" else 0

    return {"btc": btc_price, "dvol": dvol, "rv": rv, "vrp": vrp, "zscore": dvol_z,
            "trend": trend, "score": score, "position": position, "action": action,
            "size": size, "strike": strike, "premium": premium, "reasons": reasons}, None

# ╔══════════════════════════════════════════════════╗
# ║  MESSAGES                                        ║
# ╚══════════════════════════════════════════════════╝

def format_entry(sig):
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    days_to_fri = (4 - ist.weekday()) % 7 or 7
    friday = ist + timedelta(days=days_to_fri)
    msg = f"""<b>🤖 BTC WEEKLY OPTIONS SIGNAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━

<b>{sig['action']}</b>

📊 <b>Market Data:</b>
├ BTC: <b>${sig['btc']:,.0f}</b>
├ DVOL (IV): <b>{sig['dvol']:.1f}%</b>
├ RV 7d: <b>{sig['rv']:.1f}%</b>
├ VRP: <b>{sig['vrp']:+.1f}</b>
├ Z-Score: <b>{sig['zscore']:+.2f}</b>
└ Trend: <b>{sig['trend']:+.1f}%</b> from SMA20

📋 <b>Trade:</b>
├ Score: <b>{sig['score']:+.2f}</b>
├ Size: <b>{sig['size']*100:.0f}%</b>
├ Strike: <b>${sig['strike']:,.0f}</b>
├ Premium: <b>${sig['premium']:,.0f}</b> ({sig['premium']/sig['btc']*100:.2f}%)
└ Expiry: <b>{friday.strftime('%d %b %Y (Fri)')}</b>

🔍 <b>Reasons:</b>"""
    for r in sig['reasons']:
        msg += f"\n  {r}"
    if sig['position'] == "SHORT":
        msg += f"""

💡 <b>Action:</b>
Deribit → BTC Options → Weekly
Strike <b>${sig['strike']:,.0f}</b> → SELL Call + SELL Put
⚠️ Stop-loss: 2x premium"""
    elif sig['position'] == "LONG":
        msg += f"""

💡 <b>Action:</b>
Deribit → BTC Options → Weekly
Strike <b>${sig['strike']:,.0f}</b> → BUY Call + BUY Put
ℹ️ Max loss = premium"""
    else:
        msg += "\n\n💡 Trade करू नका. Monday ला नवीन signal."
    msg += f"\n\n⏰ {ist.strftime('%d %b %Y, %I:%M %p IST')}"
    return msg

def format_exit(entry):
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    btc_now = get_btc_price()
    strike, premium = entry.get("strike", 0), entry.get("premium", 0)
    pos, btc_entry = entry.get("position", "FLAT"), entry.get("btc", 0)
    intrinsic = abs(btc_now - strike)
    pnl = (premium - intrinsic) if pos == "SHORT" else (intrinsic - premium) if pos == "LONG" else 0
    size = entry.get("size", 1)
    pnl_sized = pnl * size
    pnl_pct = pnl_sized / btc_entry * 100 if btc_entry else 0
    btc_move = (btc_now - btc_entry) / btc_entry * 100 if btc_entry else 0
    emoji = "✅ PROFIT" if pnl_sized > 0 else "❌ LOSS"
    msg = f"""<b>📊 WEEKLY EXPIRY RESULT</b>
━━━━━━━━━━━━━━━━━━━━━━━━

<b>{emoji}</b>

💰 <b>Summary:</b>
├ BTC Mon: <b>${btc_entry:,.0f}</b>
├ BTC Fri: <b>${btc_now:,.0f}</b> ({btc_move:+.1f}%)
├ Strike: <b>${strike:,.0f}</b>
├ Premium: <b>${premium:,.0f}</b>
├ Intrinsic: <b>${intrinsic:,.0f}</b>
├ Position: <b>{pos}</b> @ {size*100:.0f}%
├ PnL: <b>${pnl_sized:+,.0f}</b>
└ Return: <b>{pnl_pct:+.2f}%</b>

🔄 Monday ला नवीन signal!
⏰ {ist.strftime('%d %b %Y, %I:%M %p IST')}"""
    return msg, pnl_sized

# ╔══════════════════════════════════════════════════╗
# ║  STATE                                           ║
# ╚══════════════════════════════════════════════════╝

def save_state(data):
    with open(STATE_FILE, "w") as f: json.dump(data, f, indent=2)
    print("💾 State saved")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return None

def append_log(entry):
    log = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f: log = json.load(f)
    log.append(entry); log = log[-200:]
    with open(LOG_FILE, "w") as f: json.dump(log, f, indent=2)

# ╔══════════════════════════════════════════════════╗
# ║  MAIN                                           ║
# ╚══════════════════════════════════════════════════╝

def main():
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    weekday = utc_now.weekday()
    day_name = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][weekday]
    print(f"🕐 {ist_now.strftime('%Y-%m-%d %H:%M IST')} ({day_name})")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Secrets not set!"); sys.exit(1)

    try:
        if weekday == 0:  # Monday
            print("\n📡 MONDAY — Entry signal...")
            sig, err = generate_signal()
            if err:
                send_telegram(f"❌ Signal error:\n{err}"); sys.exit(1)
            save_state({"date": ist_now.strftime("%Y-%m-%d"), "btc": sig["btc"],
                "dvol": sig["dvol"], "rv": sig["rv"], "vrp": sig["vrp"],
                "zscore": sig["zscore"], "trend": sig["trend"], "score": sig["score"],
                "position": sig["position"], "size": sig["size"],
                "strike": sig["strike"], "premium": sig["premium"]})
            send_telegram(format_entry(sig))
            print(f"✅ {sig['position']} | Score: {sig['score']:+.2f}")

        elif weekday == 4:  # Friday
            print("\n📊 FRIDAY — Exit result...")
            entry = load_state()
            if not entry:
                send_telegram("⚠️ Monday signal not found"); return
            if entry.get("position") == "FLAT":
                send_telegram("⚪ No trade this week.\n🔄 Monday ला नवीन signal!"); return
            msg, pnl = format_exit(entry)
            send_telegram(msg)
            append_log({"entry_date": entry.get("date"), "exit_date": ist_now.strftime("%Y-%m-%d"),
                "btc_entry": entry.get("btc"), "btc_exit": get_btc_price(),
                "position": entry.get("position"), "strike": entry.get("strike"), "pnl_usd": pnl})

        else:  # Test
            print(f"\n🧪 Test run ({day_name})...")
            sig, err = generate_signal()
            if err:
                send_telegram(f"🧪 <b>TEST — ERROR</b>\n\n{err}"); sys.exit(1)
            send_telegram(f"🧪 <b>TEST ({day_name})</b> — Bot working! ✅\n\n" + format_entry(sig))
            print(f"✅ Test: {sig['position']}")

    except Exception as e:
        print(f"❌ Fatal: {e}")
        try:
            send_telegram(f"🚨 <b>BOT ERROR</b>\n\n<code>{str(e)[:500]}</code>")
        except:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
