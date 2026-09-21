"""
bot.py — XAU/USD Sniper Scalping Bot
======================================
Tahlil:  H4 (trend) + H1 (zona)
Entry:   M15 + M5 (EMA9/21 kesishuvi, faqat yopilgan shamlarda)
SL:      0.7 × ATR (M15)
TP:      H1/H4 swing levellar (kamida 1R), topilmasa ATR
Cron:    */15 7-21 * * 1-5  (GitHub Actions)
Signal:  faqat London va New York sessiyalarida
"""

import os, time, requests, pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_KEY       = os.environ["TWELVE_DATA_KEY"]

SYMBOL    = "XAU/USD"
DIGITS    = 2
RUN_EVERY = 15    # daqiqa — workflow'dagi cron bilan bir xil bo'lsin
MIN_RR    = 1.0   # TP kirishdan kamida shuncha R uzoqda bo'lsin

# Sessiyalar mahalliy vaqtda (qishki/yozgi vaqtni Python o'zi hisoblaydi)
SESSIONS = {
    "London":   ("Europe/London",    8, 17),
    "New York": ("America/New_York", 8, 17),
}

# ── Vaqt ──────────────────────────────────────────────────────────────────────

def current_slot(now: datetime) -> datetime:
    """Cron slot: 10:07 -> 10:00 (GitHub kechiksa ham oyna to'g'ri olinadi)"""
    return now.replace(minute=now.minute - now.minute % RUN_EVERY,
                       second=0, microsecond=0)

def active_sessions(t: datetime) -> list:
    """Hozir ochiq sessiyalar"""
    res = []
    for name, (tz, start, end) in SESSIONS.items():
        local = t.astimezone(ZoneInfo(tz))
        if local.weekday() < 5 and start <= local.hour < end:
            res.append(name)
    return res

# ── Indikatorlar ──────────────────────────────────────────────────────────────

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def atr(df: pd.DataFrame, p: int = 14) -> float:
    """ATR — Wilder usuli (TradingView / MT5 dagi bilan bir xil)"""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / p, adjust=False).mean().iloc[-1])

def macd_hist(s: pd.Series) -> pd.Series:
    m = ema(s, 12) - ema(s, 26)
    return m - ema(m, 9)

def crossed(df: pd.DataFrame, trend: str, since: datetime, until: datetime) -> bool:
    """EMA9/21 kesishuvi (since, until] oralig'ida yopilgan shamda bo'ldimi.
    Oyna = oxirgi 15 daqiqa, shu sabab har kesishuv faqat BIR marta signal beradi."""
    d = ema(df["close"], 9) - ema(df["close"], 21)
    if trend == "BUY":
        cross = (d.shift() < 0) & (d >= 0)
    else:
        cross = (d.shift() > 0) & (d <= 0)
    window = (df["close_time"] > since) & (df["close_time"] <= until)
    return bool((cross & window).any())

def swing_highs(df: pd.DataFrame, n: int = 3) -> list:
    """Swing high: har tomonida n ta bar pastroq"""
    levels = []
    for i in range(n, len(df) - n):
        h = df["high"].iloc[i]
        if all(h > df["high"].iloc[i-j] for j in range(1, n+1)) and \
           all(h > df["high"].iloc[i+j] for j in range(1, n+1)):
            levels.append(h)
    return sorted(set(round(x, DIGITS) for x in levels))

def swing_lows(df: pd.DataFrame, n: int = 3) -> list:
    """Swing low: har tomonida n ta bar balandroq"""
    levels = []
    for i in range(n, len(df) - n):
        l = df["low"].iloc[i]
        if all(l < df["low"].iloc[i-j] for j in range(1, n+1)) and \
           all(l < df["low"].iloc[i+j] for j in range(1, n+1)):
            levels.append(l)
    return sorted(set(round(x, DIGITS) for x in levels))

def pick_tps(trend: str, price: float, sl_dist: float, atr_val: float,
             lv_h1: list, lv_h4: list) -> list:
    """3 ta TP, doim tartibli (TP1 eng yaqin):
    - eng yaqin H1 level + eng yaqin H4 levellar
    - kamida MIN_RR uzoqlikda (juda yaqin level R/R ni buzadi)
    - bir-biriga 0.3×ATR dan yaqin levellar tashlanadi (TP1 = TP2 bo'lmaydi)
    - yetmasa ATR zaxira (2×, 3×, 4× ATR)"""
    sign = 1 if trend == "BUY" else -1
    gap  = atr_val * 0.3

    def nearest(levels: list, k: int) -> list:
        ok = [l for l in levels if (l - price) * sign >= sl_dist * MIN_RR]
        return sorted(ok, key=lambda l: abs(l - price))[:k]

    tps = []
    for l in nearest(lv_h1, 1) + nearest(lv_h4, 3):
        if len(tps) < 3 and all(abs(l - t) >= gap for t in tps):
            tps.append(l)

    m = 2.0
    while len(tps) < 3:
        l = round(price + sign * atr_val * m, DIGITS)
        if all(abs(l - t) >= gap for t in tps):
            tps.append(l)
        m += 1.0

    return sorted(tps, key=lambda l: abs(l - price))

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
        if "price" in r:
            return float(r["price"])
        print(f"  [price] {r.get('message', '?')}")
    except Exception as e:
        print(f"  [price] {e}")
    return None

def get_candles(interval: str, size: int, now: datetime) -> pd.DataFrame | None:
    """Faqat YOPILGAN shamlar — shakllanayotgan oxirgi sham tashlab yuboriladi"""
    _wait()
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     SYMBOL,
                "interval":   interval,
                "outputsize": size,
                "timezone":   "UTC",
                "apikey":     TWELVE_KEY,
            },
            timeout=15
        ).json()
        if "values" not in r:
            print(f"  [{interval}] {r.get('message', '?')}")
            return None
        df = pd.DataFrame(r["values"]).iloc[::-1].reset_index(drop=True)
        for c in ["open", "high", "low", "close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["close_time"] = pd.to_datetime(df["datetime"], utc=True) + pd.Timedelta(interval)
        df = df.dropna(subset=["open", "high", "low", "close"])
        return df[df["close_time"] <= now].reset_index(drop=True)
    except Exception as e:
        print(f"  [{interval}] {e}")
        return None

# ── Tahlil ────────────────────────────────────────────────────────────────────

def analyze(now: datetime, slot: datetime) -> dict | None:
    since = slot - timedelta(minutes=RUN_EVERY)

    # ── H4: asosiy trend (400 bar — EMA200 to'g'ri "isinishi" uchun) ─────────
    h4 = get_candles("4h", 400, now)
    if h4 is None or len(h4) < 250:
        print("  H4 yetarli emas")
        return None

    e50_h4    = float(ema(h4["close"], 50).iloc[-1])
    e200_h4   = float(ema(h4["close"], 200).iloc[-1])
    buffer_h4 = atr(h4) * 0.15

    if e50_h4 > e200_h4 + buffer_h4:
        trend = "BUY"
    elif e50_h4 < e200_h4 - buffer_h4:
        trend = "SELL"
    else:
        print(f"  H4 trend aniq emas (EMA50={e50_h4:.2f} EMA200={e200_h4:.2f})")
        return None
    print(f"  H4 trend: {trend}")

    # ── H1: zona tasdiqi (200 bar — EMA50 to'liq "isinishi" uchun) ──────────
    h1 = get_candles("1h", 200, now)
    if h1 is None or len(h1) < 150:
        print("  H1 yetarli emas")
        return None

    e50_h1   = float(ema(h1["close"], 50).iloc[-1])
    close_h1 = float(h1["close"].iloc[-1])
    if trend == "BUY" and close_h1 < e50_h1:
        print("  H1: narx EMA50 ostida — BUY o'tkazildi")
        return None
    if trend == "SELL" and close_h1 > e50_h1:
        print("  H1: narx EMA50 ustida — SELL o'tkazildi")
        return None

    # ── M15 / M5: faqat oxirgi 15 daqiqada yopilgan shamdagi kesishuv ────────
    m15 = get_candles("15min", 150, now)
    if m15 is None or len(m15) < 100:
        print("  M15 yetarli emas")
        return None

    if crossed(m15, trend, since, slot):
        entry_tf = "M15"
    else:
        m5 = get_candles("5min", 100, now)
        if m5 is None or len(m5) < 60:
            print("  M5 yetarli emas")
            return None
        if not crossed(m5, trend, since, slot):
            print("  M15/M5 da yangi kesishuv yo'q")
            return None
        entry_tf = "M5"

    # ── MACD tasdiqi (M15) ───────────────────────────────────────────────────
    hist  = macd_hist(m15["close"])
    h_now = float(hist.iloc[-1])
    h_prv = float(hist.iloc[-2])
    if trend == "BUY" and not (h_now > 0 or h_now > h_prv):
        print("  MACD BUY ni tasdiqlamadi")
        return None
    if trend == "SELL" and not (h_now < 0 or h_now < h_prv):
        print("  MACD SELL ni tasdiqlamadi")
        return None

    # ── Narx: eng oxirida olinadi — entry iloji boricha yangi bo'lsin ────────
    price = get_price()
    if price is None:
        print("  Narx olinmadi")
        return None
    print(f"  Narx: {price}")

    # ── SL: 0.7 × ATR (M15) ──────────────────────────────────────────────────
    atr_m15 = atr(m15)
    sl_dist = round(atr_m15 * 0.7, DIGITS)
    sl = round(price - sl_dist if trend == "BUY" else price + sl_dist, DIGITS)

    # ── TP: haqiqiy swing levellar ───────────────────────────────────────────
    if trend == "BUY":
        tp1, tp2, tp3 = pick_tps(trend, price, sl_dist, atr_m15,
                                 swing_highs(h1), swing_highs(h4))
    else:
        tp1, tp2, tp3 = pick_tps(trend, price, sl_dist, atr_m15,
                                 swing_lows(h1), swing_lows(h4))

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
    }

# ── Telegram ──────────────────────────────────────────────────────────────────

def format_msg(s: dict) -> str:
    buy = s["action"] == "BUY"
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    return (
        f"{'🟢' if buy else '🔴'} <b>XAU/USD — {'SOTIB OL' if buy else 'SOT'}</b> 📊\n"
        f"<i>Oltin</i>\n\n"
        f"💰 Entry:  <b>{s['price']:.2f}</b>\n"
        f"🎯 TP1:   <b>{s['tp1']:.2f}</b>  (1:{s['rr1']}R)\n"
        f"🎯 TP2:   <b>{s['tp2']:.2f}</b>  (1:{s['rr2']}R)\n"
        f"🎯 TP3:   <b>{s['tp3']:.2f}</b>  (1:{s['rr3']}R)\n"
        f"🛑 SL:    <b>{s['sl']:.2f}</b>  (0.7×ATR | ATR: {s['atr']})\n\n"
        f"✅ H4 {'📈 Uptrend' if buy else '📉 Downtrend'}\n"
        f"✅ H1 narx EMA50 {'ustida' if buy else 'ostida'}\n"
        f"✅ {s['entry_tf']} EMA9/21 kesdi (yopilgan sham)\n"
        f"✅ MACD: {s['macd']}\n\n"
        f"🕐 Sessiya: {s['session']}\n"
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
    now  = datetime.now(timezone.utc)
    slot = current_slot(now)
    print(f"\n{'='*40}\nXAU/USD Bot: {now:%d.%m.%Y %H:%M} UTC\n{'='*40}")

    sessions = active_sessions(slot)
    if not sessions:
        print("  Sessiya yopiq — tekshiruv yo'q")
        return
    session = " + ".join(sessions)
    print(f"  Sessiya: {session}")

    try:
        res = analyze(now, slot)
        if res:
            res["session"] = session
            print(
                f"\n✓ {res['action']} | Entry:{res['price']} | "
                f"TP1:{res['tp1']} TP2:{res['tp2']} TP3:{res['tp3']} | SL:{res['sl']}"
            )
            send(format_msg(res))
        else:
            print("  Signal yo'q")   # Telegram'ga yuborilmaydi — spam bo'lmasin
    except Exception as e:
        print(f"XATO: {e}")
        import traceback; traceback.print_exc()

    print("\nTugadi.")

if __name__ == "__main__":
    main()