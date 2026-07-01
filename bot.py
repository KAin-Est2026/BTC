"""
bot_btc.py — BTC/USD Sniper Scalping Bot
==========================================
Tahlil:  H4 (trend) + H1 (zona)
Entry:   M15 + M5 (EMA9/21 crossover)
Filter:  RSI + Stochastic RSI + Engulfing/Pin Bar
MACD:    Histogram tasdiqi
SL:      0.7 × ATR (M15)
TP1:     H1 swing high/low
TP2:     H4 swing high/low
TP3:     H4 keyingi kuchli level
Cron:    0 */4 * * * python3 bot_btc.py
"""

import os, time, requests, pandas as pd
from datetime import datetime

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_KEY       = os.environ["TWELVE_DATA_KEY"]

SYMBOL = "BTC/USD"
DIGITS = 2

# =========================
# Indikatorlar
# =========================

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(p).mean()
    loss  = (-delta.clip(upper=0)).rolling(p).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def stoch_rsi(s: pd.Series, rsi_p: int = 14, stoch_p: int = 14, k: int = 3, d: int = 3):
    """
    Stochastic RSI — RSI ning o'z ichida stochastic hisoblash.
    K va D qaytaradi.
    """
    r = rsi(s, rsi_p)
    rsi_min = r.rolling(stoch_p).min()
    rsi_max = r.rolling(stoch_p).max()
    stoch   = 100 * (r - rsi_min) / (rsi_max - rsi_min + 1e-10)
    k_line  = stoch.rolling(k).mean()
    d_line  = k_line.rolling(d).mean()
    return k_line, d_line

def atr(df: pd.DataFrame, p: int = 14) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(p).mean().dropna()
    return float(val.iloc[-1]) if len(val) > 0 else 1.0

def engulfing(df: pd.DataFrame, direction: str) -> bool:
    o1, c1 = df["open"].iloc[-2], df["close"].iloc[-2]
    o2, c2 = df["open"].iloc[-1], df["close"].iloc[-1]
    if direction == "BUY":
        return c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1
    else:
        return c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1

def pin_bar(df: pd.DataFrame, direction: str) -> bool:
    o = df["open"].iloc[-1]
    c = df["close"].iloc[-1]
    h = df["high"].iloc[-1]
    l = df["low"].iloc[-1]
    body  = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if body == 0:
        return False
    if direction == "BUY":
        return lower >= body * 2 and upper <= body * 0.5
    else:
        return upper >= body * 2 and lower <= body * 0.5

def swing_highs(df: pd.DataFrame, n: int = 3) -> list:
    levels = []
    for i in range(n, len(df) - n):
        h = df["high"].iloc[i]
        if all(h > df["high"].iloc[i-j] for j in range(1, n+1)) and \
           all(h > df["high"].iloc[i+j] for j in range(1, n+1)):
            levels.append(h)
    return sorted(set(round(x, DIGITS) for x in levels))

def swing_lows(df: pd.DataFrame, n: int = 3) -> list:
    levels = []
    for i in range(n, len(df) - n):
        l = df["low"].iloc[i]
        if all(l < df["low"].iloc[i-j] for j in range(1, n+1)) and \
           all(l < df["low"].iloc[i+j] for j in range(1, n+1)):
            levels.append(l)
    return sorted(set(round(x, DIGITS) for x in levels))

def next_level_above(levels: list, price: float):
    above = [l for l in levels if l > price * 1.0005]
    return min(above) if above else None

def next_level_below(levels: list, price: float):
    below = [l for l in levels if l < price * 0.9995]
    return max(below) if below else None

# =========================
# Twelve Data API
# =========================

_last = 0

def _wait():
    global _last
    gap = time.time() - _last
    if gap < 8:
        time.sleep(8 - gap)
    _last = time.time()

def get_price() -> float | None:
    _wait()
    try:
        r = requests.get(
            "https://api.twelvedata.com/price",
            params={"symbol": SYMBOL, "apikey": TWELVE_KEY},
            timeout=10
        ).json()
        return float(r["price"]) if "price" in r else None
    except:
        return None

def get_candles(interval: str, size: int) -> pd.DataFrame | None:
    _wait()
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     SYMBOL,
                "interval":   interval,
                "outputsize": size,
                "apikey":     TWELVE_KEY,
            },
            timeout=15
        ).json()
        if "values" not in r:
            print(f"  [{interval}] {r.get('message','?')}")
            return None
        df = pd.DataFrame(r["values"]).iloc[::-1].reset_index(drop=True)
        for c in ["open", "high", "low", "close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    except Exception as e:
        print(f"  [{interval}] {e}")
        return None

# =========================
# Tahlil
# =========================

def analyze() -> dict | None:

    price = get_price()
    if price is None:
        print("  Narx olinmadi")
        return None
    print(f"  Narx: {price}")

    # ── H4: asosiy trend ──────────────────────────────────────────────────────
    h4 = get_candles("4h", 250)
    if h4 is None or len(h4) < 200:
        print("  H4 yetarli emas")
        return None

    e50_h4  = float(ema(h4["close"], 50).iloc[-1])
    e200_h4 = float(ema(h4["close"], 200).iloc[-1])

    if e50_h4 > e200_h4 * 1.001:
        trend = "BUY"
    elif e50_h4 < e200_h4 * 0.999:
        trend = "SELL"
    else:
        print("  H4 trend aniq emas")
        return None
    print(f"  H4 trend: {trend}")

    # ── H1: zona tasdiqi ──────────────────────────────────────────────────────
    h1 = get_candles("1h", 100)
    if h1 is None or len(h1) < 50:
        print("  H1 yetarli emas")
        return None

    e50_h1   = float(ema(h1["close"], 50).iloc[-1])
    price_h1 = float(h1["close"].iloc[-1])

    if trend == "BUY"  and price_h1 < e50_h1:
        print("  H1: narx EMA50 ostida — BUY o'tkazildi")
        return None
    if trend == "SELL" and price_h1 > e50_h1:
        print("  H1: narx EMA50 ustida — SELL o'tkazildi")
        return None

    # ── M15: entry ────────────────────────────────────────────────────────────
    m15 = get_candles("15min", 60)
    if m15 is None or len(m15) < 40:
        print("  M15 yetarli emas")
        return None

    e9_m15  = ema(m15["close"], 9)
    e21_m15 = ema(m15["close"], 21)

    cross_up_m15 = any(
        e9_m15.iloc[i-1] < e21_m15.iloc[i-1] and e9_m15.iloc[i] >= e21_m15.iloc[i]
        for i in range(-5, 0)
    )
    cross_down_m15 = any(
        e9_m15.iloc[i-1] > e21_m15.iloc[i-1] and e9_m15.iloc[i] <= e21_m15.iloc[i]
        for i in range(-5, 0)
    )

    # ── M5: sniper entry ──────────────────────────────────────────────────────
    m5 = get_candles("5min", 60)
    if m5 is None or len(m5) < 30:
        print("  M5 yetarli emas")
        return None

    e9_m5  = ema(m5["close"], 9)
    e21_m5 = ema(m5["close"], 21)

    cross_up_m5 = any(
        e9_m5.iloc[i-1] < e21_m5.iloc[i-1] and e9_m5.iloc[i] >= e21_m5.iloc[i]
        for i in range(-4, 0)
    )
    cross_down_m5 = any(
        e9_m5.iloc[i-1] > e21_m5.iloc[i-1] and e9_m5.iloc[i] <= e21_m5.iloc[i]
        for i in range(-4, 0)
    )

    m15_ok = cross_up_m15 if trend == "BUY" else cross_down_m15
    m5_ok  = cross_up_m5  if trend == "BUY" else cross_down_m5

    if not m15_ok and not m5_ok:
        print("  M15 va M5 cross yo'q")
        return None

    entry_tf = "M15" if m15_ok else "M5"
    entry_df = m15 if m15_ok else m5

    # ── RSI filtri (M15) ──────────────────────────────────────────────────────
    rsi_val = float(rsi(m15["close"]).iloc[-1])
    if trend == "BUY"  and rsi_val > 65:
        print(f"  RSI overbought: {rsi_val:.1f} — o'tkazildi")
        return None
    if trend == "SELL" and rsi_val < 35:
        print(f"  RSI oversold: {rsi_val:.1f} — o'tkazildi")
        return None
    print(f"  RSI: {rsi_val:.1f} ✓")

    # ── Stochastic RSI filtri (M15) ───────────────────────────────────────────
    k_line, d_line = stoch_rsi(m15["close"])
    k_now = float(k_line.iloc[-1])
    d_now = float(d_line.iloc[-1])
    k_prv = float(k_line.iloc[-2])
    d_prv = float(d_line.iloc[-2])

    # BUY: K va D 20 dan oshib kelayotgan bo'lsin (oversold dan chiqish)
    # SELL: K va D 80 dan tushib kelayotgan bo'lsin (overbought dan chiqish)
    if trend == "BUY":
        stoch_ok = k_now > d_now and k_prv <= d_prv and k_now < 80
        if not stoch_ok:
            print(f"  Stoch RSI BUY tasdiqlamadi K:{k_now:.1f} D:{d_now:.1f}")
            return None
    else:
        stoch_ok = k_now < d_now and k_prv >= d_prv and k_now > 20
        if not stoch_ok:
            print(f"  Stoch RSI SELL tasdiqlamadi K:{k_now:.1f} D:{d_now:.1f}")
            return None
    print(f"  Stoch RSI: K:{k_now:.1f} D:{d_now:.1f} ✓")

    # ── Candle pattern tasdiqi ────────────────────────────────────────────────
    eng = engulfing(entry_df, trend)
    pin = pin_bar(entry_df, trend)
    if not eng and not pin:
        print("  Candle pattern yo'q (engulfing/pin bar)")
        return None
    pattern = "Engulfing" if eng else "Pin Bar"
    print(f"  Pattern: {pattern} ✓")

    # ── MACD histogram tasdiqi (M15) ──────────────────────────────────────────
    macd_s = ema(m15["close"], 12) - ema(m15["close"], 26)
    hist   = macd_s - ema(macd_s, 9)
    h_now  = float(hist.iloc[-1])
    h_prv  = float(hist.iloc[-2])

    if trend == "BUY"  and not (h_now > 0 or h_now > h_prv):
        print("  MACD BUY tasdiqlamadi")
        return None
    if trend == "SELL" and not (h_now < 0 or h_now < h_prv):
        print("  MACD SELL tasdiqlamadi")
        return None

    last_price = float(m15["close"].iloc[-1])

    # ── ATR → SL ──────────────────────────────────────────────────────────────
    atr_val = atr(m15, 14)
    sl_dist = round(atr_val * 0.7, DIGITS)

    sl = round(last_price - sl_dist, DIGITS) if trend == "BUY" \
         else round(last_price + sl_dist, DIGITS)

    # ── Swing levellardan TP ──────────────────────────────────────────────────
    h1_highs = swing_highs(h1, n=3)
    h1_lows  = swing_lows(h1,  n=3)
    h4_highs = swing_highs(h4, n=3)
    h4_lows  = swing_lows(h4,  n=3)

    if trend == "BUY":
        tp1 = next_level_above(h1_highs, last_price)
        tp2 = next_level_above(h4_highs, last_price)
        h4_above = sorted([l for l in h4_highs if l > last_price * 1.0005])
        tp3 = h4_above[1] if len(h4_above) >= 2 else None

        if tp1 is None: tp1 = round(last_price + atr_val * 2.0, DIGITS)
        if tp2 is None: tp2 = round(last_price + atr_val * 3.0, DIGITS)
        if tp3 is None: tp3 = round(last_price + atr_val * 4.0, DIGITS)

        tp1 = min(tp1, tp2, tp3)
        tp3 = max(tp1, tp2, tp3)
        tp2 = sorted([tp1, tp2, tp3])[1]
    else:
        tp1 = next_level_below(h1_lows, last_price)
        tp2 = next_level_below(h4_lows, last_price)
        h4_below = sorted([l for l in h4_lows if l < last_price * 0.9995], reverse=True)
        tp3 = h4_below[1] if len(h4_below) >= 2 else None

        if tp1 is None: tp1 = round(last_price - atr_val * 2.0, DIGITS)
        if tp2 is None: tp2 = round(last_price - atr_val * 3.0, DIGITS)
        if tp3 is None: tp3 = round(last_price - atr_val * 4.0, DIGITS)

        tp1 = max(tp1, tp2, tp3)
        tp3 = min(tp1, tp2, tp3)
        tp2 = sorted([tp1, tp2, tp3], reverse=True)[1]

    def rr(tp):
        return round(abs(tp - last_price) / sl_dist, 1) if sl_dist > 0 else 0

    return {
        "action":   trend,
        "price":    round(last_price, DIGITS),
        "sl":       sl,
        "tp1":      tp1,
        "tp2":      tp2,
        "tp3":      tp3,
        "rr1":      rr(tp1),
        "rr2":      rr(tp2),
        "rr3":      rr(tp3),
        "atr":      round(atr_val, DIGITS),
        "entry_tf": entry_tf,
        "pattern":  pattern,
        "rsi":      round(rsi_val, 1),
        "stoch_k":  round(k_now, 1),
        "stoch_d":  round(d_now, 1),
        "macd":     round(h_now, 4),
    }

# =========================
# Telegram
# =========================

def format_msg(s: dict) -> str:
    e   = "🟢" if s["action"] == "BUY" else "🔴"
    act = "SOTIB OL" if s["action"] == "BUY" else "SOT"
    now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    tr  = "📈 Uptrend" if s["action"] == "BUY" else "📉 Downtrend"

    return (
        f"{e} <b>BTC/USD — {act}</b> ₿\n"
        f"<i>Bitcoin</i>\n\n"
        f"💰 Entry:  <b>{s['price']:.2f}</b>\n"
        f"🎯 TP1:   <b>{s['tp1']:.2f}</b>  (1:{s['rr1']}R)\n"
        f"🎯 TP2:   <b>{s['tp2']:.2f}</b>  (1:{s['rr2']}R)\n"
        f"🎯 TP3:   <b>{s['tp3']:.2f}</b>  (1:{s['rr3']}R)\n"
        f"🛑 SL:    <b>{s['sl']:.2f}</b>  (0.7×ATR: {s['atr']})\n\n"
        f"✅ H4 {tr}\n"
        f"✅ H1 narx EMA50 {'ustida' if s['action']=='BUY' else 'ostida'}\n"
        f"✅ {s['entry_tf']} EMA9/21 kesdi\n"
        f"✅ RSI: {s['rsi']} | Stoch K:{s['stoch_k']} D:{s['stoch_d']}\n"
        f"✅ MACD: {s['macd']} | Pattern: {s['pattern']}\n\n"
        f"⏰ {now}\n"
        f"⚠️ Risk: 1-2%"
    )

def send(msg: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        ).json()
        print("  ✓ Yuborildi" if r.get("ok") else f"  ✗ {r}")
    except Exception as e:
        print(f"  ✗ {e}")

# =========================
# Main
# =========================

def main():
    now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    print(f"\n{'='*40}\nBTC/USD Bot: {now}\n{'='*40}")

    try:
        res = analyze()
        if res:
            print(
                f"\n✓ {res['action']} | "
                f"Entry:{res['price']} | "
                f"TP1:{res['tp1']} TP2:{res['tp2']} TP3:{res['tp3']} | "
                f"SL:{res['sl']} | {res['pattern']}"
            )
            send(format_msg(res))
        else:
            now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
            send(
                f"📊 <b>BTC/USD — {now}</b>\n\n"
                f"Signal yo'q.\n"
                f"⏰ Keyingi tekshiruv 4 soatdan so'ng."
            )
    except Exception as e:
        print(f"XATO: {e}")
        import traceback; traceback.print_exc()

    print("\nTugadi.")

if __name__ == "__main__":
    main()
