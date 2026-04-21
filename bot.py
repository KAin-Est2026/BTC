import os
import time
import requests
import pandas as pd
from datetime import datetime

# --- SOZLAMALAR ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@sizning_kanalingiz")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "YOUR_API_KEY")

# Tahlil qilinadigan juftliklar (barchasi Twelve Data da bor)
SYMBOLS = [
    {"symbol": "XAU/USD",  "name": "Oltin",        "type": "forex"},
    {"symbol": "EUR/USD",  "name": "Euro/Dollar",   "type": "forex"},
    {"symbol": "GBP/USD",  "name": "Funt/Dollar",   "type": "forex"},
    {"symbol": "BTC/USD",  "name": "Bitcoin",       "type": "crypto"},
    {"symbol": "ETH/USD",  "name": "Ethereum",      "type": "crypto"},
    {"symbol": "AAPL",     "name": "Apple",         "type": "stock"},
    {"symbol": "TSLA",     "name": "Tesla",         "type": "stock"},
    {"symbol": "SPY",      "name": "S&P 500 ETF",   "type": "stock"},
]

INTERVAL = "1h"          # H1 timeframe
CHECK_EVERY = 60 * 60    # Har 1 soatda tekshiradi


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram xato: {e}")


# ── Twelve Data API ───────────────────────────────────────────────────────────

def get_candles(symbol: str, outputsize: int = 60) -> pd.DataFrame | None:
    """Twelve Data dan OHLCV ma'lumotlarini olish"""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            print(f"{symbol} xato: {data.get('message', 'nomalum')}")
            return None
        df = pd.DataFrame(data["values"])
        df = df.iloc[::-1].reset_index(drop=True)  # Eskidan yangi tartib
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"{symbol} so'rov xato: {e}")
        return None


# ── Texnik tahlil ────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def analyze(item: dict) -> dict | None:
    symbol = item["symbol"]
    df = get_candles(symbol)
    if df is None or len(df) < 30:
        return None

    close = df["close"]
    ema9  = ema(close, 9)
    ema21 = ema(close, 21)
    rsi14 = rsi(close, 14)

    prev9, prev21 = ema9.iloc[-2], ema21.iloc[-2]
    curr9, curr21 = ema9.iloc[-1], ema21.iloc[-1]
    curr_rsi   = rsi14.iloc[-1]
    curr_price = close.iloc[-1]

    # TP/SL foizi bozor turiga qarab
    pct = {"forex": 0.008, "crypto": 0.020, "stock": 0.015}.get(item["type"], 0.012)

    # BUY
    if prev9 < prev21 and curr9 > curr21 and 38 <= curr_rsi <= 65:
        return {**item,
                "action": "BUY",
                "price":  round(curr_price, 5),
                "tp1":    round(curr_price * (1 + pct),     5),
                "tp2":    round(curr_price * (1 + pct * 2), 5),
                "sl":     round(curr_price * (1 - pct),     5),
                "rsi":    round(curr_rsi, 1)}

    # SELL
    if prev9 > prev21 and curr9 < curr21 and 35 <= curr_rsi <= 62:
        return {**item,
                "action": "SELL",
                "price":  round(curr_price, 5),
                "tp1":    round(curr_price * (1 - pct),     5),
                "tp2":    round(curr_price * (1 - pct * 2), 5),
                "sl":     round(curr_price * (1 + pct),     5),
                "rsi":    round(curr_rsi, 1)}

    return None


# ── Xabar formati ─────────────────────────────────────────────────────────────

TYPE_EMOJI = {"forex": "💱", "crypto": "🪙", "stock": "📈"}

def format_signal(s: dict) -> str:
    action_emoji = "🟢" if s["action"] == "BUY" else "🔴"
    action_uz    = "SOTIB OL" if s["action"] == "BUY" else "SOT"
    t_emoji      = TYPE_EMOJI.get(s["type"], "📊")
    time_str     = datetime.utcnow().strftime("%d.%m.%Y %H:%M") + " UTC"

    return (
        f"{action_emoji} <b>{s['symbol']} — {action_uz}</b>  {t_emoji}\n"
        f"<i>{s['name']}</i>\n\n"
        f"💰 <b>Kirish narxi:</b> {s['price']}\n"
        f"🎯 <b>TP1:</b> {s['tp1']}\n"
        f"🎯 <b>TP2:</b> {s['tp2']}\n"
        f"🛑 <b>Stop Loss:</b> {s['sl']}\n\n"
        f"📊 <b>RSI(14):</b> {s['rsi']}\n"
        f"⏱ <b>Timeframe:</b> H1\n"
        f"⏰ {time_str}\n\n"
        f"⚠️ <i>Faqat tahlil. Savdo qilishdan oldin o'zingiz tekshiring.</i>"
    )


# ── Asosiy tsikl ──────────────────────────────────────────────────────────────

def main():
    print("Signal bot ishga tushdi...")
    symbols_list = ", ".join(s["symbol"] for s in SYMBOLS)
    send_telegram(
        f"✅ <b>Signal bot ishga tushdi!</b>\n\n"
        f"📋 Juftliklar: <code>{symbols_list}</code>\n"
        f"⏱ Timeframe: H1\n"
        f"🔄 Tekshiruv: har 1 soatda"
    )

    while True:
        print(f"\n[{datetime.utcnow().strftime('%H:%M')}] Tekshirilmoqda...")
        found = 0
        for item in SYMBOLS:
            try:
                result = analyze(item)
                if result:
                    msg = format_signal(result)
                    send_telegram(msg)
                    print(f"  ✓ Signal: {item['symbol']} {result['action']}")
                    found += 1
                else:
                    print(f"  – Signal yo'q: {item['symbol']}")
                time.sleep(2)   # API rate limit uchun
            except Exception as e:
                print(f"  ! Xato {item['symbol']}: {e}")

        if found == 0:
            print("  Hech qanday signal topilmadi.")

        print(f"Keyingi tekshiruv {CHECK_EVERY // 60} daqiqadan so'ng...")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
