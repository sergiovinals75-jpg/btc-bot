"""
BTC Active Range Bot — versión servidor
Replica exactamente la lógica del indicador HTML.
Corre 24/7 en la nube y envía alertas a Telegram cuando cambia la señal.

Variables de entorno requeridas:
  TG_TOKEN  → Token del bot de Telegram (ej: 123456789:AAF...)
  TG_CHAT_ID → Tu Chat ID de Telegram (ej: 5279904355)
"""

import os
import time
import math
import logging
import requests
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

POLL_INTERVAL = 300          # segundos entre checks (5 minutos)
BINANCE_URL   = "https://api.binance.com/api/v3/klines"

# Parámetros del indicador (idénticos al HTML)
LEN_RANGE   = 10
SIZE_FACTOR = 1.5
CTX_LEN     = 5
CTX_THRESH  = 0.5

TF_LABELS = {"15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D"}
TF_LIMITS = {"15m": 500,   "1h": 500,  "4h": 300,  "1d": 200}

# Estado persistente en memoria
last_signals = {"15m": None, "1h": None, "4h": None, "1d": None}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HORA ESPAÑA (UTC+1 invierno / UTC+2 verano)
# ─────────────────────────────────────────────
def is_dst(dt: datetime) -> bool:
    """Devuelve True si España está en horario de verano (CEST, UTC+2)."""
    year = dt.year
    # Último domingo de marzo
    march_end = datetime(year, 3, 31)
    dst_start = march_end - timedelta(days=(march_end.weekday() + 1) % 7)
    # Último domingo de octubre
    oct_end   = datetime(year, 10, 31)
    dst_end   = oct_end - timedelta(days=(oct_end.weekday() + 1) % 7)
    naive = dt.replace(tzinfo=None)
    return dst_start <= naive < dst_end

def utc_to_spain(dt_utc: datetime) -> datetime:
    offset = 2 if is_dst(dt_utc) else 1
    return dt_utc + timedelta(hours=offset)


# ─────────────────────────────────────────────
# MATEMÁTICAS DEL INDICADOR
# ─────────────────────────────────────────────
def calc_ema(data: list, n: int) -> list:
    if len(data) < n:
        return [None] * len(data)
    k = 2 / (n + 1)
    e = sum(data[:n]) / n
    result = [None] * (n - 1) + [e]
    for i in range(n, len(data)):
        e = data[i] * k + e * (1 - k)
        result.append(e)
    return result


def calc_rsi(closes: list, n: int = 14) -> list:
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


def calc_atr(candles: list, n: int = 14):
    if len(candles) < n + 1:
        return None
    r = candles[-(n + 1):]
    s = 0
    for i in range(1, n + 1):
        hi, lo, pc = r[i]["high"], r[i]["low"], r[i - 1]["close"]
        s += max(hi - lo, abs(hi - pc), abs(lo - pc))
    return s / n


def is_big_candle(candles: list, idx: int, length: int, factor: float) -> bool:
    if idx < length:
        return False
    slc = candles[idx - length: idx]
    rng_avg = sum(c["high"] - c["low"] for c in slc) / length
    bod_avg = sum(abs(c["close"] - c["open"]) for c in slc) / length
    c = candles[idx]
    rng  = c["high"] - c["low"]
    body = abs(c["close"] - c["open"])
    return (rng > rng_avg * factor) or (body > bod_avg * factor)


def is_continuation(candles: list, idx: int, ctx_len: int, thresh: float) -> bool:
    if idx < ctx_len:
        return False
    cur = candles[idx]
    big_bull = cur["close"] > cur["open"]
    big_bear = cur["close"] < cur["open"]
    bull = bear = 0
    for i in range(1, ctx_len + 1):
        c = candles[idx - i]
        if c["close"] > c["open"]: bull += 1
        if c["close"] < c["open"]: bear += 1
    prior_bull = bull / ctx_len >= thresh
    prior_bear = bear / ctx_len >= thresh
    return (big_bull and prior_bull) or (big_bear and prior_bear)


def detect_active_range(candles: list, length: int, factor: float) -> list:
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
        # Check break
        did_break = False
        if in_range and hi is not None and lo is not None:
            if c["close"] > hi or c["close"] < lo:
                in_range = False; hi = lo = None
                hit_hi = hit_lo = False; start_bar = None; phase = 0
                touches_lo = touches_hi = 0
                wick_low = wick_high = None
                went_below = went_above = False
                retest_buy = retest_sell = False
                below_bar = above_bar = None
                away_from_lo = away_from_hi = False
                in_touch_lo = in_touch_hi = False
                is_cont = False
                did_break = True

        big = is_big_candle(candles, i, length, factor)

        # Start new range
        if (did_break or not in_range) and big:
            hi = c["high"]; lo = c["low"]; in_range = True
            hit_hi = hit_lo = False; start_bar = i; phase = 0
            touches_lo = touches_hi = 0
            wick_low = wick_high = None
            went_below = went_above = False
            retest_buy = retest_sell = False
            below_bar = above_bar = None
            away_from_lo = away_from_hi = False
            in_touch_lo = in_touch_hi = False
            is_cont = is_continuation(candles, i, CTX_LEN, CTX_THRESH)

        # Track touches and zone logic
        if in_range and start_bar is not None and i > start_bar and hi is not None and lo is not None:
            rng   = hi - lo
            lv25  = lo + rng * 0.25
            lv75  = lo + rng * 0.75

            if c["close"] > lv25: away_from_lo = True
            if c["close"] < lv75: away_from_hi = True

            # Touch lo
            if c["low"] <= lo and c["close"] >= lo:
                if not in_touch_lo:
                    if away_from_lo or touches_lo == 0:
                        touches_lo += 1
                        away_from_lo = False
                        if not went_below:
                            went_below = True; below_bar = i; wick_low = c["low"]
                        else:
                            wick_low = min(wick_low, c["low"])
                    in_touch_lo = True
                if not hit_lo: hit_lo = True
            else:
                in_touch_lo = False

            # Touch hi
            if c["high"] >= hi and c["close"] <= hi:
                if not in_touch_hi:
                    if away_from_hi or touches_hi == 0:
                        touches_hi += 1
                        away_from_hi = False
                        if not went_above:
                            went_above = True; above_bar = i; wick_high = c["high"]
                        else:
                            wick_high = max(wick_high, c["high"])
                    in_touch_hi = True
                if not hit_hi: hit_hi = True
            else:
                in_touch_hi = False

            # Phase
            if went_below and phase == 0: phase = 1
            if went_above and phase == 0: phase = 2
            if went_above and phase == 1: phase = 2; retest_buy = False
            if went_below and phase == 2: phase = 1; retest_sell = False
            if hit_hi and hit_lo and phase != 2: phase = 2

            # Retests
            if went_below and not retest_buy and below_bar is not None and i > below_bar and c["low"] <= lo:
                retest_buy = True
            if went_above and not retest_sell and above_bar is not None and i > above_bar and c["high"] >= hi:
                retest_sell = True

        states.append({
            "in": in_range, "hi": hi, "lo": lo,
            "hit_hi": hit_hi, "hit_lo": hit_lo,
            "phase": phase, "is_cont": is_cont,
            "touches_lo": touches_lo, "touches_hi": touches_hi,
            "wick_low": wick_low, "wick_high": wick_high,
            "went_below": went_below, "went_above": went_above,
            "retest_buy": retest_buy, "retest_sell": retest_sell,
        })
    return states


def calc_bias(candles: list) -> dict:
    states = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)
    last   = states[-1] if states else None
    if not last or not last["in"]:
        return {"bias": 0, "in_range": False}
    bias = 1 if last["phase"] == 1 else (-1 if last["phase"] == 2 else 0)
    return {"bias": bias, "in_range": True}


def compute_signal(candles_map: dict, tf_key: str) -> dict | None:
    candles = candles_map.get(tf_key)
    if not candles or len(candles) < LEN_RANGE + 5:
        return None

    closes   = [c["close"] for c in candles]
    price    = closes[-1]
    last_ts  = candles[-1]["ts"]
    dt_utc   = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)

    states   = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)
    last     = states[-1]
    rsi_arr  = calc_rsi(closes, 14)
    rsi      = rsi_arr[-1]
    atr      = calc_atr(candles, 14)
    ema20    = calc_ema(closes, 20)
    ema20v   = ema20[-1]

    sc  = []
    tot = 0

    def add(name, s, max_s, label, color):
        nonlocal tot
        sc.append({"name": name, "score": s, "max": max_s, "label": label, "color": color})
        tot += s

    # 1. Active Range + Phase
    if last["in"] and last["hi"] and last["lo"]:
        if   last["phase"] == 1 and last["retest_buy"]:  add("Zona de entrada", 3, 3, "✓ Retest zona COMPRA confirmado — Entrada óptima", "green")
        elif last["phase"] == 1 and last["went_below"]:  add("Zona de entrada", 2, 3, "Precio tocó zona baja — Esperando retest", "green")
        elif last["phase"] == 1:                          add("Zona de entrada", 1, 3, "Rango activo fase alcista — Sesgo compra", "green")
        elif last["phase"] == 2 and last["retest_sell"]: add("Zona de entrada", -3, 3, "✓ Retest zona VENTA confirmado — Entrada bajista óptima", "red")
        elif last["phase"] == 2 and last["went_above"]:  add("Zona de entrada", -2, 3, "Precio tocó zona alta — Esperando retest bajista", "red")
        elif last["phase"] == 2:                          add("Zona de entrada", -1, 3, "Rango activo fase bajista — Sesgo venta", "red")
        else:                                             add("Zona de entrada", 0, 3, "Rango activo sin fase definida — Neutral", "yellow")
    else:
        add("Zona de entrada", 0, 3, "Sin rango activo — Esperar señal", "yellow")

    # 2. RSI
    if rsi is not None:
        if   rsi < 30: add("RSI (14)", 3, 3, f"RSI {rsi:.1f} — Sobreventa extrema, zona de compra", "green")
        elif rsi < 45: add("RSI (14)", 2, 3, f"RSI {rsi:.1f} — Zona baja, presión compradora", "green")
        elif rsi < 55: add("RSI (14)", 0, 3, f"RSI {rsi:.1f} — Zona neutral", "yellow")
        elif rsi < 70: add("RSI (14)", -1, 3, f"RSI {rsi:.1f} — Zona alta, precaución", "yellow")
        elif rsi < 80: add("RSI (14)", -2, 3, f"RSI {rsi:.1f} — Sobrecompra, zona de venta", "red")
        else:          add("RSI (14)", -3, 3, f"RSI {rsi:.1f} — Sobrecompra extrema", "red")

    # 3. MTF alignment
    biases_map = {k: calc_bias(v) for k, v in candles_map.items() if k in ("15m", "1h", "4h", "1d")}
    cur_bias   = 1 if last["phase"] == 1 else (-1 if last["phase"] == 2 else 0)
    biases     = [b for b in biases_map.values() if b["bias"] != 0]
    aligned    = sum(1 for b in biases if b["bias"] == cur_bias)

    if biases:
        ratio = aligned / len(biases)
        if   ratio >= 0.75: add("Alineación MTF", 2, 2, f"{aligned}/{len(biases)} TFs alineados con sesgo actual", "green")
        elif ratio >= 0.5:  add("Alineación MTF", 1, 2, f"{aligned}/{len(biases)} TFs alineados — Alineación parcial", "yellow")
        elif ratio == 0:    add("Alineación MTF", -2, 2, "TFs en contra del sesgo actual — Divergencia MTF", "red")
        else:               add("Alineación MTF", -1, 2, "Alineación MTF débil", "yellow")

    # 4. Continuation context
    if last["in"]:
        if last["is_cont"]: add("Contexto", 1, 1, "Vela grande de continuación — confirma dirección", "green")
        else:               add("Contexto", -1, 1, "Vela grande de reversión — posible trampa", "yellow")

    # 5. Touches
    if last["in"]:
        touches = last["touches_lo"] if last["phase"] == 1 else last["touches_hi"]
        if   touches >= 3: add("Fuerza zona", 2, 2, f"Zona testeada {touches} veces — Zona muy fuerte", "green")
        elif touches == 2: add("Fuerza zona", 1, 2, "Zona testeada 2 veces — Zona confirmada", "green")
        elif touches == 1: add("Fuerza zona", 0, 2, "Primera vez que toca la zona", "yellow")
        else:              add("Fuerza zona", 0, 2, "Sin toques aún", "yellow")

    max_s = sum(x["max"] for x in sc)
    norm  = ((tot + max_s) / (max_s * 2) * 100) if max_s > 0 else 50
    thr   = 5

    retest_buy_now  = last["in"] and last["phase"] == 1 and last["retest_buy"]
    retest_sell_now = last["in"] and last["phase"] == 2 and last["retest_sell"]

    if   retest_buy_now  and tot >= 3:          action, css = "COMPRA",       "buy"
    elif tot >= thr:                            action, css = "COMPRA",       "buy"
    elif tot >= math.ceil(thr / 2):             action, css = "COMPRA DÉBIL", "buy"
    elif retest_sell_now and tot <= -3:         action, css = "VENDE",        "sell"
    elif tot <= -thr:                           action, css = "VENDE",        "sell"
    elif tot <= -math.ceil(thr / 2):            action, css = "VENDE DÉBIL",  "sell"
    elif not last["in"]:                        action, css = "SIN RANGO",    "wait"
    else:                                       action, css = "ESPERA",       "neutral"

    return {
        "sc": sc, "tot": tot, "max_s": max_s, "norm": norm,
        "action": action, "css": css,
        "price": price, "dt_utc": dt_utc, "rsi": rsi, "atr": atr,
        "ema20v": ema20v, "range_state": last,
        "cur_bias": cur_bias, "biases": biases_map,
    }


# ─────────────────────────────────────────────
# BINANCE
# ─────────────────────────────────────────────
def fetch_klines(interval: str, limit: int) -> list:
    r = requests.get(
        BINANCE_URL,
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
        timeout=15
    )
    r.raise_for_status()
    return [
        {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        for k in r.json()
    ]


def fetch_all_candles() -> dict:
    data = {}
    for tf, limit in TF_LIMITS.items():
        data[tf] = fetch_klines(tf, limit)
        log.info(f"  {TF_LABELS[tf]}: {len(data[tf])} velas descargadas")
    return data


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def tg_send(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TG_TOKEN o TG_CHAT_ID no configurados")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        ok = r.json().get("ok", False)
        if not ok:
            log.error(f"Telegram error: {r.text}")
        return ok
    except Exception as e:
        log.error(f"Telegram excepción: {e}")
        return False


def build_message(sigs: dict, changed_tfs: list) -> str:
    def em(a):
        if "COMPRA" in a: return "🟢"
        if "VENDE"  in a: return "🔴"
        if "SIN RANGO" == a: return "⬜"
        return "🟡"

    changed_str = ", ".join(TF_LABELS[t] for t in changed_tfs)

    # Best signal para SL/TP
    best = sigs.get("15m") or next(iter(sigs.values()), None)
    sl_line = tp_line = "—"
    if best and best["atr"] and best["css"] in ("buy", "sell"):
        is_buy = best["css"] == "buy"
        sl = best["price"] - best["atr"] if is_buy else best["price"] + best["atr"]
        tp = best["price"] + best["atr"] * 1.5 if is_buy else best["price"] - best["atr"] * 1.5
        sl_pct = best["atr"] / best["price"] * 100
        tp_pct = best["atr"] * 1.5 / best["price"] * 100
        sl_line = f"${round(sl):,} (-{sl_pct:.2f}%)".replace(",", ".")
        tp_line = f"${round(tp):,} (+{tp_pct:.2f}%)".replace(",", ".")

    price   = best["price"] if best else 0
    dt_sp   = utc_to_spain(best["dt_utc"]) if best else datetime.now()
    fecha   = dt_sp.strftime("%d/%m %H:%M") + "h España"
    price_f = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    lines = []
    for tf in ("15m", "1h", "4h", "1d"):
        s = sigs.get(tf)
        if not s: continue
        rs      = s["range_state"]
        rsi_txt = f" · RSI:{s['rsi']:.0f}" if s["rsi"] is not None else ""
        rng_txt = " · Rango ACTIVO" if rs["in"] else ""
        lines.append(f"{em(s['action'])} <b>{TF_LABELS[tf]}:</b> {s['action']}{rsi_txt}{rng_txt}")

    rs_best = best["range_state"] if best else {}
    fib_line = ""
    if best and rs_best.get("in") and rs_best.get("hi"):
        lo_f = f"{round(rs_best['lo']):,}".replace(",", ".")
        hi_f = f"{round(rs_best['hi']):,}".replace(",", ".")
        fib_line = f"📐 Rango: ${lo_f} → ${hi_f}\n"

    if best and best["css"] in ("buy", "sell"):
        levels = f"🛑 Stop Loss: {sl_line}\n🎯 Take Profit: {tp_line}\n"
    else:
        levels = "🟡 Sin niveles activos\n"

    return (
        f"📡 <b>ACTIVE RANGE — Cambio de señal</b>\n"
        f"<i>Actualizado: {changed_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{chr(10).join(lines)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio: <b>${price_f}</b>\n"
        f"{fib_line}"
        f"{levels}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🕐 {fecha}</i>"
    )


# ─────────────────────────────────────────────
# BUCLE PRINCIPAL
# ─────────────────────────────────────────────
def check_and_alert():
    global last_signals
    log.info("── Descargando velas Binance...")
    try:
        candles_map = fetch_all_candles()
    except Exception as e:
        log.error(f"Error descargando datos: {e}")
        return

    log.info("── Calculando señales...")
    sigs = {}
    for tf in ("15m", "1h", "4h", "1d"):
        s = compute_signal(candles_map, tf)
        if s:
            sigs[tf] = s
            log.info(f"  {TF_LABELS[tf]}: {s['action']}  (score {s['tot']:+d}  RSI:{s['rsi']:.1f})")

    changed = [tf for tf, s in sigs.items() if s["action"] != last_signals[tf]]

    if not changed:
        log.info("── Sin cambios de señal.")
        return

    log.info(f"── Cambio detectado en: {[TF_LABELS[t] for t in changed]}")
    msg = build_message(sigs, changed)
    ok  = tg_send(msg)
    if ok:
        for tf in changed:
            last_signals[tf] = sigs[tf]["action"]
        log.info("── ✅ Alerta Telegram enviada.")
    else:
        log.error("── ❌ Error enviando a Telegram.")


def startup_ping():
    """Mensaje inicial al arrancar el servidor."""
    tg_send(
        "🚀 <b>ACTIVE RANGE BOT — Servidor iniciado</b>\n\n"
        "✅ Conectado a Binance\n"
        "✅ Alertas activas para 15M · 1H · 4H · 1D\n"
        "🔄 Polling cada 5 minutos\n\n"
        f"<i>{datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC</i>"
    )


def main():
    log.info("=" * 50)
    log.info("  BTC Active Range Bot arrancando...")
    log.info("=" * 50)

    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("FALTAN variables de entorno TG_TOKEN y/o TG_CHAT_ID")
        log.error("Configúralas en Render → Environment antes de continuar.")
        return

    startup_ping()

    while True:
        try:
            check_and_alert()
        except Exception as e:
            log.error(f"Error en check_and_alert: {e}", exc_info=True)

        log.info(f"── Esperando {POLL_INTERVAL // 60} minutos...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
