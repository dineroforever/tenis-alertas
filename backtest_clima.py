"""
Backtest de la estrategia de clima FAV-VÍSPERA-MAX con datos históricos públicos de Kalshi.
Solo LEE datos: no envía señales ni toca el bot de clima.

Para cada mercado de temperatura MÁXIMA de ciudades de EE.UU. ya liquidado:
  - mira los precios de todos los rangos a las 17:00 ET de la víspera (velas de 1 hora),
  - toma el rango favorito (mayor punto medio) y aplica las mismas reglas del bot:
    ask 40-60¢ · spread ≤ 2¢ · volumen ≥ 500 · edge > 0 con prob. calibrada
    (ask < 50¢ → 49,6% · ask ≥ 50¢ → 55,0%; coste = ask + 1¢ + 0,07·P·(1−P)),
    una por ciudad y máximo 5 por día (prioriza menor ask y mayor volumen),
  - compara con el resultado real.
Salida: backtest_clima.csv (todos los favoritos) y backtest_clima_resumen.txt.
"""
import csv, os, re, time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
DIAS = int(os.getenv("DIAS", "90"))
US_ICAO_OK = {"SATX", "NOLA", "PHIL"}
MESES = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
S = requests.Session()


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def get(path, **params):
    for intento in range(5):
        r = S.get(f"{KALSHI}{path}", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 + 2 * intento)
            continue
        r.raise_for_status()
        time.sleep(0.08)
        return r.json()
    r.raise_for_status()


def es_eeuu(ticker, titulo):
    suf = re.sub(r"^KXHIGHT?", "", ticker)
    if ticker == "KXHIGHUS" or "Estados Unidos" in titulo:
        return False
    if len(suf) == 4 and suf[0] != "K" and suf not in US_ICAO_OK:
        return False
    return True


def fecha_evento(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not m or m.group(2) not in MESES:
        return None
    return datetime(2000 + int(m.group(1)), MESES[m.group(2)], int(m.group(3))).date()


def coste(ask):
    return ask + 0.01 + 0.07 * ask * (1 - ask)


def vela_vispera(serie, mk, t0):
    """(bid, ask, volumen acumulado) del mercado a las 17:00 ET de la víspera."""
    ini = mk.get("open_time")
    t_ini = int(datetime.fromisoformat(ini.replace("Z", "+00:00")).timestamp()) if ini else t0 - 3 * 86400
    if t_ini >= t0:
        return None
    d = get(f"/series/{serie}/markets/{mk['ticker']}/candlesticks",
            start_ts=t_ini, end_ts=t0, period_interval=60)
    velas = d.get("candlesticks", [])
    if not velas:
        return None
    vol = sum(f(c.get("volume_fp")) or 0 for c in velas)
    ult = velas[-1]
    b = f((ult.get("yes_bid") or {}).get("close_dollars"))
    a = f((ult.get("yes_ask") or {}).get("close_dollars"))
    if a is None or b is None:
        return None
    return b, a, vol


def main():
    hoy = datetime.now(ET).date()
    desde = hoy - timedelta(days=DIAS)
    series = get("/series", category="Climate and Weather").get("series", [])
    series = [s for s in series if s["ticker"].startswith("KXHIGH") and es_eeuu(s["ticker"], s.get("title", ""))]
    print(f"Series de temperatura máxima EE.UU.: {len(series)} · desde {desde}")
    filas = []
    for s in series:
        ciudad = re.sub(r"^Temperatura máxima (diaria )?(en |de )?", "", s.get("title", "")).strip() or s["ticker"]
        cursor, eventos = None, []
        while True:
            p = dict(series_ticker=s["ticker"], status="settled", with_nested_markets="true", limit=200)
            if cursor:
                p["cursor"] = cursor
            try:
                d = get("/events", **p)
            except Exception as e:
                print("error eventos", s["ticker"], repr(e))
                break
            nuevos = d.get("events", [])
            eventos += nuevos
            cursor = d.get("cursor")
            fechas = [fecha_evento(e["event_ticker"]) for e in nuevos]
            if not cursor or not nuevos or all(x and x < desde for x in fechas):
                break
        n_ev = 0
        for ev in eventos:
            fe = fecha_evento(ev["event_ticker"])
            if not fe or fe < desde or fe >= hoy:
                continue
            vispera = datetime(fe.year, fe.month, fe.day, 17, 0, tzinfo=ET) - timedelta(days=1)
            t0 = int(vispera.timestamp())
            cotiz = []
            for mk in ev.get("markets", []):
                if mk.get("result") not in ("yes", "no"):
                    continue
                try:
                    v = vela_vispera(s["ticker"], mk, t0)
                except Exception as e:
                    print("error vela", mk["ticker"], repr(e))
                    v = None
                if v:
                    cotiz.append((mk, *v))
            if not cotiz:
                continue
            mk, bid, ask, vol = max(cotiz, key=lambda x: (x[1] + x[2]) / 2)
            spread = round(ask - bid, 2)
            p = 0.496 if ask < 0.50 else 0.55
            cu = coste(ask)
            pasa = 0.40 <= ask <= 0.60 and spread <= 0.02 and vol >= 500 and p - cu > 0
            gano = mk.get("result") == "yes"
            filas.append(dict(fecha=fe.isoformat(), ciudad=ciudad, serie=s["ticker"], ticker=mk["ticker"],
                              rango=mk.get("yes_sub_title", ""), bid=bid, ask=ask, spread=spread, volumen=round(vol),
                              pasa_filtro="si" if pasa else "no", gano="si" if gano else "no",
                              coste_unit=round(cu, 4), pl_contrato=round((1 - cu) if gano else -cu, 4),
                              senal="no"))
            n_ev += 1
        print(f"{s['ticker']:14s} {ciudad[:28]:28s} eventos={n_ev}")

    # máximo 5 señales por día, una por ciudad, priorizando menor ask y mayor volumen
    por_dia = defaultdict(list)
    for r in filas:
        if r["pasa_filtro"] == "si":
            por_dia[r["fecha"]].append(r)
    for dia, rs in por_dia.items():
        vistas = set()
        for r in sorted(rs, key=lambda r: (r["ask"], -r["volumen"])):
            if r["ciudad"] in vistas or len(vistas) >= 5:
                continue
            vistas.add(r["ciudad"])
            r["senal"] = "si"

    campos = list(filas[0].keys()) if filas else []
    with open("backtest_clima.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        for r in sorted(filas, key=lambda r: (r["fecha"], r["ciudad"])):
            w.writerow(r)

    def resumen(rs, titulo):
        if not rs:
            return f"{titulo}: sin datos"
        n = len(rs)
        g = sum(r["gano"] == "si" for r in rs)
        pl = sum(r["pl_contrato"] for r in rs)
        inv = sum(r["coste_unit"] for r in rs)
        return (f"{titulo}: {n} ops · aciertos {100 * g / n:.1f}% · P/L por contrato {pl:+.2f} "
                f"· ROI {100 * pl / inv:+.1f}%")

    sen = [r for r in filas if r["senal"] == "si"]
    lineas = [f"Backtest FAV-VÍSPERA-MAX · {desde} a {hoy} · {len(filas)} eventos con favorito", "",
              resumen(sen, "SEÑALES (reglas completas)"),
              resumen([r for r in filas if r["pasa_filtro"] == "si"], "Pasan filtro (sin tope diario)"),
              resumen(filas, "Todos los favoritos (sin filtro)"), "", "Por mes (señales):"]
    meses = defaultdict(list)
    for r in sen:
        meses[r["fecha"][:7]].append(r)
    lineas += ["  " + resumen(v, k) for k, v in sorted(meses.items())]
    lineas += ["", "Por precio de entrada (todos los favoritos):"]
    tramos = defaultdict(list)
    for r in filas:
        a = r["ask"]
        k = "<40¢" if a < 0.40 else "40-49¢" if a < 0.50 else "50-60¢" if a <= 0.60 else ">60¢"
        tramos[k].append(r)
    lineas += ["  " + resumen(v, k) for k, v in sorted(tramos.items())]
    lineas += ["", "Por ciudad (señales):"]
    ciu = defaultdict(list)
    for r in sen:
        ciu[r["ciudad"]].append(r)
    lineas += ["  " + resumen(v, k) for k, v in sorted(ciu.items(), key=lambda kv: -len(kv[1]))]
    txt = "\n".join(lineas)
    print("\n" + txt)
    with open("backtest_clima_resumen.txt", "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")


if __name__ == "__main__":
    main()
