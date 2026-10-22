"""
Resumen diario de estadísticas del bot de tenis (paper) → Telegram, a las 9 p. m. de Miami.
Solo lee los CSV del repositorio y el gasto de Apify (consulta gratuita); no opera ni llama actores.
"""
import csv, html, json, os, sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

MIAMI = ZoneInfo("America/New_York")
INICIO_FASE2 = 50          # las primeras 50 operaciones son la fase 1
META = 100
BANCA = 500.0          # banca simulada del paper (BANKROLL)


def leer(archivo):
    try:
        with open(archivo, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def resumen_ops(ops):
    n = len(ops)
    g = sum(num(o["ganancia_usd"]) > 0 for o in ops)
    pl = sum(num(o["ganancia_usd"]) for o in ops)
    inv = sum(num(o["entrada"]) * num(o["contratos"]) for o in ops)
    return n, g, pl, (100 * pl / inv if inv else 0.0)


def usd(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


def linea(titulo, ops):
    n, g, pl, roi = resumen_ops(ops)
    if not n:
        return f"{titulo}: sin operaciones"
    return f"{titulo}: {n} ops · {g} ganadas ({100 * g / n:.0f}%) · {usd(pl)} ({roi:+.1f}%)"


def a_miami(fecha_utc):
    try:
        return datetime.fromisoformat(fecha_utc).astimezone(MIAMI)
    except ValueError:
        return None


def gasto_apify():
    tok = os.getenv("APIFY_TOKEN")
    if not tok:
        return None
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits", params={"token": tok}, timeout=20)
        d = r.json()["data"]
        return d["current"]["monthlyUsageUsd"], d["limits"]["maxMonthlyUsageUsd"]
    except Exception:
        return None


def main():
    ahora = datetime.now(MIAMI)
    if os.getenv("FORZAR") != "1" and ahora.hour != 21:
        print(f"Son las {ahora:%H:%M} en Miami; el resumen sale a las 21 h.")
        return
    sal = leer("senales_salidas.csv")
    hoy = [s for s in sal if (a_miami(s["fecha_utc"]) or ahora).date() == ahora.date()]
    f2 = sal[INICIO_FASE2:]

    lineas = [f"📊 <b>Resumen bot de tenis (paper)</b> · {ahora:%d-%m-%Y}", ""]
    lineas.append("🗓 " + linea("Hoy", hoy))
    for s in hoy:
        lineas.append(f"   • {html.escape(s['jugador'])} {num(s['entrada']) * 100:.0f}¢ → {usd(num(s['ganancia_usd']))}")
    lineas += ["", "📈 " + linea("Acumulado", sal), "   " + linea("Fase 1", sal[:INICIO_FASE2]),
               "   " + linea("Fase 2", f2),
               f"🎯 Faltan {max(0, META - len(sal))} operaciones para las {META}."]

    # Interés compuesto: cada ganancia se suma a la banca y la siguiente apuesta crece en proporción
    if sal:
        banca = BANCA
        for s in sal:
            banca *= 1 + num(s["ganancia_usd"]) / BANCA
        simple = BANCA + sum(num(s["ganancia_usd"]) for s in sal)
        fechas = [d for d in (a_miami(s["fecha_utc"]) for s in sal) if d]
        dias = max(1.0, (fechas[-1] - fechas[0]).total_seconds() / 86400) if fechas else 1.0
        opd = len(sal) / dias
        m = sum(num(s["ganancia_usd"]) for s in sal) / len(sal) / BANCA   # retorno medio por op sobre la banca
        p30, p30m = BANCA * (1 + m) ** (opd * 30), BANCA * (1 + m / 2) ** (opd * 30)
        lineas += ["", f"💰 Con ${BANCA:.0f} e interés compuesto:",
                   f"   • Hoy tendrías ${banca:,.2f} (sin reinvertir: ${simple:,.2f})",
                   f"   • Proyección a 30 días ({opd:.1f} ops/día): ${p30:,.0f} · prudente (mitad de ventaja): ${p30m:,.0f}"]

    # Reglas que bloquean entradas (operaciones seguidas en paper sin tomar)
    ent_s = {e["ticker"]: e.get("resultado", "") for e in leer("sombra_entradas.csv")}
    por_motivo = defaultdict(list)
    for s in leer("sombra_salidas.csv"):
        por_motivo[ent_s.get(s["ticker"], "?")].append(s)
    if por_motivo:
        lineas += ["", "🛡 Bloqueadas por reglas (si se hubieran tomado):"]
        for k in ("edge>10", "vol<300k", "reentrada", "precio>60", "set2"):
            if por_motivo.get(k):
                n, g, pl, _ = resumen_ops(por_motivo[k])
                lineas.append(f"   • {html.escape(k)}: {n} casos · {g} ganadas · {usd(pl)}")

    # Candado de ganancia (simulado)
    cand = [c for c in leer("candado.csv") if c.get("tipo") == "real"]
    if cand:
        lineas += ["", f"🔒 Candado de ganancia (simulado, {len(cand)} ops):"]
        real = sum((num(c["precio_salida"]) - num(c["entrada"])) * num(c["contratos"]) for c in cand)
        for k in ("10_5", "10_0", "15_5", "20_10"):
            tot = 0.0
            for c in cand:
                p = c.get(f"candado_{k}") or c["precio_salida"]
                tot += (num(p) - num(c["entrada"])) * num(c["contratos"])
            lineas.append(f"   • +{k.split('_')[0]}%→+{k.split('_')[1]}%: {usd(tot)} (salida actual {usd(real)})")

    try:
        est = json.load(open("estado_alertas.json"))
        ap = est.get("apify", {})
        lineas += ["", f"💳 Apify hoy: ${num(ap.get('usd')):.2f} ({ap.get('llamadas', 0)} llamadas)"]
    except Exception:
        lineas.append("")
    g = gasto_apify()
    if g:
        lineas.append(f"💳 Apify en el mes: ${g[0]:.2f} de ${g[1]:.0f}")

    txt = "\n".join(lineas)
    print(txt)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        sys.exit("Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": txt, "parse_mode": "HTML"}, timeout=20)
    if not r.json().get("ok"):
        sys.exit(f"Telegram falló: {r.text}")


if __name__ == "__main__":
    main()
