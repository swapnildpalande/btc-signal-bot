"""
BTC Weekly Options — Fully Automated Signal Bot
=================================================
GitHub Actions वर run होतो. Laptop बंद असलं तरी चालतो.
Monday → Entry Signal | Friday → Exit Result
Telegram वर direct message येतो.

कुठलीही extra library install करायची गरज नाही.
Standard Python 3 ने सगळं चालतं.
"""

import urllib.request
import urllib.error
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

# ╔══════════════════════════════════════════════════╗
# ║  CONFIG — GitHub Secrets मधून automatic येतं      ║
# ╚══════════════════════════════════════════════════╝

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Signal thresholds
VRP_STRONG_SELL = 15
VRP_MILD_SELL = 5
VRP_MILD_BUY = -3
VRP_STRONG_BUY = -10
ZSCORE_STRONG = 1.5
ZSCORE_MILD = 0.75
TREND_THRESHOLD = 8
SIGNAL_THRESHOLD = 0.5

# State files (GitHub Actions cache मध्ये save होतात)
STATE_FILE = "signal_state.json"
LOG_FILE = "trade_log.json"

# ╔══════════════════════════════════════════════════╗
# ║  MATH — scipy शिवाय Black-Scholes               ║
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
# ║  TELEGRAM                                        ║
# ╚══════════════════════════════════════════════════╝

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

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
            else:
                print(f"❌ Telegram error: {result}")
                return False
    except Exception as e:
        print(f"❌ Telegram failed: {e}")
        return False

# ╔══════════════════════════════════════════════════╗
# ║  DERIBIT API — Free, No Key Needed               ║
# ╚══════════════════════════════════════════════════╝

def get_btc_4h_candles(days=30):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    url = (f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
           f"?instrument_name=BTC-PERPETUAL&start_timestamp={start_ms}"
           f"&end_timestamp={now_ms}&resolution=240")
    data = fetch_json(url)
    result = data.get("result", {})
    candles = []
    for i in range(len(result.get("close", []))):
        candles.append({
            "time": result["ticks"][i],
            "open": result["open"][i],
            "high": result["high"][i],
            "low": result["low"][i],
            "close": result["close"][i],
        })
    print(f"   📊 BTC 4H candles: {len(candles)}")
    return candles

def get_dvol_daily(days=60):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    url = (f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
           f"?instrument_name=DVOL&start_timestamp={start_ms}"
           f"&end_timestamp={now_ms}&resolution=1D")
    data = fetch_json(url)
    result = data.get("result", {})
    candles = []
    for i in range(len(result.get("close", []))):
        candles.append({
            "time": result["ticks"][i],
            "close": result["close"][i],
        })
    print(f"   📊 DVOL daily candles: {len(candles)}")
    return candles

def get_btc_price():
    url = "https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL"
    data = fetch_json(url)
    price = data["result"]["last_price"]
    print(f"   💰 BTC Price: ${price:,.0f}")
    return price

# ╔══════════════════════════════════════════════════╗
# ║  SIGNAL COMPUTATION                              ║
# ╚══════════════════════════════════════════════════╝

def compute_rv(candles_4h, window=42):
    if len(candles_4h) < window + 1:
        return None
    closes = [c["close"] for c in candles_4h]
    log_ret = []
    for i in range(1, len(closes)):
        if closes[i] > 0 and closes[i-1] > 0:
            log_ret.append(math.log(closes[i] / closes[i-1]))
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
    current = dvol_candles[-1]["close"]
    return current, (current - mean) / std

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
    current = recent[-1]
    return (current - sma) / sma * 100

def generate_signal():
    print("📡 Fetching live data from Deribit...")
    btc_4h = get_btc_4h_candles(30)
    dvol_daily = get_dvol_daily(60)
    btc_price = get_btc_price()

    rv = compute_rv(btc_4h)
    dvol, dvol_z = compute_dvol_z(dvol_daily)
    trend = compute_trend(btc_4h)

    if any(v is None for v in [rv, dvol, dvol_z, trend]):
        return None, "Data insufficient"

    vrp = dvol - rv

    # --- Signal Score ---
    score = 0
    reasons = []

    if vrp > VRP_STRONG_SELL:
        score += 1.5;  reasons.append(f"📈 VRP {vrp:+.1f} → Strong SELL vol")
    elif vrp > VRP_MILD_SELL:
        score += 0.75; reasons.append(f"📈 VRP {vrp:+.1f} → Mild sell vol")
    elif vrp < VRP_STRONG_BUY:
        score -= 1.5;  reasons.append(f"📉 VRP {vrp:+.1f} → Strong BUY vol")
    elif vrp < VRP_MILD_BUY:
        score -= 0.75; reasons.append(f"📉 VRP {vrp:+.1f} → Mild buy vol")
    else:
        reasons.append(f"↔️ VRP {vrp:+.1f} → Neutral")

    if dvol_z > ZSCORE_STRONG:
        score += 0.75; reasons.append(f"🔺 DVOL Z {dvol_z:+.2f} → Elevated")
    elif dvol_z > ZSCORE_MILD:
        score += 0.35; reasons.append(f"🔺 DVOL Z {dvol_z:+.2f} → Slightly high")
    elif dvol_z < -ZSCORE_STRONG:
        score -= 0.75; reasons.append(f"🔻 DVOL Z {dvol_z:+.2f} → Depressed")
    elif dvol_z < -ZSCORE_MILD:
        score -= 0.35; reasons.append(f"🔻 DVOL Z {dvol_z:+.2f} → Slightly low")

    if abs(trend) > TREND_THRESHOLD:
        if score > 0:
            score *= 0.5
            reasons.append(f"⚠️ Trend {trend:+.1f}% → Short vol halved")
        else:
            reasons.append(f"🌊 Trend {trend:+.1f}% → Supports long vol")

    # Trade decision
    strike = round(btc_price / 100) * 100
    premium = bs_straddle(btc_price, strike, 5/365, dvol/100)

    if score > SIGNAL_THRESHOLD:
        position = "SHORT"
        action = "🔴 SELL ATM STRADDLE (Premium विका)"
    elif score < -SIGNAL_THRESHOLD:
        position = "LONG"
        action = "🟢 BUY ATM STRADDLE (Premium घ्या)"
    else:
        position = "FLAT"
        action = "⚪ NO TRADE (या आठवड्यात trade नाही)"

    size = min(1.0, abs(score)/2) if position != "FLAT" else 0

    return {
        "btc": btc_price, "dvol": dvol, "rv": rv, "vrp": vrp,
        "zscore": dvol_z, "trend": trend, "score": score,
        "position": position, "action": action, "size": size,
        "strike": strike, "premium": premium, "reasons": reasons,
    }, None

# ╔══════════════════════════════════════════════════╗
# ║  MESSAGE FORMATTING                              ║
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

💡 <b>कसे trade कराल:</b>
1. Deribit → BTC Options → Weekly Expiry
2. Strike <b>${sig['strike']:,.0f}</b>
3. SELL Call + SELL Put
4. Premium ~<b>${sig['premium']:,.0f}</b> collect होईल

⚠️ Stop-loss ठेवा: 2x premium"""

    elif sig['position'] == "LONG":
        msg += f"""

💡 <b>कसे trade कराल:</b>
1. Deribit → BTC Options → Weekly Expiry
2. Strike <b>${sig['strike']:,.0f}</b>
3. BUY Call + BUY Put
4. Cost ~<b>${sig['premium']:,.0f}</b>

ℹ️ Max loss = premium paid"""

    else:
        msg += """

💡 Signal weak — trade करू नका. Monday ला नवीन signal."""

    msg += f"\n\n⏰ {ist.strftime('%d %b %Y, %I:%M %p IST')}"
    return msg

def format_exit(entry):
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    btc_now = get_btc_price()
    strike = entry.get("strike", 0)
    premium = entry.get("premium", 0)
    pos = entry.get("position", "FLAT")
    btc_entry = entry.get("btc", 0)

    intrinsic = abs(btc_now - strike)
    if pos == "SHORT":
        pnl = premium - intrinsic
    elif pos == "LONG":
        pnl = intrinsic - premium
    else:
        pnl = 0

    size = entry.get("size", 1)
    pnl_sized = pnl * size
    pnl_pct = pnl_sized / btc_entry * 100 if btc_entry else 0
    emoji = "✅ PROFIT" if pnl_sized > 0 else "❌ LOSS"
    btc_move = (btc_now - btc_entry) / btc_entry * 100 if btc_entry else 0

    msg = f"""<b>📊 WEEKLY EXPIRY RESULT</b>
━━━━━━━━━━━━━━━━━━━━━━━━

<b>{emoji}</b>

💰 <b>Summary:</b>
├ BTC Monday: <b>${btc_entry:,.0f}</b>
├ BTC Friday: <b>${btc_now:,.0f}</b> ({btc_move:+.1f}%)
├ Strike: <b>${strike:,.0f}</b>
├ Premium: <b>${premium:,.0f}</b>
├ Intrinsic: <b>${intrinsic:,.0f}</b>
├ Position: <b>{pos}</b> @ {size*100:.0f}%
├ PnL: <b>${pnl_sized:+,.0f}</b>
└ Return: <b>{pnl_pct:+.2f}%</b>

🔄 Monday ला नवीन signal येईल!
⏰ {ist.strftime('%d %b %Y, %I:%M %p IST')}"""

    return msg, pnl_sized

# ╔══════════════════════════════════════════════════╗
# ║  STATE MANAGEMENT                                ║
# ╚══════════════════════════════════════════════════╝

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"💾 State saved → {STATE_FILE}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None

def append_log(entry):
    log = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            log = json.load(f)
    log.append(entry)
    log = log[-200:]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)
    print(f"📝 Trade logged ({len(log)} total)")

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
        print("❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set!")
        print("   GitHub repo → Settings → Secrets → Add them")
        sys.exit(1)

    # ─── MONDAY: Entry Signal ───
    if weekday == 0:
        print("\n📡 MONDAY — Generating entry signal...")
        sig, err = generate_signal()
        if err:
            send_telegram(f"❌ Signal Error: {err}")
            sys.exit(1)

        save_state({
            "date": ist_now.strftime("%Y-%m-%d"),
            "btc": sig["btc"], "dvol": sig["dvol"], "rv": sig["rv"],
            "vrp": sig["vrp"], "zscore": sig["zscore"], "trend": sig["trend"],
            "score": sig["score"], "position": sig["position"],
            "size": sig["size"], "strike": sig["strike"], "premium": sig["premium"],
        })

        send_telegram(format_entry(sig))
        print(f"✅ Signal: {sig['position']} | Score: {sig['score']:+.2f}")

    # ─── FRIDAY: Exit Result ───
    elif weekday == 4:
        print("\n📊 FRIDAY — Calculating expiry result...")
        entry = load_state()

        if not entry:
            send_telegram("⚠️ Monday चा signal सापडला नाही.\nManual check करा.")
            return

        if entry.get("position") == "FLAT":
            send_telegram("⚪ या आठवड्यात trade नव्हता (FLAT).\n🔄 Monday ला नवीन signal!")
            return

        msg, pnl = format_exit(entry)
        send_telegram(msg)

        append_log({
            "entry_date": entry.get("date"),
            "exit_date": ist_now.strftime("%Y-%m-%d"),
            "btc_entry": entry.get("btc"),
            "btc_exit": get_btc_price(),
            "position": entry.get("position"),
            "strike": entry.get("strike"),
            "pnl_usd": pnl,
        })

    # ─── Manual Run (Testing) ───
    else:
        print(f"\n📡 Manual/Test run ({day_name})...")
        sig, err = generate_signal()
        if err:
            print(f"❌ {err}")
            sys.exit(1)

        # Test mode — signal generate करतो पण "(TEST)" tag लावतो
        test_msg = f"🧪 <b>TEST RUN ({day_name})</b>\n" + format_entry(sig)
        send_telegram(test_msg)
        print(f"✅ Test signal sent: {sig['position']}")

if __name__ == "__main__":
    main()
  
