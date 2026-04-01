"""
BTC Active Range Bot — versión GitHub Actions
Misma lógica del indicador HTML.
Se ejecuta una vez cada 5 minutos vía GitHub Actions.
El estado se guarda en state.json dentro del propio repositorio.

Secrets requeridos en GitHub (Settings → Secrets → Actions):
  TG_TOKEN    → Token del bot de Telegram
  TG_CHAT_ID  → Tu Chat ID de Telegram
"""

import os
import json
import math
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
STATE_FILE = Path("state.json")

BINANCE_URL = "https://api.binance.com/api/v3/klines"

LEN_RANGE   = 10
SIZE_FACTOR = 1.5
CTX_LEN     = 5
CTX_THRESH  = 0.5

TF_LABELS = {"15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D"}
TF_LIMITS = {"15m": 500,   "1h": 500,  "4h": 300,  "1d": 200}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ESTADO — lectura y escritura en state.json
# ─────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"15m": None, "1h": None, "4h": None, "1d": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────
# HORA ESPAÑA
# ─────────────────────────────────────────────
def is_dst(dt: datetime) -> bool:
    year = dt.year
    march_end = datetime(year, 3, 31)
    dst_start = march_end - timedelta(days=(march_end.weekday() + 1) % 7)
    oct_end   = datetime(year, 10, 31)
    dst_end   = oct_end - timedelta(days=(oct_end.weekday() + 1) % 7)
    return dst_start <= dt.replace(tzinfo=None) < dst_end

def utc_to_spain(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=2 if is_dst(dt_utc) else 1)


# ─────────────────────────────────────────────
# MATEMÁTICAS DEL INDICADOR
# ─────────────────────────────────────────────
def calc_ema(data, n):
    if len(data) < n:
        return [None] * len(data)
    k = 2 / (n + 1)
    e = sum(data[:n]) / n
    result = [None] * (n - 1) + [e]
    for i in range(n, len(data)):
        e = data[i] * k + e * (1 - k)
        result.append(e)
    return result


def calc_rsi(closes, n=14):
    if len(closes) < n + 1:
        return [None] * len(closes)
    rsi = [None] * n
    gains, losses = [], []
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains) / n
    al = sum(losses) / n
    rsi.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        rsi.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    return rsi


def calc_atr(candles, n=14):
    if len(candles) < n + 1:
        return None
    r = candles[-(n + 1):]
    s = sum(max(r[i]["high"] - r[i]["low"],
                abs(r[i]["high"] - r[i-1]["close"]),
                abs(r[i]["low"]  - r[i-1]["close"])) for i in range(1, n + 1))
    return s / n


def is_big_candle(candles, idx, length, factor):
    if idx < length:
        return False
    slc = candles[idx - length: idx]
    rng_avg = sum(c["high"] - c["low"] for c in slc) / length
    bod_avg = sum(abs(c["close"] - c["open"]) for c in slc) / length
    c = candles[idx]
    return (c["high"] - c["low"] > rng_avg * factor) or \
           (abs(c["close"] - c["open"]) > bod_avg * factor)


def is_continuation(candles, idx, ctx_len, thresh):
    if idx < ctx_len:
        return False
    cur = candles[idx]
    bull = bear = 0
    for i in range(1, ctx_len + 1):
        c = candles[idx - i]
        if c["close"] > c["open"]: bull += 1
        if c["close"] < c["open"]: bear += 1
    return ((cur["close"] > cur["open"]) and bull / ctx_len >= thresh) or \
           ((cur["close"] < cur["open"]) and bear / ctx_len >= thresh)


def detect_active_range(candles, length, factor):
    in_range = False
    hi = lo = None
    hit_hi = hit_lo = False
    start_bar = None
    phase = 0
    touches_lo = touches_hi = 0
    wick_low = wick_high = None
    went_below = went_above = False
    retest_buy = retest_sell = False
    below_bar = above_bar = None
    away_from_lo = away_from_hi = False
    in_touch_lo = in_touch_hi = False
    is_cont = False
    states = []

    for i, c in enumerate(candles):
        did_break = False
        if in_range and hi is not None:
            if c["close"] > hi or c["close"] < lo:
                in_range = False; hi = lo = None
                hit_hi = hit_lo = False; start_bar = None; phase = 0
                touches_lo = touches_hi = 0; wick_low = wick_high = None
                went_below = went_above = False; retest_buy = retest_sell = False
                below_bar = above_bar = None; away_from_lo = away_from_hi = False
                in_touch_lo = in_touch_hi = False; is_cont = False
                did_break = True

        big = is_big_candle(candles, i, length, factor)

        if (did_break or not in_range) and big:
            hi = c["high"]; lo = c["low"]; in_range = True
            hit_hi = hit_lo = False; start_bar = i; phase = 0
            touches_lo = touches_hi = 0; wick_low = wick_high = None
            went_below = went_above = False; retest_buy = retest_sell = False
            below_bar = above_bar = None; away_from_lo = away_from_hi = False
            in_touch_lo = in_touch_hi = False
            is_cont = is_continuation(candles, i, CTX_LEN, CTX_THRESH)

        if in_range and start_bar is not None and i > start_bar:
            rng = hi - lo
            if c["close"] > lo + rng * 0.25: away_from_lo = True
            if c["close"] < lo + rng * 0.75: away_from_hi = True

            if c["low"] <= lo and c["close"] >= lo:
                if not in_touch_lo:
                    if away_from_lo or touches_lo == 0:
                        touches_lo += 1; away_from_lo = False
                        if not went_below: went_below = True; below_bar = i; wick_low = c["low"]
                        else: wick_low = min(wick_low, c["low"])
                    in_touch_lo = True
                hit_lo = True
            else:
                in_touch_lo = False

            if c["high"] >= hi and c["close"] <= hi:
                if not in_touch_hi:
                    if away_from_hi or touches_hi == 0:
                        touches_hi += 1; away_from_hi = False
                        if not went_above: went_above = True; above_bar = i; wick_high = c["high"]
                        else: wick_high = max(wick_high, c["high"])
                    in_touch_hi = True
                hit_hi = True
            else:
                in_touch_hi = False

            if went_below and phase == 0: phase = 1
            if went_above and phase == 0: phase = 2
            if went_above and phase == 1: phase = 2; retest_buy = False
            if went_below and phase == 2: phase = 1; retest_sell = False
            if hit_hi and hit_lo and phase != 2: phase = 2

            if went_below and not retest_buy and below_bar is not None and i > below_bar and c["low"] <= lo:
                retest_buy = True
            if went_above and not retest_sell and above_bar is not None and i > above_bar and c["high"] >= hi:
                retest_sell = True

        states.append({
            "in": in_range, "hi": hi, "lo": lo,
            "hit_hi": hit_hi, "hit_lo": hit_lo,
            "phase": phase, "is_cont": is_cont,
            "touches_lo": touches_lo, "touches_hi": touches_hi,
            "went_below": went_below, "went_above": went_above,
            "retest_buy": retest_buy, "retest_sell": retest_sell,
        })
    return states


def calc_bias(candles):
    states = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)
    last = states[-1] if states else None
    if not last or not last["in"]:
        return {"bias": 0}
    return {"bias": 1 if last["phase"] == 1 else (-1 if last["phase"] == 2 else 0)}


def compute_signal(candles_map, tf_key):
    candles = candles_map.get(tf_key)
    if not candles or len(candles) < LEN_RANGE + 5:
        return None

    closes  = [c["close"] for c in candles]
    price   = closes[-1]
    dt_utc  = datetime.fromtimestamp(candles[-1]["ts"] / 1000, tz=timezone.utc)
    states  = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)
    last    = states[-1]
    rsi_arr = calc_rsi(closes, 14)
    rsi     = rsi_arr[-1]
    atr     = calc_atr(candles, 14)

    sc = []; tot = 0

    def add(name, s, max_s, label, color):
        nonlocal tot
        sc.append({"name": name, "score": s, "max": max_s, "label": label, "color": color})
        tot += s

    if last["in"] and last["hi"]:
        if   last["phase"] == 1 and last["retest_buy"]:  add("Zona", 3, 3, "✓ Retest COMPRA confirmado", "green")
        elif last["phase"] == 1 and last["went_below"]:  add("Zona", 2, 3, "Tocó zona baja — Esperando retest", "green")
        elif last["phase"] == 1:                          add("Zona", 1, 3, "Fase alcista — Sesgo compra", "green")
        elif last["phase"] == 2 and last["retest_sell"]: add("Zona", -3, 3, "✓ Retest VENTA confirmado", "red")
        elif last["phase"] == 2 and last["went_above"]:  add("Zona", -2, 3, "Tocó zona alta — Esperando retest bajista", "red")
        elif last["phase"] == 2:                          add("Zona", -1, 3, "Fase bajista — Sesgo venta", "red")
        else:                                             add("Zona", 0, 3, "Sin fase definida", "yellow")
    else:
        add("Zona", 0, 3, "Sin rango activo", "yellow")

    if rsi is not None:
        if   rsi < 30: add("RSI", 3, 3, f"RSI {rsi:.1f} — Sobreventa extrema", "green")
        elif rsi < 45: add("RSI", 2, 3, f"RSI {rsi:.1f} — Presión compradora", "green")
        elif rsi < 55: add("RSI", 0, 3, f"RSI {rsi:.1f} — Neutral", "yellow")
        elif rsi < 70: add("RSI", -1, 3, f"RSI {rsi:.1f} — Precaución", "yellow")
        elif rsi < 80: add("RSI", -2, 3, f"RSI {rsi:.1f} — Sobrecompra", "red")
        else:          add("RSI", -3, 3, f"RSI {rsi:.1f} — Sobrecompra extrema", "red")

    biases    = [calc_bias(v) for k, v in candles_map.items() if k in TF_LABELS]
    cur_bias  = 1 if last["phase"] == 1 else (-1 if last["phase"] == 2 else 0)
    active_b  = [b for b in biases if b["bias"] != 0]
    aligned   = sum(1 for b in active_b if b["bias"] == cur_bias)

    if active_b:
        ratio = aligned / len(active_b)
        if   ratio >= 0.75: add("MTF", 2, 2, f"{aligned}/{len(active_b)} TFs alineados", "green")
        elif ratio >= 0.5:  add("MTF", 1, 2, "Alineación parcial", "yellow")
        elif ratio == 0:    add("MTF", -2, 2, "Divergencia MTF", "red")
        else:               add("MTF", -1, 2, "Alineación débil", "yellow")

    if last["in"]:
        add("Contexto", 1 if last["is_cont"] else -1, 1,
            "Continuación" if last["is_cont"] else "Posible reversión", "green")
        touches = last["touches_lo"] if last["phase"] == 1 else last["touches_hi"]
        if   touches >= 3: add("Fuerza", 2, 2, f"Zona testeada {touches}x — Muy fuerte", "green")
        elif touches == 2: add("Fuerza", 1, 2, "Zona confirmada (2 toques)", "green")
        else:              add("Fuerza", 0, 2, "Zona sin confirmar", "yellow")

    max_s = sum(x["max"] for x in sc)
    norm  = ((tot + max_s) / (max_s * 2) * 100) if max_s > 0 else 50
    thr   = 5

    rb = last["in"] and last["phase"] == 1 and last["retest_buy"]
    rs = last["in"] and last["phase"] == 2 and last["retest_sell"]

    if   rb and tot >= 3:              action, css = "COMPRA",       "buy"
    elif tot >= thr:                   action, css = "COMPRA",       "buy"
    elif tot >= math.ceil(thr / 2):    action, css = "COMPRA DÉBIL", "buy"
    elif rs and tot <= -3:             action, css = "VENDE",        "sell"
    elif tot <= -thr:                  action, css = "VENDE",        "sell"
    elif tot <= -math.ceil(thr / 2):   action, css = "VENDE DÉBIL",  "sell"
    elif not last["in"]:               action, css = "SIN RANGO",    "wait"
    else:                              action, css = "ESPERA",       "neutral"

    return {
        "action": action, "css": css, "tot": tot, "max_s": max_s, "norm": norm,
        "price": price, "dt_utc": dt_utc, "rsi": rsi, "atr": atr,
        "range_state": last,
    }


# ─────────────────────────────────────────────
# BINANCE
# ─────────────────────────────────────────────
def fetch_all_candles():
    data = {}
    for tf, limit in TF_LIMITS.items():
        r = requests.get(BINANCE_URL,
                         params={"symbol": "BTCUSDT", "interval": tf, "limit": limit},
                         timeout=15)
        r.raise_for_status()
        data[tf] = [{"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                    for k in r.json()]
        log.info(f"  {TF_LABELS[tf]}: {len(data[tf])} velas")
    return data


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def tg_send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10
    )
    return r.json().get("ok", False)


def build_message(sigs, changed_tfs):
    def em(a):
        return "🟢" if "COMPRA" in a else "🔴" if "VENDE" in a else "⬜" if a == "SIN RANGO" else "🟡"

    best = sigs.get("15m") or next(iter(sigs.values()), None)
    sl_line = tp_line = "—"
    if best and best["atr"] and best["css"] in ("buy", "sell"):
        is_buy  = best["css"] == "buy"
        sl      = best["price"] - best["atr"] if is_buy else best["price"] + best["atr"]
        tp      = best["price"] + best["atr"] * 1.5 if is_buy else best["price"] - best["atr"] * 1.5
        sl_line = f"${round(sl):,} (-{best['atr']/best['price']*100:.2f}%)".replace(",",".")
        tp_line = f"${round(tp):,} (+{best['atr']*1.5/best['price']*100:.2f}%)".replace(",",".")

    price_f = f"{best['price']:,.2f}".replace(",","X").replace(".","," ).replace("X",".") if best else "—"
    dt_sp   = utc_to_spain(best["dt_utc"]) if best else datetime.now()
    fecha   = dt_sp.strftime("%d/%m %H:%M") + "h España"
    changed = ", ".join(TF_LABELS[t] for t in changed_tfs)

    lines = []
    for tf in ("15m", "1h", "4h", "1d"):
        s = sigs.get(tf)
        if not s: continue
        rsi_txt = f" · RSI:{s['rsi']:.0f}" if s["rsi"] is not None else ""
        rng_txt = " · Rango ACTIVO" if s["range_state"]["in"] else ""
        lines.append(f"{em(s['action'])} <b>{TF_LABELS[tf]}:</b> {s['action']}{rsi_txt}{rng_txt}")

    rs   = best["range_state"] if best else {}
    fib  = (f"📐 Rango: ${round(rs['lo']):,} → ${round(rs['hi']):,}\n".replace(",",".")
            if best and rs.get("in") and rs.get("hi") else "")
    lvls = (f"🛑 Stop Loss: {sl_line}\n🎯 Take Profit: {tp_line}\n"
            if best and best["css"] in ("buy","sell") else "🟡 Sin niveles activos\n")

    return (
        f"📡 <b>ACTIVE RANGE — Cambio de señal</b>\n"
        f"<i>Actualizado: {changed}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio: <b>${price_f}</b>\n"
        f"{fib}{lvls}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🕐 {fecha}</i>"
    )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    log.info("── BTC Active Range Bot — inicio")

    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("Faltan TG_TOKEN y/o TG_CHAT_ID")
        return

    last_signals = load_state()
    log.info(f"Estado previo cargado: {last_signals}")

    log.info("── Descargando velas Binance...")
    try:
        candles_map = fetch_all_candles()
    except Exception as e:
        log.error(f"Error Binance: {e}")
        return

    log.info("── Calculando señales...")
    sigs = {}
    for tf in TF_LABELS:
        s = compute_signal(candles_map, tf)
        if s:
            sigs[tf] = s
            log.info(f"  {TF_LABELS[tf]}: {s['action']}  score={s['tot']:+d}  RSI={s['rsi']:.1f}")

    changed = [tf for tf, s in sigs.items() if s["action"] != last_signals.get(tf)]

    if not changed:
        log.info("── Sin cambios de señal. Fin.")
        return

    log.info(f"── Cambio en: {[TF_LABELS[t] for t in changed]}")
    ok = tg_send(build_message(sigs, changed))

    if ok:
        new_state = {**last_signals, **{tf: sigs[tf]["action"] for tf in changed}}
        save_state(new_state)
        log.info("── ✅ Alerta enviada y estado guardado.")
    else:
        log.error("── ❌ Error enviando a Telegram.")


if __name__ == "__main__":
    main()
