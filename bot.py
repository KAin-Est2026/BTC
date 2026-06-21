"""
bot.py — BTC/USD Sniper Scalping Bot
======================================
Tahlil:  H4 (trend) + H1 (zona) + ADX filtri
Entry:   M15 + M5 (EMA9/21 crossover)
SL:      0.7 × ATR (M15)
TP1:     Torroq target (max 1.2R) — win-rate uchun
TP2:     H4 swing high/low
TP3:     H4 keyingi kuchli level
Filtrlar: ADX(H4) >= ADX_MIN, yangiliklar oynasi (NFP/FOMC) bloklanadi
Cron:    0 */4 * * * python3 bot.py

ESLATMA: hech qanday filtr 100% win-rate kafolatlamaydi — bu faqat
signal sifatini yaxshilaydi (kuchsiz trend va yangilik shovqinini kesib tashlaydi).
"""

import os, time, requests, pandas as pd
from datetime import datetime, time as dtime

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_KEY       = os.environ["TWELVE_DATA_KEY"]

SYMBOL = "BTC/USD"
DIGITS = 2

# ── Filtr sozlamalari ───────────────────────────────────────────────────────
ADX_MIN    = 20    # H4 ADX shundan past bo'lsa — trend kuchsiz, signal yo'q
TP1_MAX_R  = 1.2   # TP1 SL masofasidan necha barobar uzoq bo'lishi mumkin (torroq = ko'proq hit)

# Qo'lda kiritiladigan yuqori ta'sirli yangiliklar (FOMC, CPI va h.k.)
# Format: "YYYY-MM-DDTHH:MM" (UTC). Har birining oldidan/keyin 60 daqiqa bloklanadi.
NEWS_BLACKOUT_MANUAL = [
    # "2026-07-29T18:00",  # misol: FOMC qarori
]
NEWS_BLACKOUT_PAD_MIN = 60

# ── Indikatorlar ──────────────────────────────────────────────────────────────

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def atr(df: pd.DataFrame, p: int = 14) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(p).mean().dropna()
    return float(val.iloc[-1]) if len(val) > 0 else 1.0

def macd_hist(s: pd.Series) -> pd.Series:
    m = ema(s, 12) - ema(s, 26)
    return m - ema(m, 9)

def adx(df: pd.DataFrame, p: int = 14) -> float:
    """Wilder ADX — trend kuchini o'lchaydi (0-100). >25 kuchli, <20 kuchsiz/flet."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm  = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_w     = tr.ewm(alpha=1/p, adjust=False).mean()
    plus_di   = 100 * (plus_dm.ewm(alpha=1/p, adjust=False).mean()  / atr_w)
    minus_di  = 100 * (minus_dm.ewm(alpha=1/p, adjust=False).mean() / atr_w)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    val = dx.ewm(alpha=1/p, adjust=False).mean().dropna()
    return float(val.iloc[-1]) if len(val) > 0 else 0.0

def swing_highs(df: pd.DataFrame, n: int = 3) -> list:
    """Swing high levellarni topish — har tomonida n ta bar past bo'lishi kerak"""
    levels = []
    for i in range(n, len(df) - n):
        h = df["high"].iloc[i]
        if all(h > df["high"].iloc[i-j] for j in range(1, n+1)) and \
           all(h > df["high"].iloc[i+j] for j in range(1, n+1)):
            levels.append(h)
    return sorted(set(round(x, DIGITS) for x in levels))

def swing_lows(df: pd.DataFrame, n: int = 3) -> list:
    """Swing low levellarni topish"""
    levels = []
    for i in range(n, len(df) - n):
        l = df["low"].iloc[i]
        if all(l < df["low"].iloc[i-j] for j in range(1, n+1)) and \
           all(l < df["low"].iloc[i+j] for j in range(1, n+1)):
            levels.append(l)
    return sorted(set(round(x, DIGITS) for x in levels))

def next_level_above(levels: list, price: float) -> float | None:
    """Narxdan yuqoridagi eng yaqin level"""
    above = [l for l in levels if l > price * 1.0005]
    return min(above) if above else None

def next_level_below(levels: list, price: float) -> float | None:
    """Narxdan pastdagi eng yaqin level"""
    below = [l for l in levels if l < price * 0.9995]
    return max(below) if below else None

# ── API ───────────────────────────────────────────────────────────────────────

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

# ── Yangiliklar oynasi filtri ────────────────────────────────────────────────

def is_nfp_day(dt: datetime) -> bool:
    """Oyning birinchi Jumasi — NFP odatda shu kuni chiqadi."""
    return dt.weekday() == 4 and 1 <= dt.day <= 7

def is_news_blackout(dt: datetime | None = None) -> tuple[bool, str | None]:
    dt = dt or datetime.utcnow()

    # NFP: 12:00–14:00 UTC oralig'i (EST/EDT farqini qoplash uchun keng oyna)
    if is_nfp_day(dt) and dtime(12, 0) <= dt.time() <= dtime(14, 0):
        return True, "NFP"

    # Qo'lda kiritilgan voqealar (FOMC, CPI va h.k.)
    for ev in NEWS_BLACKOUT_MANUAL:
        ev_dt = datetime.fromisoformat(ev)
        delta_min = abs((dt - ev_dt).total_seconds()) / 60
        if delta_min <= NEWS_BLACKOUT_PAD_MIN:
            return True, "Manual hodisa"

    return False, None

# ── Tahlil ────────────────────────────────────────────────────────────────────

def analyze() -> dict | None:

    # Real narx
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

    # ── ADX filtri: kuchsiz trendda signal bermaymiz ────────────────────────────
    adx_h4 = adx(h4, 14)
    if adx_h4 < ADX_MIN:
        print(f"  ADX past ({adx_h4:.1f} < {ADX_MIN}) — trend kuchsiz, signal yo'q")
        return None
    print(f"  ADX(H4): {adx_h4:.1f}")

    # ── H1: zona tasdiqi ──────────────────────────────────────────────────────
    h1 = get_candles("1h", 100)
    if h1 is None or len(h1) < 50:
        print("  H1 yetarli emas")
        return None

    e50_h1    = float(ema(h1["close"], 50).iloc[-1])
    price_h1  = float(h1["close"].iloc[-1])

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

    if trend == "BUY":
        m15_ok = cross_up_m15
        m5_ok  = cross_up_m5
    else:
        m15_ok = cross_down_m15
        m5_ok  = cross_down_m5

    if not m15_ok and not m5_ok:
        print("  M15 va M5 cross yo'q")
        return None

    entry_tf = "M15" if m15_ok else "M5"

    # ── MACD tasdiqi (M15) ────────────────────────────────────────────────────
    hist  = macd_hist(m15["close"])
    h_now = float(hist.iloc[-1])
    h_prv = float(hist.iloc[-2])

    if trend == "BUY"  and not (h_now > 0 or h_now > h_prv):
        print("  MACD BUY tasdiqlamadi")
        return None
    if trend == "SELL" and not (h_now < 0 or h_now < h_prv):
        print("  MACD SELL tasdiqlamadi")
        return None

    # ── SL: 0.7 × ATR (M15) ──────────────────────────────────────────────────
    atr_m15 = atr(m15, 14)
    sl_dist = round(atr_m15 * 0.7, DIGITS)

    if trend == "BUY":
        sl = round(price - sl_dist, DIGITS)
    else:
        sl = round(price + sl_dist, DIGITS)

    # ── TP: haqiqiy swing levellar ────────────────────────────────────────────
    h1_highs = swing_highs(h1, n=3)
    h1_lows  = swing_lows(h1,  n=3)
    h4_highs = swing_highs(h4, n=3)
    h4_lows  = swing_lows(h4,  n=3)

    if trend == "BUY":
        tp1 = next_level_above(h1_highs, price)
        tp2 = next_level_above(h4_highs, price)
        # TP3: H4 dan ikkinchi level
        h4_above = sorted([l for l in h4_highs if l > price * 1.0005])
        tp3 = h4_above[1] if len(h4_above) >= 2 else None

        # Fallback: swing topilmasa ATR ishlatish
        if tp1 is None: tp1 = round(price + atr_m15 * 2.0, DIGITS)
        if tp2 is None: tp2 = round(price + atr_m15 * 3.0, DIGITS)
        if tp3 is None: tp3 = round(price + atr_m15 * 4.0, DIGITS)

        # Scalping uchun TP1'ni torroq qilish — hit-rate oshsin, TP2/TP3 bonus bo'lib qoladi
        tp1 = min(tp1, round(price + sl_dist * TP1_MAX_R, DIGITS))

        # Tartib: tp1 < tp2 < tp3
        tp1 = min(tp1, tp2, tp3)
        tp3 = max(tp1, tp2, tp3)
        tp2 = sorted([tp1, tp2, tp3])[1]

    else:
        tp1 = next_level_below(h1_lows, price)
        tp2 = next_level_below(h4_lows, price)
        h4_below = sorted([l for l in h4_lows if l < price * 0.9995], reverse=True)
        tp3 = h4_below[1] if len(h4_below) >= 2 else None

        if tp1 is None: tp1 = round(price - atr_m15 * 2.0, DIGITS)
        if tp2 is None: tp2 = round(price - atr_m15 * 3.0, DIGITS)
        if tp3 is None: tp3 = round(price - atr_m15 * 4.0, DIGITS)

        # Scalping uchun TP1'ni torroq qilish — hit-rate oshsin, TP2/TP3 bonus bo'lib qoladi
        tp1 = max(tp1, round(price - sl_dist * TP1_MAX_R, DIGITS))

        tp1 = max(tp1, tp2, tp3)
        tp3 = min(tp1, tp2, tp3)
        tp2 = sorted([tp1, tp2, tp3], reverse=True)[1]

    # R/R hisoblash
    def rr(tp):
        return round(abs(tp - price) / sl_dist, 1) if sl_dist > 0 else 0

    return {
        "action":   trend,
        "price":    round(price, DIGITS),
        "sl":       sl,
        "tp1":      tp1,
        "tp2":      tp2,
        "tp3":      tp3,
        "rr1":      rr(tp1),
        "rr2":      rr(tp2),
        "rr3":      rr(tp3),
        "atr":      round(atr_m15, DIGITS),
        "entry_tf": entry_tf,
        "macd":     round(h_now, 4),
        "adx":      round(adx_h4, 1),
    }

# ── Telegram ──────────────────────────────────────────────────────────────────

def format_msg(s: dict) -> str:
    e   = "🟢" if s["action"] == "BUY" else "🔴"
    act = "SOTIB OL" if s["action"] == "BUY" else "SOT"
    now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    tr  = "📈 Uptrend" if s["action"] == "BUY" else "📉 Downtrend"

    return (
        f"{e} <b>BTC/USD — {act}</b> 🪙\n"
        f"<i>Bitcoin</i>\n\n"
        f"💰 Entry:  <b>{s['price']:.2f}</b>\n"
        f"🎯 TP1:   <b>{s['tp1']:.2f}</b>  (1:{s['rr1']}R)\n"
        f"🎯 TP2:   <b>{s['tp2']:.2f}</b>  (1:{s['rr2']}R)\n"
        f"🎯 TP3:   <b>{s['tp3']:.2f}</b>  (1:{s['rr3']}R)\n"
        f"🛑 SL:    <b>{s['sl']:.2f}</b>  (0.7R | ATR:{s['atr']})\n\n"
        f"✅ H4 {tr}\n"
        f"✅ H1 narx EMA50 {'ustida' if s['action']=='BUY' else 'ostida'}\n"
        f"✅ {s['entry_tf']} EMA9/21 kesdi\n"
        f"✅ MACD: {s['macd']}\n"
        f"✅ ADX(H4): {s['adx']} (≥{ADX_MIN})\n\n"
        f"🎯 TP1 — torroq target (scalp uchun)\n"
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

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    print(f"\n{'='*40}\nBTC/USD Bot: {now}\n{'='*40}")

    blocked, reason = is_news_blackout()
    if blocked:
        print(f"  Yangiliklar oynasi: {reason} — tahlil o'tkazib yuborildi")
        send(
            f"⏸ <b>BTC/USD</b>\n\n"
            f"Yuqori ta'sirli yangilik oynasi ({reason}) tufayli "
            f"bu safar tahlil o'tkazib yuborildi.\n"
            f"⏰ {now}"
        )
        return

    try:
        res = analyze()
        if res:
            print(
                f"\n✓ {res['action']} | "
                f"Entry:{res['price']} | "
                f"TP1:{res['tp1']} TP2:{res['tp2']} TP3:{res['tp3']} | "
                f"SL:{res['sl']}"
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
