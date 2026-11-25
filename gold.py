#!/usr/bin/env python3
"""trader-gold: analisis y planes para XAU/USD (swing, 4H y diario). Solo biblioteca estandar.
Nunca coloca ordenes. Escribe JSON en ./trader/gold/ (relativo al directorio actual)."""
import argparse, bisect, csv, html, io, json, math, os, random, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone, date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
GDL = timezone(timedelta(hours=-6))  # Guadalajara: UTC-6 fijo, sin horario de verano desde 2022
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
    LON = ZoneInfo("Europe/London")
except Exception:  # sin tzdata: aproximacion
    NY = timezone(timedelta(hours=-5))
    LON = timezone.utc
OUT = None  # se fija en main()

# ---------- calendario de respaldo (ver references/calendario.md) ----------
# Decision FOMC = ultimo dia de la reunion. 2026 publicado por la Fed; 2027 tentativo (verificar).
FOMC = {
    "2026": ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"],
    "2027": ["2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16", "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15"],
}
# NFP: normalmente el primer viernes del mes (excepciones por feriados): fecha aproximada, verificar.
# CPI: normalmente entre el dia 10 y 15 del mes: sin fecha exacta, verificar.

# ---------- utilidades ----------
def http(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def fmt(x, n=2):
    return "n/d" if x is None else f"{x:,.{n}f}"

def gdl(ts):
    return datetime.fromtimestamp(ts, GDL)

def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def load(name):
    p = OUT / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))

def yahoo(symbol, interval, rng):
    d = json.loads(http(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={rng}"))
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(r["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append((t, o, h, l, c))
    return bars

def get_gold(interval, rng):
    """Spot XAUUSD=X primero; si falla, futuros GC=F. Devuelve (barras, simbolo)."""
    for sym in ("XAUUSD=X", "GC=F"):
        try:
            b = yahoo(sym, interval, rng)
            if len(b) > 50:
                return b, sym
        except Exception:
            pass
    return None, None

def stooq_daily():
    txt = http("https://stooq.com/q/d/l/?s=xauusd&i=d")
    rows = list(csv.DictReader(io.StringIO(txt)))
    bars = []
    for r in rows:
        dt = datetime.strptime(r["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        bars.append((int(dt.timestamp()), float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])))
    if len(bars) < 50:
        raise ValueError("stooq sin datos")
    return bars

def resample(bars, secs):
    out, cur = [], None
    for t, o, h, l, c in bars:
        k = t - (t % secs)
        if cur is None or cur[0] != k:
            if cur:
                out.append(tuple(cur))
            cur = [k, o, h, l, c]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
    if cur:
        out.append(tuple(cur))
    return out

def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

def atr_series(bars, n=14):
    """Lista de ATR (Wilder) alineada con bars; None al inicio."""
    res = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1][4] if i else b[4]
        trs.append(max(b[2] - b[3], abs(b[2] - pc), abs(b[3] - pc)))
    if len(bars) <= n:
        return res
    a = sum(trs[1:n + 1]) / n
    res[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        res[i] = a
    return res

def trend_label(close, e20, e50, e200):
    if None in (e20, e50, e200):
        if None not in (e20, e50):
            return "alcista" if close > e20 > e50 else "bajista" if close < e20 < e50 else "lateral/mixta"
        return "sin datos"
    if close > e50 > e200 and e20 > e50:
        return "alcista"
    if close < e50 < e200 and e20 < e50:
        return "bajista"
    if close > e200:
        return "mixta con sesgo alcista"
    return "mixta con sesgo bajista"

# ---------- sesiones ----------
def sesiones(now_utc):
    d = now_utc.date()
    def rango(tz, h1, h2):
        a = datetime(d.year, d.month, d.day, h1, tzinfo=tz).astimezone(GDL)
        b = datetime(d.year, d.month, d.day, h2, tzinfo=tz).astimezone(GDL)
        return a, b
    tokyo = timezone(timedelta(hours=9))
    return {
        "Asia (Tokio 09-18 h)": rango(tokyo, 9, 18),
        "Londres (08-17 h local)": rango(LON, 8, 17),
        "Nueva York (08-17 h local)": rango(NY, 8, 17),
    }

def mercado_abierto(now_utc):
    n = now_utc.astimezone(NY)
    wd, h = n.weekday(), n.hour + n.minute / 60
    if wd == 5:
        return False, "cerrado (fin de semana)"
    if wd == 6 and h < 17:
        return False, "cerrado (abre domingo 17:00 NY = 16:00 Guadalajara aprox.)"
    if wd == 4 and h >= 16:
        return False, "cerrado (cierre semanal viernes 16:00 NY)"
    if wd in (0, 1, 2, 3) and 16 <= h < 17:
        return False, "pausa diaria (16:00-17:00 NY)"
    return True, "abierto"

def estado_sesiones(now_utc):
    ab, txt = mercado_abierto(now_utc)
    lines = [f"Mercado: {txt}"]
    nowg = now_utc.astimezone(GDL)
    for nombre, (a, b) in sesiones(now_utc).items():
        if a <= nowg < b:
            st = "ACTIVA"
        elif nowg < a:
            st = "aun no abre"
        else:
            st = "ya cerro"
        lines.append(f"  {nombre}: {a:%H:%M}-{b:%H:%M} Guadalajara -> {st}")
    return ab, lines

# ---------- macro ----------
def fred(sid, timeout=15):
    txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", timeout=timeout)
    rows = list(csv.reader(io.StringIO(txt)))[1:]
    vals = [(r[0], float(r[1])) for r in rows if len(r) > 1 and r[1] not in (".", "")]
    return vals

def fred_retry(sid, timeout=15):
    for _ in range(1):
        try:
            return fred(sid, timeout)
        except Exception:
            pass
    return None

def treasury_real():
    """Rendimiento real 10y (TIPS) del Tesoro de EE.UU.; misma fuente que FRED DFII10.
    Devuelve [(YYYY-MM-DD, valor)] ordenado ascendente, o None."""
    y = datetime.now(timezone.utc).year
    vals = {}
    for yr in (y - 1, y):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{yr}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={yr}")
        try:
            rows = list(csv.DictReader(io.StringIO(http(url, timeout=20))))
        except Exception:
            continue
        for r in rows:
            v = (r.get("10 YR") or "").strip()
            if not v:
                continue
            m, d, yy = r["Date"].split("/")
            vals[f"{yy}-{m.zfill(2)}-{d.zfill(2)}"] = float(v)
    return sorted(vals.items()) or None

def calendario(now_utc):
    """Eventos USD de alto impacto de esta semana. Devuelve (lista, fuente)."""
    try:
        data = json.loads(http("https://nfs.faireconomy.media/ff_calendar_thisweek.json"))
        ev = []
        kw = ("PCE", "GDP", "Retail", "ISM", "JOLTS", "Powell", "FOMC", "Fed ")
        for e in data:
            if e.get("country") != "USD":
                continue
            imp = e.get("impact")
            if imp == "High" or (imp == "Medium" and any(k in e.get("title", "") for k in kw)):
                t = datetime.fromisoformat(e["date"]).astimezone(GDL)
                ev.append({"cuando_gdl": t.strftime("%Y-%m-%d %H:%M"), "evento": e["title"], "impacto": imp,
                           "previo": e.get("previous"), "pronostico": e.get("forecast")})
        ev.sort(key=lambda x: x["cuando_gdl"])
        return ev, "Faireconomy (ForexFactory) esta semana"
    except Exception:
        pass
    # respaldo
    ev = []
    hoy = now_utc.astimezone(NY).date()
    fin = hoy + timedelta(days=7)
    for ds in FOMC.get(str(hoy.year), []) + FOMC.get(str(fin.year), []):
        d = date.fromisoformat(ds)
        if hoy <= d <= fin:
            ev.append({"cuando_gdl": ds + " 12:00", "evento": "Decision de tasas FOMC (verificar fecha)", "impacto": "High"})
    for dd in range(8):
        d = hoy + timedelta(days=dd)
        if d.weekday() == 4 and d.day <= 7:
            ev.append({"cuando_gdl": d.isoformat() + " 06:30", "evento": "NFP / Empleo EE.UU. (primer viernes, verificar)", "impacto": "High"})
    return ev, "respaldo local (verificar fechas en references/calendario.md)"

# ---------- datos tecnicos ----------
def cargar_series():
    h1, sym = get_gold("1h", "730d")
    nota = None
    d1, sym_d = get_gold("1d", "5y")
    if d1 is None:
        try:
            d1 = stooq_daily(); sym_d = "Stooq XAUUSD"
        except Exception:
            pass
    if h1 is None or d1 is None:
        raise SystemExit("No se pudieron obtener datos de precio (Yahoo y Stooq fallaron). Intenta mas tarde.")
    if sym == "GC=F":
        nota = "Fuente: futuros GC=F (Yahoo no entrego spot XAUUSD=X). Puede diferir unos dolares del spot."
    h4 = resample(h1, 4 * 3600)
    return h1, h4, d1, sym, nota

def analiza_tf(bars):
    cl = [b[4] for b in bars]
    a = atr_series(bars)
    e20, e50, e200 = ema(cl, 20), ema(cl, 50), ema(cl, 200)
    return {"cierre": cl[-1], "ema20": e20, "ema50": e50, "ema200": e200, "rsi14": rsi(cl),
            "atr14": a[-1], "tendencia": trend_label(cl[-1], e20, e50, e200)}

def redondos(p):
    return {"50_abajo": math.floor(p / 50) * 50, "50_arriba": math.ceil(p / 50) * 50 if p % 50 else p + 50,
            "100_abajo": math.floor(p / 100) * 100, "100_arriba": math.ceil(p / 100) * 100 if p % 100 else p + 100}

def niveles(h1, d1, now_utc):
    hoy = now_utc.astimezone(NY).date()
    comp = [b for b in d1 if datetime.fromtimestamp(b[0], timezone.utc).date() < hoy] or d1[:-1]
    prev = comp[-1]
    lunes = hoy - timedelta(days=hoy.weekday())
    sem = [b for b in d1 if datetime.fromtimestamp(b[0], timezone.utc).date() >= lunes] or d1[-1:]
    # sesion asiatica: 00:00-09:00 UTC (Tokio 09-18 h) mas reciente iniciada
    d = now_utc.date()
    ini = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if now_utc < ini:
        ini -= timedelta(days=1)
    asia = [b for b in h1 if ini.timestamp() <= b[0] < ini.timestamp() + 9 * 3600]
    return {"max_dia_previo": prev[2], "min_dia_previo": prev[3],
            "max_semana": max(b[2] for b in sem), "min_semana": min(b[3] for b in sem),
            "max_asia": max((b[2] for b in asia), default=None), "min_asia": min((b[3] for b in asia), default=None)}

def tecnico_data():
    h1, h4, d1, sym, nota = cargar_series()
    now = datetime.now(timezone.utc)
    precio = h1[-1][4]
    res = {"generado_gdl": now.astimezone(GDL).strftime("%Y-%m-%d %H:%M"), "simbolo": sym, "nota_fuente": nota,
           "precio": precio, "diario": analiza_tf(d1), "4h": analiza_tf(h4), "niveles": niveles(h1, d1, now),
           "redondos": redondos(precio)}
    return res, (h1, h4, d1)

def imprime_tecnico(t):
    print(f"XAU/USD ({t['simbolo']}) precio {fmt(t['precio'])}  | {t['generado_gdl']} hora Guadalajara")
    if t["nota_fuente"]:
        print("  Aviso:", t["nota_fuente"])
    for k, nom in (("diario", "DIARIO"), ("4h", "4H")):
        x = t[k]
        print(f"  {nom}: tendencia {x['tendencia']} | EMA20 {fmt(x['ema20'])} EMA50 {fmt(x['ema50'])} EMA200 {fmt(x['ema200'])} | RSI14 {fmt(x['rsi14'],1)} | ATR14 {fmt(x['atr14'])}")
    n = t["niveles"]
    print(f"  Dia previo: max {fmt(n['max_dia_previo'])} / min {fmt(n['min_dia_previo'])}")
    print(f"  Semana actual: max {fmt(n['max_semana'])} / min {fmt(n['min_semana'])}")
    print(f"  Sesion asiatica: max {fmt(n['max_asia'])} / min {fmt(n['min_asia'])}")
    r = t["redondos"]
    print(f"  Redondos: $50: {fmt(r['50_abajo'],0)} / {fmt(r['50_arriba'],0)} | $100: {fmt(r['100_abajo'],0)} / {fmt(r['100_arriba'],0)}")

# ---------- subcomandos ----------
def cmd_tecnico(a):
    t, _ = tecnico_data()
    save("tecnico.json", t)
    imprime_tecnico(t)
    print("  (guardado trader/gold/tecnico.json)")

def briefing_data(t, d1, now):
    """Datos del briefing (sin imprimir ni guardar). Reutilizado por briefing y dashboard."""
    prev_close = d1[-2][4] if len(d1) > 1 else None
    chg = t["precio"] - prev_close if prev_close else None
    pct = chg / prev_close * 100 if prev_close else None
    brief = {"generado_gdl": t["generado_gdl"], "simbolo": t["simbolo"], "nota_fuente": t["nota_fuente"],
             "precio": t["precio"], "cambio": chg, "cambio_pct": pct}
    precio = {"precio": t["precio"], "simbolo": t["simbolo"], "generado_gdl": t["generado_gdl"], "cambio": chg, "cambio_pct": pct}
    # DXY
    dxy = None
    try:
        db = yahoo("DX-Y.NYB", "1d", "1mo")
        c = [b[4] for b in db]
        dxy = {"valor": c[-1], "cambio_pct_1d": (c[-1] / c[-2] - 1) * 100, "cambio_pct_5d": (c[-1] / c[-6] - 1) * 100}
    except Exception:
        pass
    ry, fuente_real = treasury_real(), "Tesoro EE.UU."
    if not ry:
        ry, fuente_real = fred_retry("DFII10"), "FRED DFII10"
    usd = fred_retry("DTWEXBGS", timeout=6)
    real = None
    if ry and len(ry) > 6:
        real = {"fuente": fuente_real, "fecha": ry[-1][0], "valor": ry[-1][1], "cambio_pb_1d": (ry[-1][1] - ry[-2][1]) * 100, "cambio_pb_5d": (ry[-1][1] - ry[-6][1]) * 100}
    amp = None
    if usd and len(usd) > 6:
        amp = {"fecha": usd[-1][0], "valor": usd[-1][1], "cambio_pct_5d": (usd[-1][1] / usd[-6][1] - 1) * 100}
    brief.update({"dxy": dxy, "rendimiento_real_10y": real, "dolar_amplio": amp})
    ab, ses = estado_sesiones(now)
    ev, fuente_ev = calendario(now)
    brief["eventos"] = ev; brief["fuente_eventos"] = fuente_ev
    # sesgo
    score, razones = 0, []
    for k, nom in (("diario", "diario"), ("4h", "4H")):
        tr = t[k]["tendencia"]
        if tr == "alcista": score += 1; razones.append(f"tendencia {nom} alcista (+1)")
        elif tr == "bajista": score -= 1; razones.append(f"tendencia {nom} bajista (-1)")
        else: razones.append(f"tendencia {nom} {tr} (0)")
    if real:
        if real["cambio_pb_5d"] > 5: score -= 1; razones.append("rendimiento real al alza en 5 dias: viento en contra (-1)")
        elif real["cambio_pb_5d"] < -5: score += 1; razones.append("rendimiento real a la baja en 5 dias: viento a favor (+1)")
        else: razones.append("rendimiento real sin cambio claro (0)")
    else:
        razones.append("rendimiento real: sin datos (Tesoro y FRED no respondieron)")
    dref = dxy["cambio_pct_5d"] if dxy else (amp["cambio_pct_5d"] if amp else None)
    if dref is not None:
        if dref > 0.5: score -= 1; razones.append("dolar al alza en 5 dias: viento en contra (-1)")
        elif dref < -0.5: score += 1; razones.append("dolar a la baja en 5 dias: viento a favor (+1)")
        else: razones.append("dolar sin cambio claro (0)")
    sesgo = "alcista" if score >= 2 else "bajista" if score <= -2 else "neutral / mixto"
    brief.update({"sesgo": sesgo, "puntaje": score, "razones": razones, "mercado_abierto": ab})
    return brief, precio, ses, dxy, real, amp, ev, fuente_ev, score, razones, sesgo, chg, pct

def cmd_briefing(a):
    t, (h1, h4, d1) = tecnico_data()
    now = datetime.now(timezone.utc)
    brief, precio, ses, dxy, real, amp, ev, fuente_ev, score, razones, sesgo, chg, pct = briefing_data(t, d1, now)
    save("briefing.json", brief); save("precio.json", precio); save("tecnico.json", t)
    # salida
    print(f"=== ORO XAU/USD  ({t['generado_gdl']} hora Guadalajara, UTC-6) ===")
    print(f"Precio {fmt(t['precio'])} ({t['simbolo']}) | cambio vs cierre previo: {fmt(chg)} ({fmt(pct)}%)")
    if t["nota_fuente"]: print("Aviso:", t["nota_fuente"])
    print(f"DXY: {fmt(dxy['valor']) if dxy else 'n/d'}" + (f" ({dxy['cambio_pct_1d']:+.2f}% hoy, {dxy['cambio_pct_5d']:+.2f}% en 5d)" if dxy else ""))
    print("Rendimiento real 10y (TIPS): " + (f"{real['valor']:.2f}% al {real['fecha']} ({real['cambio_pb_5d']:+.0f} pb en 5 obs., {real['fuente']})" if real else "n/d (Tesoro y FRED no respondieron)"))
    print("Dolar amplio (DTWEXBGS): " + (f"{amp['valor']:.2f} al {amp['fecha']} ({amp['cambio_pct_5d']:+.2f}% en 5 obs.)" if amp else "n/d (FRED no respondio; el sesgo usa el DXY)"))
    print("Sesiones:"); print("\n".join(ses))
    print(f"Eventos USD de alto impacto ({fuente_ev}):")
    if ev:
        for e in ev: print(f"  {e['cuando_gdl']} GDL  {e['evento']}" + (f" [{e['impacto']}]" if e['impacto'] != 'High' else ""))
    else:
        print("  ninguno encontrado")
    print(f"Tecnico: diario {t['diario']['tendencia']}, 4H {t['4h']['tendencia']}, RSI diario {fmt(t['diario']['rsi14'],1)}, ATR 4H {fmt(t['4h']['atr14'])}")
    print(f"SESGO: {sesgo} (puntaje {score:+d}). " + "; ".join(razones))
    print("Esto es analisis, no una recomendacion: la decision es tuya. Para un plan: /trader-gold plan largo|corto")

def cmd_plan(a):
    d = a.dir.lower()
    if d not in ("largo", "corto"):
        raise SystemExit("Direccion invalida: usa 'largo' o 'corto'.")
    h1, h4, d1, sym, nota = cargar_series()
    price = h1[-1][4]
    entrada = a.entrada if a.entrada else price
    serie = d1 if a.tf == "1d" else h4
    atr = atr_series(serie)[-1]
    if a.stop_precio is not None:
        stop = a.stop_precio
    else:
        mult = a.stop_atr
        stop = entrada - mult * atr if d == "largo" else entrada + mult * atr
    riesgo = abs(entrada - stop)
    r = a.r
    if a.objetivo is not None:
        obj = a.objetivo
    else:
        obj = entrada + r * riesgo if d == "largo" else entrada - r * riesgo
    stop, obj, entrada = round(stop, 2), round(obj, 2), round(entrada, 2)
    riesgo = abs(entrada - stop)
    errs = []
    if d == "largo":
        if stop >= entrada: errs.append("El stop debe estar POR DEBAJO de la entrada en un largo.")
        if obj <= entrada: errs.append("El objetivo debe estar POR ENCIMA de la entrada en un largo.")
    else:
        if stop <= entrada: errs.append("El stop debe estar POR ENCIMA de la entrada en un corto.")
        if obj >= entrada: errs.append("El objetivo debe estar POR DEBAJO de la entrada en un corto.")
    if not errs and riesgo < 0.1 * atr:
        errs.append(f"El stop esta demasiado cerca ({fmt(riesgo)} < 10% del ATR): el spread lo barreria.")
    if errs:
        print("PLAN RECHAZADO:"); [print(" -", e) for e in errs]; sys.exit(1)
    rr = abs(obj - entrada) / riesgo
    plan = {"instrumento": "XAU/USD", "simbolo_datos": sym, "direccion": d, "entrada": entrada, "stop": stop, "objetivo": obj,
            "riesgo_usd_por_oz": round(riesgo, 2), "recompensa_usd_por_oz": round(abs(obj - entrada), 2), "r_multiplo": round(rr, 2),
            "atr14": round(atr, 2), "atr_tf": a.tf, "stop_en_atr": round(riesgo / atr, 2),
            "creado_gdl": datetime.now(GDL).strftime("%Y-%m-%d %H:%M"), "nota_fuente": nota}
    save("plan.json", plan)
    print(f"PLAN {d.upper()} XAU/USD  ({plan['creado_gdl']} hora Guadalajara)")
    print(f"  Entrada {fmt(entrada)} | Stop {fmt(stop)} | Objetivo {fmt(obj)}")
    print(f"  Riesgo ${fmt(riesgo)} por onza ({riesgo/atr:.2f}x ATR14 {a.tf.upper()} = {fmt(atr)}) | Recompensa ${fmt(abs(obj-entrada))} = {rr:.2f}R")
    if nota: print("  Aviso:", nota)
    print("  Guardado trader/gold/plan.json. Siguiente: timemachine y tamano. La decision es tuya.")

def sim_resultado(direccion, entry, stop, target, bars_fwd):
    """bars_fwd: lista de (alto, bajo, cierre) en precios. Devuelve ('T'|'S'|'N', R)."""
    riesgo = abs(entry - stop)
    long = direccion == "largo"
    for hi, lo, cl in bars_fwd:
        hit_t = hi >= target if long else lo <= target
        hit_s = lo <= stop if long else hi >= stop
        if hit_s:  # si ambos en la misma barra, se asume stop primero (conservador)
            return "S", -1.0
        if hit_t:
            return "T", abs(target - entry) / riesgo
    cl = bars_fwd[-1][2]
    return "N", ((cl - entry) if long else (entry - cl)) / riesgo

def simula_plan(plan, h1, h4, d1, barra, dias, rutas, semilla):
    """Montecarlo (bootstrap) + repeticion historica. plan: direccion, entrada, stop, objetivo,
    riesgo_usd_por_oz, recompensa_usd_por_oz, atr14, atr_tf. Devuelve (mc, hist, n_barras)."""
    base = h4 if barra == "4h" else h1
    por_dia = 6 if barra == "4h" else 23
    n = dias * por_dia
    d, e, s, t = plan["direccion"], plan["entrada"], plan["stop"], plan["objetivo"]
    rng = random.Random(semilla)
    rat = [(base[i][2] / base[i - 1][4], base[i][3] / base[i - 1][4], base[i][4] / base[i - 1][4]) for i in range(1, len(base))]
    cT = cS = cN = 0; sumR = 0.0
    for _ in range(rutas):
        p = e; fw = []
        for _k in range(n):
            hi_r, lo_r, c_r = rat[rng.randrange(len(rat))]
            fw.append((p * hi_r, p * lo_r, p * c_r)); p = p * c_r
        o, r = sim_resultado(d, e, s, t, fw)
        sumR += r
        if o == "T": cT += 1
        elif o == "S": cS += 1
        else: cN += 1
    mc = {"objetivo_primero_pct": cT / rutas * 100, "stop_primero_pct": cS / rutas * 100, "ninguno_pct": cN / rutas * 100, "r_promedio": sumR / rutas}
    serie = d1 if plan["atr_tf"] == "1d" else h4
    sper = 86400 if plan["atr_tf"] == "1d" else 4 * 3600
    atrs = atr_series(serie); starts = [b[0] for b in serie]
    k_s = plan["riesgo_usd_por_oz"] / plan["atr14"]
    k_t = plan["recompensa_usd_por_oz"] / plan["atr14"]
    hT = hS = hN = 0; hR = 0.0
    for i in range(len(base) - n):
        j = bisect.bisect_right(starts, base[i][0] - sper) - 1
        if j < 0 or atrs[j] is None: continue
        entry = base[i][4]; A = atrs[j]
        stp = entry - k_s * A if d == "largo" else entry + k_s * A
        tgt = entry + k_t * A if d == "largo" else entry - k_t * A
        fw = [(b[2], b[3], b[4]) for b in base[i + 1:i + 1 + n]]
        o, r = sim_resultado(d, entry, stp, tgt, fw)
        hR += r
        if o == "T": hT += 1
        elif o == "S": hS += 1
        else: hN += 1
    ht = max(hT + hS + hN, 1)
    hist = {"casos": hT + hS + hN, "objetivo_primero_pct": hT / ht * 100, "stop_primero_pct": hS / ht * 100, "ninguno_pct": hN / ht * 100, "r_promedio": hR / ht}
    return mc, hist, n

def cmd_timemachine(a):
    plan = load("plan.json")
    if not plan:
        raise SystemExit("No hay trader/gold/plan.json. Primero corre: plan --dir largo|corto")
    h1, h4, d1, sym, nota = cargar_series()
    d, e, s, t = plan["direccion"], plan["entrada"], plan["stop"], plan["objetivo"]
    mc, hist, n = simula_plan(plan, h1, h4, d1, a.barra, a.dias, a.rutas, a.semilla)
    ht = hist["casos"]
    # veredicto honesto
    be = 1 / (1 + plan["r_multiplo"]) * 100
    if mc["r_promedio"] > 0.1 and hist["r_promedio"] > 0.1:
        v = "Las dos pruebas dan R promedio positivo. Es una ventaja posible, no una garantia: el pasado no promete el futuro."
    elif mc["r_promedio"] < -0.1 and hist["r_promedio"] < -0.1:
        v = "Las dos pruebas dan R promedio negativo con estas distancias: la historia no respalda este plan tal como esta."
    else:
        v = "Resultado cercano a una moneda al aire (R promedio cerca de 0 o pruebas en desacuerdo): no se ve una ventaja clara en estas distancias."
    v += f" Para {plan['r_multiplo']:.1f}R se necesita acertar al menos {be:.0f}% de las veces solo para empatar (sin costos)."
    res = {"plan": plan, "barra": a.barra, "barras_horizonte": n, "dias": a.dias, "rutas": a.rutas, "montecarlo": mc, "historico": hist, "veredicto": v,
           "generado_gdl": datetime.now(GDL).strftime("%Y-%m-%d %H:%M")}
    save("timemachine.json", res)
    print(f"MAQUINA DEL TIEMPO XAU/USD {d.upper()} | entrada {fmt(e)} stop {fmt(s)} objetivo {fmt(t)} ({plan['r_multiplo']:.1f}R)")
    print(f"Horizonte: {a.dias} dias de trading = {n} barras de {a.barra.upper()} | datos: {sym}, ~2 anos de 1h")
    print(f"A) Montecarlo ({a.rutas:,} rutas, bootstrap de barras reales): objetivo primero {mc['objetivo_primero_pct']:.1f}% | stop primero {mc['stop_primero_pct']:.1f}% | ninguno {mc['ninguno_pct']:.1f}% | R promedio {mc['r_promedio']:+.2f}")
    print(f"B) Repeticion historica ({ht:,} puntos de partida, se traslapan): objetivo {hist['objetivo_primero_pct']:.1f}% | stop {hist['stop_primero_pct']:.1f}% | ninguno {hist['ninguno_pct']:.1f}% | R promedio {hist['r_promedio']:+.2f}")
    print("Veredicto:", v)
    print("Nota: si objetivo y stop caen en la misma barra se cuenta el stop. Es una simulacion, no una prediccion.")

def cmd_tamano(a):
    cfg = load("config.json") or {}
    if a.cuenta is not None:
        cfg["cuenta_usd"] = a.cuenta
    if a.riesgo is not None:
        cfg["riesgo_pct"] = a.riesgo
    if a.tope_dia is not None:
        cfg["tope_diario_pct"] = a.tope_dia
    cfg.setdefault("riesgo_pct", 1.0); cfg.setdefault("tope_diario_pct", 3.0)
    if not cfg.get("cuenta_usd"):
        print("FALTA_CUENTA: necesito el tamano de tu cuenta en USD. Corre: tamano --cuenta 5000")
        sys.exit(2)
    save("config.json", cfg)
    if a.dist is not None:
        dist = a.dist; src = "distancia dada"
    else:
        plan = load("plan.json")
        if not plan:
            raise SystemExit("No hay plan.json ni --dist. Primero corre: plan --dir largo|corto")
        dist = plan["riesgo_usd_por_oz"]; src = "plan.json"
    cuenta, rp = cfg["cuenta_usd"], cfg["riesgo_pct"]
    riesgo_usd = cuenta * rp / 100
    perd_lote = dist * 100  # 1 lote = 100 oz
    lotes = math.floor(riesgo_usd / perd_lote * 100 + 1e-9) / 100
    tope_usd = cuenta * cfg["tope_diario_pct"] / 100
    res = {"cuenta_usd": cuenta, "riesgo_pct": rp, "riesgo_usd": round(riesgo_usd, 2), "tope_diario_pct": cfg["tope_diario_pct"],
           "tope_diario_usd": round(tope_usd, 2), "distancia_stop_usd_oz": dist, "origen_distancia": src, "lotes": lotes,
           "generado_gdl": datetime.now(GDL).strftime("%Y-%m-%d %H:%M")}
    if lotes < 0.01:
        min_r = 0.01 * perd_lote
        res["rechazado"] = True
        save("size.json", res)
        print(f"RECHAZADO: con ${fmt(riesgo_usd)} de riesgo ({rp}% de ${fmt(cuenta,0)}) y un stop de ${fmt(dist)}/oz no alcanza ni el lote minimo de 0.01.")
        print(f"El lote minimo arriesgaria ${fmt(min_r)} ({min_r/cuenta*100:.2f}% de la cuenta). No se ajusta el riesgo a escondidas: reduce el stop, sube la cuenta o decide tu mismo.")
        sys.exit(1)
    real = lotes * perd_lote
    res.update({"perdida_real_en_stop_usd": round(real, 2), "perdida_real_pct": round(real / cuenta * 100, 3), "onzas": round(lotes * 100, 2),
                "operaciones_hasta_tope": int(tope_usd // real) if real else None, "rechazado": False})
    plan = load("plan.json")
    if plan and a.dist is None:
        res["ganancia_en_objetivo_usd"] = round(lotes * 100 * plan["recompensa_usd_por_oz"], 2)
    save("size.json", res)
    print(f"TAMANO: {lotes:.2f} lotes ({res['onzas']:.0f} oz)  | cuenta ${fmt(cuenta,0)}, riesgo {rp}% = ${fmt(riesgo_usd)}")
    print(f"  Stop ${fmt(dist)}/oz ({src}) -> perdida en el stop ${fmt(real)} ({res['perdida_real_pct']:.2f}% de la cuenta)")
    if "ganancia_en_objetivo_usd" in res:
        print(f"  Ganancia en el objetivo: ${fmt(res['ganancia_en_objetivo_usd'])}")
    print(f"  Tope diario {cfg['tope_diario_pct']}% = ${fmt(tope_usd)} (~{res['operaciones_hasta_tope']} perdidas completas); al llegar, deja de operar el dia.")
    print("  Referencia: 1 lote = 100 oz; mover $1 con 0.01 lote = $1. Revisa el margen de tu broker. La decision es tuya.")

# ---------- noticias ----------
FEEDS = [
    ("Google News EN", "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+OR+%22gold+futures%22+when:3d&hl=en-US&gl=US&ceid=US:en", False),
    ("Google News ES", "https://news.google.com/rss/search?q=%22precio+del+oro%22+OR+XAUUSD+when:3d&hl=es-419&gl=MX&ceid=MX:es-419", False),
    ("Google News macro", "https://news.google.com/rss/search?q=Fed+OR+Powell+OR+Treasury+yields+OR+CPI+OR+payrolls+when:2d&hl=en-US&gl=US&ceid=US:en", True),
    ("FXStreet", "https://www.fxstreet.com/rss/news", True),
    ("Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", True),
]
# categoria -> palabras clave (minusculas, ingles y espanol)
CATEGORIAS = [
    ("fed", ["fed", "fomc", "powell", "monetary policy", "rate cut", "rate hike", "interest rate", "tasas", "reserva federal", "minutes", "minutas"]),
    ("inflacion", ["inflation", "cpi", "pce", "ppi", "inflacion", "inflación"]),
    ("empleo", ["payroll", "nfp", "jobless", "unemployment", "jobs report", "empleo", "nóminas", "nominas"]),
    ("dolar", ["dollar", "dxy", "usd", "dólar", "dolar"]),
    ("rendimientos", ["yield", "treasur", "tips", "bond", "rendimiento", "bonos"]),
    ("geopolitica", ["war", "iran", "israel", "russia", "ukraine", "china", "taiwan", "missile", "sanction", "conflict", "guerra", "sanciones", "geopolit", "safe haven", "refugio"]),
    ("aranceles", ["tariff", "trade war", "arancel"]),
    ("bancos_centrales", ["central bank", "pboc", "reserves", "bancos centrales", "world gold council", "wgc"]),
    ("etf_flujos", ["etf", "spdr", "gld", "holdings", "inflows", "outflows", "flujos"]),
    ("fiscal", ["debt ceiling", "deficit", "shutdown", "fiscal", "deuda"]),
]
ORO = ["gold", "xau", "oro", "bullion", "precious metal"]
BASURA = ["drill", "intersect", "g/t", "placer", "exploration", "claims", "mining stock", "royalty", "tsx-v", "tsxv",
          "india:", "en la india", "jewel", "joyer", "(tsx:", "(nyse:", "(asx:", "(nasdaq:", "stock declines", "stock rises",
          "gold rate", "mcx", "rupee", "18k", "22k", "24k", "bangalore", "mumbai", "pakistan", "dubai gold"]

def _tiene(low, kws):
    import re
    return any(re.search(r"\b" + re.escape(k), low) for k in kws)

def _norm(t):
    import re
    return re.sub(r"[^a-z0-9áéíóúñ ]", "", t.lower())[:90]

def noticias_fetch(horas):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    ahora = datetime.now(timezone.utc)
    items, fallas = {}, []
    for nombre, url, requiere_tema in FEEDS:
        try:
            root = ET.fromstring(http(url, timeout=20))
        except Exception:
            fallas.append(nombre); continue
        for it in root.iter("item"):
            titulo = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
            except Exception:
                continue
            if (ahora - pub).total_seconds() > horas * 3600:
                continue
            medio = nombre
            if nombre.startswith("Google") and " - " in titulo:
                titulo, medio = titulo.rsplit(" - ", 1)
            low = titulo.lower()
            if any(b in low for b in BASURA):
                continue
            cats = [c for c, kws in CATEGORIAS if _tiene(low, kws)]
            es_oro = _tiene(low, ORO)
            if requiere_tema and not (es_oro or cats):
                continue
            if nombre == "Reserva Federal" and "fed" not in cats:
                continue
            clave = _norm(titulo)
            if clave in items:
                continue
            items[clave] = {"id": clave, "titulo": titulo, "medio": medio, "fuente": nombre, "link": link,
                            "utc": pub.isoformat(), "gdl": pub.astimezone(GDL).strftime("%Y-%m-%d %H:%M"),
                            "categorias": cats or (["oro"] if es_oro else ["otros"]), "menciona_oro": es_oro,
                            "relevancia": (2 if es_oro else 0) + len(cats)}
    lista = sorted(items.values(), key=lambda x: x["utc"], reverse=True)
    return lista, fallas

def cmd_noticias(a):
    lista, fallas = noticias_fetch(a.horas)
    vistas_f = OUT / "noticias_vistas.json"
    try:
        vistas = set(json.loads(vistas_f.read_text(encoding="utf-8")))
    except Exception:
        vistas = set()
    nuevas = [n for n in lista if n["id"] not in vistas]
    mostrar = lista if a.todas else nuevas
    mostrar = sorted(mostrar, key=lambda x: (x["relevancia"] >= 2, x["utc"]), reverse=True)[:a.max]
    mostrar.sort(key=lambda x: x["utc"], reverse=True)
    ahora = datetime.now(GDL).strftime("%Y-%m-%d %H:%M")
    save("noticias.json", {"generado_gdl": ahora, "horas": a.horas, "fallas": fallas, "mostradas": mostrar, "total_ventana": len(lista)})
    OUT.mkdir(parents=True, exist_ok=True)
    if not getattr(a, "no_marcar", False):
        vistas_f.write_text(json.dumps(sorted(vistas | {n["id"] for n in lista})[-3000:], ensure_ascii=False), encoding="utf-8")
    print(f"=== NOTICIAS ORO / MACRO USD  ({ahora} hora Guadalajara, ultimas {a.horas} h) ===")
    print(f"{len(lista)} titulares en la ventana, {len(nuevas)} nuevos desde la ultima revision; muestro {len(mostrar)}" + (" (todas)" if a.todas else ""))
    if fallas:
        print("Sin respuesta:", ", ".join(fallas))
    if not mostrar:
        print("  Nada nuevo. Usa --todas para ver todo lo de la ventana.")
    conteo = {}
    for n in mostrar:
        for c in n["categorias"]:
            conteo[c] = conteo.get(c, 0) + 1
        print(f"  {n['gdl']}  [{', '.join(n['categorias'])}]  {n['titulo']}  ({n['medio']})")
    if conteo:
        print("Temas: " + ", ".join(f"{k} {v}" for k, v in sorted(conteo.items(), key=lambda kv: -kv[1])))
    print("(guardado trader/gold/noticias.json con los links)")

# ---------- escenarios ----------
def ema_series(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out

def dir_tendencia(t):
    """Devuelve (direccion, explicacion). Si diario y 4H no coinciden, manda el 4H."""
    ld, l4 = t["diario"]["tendencia"], t["4h"]["tendencia"]
    if ld == l4 and ld in ("alcista", "bajista"):
        return ld, f"diario y 4H {ld}"
    if "alcista" in l4:
        return "alcista", f"4H manda ({l4}); diario {ld}"
    if "bajista" in l4:
        return "bajista", f"4H manda ({l4}); diario {ld}"
    x = t["4h"]
    d = "alcista" if x["ema50"] and x["cierre"] >= x["ema50"] else "bajista"
    return d, f"4H lateral; se usa el cierre vs EMA50 ({d})"

def candidatos_niveles(t, atr):
    p = t["precio"]; n = t["niveles"]
    c = []
    for k, nom in (("max_semana", "maximo de la semana"), ("min_semana", "minimo de la semana"),
                   ("max_dia_previo", "maximo del dia previo"), ("min_dia_previo", "minimo del dia previo"),
                   ("max_asia", "maximo asiatico"), ("min_asia", "minimo asiatico")):
        if n.get(k) is not None:
            c.append((nom, n[k]))
    lo = math.floor((p - 6 * atr) / 50) * 50
    v = lo
    while v <= p + 6 * atr:
        c.append((f"redondo ${v:,.0f}", float(v)))
        v += 50
    return c

def veredicto_sim(mc, hist):
    r = (mc["r_promedio"] + hist["r_promedio"]) / 2
    if mc["r_promedio"] > 0.1 and hist["r_promedio"] > 0.1:
        v = "R promedio positivo en ambas pruebas: ventaja posible, no garantia"
    elif mc["r_promedio"] < -0.1 and hist["r_promedio"] < -0.1:
        v = "R promedio negativo en ambas pruebas: la historia no lo respalda"
    else:
        v = "cerca de moneda al aire: sin ventaja clara"
    return r, v

def escenarios_data(t, h1, h4, d1, ev, now, rutas=3000, dias=5):
    p = t["precio"]
    atr = t["4h"]["atr14"]
    dire, why = dir_tendencia(t)
    cands = candidatos_niveles(t, atr)
    minimo = 0.25 * atr
    sop = [c for c in cands if c[1] <= p - minimo]
    res = [c for c in cands if c[1] >= p + minimo]
    S = max(sop, key=lambda c: c[1]) if sop else None
    R = min(res, key=lambda c: c[1]) if res else None
    stop_d = 1.5 * atr
    rsi_d, rsi_4 = t["diario"]["rsi14"], t["4h"]["rsi14"]
    esc = []
    def nuevo(id_, nombre, d, nivel, disp, contra=False):
        e = round(nivel[1], 2)
        stop = e - stop_d if d == "largo" else e + stop_d
        obj = e + 2 * stop_d if d == "largo" else e - 2 * stop_d
        return {"id": id_, "nombre": nombre, "direccion": d, "nivel": nivel[0], "disparador": disp,
                "entrada": e, "stop": round(stop, 2), "objetivo": round(obj, 2), "r_multiplo": 2.0,
                "riesgo_usd_oz": round(stop_d, 2), "distancia_precio": round(e - p, 2),
                "contra_tendencia": contra}
    if dire == "bajista":
        if S:
            esc.append(nuevo("A", "Corto por ruptura", "corto", S, f"Cierre de vela 4H por debajo de {fmt(S[1])} ({S[0]})"))
        if R:
            esc.append(nuevo("B", "Corto si rechaza (pullback)", "corto", R, f"Precio sube a {fmt(R[1])} ({R[0]}) y una vela 4H cierra de nuevo por debajo"))
        if R and ((rsi_d is not None and rsi_d < 30) or (rsi_4 is not None and rsi_4 < 30)):
            esc.append(nuevo("C", "Rebote largo (contra tendencia, mas riesgo)", "largo", R, f"Cierre de vela 4H por encima de {fmt(R[1])} ({R[0]}); RSI en sobreventa", contra=True))
    else:
        if R:
            esc.append(nuevo("A", "Largo por ruptura", "largo", R, f"Cierre de vela 4H por encima de {fmt(R[1])} ({R[0]})"))
        if S:
            esc.append(nuevo("B", "Largo si rechaza (pullback)", "largo", S, f"Precio baja a {fmt(S[1])} ({S[0]}) y una vela 4H cierra de nuevo por encima"))
        if S and ((rsi_d is not None and rsi_d > 70) or (rsi_4 is not None and rsi_4 > 70)):
            esc.append(nuevo("C", "Rebote corto (contra tendencia, mas riesgo)", "corto", S, f"Cierre de vela 4H por debajo de {fmt(S[1])} ({S[0]}); RSI en sobrecompra", contra=True))
    # cuenta
    cfg = load("config.json") or {}
    cuenta = cfg.get("cuenta_usd"); rp = cfg.get("riesgo_pct", 1.0)
    for e in esc:
        plan = {"direccion": e["direccion"], "entrada": e["entrada"], "stop": e["stop"], "objetivo": e["objetivo"],
                "riesgo_usd_por_oz": e["riesgo_usd_oz"], "recompensa_usd_por_oz": round(2 * stop_d, 2), "atr14": atr, "atr_tf": "4h"}
        mc, hist, n = simula_plan(plan, h1, h4, d1, "1h", dias, rutas, 19)
        r, v = veredicto_sim(mc, hist)
        e["simulacion"] = {"montecarlo": mc, "historico": hist, "r_promedio": r, "veredicto": v, "rutas": rutas, "dias": dias}
        if cuenta:
            riesgo_usd = cuenta * rp / 100
            lotes = math.floor(riesgo_usd / (e["riesgo_usd_oz"] * 100) * 100 + 1e-9) / 100
            if lotes < 0.01:
                e["lotes"] = {"rechazado": True, "nota": f"con ${fmt(riesgo_usd)} de riesgo no alcanza el lote minimo 0.01 (arriesgaria ${fmt(e['riesgo_usd_oz'] * 1)} por 0.01 lote)"}
            else:
                e["lotes"] = {"rechazado": False, "lotes": lotes, "perdida_en_stop_usd": round(lotes * e["riesgo_usd_oz"] * 100, 2)}
    if esc:
        esc.sort(key=lambda e: e["simulacion"]["r_promedio"], reverse=True)
        esc[0]["mejor"] = True
    # no operar
    no = []
    ab, txt = mercado_abierto(now)
    if not ab:
        no.append(f"Mercado {txt}.")
    nowg = now.astimezone(GDL)
    for x in ev or []:
        try:
            et = datetime.strptime(x["cuando_gdl"], "%Y-%m-%d %H:%M").replace(tzinfo=GDL)
        except Exception:
            continue
        if x.get("impacto") == "High" and 0 <= (et - nowg).total_seconds() <= 7200:
            no.append(f"Evento USD de alto impacto en menos de 2 h: {x['evento']} a las {et:%H:%M} GDL.")
    if nowg.weekday() == 4 and nowg.hour >= 13:
        no.append("Viernes despues de las 13:00 GDL: liquidez baja y cierre semanal cerca.")
    a4 = [x for x in atr_series(h4) if x is not None][-50:]
    if a4 and atr > 1.5 * (sum(a4) / len(a4)):
        no.append(f"ATR 4H inusualmente alto ({fmt(atr)} vs promedio 50 barras {fmt(sum(a4) / len(a4))}): mercado muy volatil.")
    return {"generado_gdl": nowg.strftime("%Y-%m-%d %H:%M"), "precio": p, "atr4h": atr, "tendencia": dire, "tendencia_razon": why,
            "rsi_diario": rsi_d, "rsi_4h": rsi_4, "soporte": S, "resistencia": R, "cuenta_configurada": bool(cuenta),
            "no_operar": no, "escenarios": esc}

def cmd_escenarios(a):
    t, (h1, h4, d1) = tecnico_data()
    now = datetime.now(timezone.utc)
    ev, fuente = calendario(now)
    E = escenarios_data(t, h1, h4, d1, ev, now, a.rutas, a.dias)
    save("escenarios.json", E)
    print(f"=== ESCENARIOS DEL DIA XAU/USD ({E['generado_gdl']} hora Guadalajara) ===")
    print(f"Precio {fmt(E['precio'])} | ATR 4H {fmt(E['atr4h'])} | tendencia {E['tendencia']} ({E['tendencia_razon']}) | RSI diario {fmt(E['rsi_diario'],1)}, 4H {fmt(E['rsi_4h'],1)}")
    if t["nota_fuente"]:
        print("Aviso:", t["nota_fuente"])
    if E["no_operar"]:
        print("CONDICIONES PARA NO OPERAR:")
        for x in E["no_operar"]:
            print("  !", x)
    if not E["escenarios"]:
        print("No hay niveles utiles para armar escenarios.")
    for e in E["escenarios"]:
        s = e["simulacion"]; mc, h = s["montecarlo"], s["historico"]
        tag = "  <- mejor balance segun la simulacion" if e.get("mejor") else ""
        print(f"\n[{e['id']}] {e['nombre']} ({e['direccion']}){tag}")
        print(f"  Disparador: {e['disparador']}")
        print(f"  Entrada {fmt(e['entrada'])} | Stop {fmt(e['stop'])} | Objetivo {fmt(e['objetivo'])} | {e['r_multiplo']:.0f}R | riesgo ${fmt(e['riesgo_usd_oz'])}/oz | a {e['distancia_precio']:+.2f} del precio")
        print(f"  Montecarlo ({s['rutas']:,} rutas, {s['dias']} d): objetivo {mc['objetivo_primero_pct']:.0f}% | stop {mc['stop_primero_pct']:.0f}% | ninguno {mc['ninguno_pct']:.0f}% | R {mc['r_promedio']:+.2f}")
        print(f"  Historico ({h['casos']:,} casos): objetivo {h['objetivo_primero_pct']:.0f}% | stop {h['stop_primero_pct']:.0f}% | ninguno {h['ninguno_pct']:.0f}% | R {h['r_promedio']:+.2f}")
        print(f"  R promedio {s['r_promedio']:+.2f}: {s['veredicto']}")
        if "lotes" in e:
            l = e["lotes"]
            print("  Lotes: " + (f"RECHAZADO, {l['nota']}" if l["rechazado"] else f"{l['lotes']:.2f} (perdida en el stop ${fmt(l['perdida_en_stop_usd'])})"))
        if e["contra_tendencia"]:
            print("  OJO: contra tendencia, mas riesgo.")
    if not E["cuenta_configurada"]:
        print("\n(Sin cuenta en config.json: no se calcularon lotes. Usa: tamano --cuenta 5000)")
    print("\nSon escenarios condicionales, no recomendaciones. La decision es tuya.")

# ---------- dashboard ----------
def sesion_activa(now):
    nowg = now.astimezone(GDL)
    act = [n.split(" (")[0] for n, (a, b) in sesiones(now).items() if a <= nowg < b]
    ab, txt = mercado_abierto(now)
    if not ab:
        return "Mercado cerrado"
    return " + ".join(act) if act else "Entre sesiones"

DASH_HTML = r"""<!doctype html>
<html lang="es-MX"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oro XAU/USD - Tablero</title>
<style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#1c1a16;--mut:#6b665c;--line:#e4dfd3;--up:#12805c;--dn:#c0392b;--gold:#b8860b;--goldbg:#fbf3d9;--warnbg:#fde8e6;--chart-grid:#ece7da}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#14130f;--card:#1e1c17;--ink:#f1ede2;--mut:#a39d8e;--line:#34312a;--up:#3ec99a;--dn:#ff7b6b;--gold:#e5b94a;--goldbg:#3a2f12;--warnbg:#3b1d1a;--chart-grid:#2a2721}}
:root[data-theme="dark"]{--bg:#14130f;--card:#1e1c17;--ink:#f1ede2;--mut:#a39d8e;--line:#34312a;--up:#3ec99a;--dn:#ff7b6b;--gold:#e5b94a;--goldbg:#3a2f12;--warnbg:#3b1d1a;--chart-grid:#2a2721}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.45}
body{background:var(--bg);overflow-x:hidden}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.2rem;margin:0}h2{font-size:1.05rem;margin:0 0 10px;color:var(--gold)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px;min-width:0}
.head{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between}
.price{font-size:2rem;font-weight:700}.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut);font-size:.85rem}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:600;border:1px solid var(--gold);background:var(--goldbg);color:var(--gold)}
.badge.alcista{border-color:var(--up);color:var(--up)}.badge.bajista{border-color:var(--dn);color:var(--dn)}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;min-width:0}
.kpi b{display:block;font-size:1.1rem}.kpi span{font-size:.78rem;color:var(--mut)}
.btns button{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 14px;font-size:.9rem;cursor:pointer}
.btns button.on{border-color:var(--gold);color:var(--gold);font-weight:600}
#chart{width:100%;height:440px;margin-top:10px}
.legend{font-size:.78rem;color:var(--mut);margin-top:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.esc{border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.esc.best{border:2px solid var(--gold);background:var(--goldbg)}
.esc h3{margin:0 0 4px;font-size:1rem}
.tag{font-size:.75rem;font-weight:600;color:var(--gold)}
.tag.warn{color:var(--dn)}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:6px 0;background:var(--line)}
.bar i{display:block;height:100%}.b-t{background:var(--up)}.b-s{background:var(--dn)}.b-n{background:var(--mut);opacity:.5}
table.k{width:100%;border-collapse:collapse;font-size:.85rem}table.k td{padding:2px 0}table.k td:last-child{text-align:right}
.warnbox{background:var(--warnbg);border:1px solid var(--dn);color:var(--dn);border-radius:10px;padding:10px 14px;margin-bottom:12px}
.warnbox ul{margin:4px 0 0 18px;padding:0}
.vivo{margin-top:6px;font-size:.9rem;font-variant-numeric:tabular-nums}.vivo b{font-size:1.05rem}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--up);margin-right:6px;vertical-align:middle}
ul.l{list-style:none;margin:0;padding:0}ul.l li{padding:8px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}ul.l li:last-child{border:0}
.chip{display:inline-block;font-size:.7rem;padding:1px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut);margin-right:4px}
a{color:var(--gold)}
footer{font-size:.8rem;color:var(--mut);padding:8px 0 24px}
@media (max-width:700px){.kpis{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.price{font-size:1.7rem}#chart{height:340px}}
</style></head><body><div class="wrap">
__HEADER__
__KPIS__
<div class="card"><h2>Grafica</h2>
<div class="btns"><button id="b4" class="on">4H</button> <button id="bd">Diario</button> <button id="bn" class="on">Niveles</button></div>
<div id="chart"></div>
<div class="legend" id="nivlist"></div>
<div class="legend" id="ult"></div>
<div class="legend">EMA20 azul, EMA50 naranja, EMA200 morado. Lineas solidas: el escenario destacado, que solo cuenta si se cumple su disparador (entrada dorada, stop rojo, objetivo verde); el % es la probabilidad simulada de tocar ese nivel primero, no un pronostico. Punteadas grises: niveles cercanos (boton Niveles para ocultarlos). Hora de Guadalajara (UTC-6). Cada vela se marca con su hora de apertura.</div></div>
__ESC__
__EVENTOS__
__NOTICIAS__
<footer>Solo analisis, no es una recomendacion de inversion. La decision de operar, el tamano y el riesgo son tuyos. Operar oro con apalancamiento puede perder mas que el deposito. __FUENTE__</footer>
</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script id="datos" type="application/json">__DATA__</script>
<script>
(function(){
var D=JSON.parse(document.getElementById('datos').textContent);
var el=document.getElementById('chart');
if(typeof LightweightCharts==='undefined'){el.textContent='No se pudo cargar la libreria de graficas (sin internet).';return;}
function css(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
var chart=LightweightCharts.createChart(el,{width:el.clientWidth,height:el.clientHeight,
 layout:{background:{color:css('--card')},textColor:css('--ink'),fontSize:11},
 grid:{vertLines:{color:css('--chart-grid')},horzLines:{color:css('--chart-grid')}},
 rightPriceScale:{borderColor:css('--line')},timeScale:{borderColor:css('--line'),timeVisible:true,secondsVisible:false}});
var up=css('--up'),dn=css('--dn');
var cs=chart.addCandlestickSeries({upColor:up,downColor:dn,borderUpColor:up,borderDownColor:dn,wickUpColor:up,wickDownColor:dn});
var cols={ema20:'#3b82f6',ema50:'#f59e0b',ema200:'#8b5cf6'};
var es={};['ema20','ema50','ema200'].forEach(function(k){es[k]=chart.addLineSeries({color:cols[k],lineWidth:1,priceLineVisible:false,lastValueVisible:false});});
var lines=[];
function setTF(tf){
 var d=D[tf];cs.setData(d.velas);
 ['ema20','ema50','ema200'].forEach(function(k){es[k].setData(d[k]);});
 chart.applyOptions({timeScale:{timeVisible:tf==='h4'}});
 var nv=d.velas.length,vis=el.clientWidth<600?(tf==='h4'?45:60):(tf==='h4'?100:140);
 chart.timeScale().setVisibleLogicalRange({from:Math.max(0,nv-vis),to:nv+2});
 document.getElementById('b4').className=tf==='h4'?'on':'';
 document.getElementById('bd').className=tf==='d'?'on':'';
 var v=d.velas[d.velas.length-1],t=new Date(v.time*1000),p=function(n){return (n<10?'0':'')+n;};
 var u=document.getElementById('ult');
 if(tf==='h4'){var f=new Date((v.time+14400)*1000);
  u.textContent='Ultima vela 4H (en curso): abrio '+p(t.getUTCHours())+':00 y cierra '+p(f.getUTCHours())+':00. Cierre actual '+v.close.toFixed(2)+'. Datos al '+D.generado+' (hora Guadalajara).';}
 else{u.textContent='Ultima vela diaria (en curso). Cierre actual '+v.close.toFixed(2)+'. Datos al '+D.generado+' (hora Guadalajara).';}
}
// Lineas: el escenario (entrada/stop/objetivo) con etiqueta en el eje; los niveles finos, sin etiqueta
// en el eje, agrupados cuando quedan juntos y solo los cercanos al precio.
var atr=D.atr||20,tol=Math.max(1,atr*0.15);
var vel=D.h4.velas,ref=vel[vel.length-1].close;
var esc_=[],niv=[];
D.lineas.forEach(function(l){(l.tipo==='nivel'?niv:esc_).push({precio:l.precio,titulo:l.titulo,tipo:l.tipo});});
esc_.forEach(function(e){e.titulo=e.titulo.replace(/ [A-Z]$/,'');});
niv=niv.filter(function(n){
 var s=esc_.filter(function(e){return Math.abs(e.precio-n.precio)<=tol;})[0];
 if(s){s.titulo+=' / '+n.titulo;return false;}
 return Math.abs(n.precio-ref)<=atr*3;
}).sort(function(a,b){return a.precio-b.precio;});
var grp=[];niv.forEach(function(n){var g=grp[grp.length-1];
 if(g&&n.precio-g.precio<=tol){g.titulo+=' / '+n.titulo;}else{grp.push(n);}});
var nivLines=[];
esc_.forEach(function(l){
 var c=l.tipo==='stop'?dn:l.tipo==='obj'?up:css('--gold');
 cs.createPriceLine({price:l.precio,color:c,lineWidth:2,lineStyle:0,axisLabelVisible:true,title:l.titulo});
});
function showNiv(on){
 nivLines.forEach(function(p){cs.removePriceLine(p);});nivLines=[];
 if(on)grp.forEach(function(l){nivLines.push(cs.createPriceLine({price:l.precio,color:css('--mut'),lineWidth:1,lineStyle:1,axisLabelVisible:false,title:l.titulo}));});
 var b=document.getElementById('bn');if(b)b.className=on?'on':'';
}
var nivOn=true;showNiv(true);
// Los nombres de los niveles van abajo de la grafica (en el eje se amontonan).
(function(){var box=document.getElementById('nivlist');if(!box)return;
 var all=grp.slice().sort(function(a,b){return b.precio-a.precio;});
 box.textContent='';var h=document.createElement('b');h.textContent='Niveles cercanos: ';box.appendChild(h);
 all.forEach(function(l,i){var s=document.createElement('span');s.className=l.precio>ref?'dn':'up';
  s.textContent=/^\$[\d,]+$/.test(l.titulo)?l.titulo:l.titulo+' '+l.precio.toLocaleString('en-US',{minimumFractionDigits:2});box.appendChild(s);
  if(i<all.length-1)box.appendChild(document.createTextNode('  |  '));});
 var n=document.createElement('div');n.textContent='Rojo = resistencia (arriba del precio), verde = soporte (abajo).';box.appendChild(n);})();
document.getElementById('bn').onclick=function(){nivOn=!nivOn;showNiv(nivOn);};
document.getElementById('b4').onclick=function(){setTF('h4');};
document.getElementById('bd').onclick=function(){setTF('d');};
setTF('h4');
function theme(){chart.applyOptions({layout:{background:{color:css('--card')},textColor:css('--ink')},grid:{vertLines:{color:css('--chart-grid')},horzLines:{color:css('--chart-grid')}}});}
try{window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',theme);}catch(e){}
window.addEventListener('resize',function(){chart.applyOptions({width:el.clientWidth});});
})();
(function(){
// Precio spot en vivo (gold-api.com; respaldo Binance PAXG). Si la red lo bloquea, no se muestra nada.
var box=document.getElementById('vivo');if(!box||!window.fetch)return;
var prev=null,last=null;
function show(p,src){
 var c=prev===null?'':(p>prev?'up':p<prev?'dn':'');prev=p;last=Date.now();
 box.hidden=false;box.textContent='';
 var d=document.createElement('span');d.className='dot';box.appendChild(d);
 box.appendChild(document.createTextNode('Spot en vivo '));
 var b=document.createElement('b');b.className=c;b.textContent=p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});box.appendChild(b);
 var s=document.createElement('span');s.className='mut';s.id='vivo-t';s.textContent=' ('+src+', ahora)';box.appendChild(s);
}
function tick(){
 fetch('https://api.gold-api.com/price/XAU',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
  if(typeof j.price!=='number')throw 0;show(j.price,'gold-api.com');
 }).catch(function(){
  return fetch('https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
   var p=parseFloat(j.price);if(!isFinite(p))throw 0;show(p,'Binance PAXG aprox.');});
 }).catch(function(){});
}
tick();setInterval(tick,30000);
setInterval(function(){var t=document.getElementById('vivo-t');if(t&&last){var s=Math.round((Date.now()-last)/1000);t.textContent=t.textContent.replace(/, .*\)$/,', hace '+s+' s)');}},5000);
// En la version publicada (http/https) recarga cada 15 min para traer graficas, escenarios y noticias nuevas.
// Se agrega ?t= para saltarse la copia guardada del navegador (GitHub Pages la guarda 10 min).
if(/^https?:$/.test(location.protocol)){setTimeout(function(){location.replace(location.pathname+'?t='+Date.now());},15*60*1000);}
})();
</script></body></html>
"""

def _serie_velas(bars, daily=False):
    velas, vistos = [], {}
    for b in bars:
        if daily:
            tm = (datetime.fromtimestamp(b[0], timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
        else:
            tm = b[0] - 6 * 3600
        vistos[tm] = {"time": tm, "open": round(b[1], 2), "high": round(b[2], 2), "low": round(b[3], 2), "close": round(b[4], 2)}
    keys = sorted(vistos)
    return [vistos[k] for k in keys]

def _chart_bloque(bars, n, daily=False):
    cl = [b[4] for b in bars]
    out = {}
    sub = bars[-n:]
    velas = _serie_velas(sub, daily)
    times = [v["time"] for v in velas]
    for k, per in (("ema20", 20), ("ema50", 50), ("ema200", 200)):
        ser = ema_series(cl, per)[-len(sub):]
        # alinear con velas deduplicadas (mismo orden, misma longitud salvo duplicados)
        pts = []
        seen = {}
        for b, v in zip(sub, ser):
            tm = ((datetime.fromtimestamp(b[0], timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")) if daily else b[0] - 6 * 3600
            if v is not None:
                seen[tm] = round(v, 2)
        pts = [{"time": tm, "value": seen[tm]} for tm in times if tm in seen]
        out[k] = pts
    out["velas"] = velas
    return out

def render_dashboard(t, h4, d1, brief, E, noticias, ev, now):
    esc = lambda s: html.escape(str(s), quote=True)
    p = t["precio"]; chg, pct = brief.get("cambio"), brief.get("cambio_pct")
    cls = "up" if (chg or 0) >= 0 else "dn"
    sesgo = brief["sesgo"]
    bcls = "alcista" if sesgo == "alcista" else "bajista" if sesgo == "bajista" else ""
    fuente = f"Datos: Yahoo ({t['simbolo']}), Tesoro EE.UU., Faireconomy, Google News/FXStreet/Fed."
    nota = ("<div class='mut'>" + esc(t["nota_fuente"]) + "</div>") if t["nota_fuente"] else ""
    head = (f"<div class='card'><div class='head'><div><h1>Oro XAU/USD</h1><div class='price'>{p:,.2f}</div>"
            f"<div class='{cls}'>{(chg or 0):+,.2f} ({(pct or 0):+.2f}%) vs cierre previo</div>{nota}"
            f"<div id='vivo' class='vivo' hidden></div></div>"
            f"<div style='text-align:right'><span class='badge {bcls}'>Sesgo {esc(sesgo)} ({brief['puntaje']:+d})</span>"
            f"<div class='mut'>{esc(t['generado_gdl'])} hora Guadalajara</div><div class='mut'>Fuente de precio: {esc(t['simbolo'])}</div></div></div>"
            f"<div class='mut' style='margin-top:8px'>{esc('; '.join(brief['razones']))}</div></div>")
    dxy, real = brief.get("dxy"), brief.get("rendimiento_real_10y")
    def kpi(lbl, val, c=""):
        return f"<div class='kpi'><span>{esc(lbl)}</span><b class='{c}'>{esc(val)}</b></div>"
    kp = "<div class='kpis'>"
    kp += kpi("DXY (5 d)", f"{dxy['valor']:.2f} ({dxy['cambio_pct_5d']:+.2f}%)" if dxy else "n/d", "up" if dxy and dxy["cambio_pct_5d"] >= 0 else "dn")
    kp += kpi("Tasa real 10y (5 obs.)", f"{real['valor']:.2f}% ({real['cambio_pb_5d']:+.0f} pb)" if real else "n/d")
    kp += kpi("RSI diario / 4H", f"{fmt(t['diario']['rsi14'],0)} / {fmt(t['4h']['rsi14'],0)}")
    kp += kpi("ATR 4H", fmt(t["4h"]["atr14"]))
    kp += kpi("Sesion activa", sesion_activa(now))
    kp += "</div>"
    # escenarios
    s = "<div class='card'><h2>Escenarios condicionales</h2>"
    s += f"<div class='mut'>Tendencia {esc(E['tendencia'])} ({esc(E['tendencia_razon'])}). Simulacion: Montecarlo y repeticion historica, {E['escenarios'][0]['simulacion']['dias'] if E['escenarios'] else 5} dias.</div>"
    if E["no_operar"]:
        s += "<div class='warnbox'><b>Condiciones para NO operar</b><ul>" + "".join(f"<li>{esc(x)}</li>" for x in E["no_operar"]) + "</ul></div>"
    s += "<div class='grid'>"
    for e in E["escenarios"]:
        sm = e["simulacion"]; mc = sm["montecarlo"]; h = sm["historico"]
        tags = ""
        if e.get("mejor"):
            tags += "<div class='tag'>Mejor balance segun la simulacion</div>"
        if e["contra_tendencia"]:
            tags += "<div class='tag warn'>Contra tendencia, mas riesgo</div>"
        def bar(m):
            return (f"<div class='bar'><i class='b-t' style='width:{m['objetivo_primero_pct']:.1f}%'></i><i class='b-s' style='width:{m['stop_primero_pct']:.1f}%'></i><i class='b-n' style='width:{m['ninguno_pct']:.1f}%'></i></div>"
                    f"<div class='mut'>objetivo {m['objetivo_primero_pct']:.0f}% | stop {m['stop_primero_pct']:.0f}% | ninguno {m['ninguno_pct']:.0f}% | R {m['r_promedio']:+.2f}</div>")
        lot = ""
        if "lotes" in e:
            l = e["lotes"]
            lot = f"<tr><td>Lotes</td><td>{esc(l['nota']) if l['rechazado'] else format(l['lotes'], '.2f') + ' (perdida en stop $' + fmt(l['perdida_en_stop_usd']) + ')'}</td></tr>"
        s += (f"<div class='esc{' best' if e.get('mejor') else ''}'><h3>[{esc(e['id'])}] {esc(e['nombre'])}</h3>{tags}"
              f"<div class='mut'>{esc(e['disparador'])}</div>"
              f"<table class='k'><tr><td>Entrada</td><td>{fmt(e['entrada'])}</td></tr><tr><td>Stop</td><td>{fmt(e['stop'])}</td></tr>"
              f"<tr><td>Objetivo</td><td>{fmt(e['objetivo'])}</td></tr><tr><td>R</td><td>{e['r_multiplo']:.0f}R</td></tr>"
              f"<tr><td>Riesgo por onza</td><td>${fmt(e['riesgo_usd_oz'])}</td></tr><tr><td>Distancia al precio</td><td>{e['distancia_precio']:+.2f}</td></tr>{lot}</table>"
              f"<div class='mut' style='margin-top:6px'>Montecarlo</div>{bar(mc)}<div class='mut'>Historico ({h['casos']:,} casos)</div>{bar(h)}"
              f"<div class='mut'>{esc(sm['veredicto'])} (R prom. {sm['r_promedio']:+.2f})</div></div>")
    if not E["escenarios"]:
        s += "<div class='mut'>No hay niveles utiles para armar escenarios.</div>"
    s += "</div><div class='mut' style='margin-top:10px'>Son escenarios condicionales, no recomendaciones. La decision es tuya.</div></div>"
    # eventos
    nowg = now.astimezone(GDL); lim = nowg + timedelta(hours=48)
    prox = []
    for x in ev:
        try:
            et = datetime.strptime(x["cuando_gdl"], "%Y-%m-%d %H:%M").replace(tzinfo=GDL)
        except Exception:
            continue
        if nowg - timedelta(minutes=30) <= et <= lim:
            prox.append(x)
    evh = "<div class='card'><h2>Eventos proximos 48 h (USD)</h2><ul class='l'>"
    evh += "".join(f"<li><b>{esc(x['cuando_gdl'])}</b> GDL - {esc(x['evento'])} <span class='chip'>{esc(x.get('impacto',''))}</span>"
                   + (f"<span class='mut'> previo {esc(x.get('previo'))} / pron. {esc(x.get('pronostico'))}</span>" if x.get("previo") or x.get("pronostico") else "") + "</li>" for x in prox)
    if not prox:
        evh += "<li class='mut'>Ninguno en las proximas 48 h.</li>"
    evh += "</ul></div>"
    # noticias
    top = sorted(noticias, key=lambda n: (n["relevancia"], n["utc"]), reverse=True)[:10]
    top.sort(key=lambda n: n["utc"], reverse=True)
    nh = "<div class='card'><h2>Noticias (ultimas 48 h)</h2><ul class='l'>"
    for n in top:
        lk = n.get("link") or ""
        ok = lk.lower().startswith(("http://", "https://"))
        tit = esc(n["titulo"])
        tit = f"<a href='{esc(lk)}' target='_blank' rel='noopener noreferrer'>{tit}</a>" if ok else tit
        nh += f"<li>{tit}<div class='mut'>{esc(n['medio'])} - {esc(n['gdl'])} GDL " + "".join(f"<span class='chip'>{esc(c)}</span>" for c in n["categorias"]) + "</div></li>"
    if not top:
        nh += "<li class='mut'>Sin titulares (fuentes sin respuesta).</li>"
    nh += "</ul></div>"
    # datos de la grafica
    top_e = next((e for e in E["escenarios"] if e.get("mejor")), None)
    n_ = t["niveles"]; lineas = []
    def add(v, tit, tipo="nivel"):
        if v is not None and not any(abs(l["precio"] - v) < 0.005 and l["tipo"] == tipo for l in lineas):
            lineas.append({"precio": round(v, 2), "titulo": tit, "tipo": tipo})
    add(n_["max_dia_previo"], "Max ayer"); add(n_["min_dia_previo"], "Min ayer")
    add(n_["max_semana"], "Max sem"); add(n_["min_semana"], "Min sem")
    add(n_["max_asia"], "Max Asia"); add(n_["min_asia"], "Min Asia")
    r_ = t["redondos"]
    for k in ("50_abajo", "50_arriba", "100_abajo", "100_arriba"):
        add(r_[k], f"${r_[k]:,.0f}")
    if top_e:
        sm = top_e["simulacion"]; m_, h_ = sm["montecarlo"], sm["historico"]
        def rango(k):
            a_, b_ = sorted((m_[k], h_[k]))
            return f"{a_:.0f}%" if round(a_) == round(b_) else f"{a_:.0f}-{b_:.0f}%"
        nom = "rebote" if top_e["direccion"] == "largo" else "corto"
        add(top_e["entrada"], f"Entrada {nom} (si se activa) {top_e['id']}", "entrada")
        add(top_e["stop"], f"Stop {rango('stop_primero_pct')} {top_e['id']}", "stop")
        add(top_e["objetivo"], f"Objetivo {rango('objetivo_primero_pct')} {top_e['id']}", "obj")
    data = {"h4": _chart_bloque(h4, 150), "d": _chart_bloque(d1, 200, True), "lineas": lineas, "atr": t["4h"]["atr14"],
            "generado": datetime.now(GDL).strftime("%Y-%m-%d %H:%M")}
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (DASH_HTML.replace("__HEADER__", head).replace("__KPIS__", kp).replace("__ESC__", s).replace("__EVENTOS__", evh)
            .replace("__NOTICIAS__", nh).replace("__FUENTE__", esc(fuente)).replace("__DATA__", js))
    return page

def cmd_dashboard(a):
    t, (h1, h4, d1) = tecnico_data()
    now = datetime.now(timezone.utc)
    brief, precio, ses, dxy, real, amp, ev, fuente_ev, score, razones, sesgo, chg, pct = briefing_data(t, d1, now)
    save("briefing.json", brief); save("precio.json", precio); save("tecnico.json", t)
    E = escenarios_data(t, h1, h4, d1, ev, now, a.rutas, 5)
    save("escenarios.json", E)
    lista, fallas = noticias_fetch(48)
    save("noticias.json", {"generado_gdl": now.astimezone(GDL).strftime("%Y-%m-%d %H:%M"), "horas": 48, "fallas": fallas, "mostradas": lista[:30], "total_ventana": len(lista)})
    page = render_dashboard(t, h4, d1, brief, E, lista, ev, now)
    OUT.mkdir(parents=True, exist_ok=True)
    path = (OUT / "dashboard.html").resolve()
    path.write_text(page, encoding="utf-8")
    print(f"Dashboard listo ({E['generado_gdl']} hora Guadalajara): {len(E['escenarios'])} escenarios, {len(lista)} titulares" + (f", sin respuesta: {', '.join(fallas)}" if fallas else ""))
    if E["no_operar"]:
        print("Condiciones para NO operar: " + " | ".join(E["no_operar"]))
    print(str(path))
    if a.abrir:
        try:
            os.startfile(str(path))
        except Exception as ex:
            print("No pude abrirlo automaticamente:", ex)

def main():
    global OUT
    from pathlib import Path
    OUT = Path.cwd() / "trader" / "gold"
    ap = argparse.ArgumentParser(description="trader-gold: analisis XAU/USD (no coloca ordenes)")
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("briefing"); sp.add_parser("tecnico")
    p = sp.add_parser("plan")
    p.add_argument("--dir", required=True, help="largo | corto")
    p.add_argument("--entrada", type=float); p.add_argument("--tf", choices=["4h", "1d"], default="4h")
    p.add_argument("--stop-atr", type=float, default=1.5); p.add_argument("--stop-precio", type=float)
    p.add_argument("--r", type=float, default=2.0); p.add_argument("--objetivo", type=float)
    p = sp.add_parser("timemachine")
    p.add_argument("--dias", type=int, default=5); p.add_argument("--barra", choices=["1h", "4h"], default="1h")
    p.add_argument("--rutas", type=int, default=10000); p.add_argument("--semilla", type=int, default=19)
    p = sp.add_parser("tamano")
    p.add_argument("--cuenta", type=float); p.add_argument("--riesgo", type=float); p.add_argument("--tope-dia", type=float)
    p.add_argument("--dist", type=float, help="distancia del stop en USD por onza (si no, usa plan.json)")
    p = sp.add_parser("noticias")
    p.add_argument("--horas", type=int, default=48); p.add_argument("--max", type=int, default=20)
    p.add_argument("--todas", action="store_true", help="muestra todo lo de la ventana, no solo lo nuevo")
    p.add_argument("--no-marcar", action="store_true", help="no actualiza noticias_vistas.json")
    p = sp.add_parser("escenarios"); p.add_argument("--rutas", type=int, default=3000); p.add_argument("--dias", type=int, default=5)
    p = sp.add_parser("dashboard"); p.add_argument("--abrir", action="store_true"); p.add_argument("--rutas", type=int, default=3000)
    a = ap.parse_args()
    {"noticias": cmd_noticias, "briefing": cmd_briefing, "tecnico": cmd_tecnico, "plan": cmd_plan, "timemachine": cmd_timemachine, "tamano": cmd_tamano, "escenarios": cmd_escenarios, "dashboard": cmd_dashboard,
     None: cmd_briefing}[a.cmd](a)

if __name__ == "__main__":
    main()
