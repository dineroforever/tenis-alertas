"""Auditoría de solo lectura: ¿las salidas "valor justo" del paper se podían ejecutar en Kalshi?

Para cada salida valor_justo de senales_salidas.csv busca las velas de 1 minuto del bid YES en Kalshi
alrededor de la hora de salida y compara el precio que anotó el paper con el bid real de ese minuto.
No envía órdenes ni usa llaves: solo datos públicos. Escribe auditoria_paper.csv y auditoria_resumen.txt.
"""
import csv, math, time
from datetime import datetime
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def fee_total(p, n):
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 6)) / 100


def velas(tk, t):
    serie = tk.split("-")[0]
    for intento in range(4):
        r = requests.get(f"{KALSHI}/series/{serie}/markets/{tk}/candlesticks",
                         params={"start_ts": t - 180, "end_ts": t + 120, "period_interval": 1}, timeout=20)
        if r.status_code == 429:
            time.sleep(2 * (intento + 1))
            continue
        return r.json().get("candlesticks", []) if r.ok else []
    return []


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    filas = [x for x in csv.DictReader(open("senales_salidas.csv")) if x["motivo"] == "valor_justo"]
    out = []
    for x in filas:
        t = int(datetime.fromisoformat(x["fecha_utc"]).timestamp())
        vs = [v for v in velas(x["ticker"], t) if v.get("yes_bid")]
        cont = next((v for v in vs if v["end_period_ts"] > t), None)  # vela que contiene la salida
        if not cont:
            out.append(dict(x, bid_abre="", bid_max="", bid_min="", bid_cierra="", estado="sin_datos",
                            g_real="", dif=""))
            continue
        yb = cont["yes_bid"]
        abre, mx, mn, cie = f(yb.get("open_dollars")), f(yb.get("high_dollars")), f(yb.get("low_dollars")), f(yb.get("close_dollars"))
        anot, n, e = float(x["precio_salida"]), int(x["contratos"]), float(x["entrada"])
        # precio ejecutable estimado: el bid al inicio del minuto, sin superar lo anotado ni el máximo del minuto
        ejec = min(anot, abre if abre is not None else anot, mx if mx is not None else anot)
        if mx is not None and anot > mx + 0.005:
            estado = "imposible"  # el paper anotó un precio que nunca hubo en ese minuto
        elif abre is not None and anot > abre + 0.005:
            estado = "optimista"
        else:
            estado = "ok"
        g_real = round(n * ejec - fee_total(ejec, n) - n * e, 2)
        out.append(dict(x, bid_abre=abre, bid_max=mx, bid_min=mn, bid_cierra=cie, estado=estado,
                        g_real=g_real, dif=round(g_real - float(x["ganancia_usd"]), 2)))
        time.sleep(0.15)

    campos = list(out[0].keys()) if out else []
    with open("auditoria_paper.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(out)

    todas = list(csv.DictReader(open("senales_salidas.csv")))
    liq = sum(float(r["ganancia_usd"]) for r in todas if r["motivo"] == "liquidacion")
    con = [r for r in out if r["estado"] != "sin_datos"]
    paper_vj = sum(float(r["ganancia_usd"]) for r in con)
    real_vj = sum(r["g_real"] for r in con)
    sin = [r for r in out if r["estado"] == "sin_datos"]
    paper_sin = sum(float(r["ganancia_usd"]) for r in sin)
    cuenta = {k: sum(1 for r in out if r["estado"] == k) for k in ("ok", "optimista", "imposible", "sin_datos")}
    peores = sorted(con, key=lambda r: r["dif"])[:8]
    lin = [
        f"Auditoría paper tenis — {datetime.now():%Y-%m-%d %H:%M} UTC",
        f"Salidas valor_justo: {len(out)}  ->  ok {cuenta['ok']} · optimistas {cuenta['optimista']} · "
        f"imposibles {cuenta['imposible']} · sin datos {cuenta['sin_datos']}",
        f"Valor justo con datos: paper ${paper_vj:.2f}  vs  ejecutable ${real_vj:.2f}  (dif ${real_vj - paper_vj:.2f})",
        f"Valor justo sin datos (se dejan como paper): ${paper_sin:.2f}",
        f"Liquidaciones (no cambian): ${liq:.2f}",
        f"TOTAL paper ${paper_vj + paper_sin + liq:.2f}  ->  TOTAL ajustado ${real_vj + paper_sin + liq:.2f}",
        "Peores diferencias:",
    ] + [f"  {r['fecha_utc'][:16]} {r['jugador']}: anotó {float(r['precio_salida'])*100:.0f}¢, bid real "
         f"{(r['bid_abre'] or 0)*100:.0f}¢ (máx {(r['bid_max'] or 0)*100:.0f}¢) -> {r['dif']:+.2f}" for r in peores]
    open("auditoria_resumen.txt", "w").write("\n".join(lin) + "\n")
    print("\n".join(lin))


if __name__ == "__main__":
    main()
