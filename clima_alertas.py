"""
Señales de clima Kalshi -> Telegram (PAPER TRADING).
Estrategia "FAV-VÍSPERA-MAX" (la misma de la tarea programada de Claude):
en la víspera (~17:10 ET), por cada mercado de temperatura MÁXIMA de ciudades de EE.UU.
con evento MAÑANA, toma el rango favorito y da señal YES solo si:
  ask 40-60¢ · spread ≤ 2¢ · volumen ≥ 500 · edge neto > 0 con prob. calibrada
  (ask 40-49¢ → 49,6% · ask 50-60¢ → 55,0%; coste = ask + 1¢ + 0,07·P·(1−P)).
Clase A = ask ≤ 49¢, spread ≤ 1¢, volumen ≥ 1.000. Clase B = resto. Máx. 5/día, una por ciudad.
También liquida las señales pendientes y lleva la estadística real en clima_senales.csv.
"""
import csv, math, os, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
FORZAR = os.getenv("FORZAR", "0") == "1"          # correr aunque no sean las 17 h ET
LOG = os.getenv("LOG_CLIMA", "clima_senales.csv")
BANK, RIESGO = 1000.0, 0.01
MAX_DIA = 5
US_ICAO_OK = {"SATX", "NOLA", "PHIL"}            # sufijos de 4 letras que sí son de EE.UU.
CAMPOS = ["id", "fecha_senal", "fecha_evento", "ciudad", "ticker", "rango", "bid", "ask", "spread",
          "volumen", "prob_est", "edge", "clase", "contratos", "coste_unit", "estado", "temp_real", "pl"]
MESES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def get(url, **params):
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def es_eeuu(ticker, titulo):
    suf = re.sub(r"^KXHIGHT?", "", ticker)
    if ticker == "KXHIGHUS" or "Estados Unidos" in titulo:
        return False
    if len(suf) == 4 and suf[0] != "K" and suf not in US_ICAO_OK:
        return False  # código de aeropuerto extranjero (LFPG, EGLL, RKSI…)
    return True


def ciudad_de(titulo):
    return re.sub(r"^Temperatura máxima (diaria )?(en |de )?", "", titulo).strip()


def coste(ask):
    return ask + 0.01 + 0.07 * ask * (1 - ask)


def telegram(txt):
    if DRY_RUN or not TG_TOKEN:
        print("---- [DRY RUN] ----\n" + txt)
        return True
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for modo in ("HTML", None):
        body = {"chat_id": TG_CHAT, "text": txt if modo else re.sub(r"</?[bi]>", "", txt),
                "disable_web_page_preview": True}
        if modo:
            body["parse_mode"] = modo
        try:
            r = requests.post(url, json=body, timeout=15)
            if r.ok:
                print("Telegram OK")
                return True
            print("Telegram FALLO:", r.status_code, r.text[:300])
        except Exception as e:
            print("Telegram FALLO (red):", repr(e))
    return False


# ───────── registro ─────────
def leer():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def escribir(filas):
    with open(LOG, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS)
        w.writeheader()
        for r in filas:
            w.writerow({k: r.get(k, "") for k in CAMPOS})


def liquidar(filas, hoy):
    liquidadas = []
    for r in filas:
        if r["estado"] != "pendiente" or r["fecha_evento"] >= hoy.isoformat():
            continue
        try:
            m = get(f"{KALSHI}/markets/{r['ticker']}").get("market", {})
        except Exception as e:
            print("No pude liquidar", r["ticker"], repr(e))
            continue
        res = m.get("result")
        if res not in ("yes", "no"):
            continue
        n, cu = int(r["contratos"]), float(r["coste_unit"])
        r["estado"] = "gano" if res == "yes" else "perdio"
        r["pl"] = f"{(n * (1 - cu) if res == 'yes' else -n * cu):.2f}"
        r["temp_real"] = str(m.get("expiration_value") or "")
        liquidadas.append(r)
    return liquidadas


def acumulado(filas):
    liq = [r for r in filas if r["estado"] in ("gano", "perdio")]
    if not liq:
        return "Real (paper): aún sin señales liquidadas"
    pl = sum(float(r["pl"]) for r in liq)
    inv = sum(int(r["contratos"]) * float(r["coste_unit"]) for r in liq)
    wr = 100 * sum(r["estado"] == "gano" for r in liq) / len(liq)
    return f"Real (paper): {len(liq)} liquidadas · WR {wr:.0f}% · P/L ${pl:+.2f} · ROI {100 * pl / inv:+.1f}%"


# ───────── escaneo ─────────
def escanear(manana):
    sufijo = f"{manana:%y}{MESES[manana.month - 1]}{manana:%d}"
    series = get(f"{KALSHI}/series", category="Climate and Weather").get("series", [])
    series = [s for s in series if s["ticker"].startswith("KXHIGH") and es_eeuu(s["ticker"], s.get("title", ""))]
    cands = []
    for s in series:
        try:
            evs = get(f"{KALSHI}/events", series_ticker=s["ticker"], status="open",
                      with_nested_markets="true").get("events", [])
        except Exception as e:
            print("Serie con error", s["ticker"], repr(e))
            continue
        for ev in evs:
            if not ev["event_ticker"].endswith(sufijo):
                continue
            mks = [m for m in ev.get("markets", []) if f(m.get("yes_ask_dollars")) and f(m.get("yes_bid_dollars")) is not None]
            if not mks:
                continue
            fav = max(mks, key=lambda m: (f(m["yes_ask_dollars"]) + f(m["yes_bid_dollars"])) / 2)
            ask, bid, vol = f(fav["yes_ask_dollars"]), f(fav["yes_bid_dollars"]), f(fav.get("volume_fp")) or 0
            spread = round(ask - bid, 2)
            if not (0.40 <= ask <= 0.60) or spread > 0.02 or vol < 500:
                continue
            p = 0.496 if ask < 0.50 else 0.55
            cu = coste(ask)
            edge = p - cu
            if edge <= 0:
                continue
            clase = "A" if ask <= 0.49 and spread <= 0.01 and vol >= 1000 else "B"
            cierre = datetime.fromisoformat(fav["close_time"].replace("Z", "+00:00"))
            cands.append(dict(ciudad=ciudad_de(s.get("title", "")), ticker=fav["ticker"], serie=s["ticker"],
                              evento=ev["event_ticker"], rango=fav.get("yes_sub_title", ""), ask=ask, bid=bid,
                              spread=spread, vol=vol, p=p, cu=cu, edge=edge, clase=clase,
                              horas=(cierre - datetime.now(cierre.tzinfo)).total_seconds() / 3600))
    # una por ciudad, priorizando menor ask y mayor volumen
    cands.sort(key=lambda c: (c["ask"], -c["vol"]))
    vistas, out = set(), []
    for c in cands:
        if c["ciudad"] in vistas:
            continue
        vistas.add(c["ciudad"])
        out.append(c)
        if len(out) == MAX_DIA:
            break
    return out


def mensaje(c, manana, acum, ident):
    n = int(BANK * RIESGO // c["cu"])
    link = f"https://kalshi.com/markets/{c['serie'].lower()}/m/{c['evento'].lower()}"
    return (
        f"🟢 <b>SEÑAL CLIMA · {c['ciudad'].upper()}</b> · Clase {c['clase']}\n"
        f"👉 En Kalshi compra <b>SÍ (Yes) en «{c['rango']}»</b>\n"
        f"Evento: {manana:%d/%m/%Y} · cierra en {c['horas']:.0f} h\n\n"
        f"Precio: bid {c['bid']*100:.0f}¢ / ask {c['ask']*100:.0f}¢ · Prob. implícita {c['ask']*100:.0f}%\n"
        f"Prob. estimada (calibrada): {c['p']*100:.1f}%\n"
        f"EDGE neto: <b>+{c['edge']*100:.1f} pts</b>\n"
        f"Volumen: {c['vol']:,.0f} contratos · Spread {c['spread']*100:.0f}¢\n\n"
        f"Histórico backtest: 312 ops · WR 55,8% · ROI +8,3%\n"
        f"{acum}\n"
        f"Tamaño: ${n * c['cu']:.2f} ({n} contratos) · Riesgo 1%\n"
        f"{link}\n"
        f"ID: {ident}\n"
        f"⚠️ <b>PAPER TRADE — NO EJECUTAR</b>"
    ), n


def main():
    ahora = datetime.now(ET)
    if not FORZAR and ahora.hour != 17:
        print(f"Son las {ahora:%H:%M} ET; esta pasada solo corre a las 17 h ET. Nada que hacer.")
        return
    hoy, manana = ahora.date(), (ahora + timedelta(days=1)).date()
    filas = leer()
    liq = liquidar(filas, hoy)
    ya = {r["fecha_evento"] + r["ciudad"] for r in filas}
    senales = [c for c in escanear(manana) if manana.isoformat() + c["ciudad"] not in ya]
    acum = acumulado(filas)
    for c in senales:
        ident = f"CLIMA-{manana:%Y%m%d}-{re.sub(r'[^A-Z]', '', c['ciudad'].upper())[:6]}"
        txt, n = mensaje(c, manana, acum, ident)
        telegram(txt)
        filas.append(dict(id=ident, fecha_senal=ahora.isoformat(timespec="minutes"), fecha_evento=manana.isoformat(),
                          ciudad=c["ciudad"], ticker=c["ticker"], rango=c["rango"], bid=round(c["bid"] * 100),
                          ask=round(c["ask"] * 100), spread=round(c["spread"] * 100), volumen=round(c["vol"]),
                          prob_est=c["p"] * 100, edge=round(c["edge"] * 100, 1), clase=c["clase"], contratos=n,
                          coste_unit=f"{c['cu']:.4f}", estado="pendiente", temp_real="", pl=""))
    if senales and liq:
        lineas = "\n".join(f"{r['id']} · {'GANÓ ✅' if r['estado'] == 'gano' else 'PERDIÓ ❌'} · "
                           f"temp. real {r['temp_real']} · P/L ${float(r['pl']):+.2f}" for r in liq)
        telegram(f"📋 <b>Resultados liquidados (clima, paper)</b>\n{lineas}\n\n{acumulado(filas)}")
    escribir(filas)
    print(f"{ahora:%Y-%m-%d %H:%M} ET · señales={len(senales)} · liquidadas={len(liq)}")


if __name__ == "__main__":
    main()
