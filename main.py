"""
BTC Active Range Bot — version GitHub Actions
Se ejecuta cada 5 minutos. Envia alerta si cambia la senal.
Envia resumen diario a las 9:00h hora Espana.

Secrets: TG_TOKEN, TG_CHAT_ID
"""

import os
import json
import math
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
STATE_FILE = Path("state.json")
BINANCE_URL  = "https://data-api.binance.vision/api/v3/klines"
BINANCE_TICK = "https://data-api.binance.vision/api/v3/ticker/price"

LEN_RANGE   = 10
SIZE_FACTOR = 1.0
CTX_LEN     = 5
CTX_THRESH  = 0.5
DAILY_HOUR  = 9
VOL_PERIOD  = 20

TF_ORDER  = ["15m", "1h", "4h", "1d", "1w"]
TF_LABELS = {"15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
TF_LIMITS = {"15m": 500,   "1h": 500,  "4h": 300,  "1d": 200,  "1w": 100}

# Peso de cada TF en el calculo de probabilidad (mayor TF = mas peso)
TF_WEIGHTS = {"15m": 1, "1h": 2, "4h": 3, "1d": 4, "1w": 5}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def load_state():
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            if "signals" in d:
                for tf in TF_ORDER:
                    d["signals"].setdefault(tf, None)
                return d
            return {"signals": {tf: None for tf in TF_ORDER}, "last_daily": None}
        except Exception:
            pass
    return {"signals": {tf: None for tf in TF_ORDER}, "last_daily": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def is_dst(dt):
    y = dt.year
    ms = datetime(y, 3, 31); ds = ms - timedelta(days=(ms.weekday()+1)%7)
    me = datetime(y, 10, 31); de = me - timedelta(days=(me.weekday()+1)%7)
    return ds <= dt.replace(tzinfo=None) < de

def utc_to_spain(dt):
    return dt + timedelta(hours=2 if is_dst(dt) else 1)

def now_spain():
    return utc_to_spain(datetime.now(tz=timezone.utc))

def calc_rsi(closes, n=14):
    if len(closes) < n+1: return [None]*len(closes)
    rsi = [None]*n
    g = [max(closes[i]-closes[i-1],0) for i in range(1,n+1)]
    l = [max(closes[i-1]-closes[i],0) for i in range(1,n+1)]
    ag, al = sum(g)/n, sum(l)/n
    rsi.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(n+1, len(closes)):
        d = closes[i]-closes[i-1]
        ag = (ag*(n-1)+max(d,0))/n; al = (al*(n-1)+max(-d,0))/n
        rsi.append(100 if al==0 else 100-100/(1+ag/al))
    return rsi

def calc_atr(candles, n=14):
    if len(candles) < n+1: return None
    r = candles[-(n+1):]
    return sum(max(r[i]["high"]-r[i]["low"], abs(r[i]["high"]-r[i-1]["close"]), abs(r[i]["low"]-r[i-1]["close"])) for i in range(1,n+1)) / n

def calc_volume_ratio(candles, period=VOL_PERIOD):
    if len(candles) < period+1: return None
    vols = [c["volume"] for c in candles]
    avg  = sum(vols[-(period+1):-1]) / period
    if avg == 0: return None
    return vols[-1] / avg

def is_big_candle(candles, idx, length, factor):
    if idx < length: return False
    slc = candles[idx-length:idx]
    ra = sum(c["high"]-c["low"] for c in slc)/length
    ba = sum(abs(c["close"]-c["open"]) for c in slc)/length
    c = candles[idx]
    return (c["high"]-c["low"] > ra*factor) or (abs(c["close"]-c["open"]) > ba*factor)

def is_continuation(candles, idx, ctx_len, thresh):
    if idx < ctx_len: return False
    cur = candles[idx]; bull = bear = 0
    for i in range(1, ctx_len+1):
        c = candles[idx-i]
        if c["close"] > c["open"]: bull += 1
        if c["close"] < c["open"]: bear += 1
    return ((cur["close"]>cur["open"]) and bull/ctx_len>=thresh) or ((cur["close"]<cur["open"]) and bear/ctx_len>=thresh)

def detect_active_range(candles, length, factor):
    in_range=False; hi=lo=None; hit_hi=hit_lo=False; start_bar=None; phase=0
    tlo=thi=0; went_below=went_above=False; retest_buy=retest_sell=False
    below_bar=above_bar=None; afl=afh=False; itl=ith=False; is_cont=False; states=[]
    for i,c in enumerate(candles):
        did_break=False
        if in_range and hi is not None and (c["close"]>hi or c["close"]<lo):
            in_range=False; hi=lo=None; hit_hi=hit_lo=False; start_bar=None; phase=0
            tlo=thi=0; went_below=went_above=False; retest_buy=retest_sell=False
            below_bar=above_bar=None; afl=afh=False; itl=ith=False; is_cont=False; did_break=True
        big = is_big_candle(candles, i, length, factor)
        if (did_break or not in_range) and big:
            hi=c["high"]; lo=c["low"]; in_range=True; hit_hi=hit_lo=False; start_bar=i; phase=0
            tlo=thi=0; went_below=went_above=False; retest_buy=retest_sell=False
            below_bar=above_bar=None; afl=afh=False; itl=ith=False
            is_cont=is_continuation(candles,i,CTX_LEN,CTX_THRESH)
        if in_range and start_bar is not None and i>start_bar:
            rng=hi-lo
            if c["close"]>lo+rng*0.25: afl=True
            if c["close"]<lo+rng*0.75: afh=True
            if c["low"]<=lo and c["close"]>=lo:
                if not itl:
                    if afl or tlo==0:
                        tlo+=1; afl=False
                        if not went_below: went_below=True; below_bar=i
                        itl=True
                hit_lo=True
            else: itl=False
            if c["high"]>=hi and c["close"]<=hi:
                if not ith:
                    if afh or thi==0:
                        thi+=1; afh=False
                        if not went_above: went_above=True; above_bar=i
                        ith=True
                hit_hi=True
            else: ith=False
            if went_below and phase==0: phase=1
            if went_above and phase==0: phase=2
            if went_above and phase==1: phase=2; retest_buy=False
            if went_below and phase==2: phase=1; retest_sell=False
            if hit_hi and hit_lo and phase!=2: phase=2
            if went_below and not retest_buy and below_bar is not None and i>below_bar and c["low"]<=lo: retest_buy=True
            if went_above and not retest_sell and above_bar is not None and i>above_bar and c["high"]>=hi: retest_sell=True
        states.append({"in":in_range,"hi":hi,"lo":lo,"hit_hi":hit_hi,"hit_lo":hit_lo,
                       "phase":phase,"is_cont":is_cont,"touches_lo":tlo,"touches_hi":thi,
                       "went_below":went_below,"went_above":went_above,"retest_buy":retest_buy,"retest_sell":retest_sell})
    return states

def calc_bias(candles):
    last = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)[-1]
    if not last or not last["in"]: return {"bias":0}
    return {"bias": 1 if last["phase"]==1 else (-1 if last["phase"]==2 else 0)}

def fetch_current_price():
    try:
        r = requests.get(BINANCE_TICK, params={"symbol": "BTCUSDT"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"No se pudo obtener precio actual: {e}")
        return None

def compute_signal(candles_map, tf_key, current_price=None):
    candles = candles_map.get(tf_key)
    if not candles or len(candles) < LEN_RANGE+5: return None
    closes = [c["close"] for c in candles]
    price  = current_price if current_price is not None else closes[-1]
    states = detect_active_range(candles, LEN_RANGE, SIZE_FACTOR)
    last   = states[-1]
    rsi    = calc_rsi(closes, 14)[-1]
    atr    = calc_atr(candles, 14)
    vol_ratio = calc_volume_ratio(candles)
    sc=[]; tot=0

    def add(name,s,ms,label,color):
        nonlocal tot; sc.append({"name":name,"score":s,"max":ms,"label":label,"color":color}); tot+=s

    if last["in"] and last["hi"]:
        if   last["phase"]==1 and last["retest_buy"]:  add("Zona",3,3,"Retest COMPRA confirmado","green")
        elif last["phase"]==1 and last["went_below"]:  add("Zona",2,3,"Toco zona baja","green")
        elif last["phase"]==1:                          add("Zona",1,3,"Fase alcista","green")
        elif last["phase"]==2 and last["retest_sell"]: add("Zona",-3,3,"Retest VENTA confirmado","red")
        elif last["phase"]==2 and last["went_above"]:  add("Zona",-2,3,"Toco zona alta","red")
        elif last["phase"]==2:                          add("Zona",-1,3,"Fase bajista","red")
        else:                                           add("Zona",0,3,"Sin fase","yellow")
    else: add("Zona",0,3,"Sin rango activo","yellow")

    if rsi is not None:
        if   rsi<30:  add("RSI",3,3,f"RSI {rsi:.1f} Sobreventa extrema","green")
        elif rsi<45:  add("RSI",2,3,f"RSI {rsi:.1f} Presion compradora","green")
        elif rsi<55:  add("RSI",0,3,f"RSI {rsi:.1f} Neutral","yellow")
        elif rsi<70:  add("RSI",-1,3,f"RSI {rsi:.1f} Precaucion","yellow")
        elif rsi<80:  add("RSI",-2,3,f"RSI {rsi:.1f} Sobrecompra","red")
        else:         add("RSI",-3,3,f"RSI {rsi:.1f} Sobrecompra extrema","red")

    if vol_ratio is not None:
        cur_bias = 1 if last["phase"]==1 else (-1 if last["phase"]==2 else 0)
        if vol_ratio >= 1.5:
            if cur_bias != 0: add("Vol",2,2,f"Volumen {vol_ratio:.1f}x Confirma movimiento","green")
            else:             add("Vol",1,2,f"Volumen {vol_ratio:.1f}x Alto sin sesgo","yellow")
        elif vol_ratio >= 1.2: add("Vol",1,2,f"Volumen {vol_ratio:.1f}x Por encima media","green")
        elif vol_ratio < 0.8 and last["in"]: add("Vol",-1,2,f"Volumen {vol_ratio:.1f}x Bajo senal debil","red")
        else: add("Vol",0,2,f"Volumen {vol_ratio:.1f}x Normal","yellow")

    biases   = [calc_bias(v) for k,v in candles_map.items() if k in TF_LABELS]
    cur_bias = 1 if last["phase"]==1 else (-1 if last["phase"]==2 else 0)
    active_b = [b for b in biases if b["bias"]!=0]
    aligned  = sum(1 for b in active_b if b["bias"]==cur_bias)
    if active_b:
        ratio = aligned/len(active_b)
        if   ratio>=0.75: add("MTF",2,2,f"{aligned}/{len(active_b)} TFs alineados","green")
        elif ratio>=0.5:  add("MTF",1,2,"Alineacion parcial","yellow")
        elif ratio==0:    add("MTF",-2,2,"Divergencia MTF","red")
        else:             add("MTF",-1,2,"Alineacion debil","yellow")

    if last["in"]:
        add("Contexto",1 if last["is_cont"] else -1,1,"Continuacion" if last["is_cont"] else "Posible reversion","green")
        touches = last["touches_lo"] if last["phase"]==1 else last["touches_hi"]
        if   touches>=3: add("Fuerza",2,2,f"Zona testeada {touches}x","green")
        elif touches==2: add("Fuerza",1,2,"Zona confirmada 2 toques","green")
        else:            add("Fuerza",0,2,"Zona sin confirmar","yellow")

    max_s = sum(x["max"] for x in sc)
    thr   = 5
    rb = last["in"] and last["phase"]==1 and last["retest_buy"]
    rs = last["in"] and last["phase"]==2 and last["retest_sell"]

    if   rb and tot>=3:             action,css = "COMPRA","buy"
    elif tot>=thr:                  action,css = "COMPRA","buy"
    elif tot>=math.ceil(thr/2):     action,css = "COMPRA DEBIL","buy"
    elif rs and tot<=-3:            action,css = "VENDE","sell"
    elif tot<=-thr:                 action,css = "VENDE","sell"
    elif tot<=-math.ceil(thr/2):    action,css = "VENDE DEBIL","sell"
    elif not last["in"]:            action,css = "SIN RANGO","wait"
    else:                           action,css = "ESPERA","neutral"

    return {"action":action,"css":css,"tot":tot,"max_s":max_s,"price":price,
            "rsi":rsi,"atr":atr,"vol_ratio":vol_ratio,"range_state":last}


# ─────────────────────────────────────────────
# PROBABILIDAD DE EXITO
# ─────────────────────────────────────────────
def calc_probability(sigs):
    """
    Calcula probabilidad de exito ponderada por:
    - Score normalizado de cada TF (pesos mayores a TFs mayores)
    - Confluencia entre TFs
    - Volumen
    - RSI en zona correcta
    - Retest confirmado
    Devuelve: (prob_int, direccion, label, barra)
    """
    if not sigs: return 50, "neutral", "Sin datos", "░░░░░░░░░░"

    # Direccion dominante
    n_buy  = sum(1 for s in sigs.values() if s["css"] in ("buy",))
    n_sell = sum(1 for s in sigs.values() if s["css"] in ("sell",))
    if n_buy == 0 and n_sell == 0:
        return 50, "neutral", "Sin direccion clara", "░░░░░░░░░░"
    direction = "buy" if n_buy >= n_sell else "sell"

    # Score ponderado por TF
    total_weight = 0
    weighted_score = 0
    for tf, s in sigs.items():
        w = TF_WEIGHTS.get(tf, 1)
        total_weight += w
        max_s = s["max_s"] if s["max_s"] > 0 else 1
        norm = (s["tot"] + max_s) / (max_s * 2) * 100
        # Si va en contra de la direccion dominante penalizar
        if direction == "buy"  and s["css"] == "sell": norm = 100 - norm
        if direction == "sell" and s["css"] == "buy":  norm = 100 - norm
        weighted_score += norm * w

    prob = weighted_score / total_weight if total_weight > 0 else 50

    # Bonus/penalizacion por volumen (max +-6 puntos)
    vol_adj = 0
    for s in sigs.values():
        vr = s.get("vol_ratio")
        if vr is None: continue
        if   vr >= 1.5: vol_adj += 2
        elif vr >= 1.2: vol_adj += 1
        elif vr < 0.8:  vol_adj -= 2
    vol_adj = max(-6, min(6, vol_adj))
    prob += vol_adj

    # Bonus por RSI en zona correcta (max +-4 puntos)
    rsi_adj = 0
    for s in sigs.values():
        rsi = s.get("rsi")
        if rsi is None: continue
        if direction == "buy":
            if   rsi < 30: rsi_adj += 2
            elif rsi < 45: rsi_adj += 1
            elif rsi > 70: rsi_adj -= 2
        else:
            if   rsi > 70: rsi_adj += 2
            elif rsi > 55: rsi_adj += 1
            elif rsi < 30: rsi_adj -= 2
    rsi_adj = max(-4, min(4, rsi_adj))
    prob += rsi_adj

    # Bonus por retest confirmado (el mejor escenario posible)
    for s in sigs.values():
        rs = s.get("range_state", {})
        if direction == "buy"  and rs.get("retest_buy"):  prob += 5; break
        if direction == "sell" and rs.get("retest_sell"): prob += 5; break

    # Penalizacion por divergencia entre TFs
    if n_buy > 0 and n_sell > 0:
        prob -= min(n_buy, n_sell) * 3

    # Clamp entre 10% y 95%
    prob = max(10, min(95, round(prob)))

    # Etiqueta
    if   prob >= 80: label = "Muy Alta"
    elif prob >= 65: label = "Alta"
    elif prob >= 50: label = "Media"
    elif prob >= 35: label = "Baja"
    else:            label = "Muy Baja"

    # Barra visual (10 bloques)
    filled = round(prob / 10)
    barra  = "█" * filled + "░" * (10 - filled)

    return prob, direction, label, barra


def prob_header(sigs):
    """Bloque de probabilidad para poner al inicio del mensaje."""
    prob, direction, label, barra = calc_probability(sigs)
    if   prob >= 80: em = "🟢"
    elif prob >= 65: em = "🟡"
    elif prob >= 50: em = "🟠"
    else:            em = "🔴"
    dir_txt = "COMPRA 📈" if direction == "buy" else ("VENTA 📉" if direction == "sell" else "NEUTRAL")
    return (f"{em} <b>Probabilidad de exito: {prob}%</b>\n"
            f"<code>{barra}</code> {label}\n"
            f"Direccion dominante: <b>{dir_txt}</b>")


def calc_confluence(sigs):
    n_buy=n_sell=n_wait=n_none=0
    for s in sigs.values():
        c=s["css"]
        if c=="buy": n_buy+=1
        elif c=="sell": n_sell+=1
        elif c=="wait": n_none+=1
        else: n_wait+=1
    total=len(sigs)
    if n_buy>=4:               return "🟢🟢",f"ALCISTA FUERTE — {n_buy}/{total} TFs en compra",n_buy,n_sell,n_wait,n_none
    elif n_buy>=3:             return "🟢",f"Alcista — {n_buy}/{total} TFs en compra",n_buy,n_sell,n_wait,n_none
    elif n_sell>=4:            return "🔴🔴",f"BAJISTA FUERTE — {n_sell}/{total} TFs en venta",n_buy,n_sell,n_wait,n_none
    elif n_sell>=3:            return "🔴",f"Bajista — {n_sell}/{total} TFs en venta",n_buy,n_sell,n_wait,n_none
    elif n_buy>0 and n_sell>0: return "⚠️",f"Divergencia — {n_buy} compra vs {n_sell} venta",n_buy,n_sell,n_wait,n_none
    elif n_wait>=3:            return "🟡",f"Sin direccion clara — {n_wait}/{total} en espera",n_buy,n_sell,n_wait,n_none
    else:                      return "⬜","Sin rango activo en la mayoria de TFs",n_buy,n_sell,n_wait,n_none


def fetch_all_candles():
    data={}
    for tf in TF_ORDER:
        limit = TF_LIMITS[tf]
        r=requests.get(BINANCE_URL,params={"symbol":"BTCUSDT","interval":tf,"limit":limit},timeout=15)
        r.raise_for_status()
        data[tf]=[{"ts":int(k[0]),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"volume":float(k[5])} for k in r.json()]
        log.info(f"  {TF_LABELS[tf]}: {len(data[tf])} velas")
    return data

def tg_send(text):
    r=requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id":TG_CHAT_ID,"text":text,"parse_mode":"HTML"},timeout=10)
    return r.json().get("ok",False)

def fmt_price(p):
    return f"{p:,.2f}".replace(",","X").replace(".",",").replace("X",".")

def vol_emoji(vol_ratio):
    if vol_ratio is None: return ""
    if vol_ratio >= 1.5:  return " 🔥"
    if vol_ratio >= 1.2:  return " ⬆️"
    if vol_ratio < 0.8:   return " ⬇️"
    return ""

def build_tf_block(tf, s):
    em={"buy":"🟢","sell":"🔴","wait":"⬜","neutral":"🟡"}.get(s["css"],"🟡")
    rsi_txt=f" · RSI {s['rsi']:.0f}" if s["rsi"] is not None else ""
    rng_txt=" · Rango ACTIVO" if s["range_state"]["in"] else ""
    vol_txt=f" · Vol {s['vol_ratio']:.1f}x{vol_emoji(s['vol_ratio'])}" if s.get("vol_ratio") else ""
    price_f=fmt_price(s["price"])
    if s["atr"] and s["css"] in ("buy","sell"):
        is_buy=s["css"]=="buy"
        sl=s["price"]-s["atr"] if is_buy else s["price"]+s["atr"]
        tp=s["price"]+s["atr"]*1.5 if is_buy else s["price"]-s["atr"]*1.5
        sl_pct=abs(sl-s["price"])/s["price"]*100
        tp_pct=abs(tp-s["price"])/s["price"]*100
        ds="-" if is_buy else "+"; dt="+" if is_buy else "-"
        lvls=f"   🛑 SL: ${round(sl):,} ({ds}{sl_pct:.2f}%)\n   🎯 TP: ${round(tp):,} ({dt}{tp_pct:.2f}%)".replace(",",".")
    else:
        lvls="   🟡 Sin niveles activos"
    rs=s["range_state"]
    rng_line=f"   📐 Rango: ${round(rs['lo']):,} - ${round(rs['hi']):,}\n".replace(",",".") if rs.get("in") and rs.get("hi") else ""
    return (f"{em} <b>{TF_LABELS[tf]}:</b> {s['action']}{rsi_txt}{vol_txt}{rng_txt}\n"
            f"   💰 Precio: <b>${price_f}</b>\n"
            f"{rng_line}{lvls}")

def build_alert_message(sigs, changed_tfs):
    fecha  = now_spain().strftime("%d/%m %H:%M") + "h"
    changed = ", ".join(TF_LABELS[t] for t in TF_ORDER if t in changed_tfs)
    conf_em,conf_txt,n_buy,n_sell,n_wait,n_none = calc_confluence(sigs)
    blocks = [build_tf_block(tf,sigs[tf]) for tf in TF_ORDER if sigs.get(tf)]
    return (
        f"📡 <b>ACTIVE RANGE — Cambio de senal</b>\n"
        f"<i>Cambiaron: {changed}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{prob_header(sigs)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{conf_em} <b>Confluencia:</b> {conf_txt}\n"
        f"📊 Compra:{n_buy}  Venta:{n_sell}  Espera:{n_wait}  Sin rango:{n_none}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n━━━━━━━━━━━━━━━━━━━━\n".join(blocks)
        + f"\n━━━━━━━━━━━━━━━━━━━━\n<i>Hora Espana: {fecha}</i>"
    )

def build_daily_message(sigs):
    fecha = now_spain().strftime("%d/%m/%Y")
    conf_em,conf_txt,n_buy,n_sell,n_wait,n_none = calc_confluence(sigs)
    blocks = [build_tf_block(tf,sigs[tf]) for tf in TF_ORDER if sigs.get(tf)]
    return (
        f"☀️ <b>RESUMEN DIARIO — {fecha}</b>\n"
        f"<i>BTC Active Range Bot · 9:00h Espana</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{prob_header(sigs)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{conf_em} <b>Confluencia:</b> {conf_txt}\n"
        f"📊 Compra:{n_buy}  Venta:{n_sell}  Espera:{n_wait}  Sin rango:{n_none}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n━━━━━━━━━━━━━━━━━━━━\n".join(blocks)
        + f"\n━━━━━━━━━━━━━━━━━━━━\n<i>Proximo resumen manana a las 9:00h</i>"
    )


def main():
    log.info("── BTC Active Range Bot — inicio")
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("Faltan TG_TOKEN y/o TG_CHAT_ID"); return

    state        = load_state()
    last_signals = state["signals"]
    last_daily   = state.get("last_daily")
    log.info(f"Estado previo: {last_signals}")

    log.info("── Descargando velas Binance...")
    try:
        candles_map = fetch_all_candles()
    except Exception as e:
        log.error(f"Error Binance: {e}"); return

    log.info("── Obteniendo precio actual...")
    current_price = fetch_current_price()
    if current_price:
        log.info(f"  Precio actual: ${current_price:,.2f}")
    else:
        log.warning("  Usando precio de ultima vela cerrada")

    log.info("── Calculando senales...")
    sigs={}
    for tf in TF_ORDER:
        try:
            s=compute_signal(candles_map, tf, current_price)
            if s:
                sigs[tf]=s
                vol_info=f"  Vol:{s['vol_ratio']:.2f}x" if s.get("vol_ratio") else ""
                log.info(f"  {TF_LABELS[tf]}: {s['action']}  score={s['tot']:+d}  RSI={s['rsi']:.1f}{vol_info}")
        except Exception as e:
            log.error(f"  Error calculando {TF_LABELS[tf]}: {e}")

    # Log probabilidad
    prob, direction, label, _ = calc_probability(sigs)
    log.info(f"── Probabilidad: {prob}% ({label}) direccion={direction}")

    # Detectar cambios en TODOS los timeframes
    changed=[]
    for tf in TF_ORDER:
        if tf in sigs:
            prev = last_signals.get(tf)
            curr = sigs[tf]["action"]
            if curr != prev:
                changed.append(tf)
                log.info(f"  CAMBIO {TF_LABELS[tf]}: {prev} → {curr}")
            else:
                log.info(f"  SIN CAMBIO {TF_LABELS[tf]}: {curr}")

    now_es=now_spain(); today_str=now_es.strftime("%Y-%m-%d"); sent_daily=False

    if now_es.hour==DAILY_HOUR and last_daily!=today_str:
        log.info("── Enviando resumen diario...")
        if tg_send(build_daily_message(sigs)):
            state["last_daily"]=today_str; sent_daily=True
            log.info("── Resumen diario enviado.")
        else:
            log.error("── Error enviando resumen diario.")

    if not changed:
        log.info("── Sin cambios en ningun TF. Fin.")
        if sent_daily: save_state(state)
        return

    log.info(f"── Cambios detectados en: {[TF_LABELS[t] for t in changed]}")
    if tg_send(build_alert_message(sigs, changed)):
        for tf in changed:
            state["signals"][tf] = sigs[tf]["action"]
        save_state(state)
        log.info("── Alerta enviada y estado guardado.")
    else:
        log.error("── Error enviando alerta.")
        if sent_daily: save_state(state)

if __name__=="__main__":
    main()
```

### Así verás el mensaje ahora:
```
 ACTIVE RANGE — Cambio de senal
━━━━━━━━━━━━━━━━━━━━
🟢 Probabilidad de exito: 78%
████████░░ Alta
Direccion dominante: COMPRA 📈
━━━━━━━━━━━━━━━━━━━━
🟢 Confluencia: Alcista — 3/5 TFs...
