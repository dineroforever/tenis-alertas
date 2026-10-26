"""
Modo dinero real en Kalshi (prueba de 25 operaciones autorizada por Roberto el 25-oct-2026).

REAL_MODO:
  off     -> no hace nada
  ensayo  -> lee el saldo y calcula las órdenes, pero NO las envía (1 día)
  real    -> envía órdenes reales

Reglas autorizadas:
  - apuesta = 5% del saldo real en cada operación (interés compuesto; ≈$2.50 al inicio), una orden por partido;
  - tope de pérdida diaria $7.50 (cuenta también lo que está en juego en posiciones abiertas);
  - se detiene si pierde $15 desde el máximo o acumula 8 operaciones negativas;
  - se detiene al completar 25 operaciones.
Las entradas y salidas siguen exactamente al paper (que sigue corriendo en paralelo).
"""
import base64, csv, math, os, time, uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

MODO = os.getenv("REAL_MODO", "off")
PCT = float(os.getenv("REAL_PCT", "0.05"))  # interés compuesto: 5% del saldo real (≈$2.50 con $49)
APUESTA_MIN = 1.0
TOPE_DIA = float(os.getenv("REAL_TOPE_DIA", "7.50"))
FRENO_DD = float(os.getenv("REAL_FRENO_DD", "15"))
MAX_NEG = int(os.getenv("REAL_MAX_NEG", "8"))
META_OPS = int(os.getenv("REAL_META_OPS", "25"))
DESLIZ = 0.01  # se acepta pagar hasta 1¢ más que el ask del paper (o vender 1¢ bajo el bid)
BASE = "https://api.elections.kalshi.com/trade-api/v2"
LOG = os.getenv("LOG_REAL", "real_ops.csv")
MIAMI = ZoneInfo("America/New_York")
_KEY = None


# ---------- API firmada ----------
def _pem():
    """Normaliza la clave privada aunque se haya pegado en una sola línea o sin encabezados."""
    import re
    raw = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n").replace("\r", "").strip().strip('"')
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", raw, re.S)
    tipo, cuerpo = (m.group(1), m.group(2)) if m else ("PRIVATE KEY", raw)
    cuerpo = re.sub(r"\s+", "", cuerpo)
    lineas = "\n".join(cuerpo[i:i + 64] for i in range(0, len(cuerpo), 64))
    return f"-----BEGIN {tipo}-----\n{lineas}\n-----END {tipo}-----\n"


def diagnostico_clave():
    """Describe el formato del secreto SIN revelar su contenido."""
    raw = os.getenv("KALSHI_PRIVATE_KEY", "")
    kid = os.getenv("KALSHI_KEY_ID", "").strip()
    return (f"clave: {len(raw)} caracteres, {raw.count(chr(10)) + 1} líneas, "
            f"encabezado BEGIN={'sí' if '-----BEGIN' in raw else 'no'}, "
            f"tipo={'RSA' if 'RSA PRIVATE' in raw else ('PKCS8' if 'BEGIN PRIVATE' in raw else ('PUBLICA' if 'PUBLIC' in raw else '?'))}; "
            f"key_id: {len(kid)} caracteres")


def _clave():
    global _KEY
    if _KEY is None:
        from cryptography.hazmat.primitives import serialization
        pem = _pem()
        try:
            _KEY = serialization.load_pem_private_key(pem.encode(), password=None)
        except ValueError:  # sin encabezados: probar el formato RSA (PKCS#1)
            _KEY = serialization.load_pem_private_key(
                pem.replace("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY")
                   .replace("END PRIVATE KEY", "END RSA PRIVATE KEY").encode(), password=None)
    return _KEY


def _api(metodo, ruta, cuerpo=None, params=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = str(int(time.time() * 1000))
    path = "/trade-api/v2" + ruta
    firma = _clave().sign((ts + metodo + path).encode(),
                          padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                          hashes.SHA256())
    h = {"KALSHI-ACCESS-KEY": os.getenv("KALSHI_KEY_ID", "").strip(),
         "KALSHI-ACCESS-TIMESTAMP": ts,
         "KALSHI-ACCESS-SIGNATURE": base64.b64encode(firma).decode(),
         "Content-Type": "application/json"}
    r = requests.request(metodo, BASE + ruta, headers=h, json=cuerpo, params=params, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Kalshi {metodo} {ruta} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def _num(d, *claves):
    for k in claves:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def saldo():
    d = _api("GET", "/portfolio/balance")
    c = _num(d, "balance")
    if c is not None:
        return c / 100
    return _num(d, "balance_dollars")


def posicion(ticker):
    d = _api("GET", "/portfolio/positions", params={"ticker": ticker})
    for p in d.get("market_positions", []) or []:
        if p.get("ticker") == ticker:
            v = _num(p, "position_fp", "position")
            return int(round(v or 0))
    return 0


def _orden(ticker, accion, contratos, precio):
    cuerpo = {"ticker": ticker, "action": accion, "side": "yes", "type": "limit",
              "count": int(contratos), "yes_price": int(round(precio * 100)),
              "time_in_force": "immediate_or_cancel", "client_order_id": str(uuid.uuid4())}
    return _api("POST", "/portfolio/orders", cuerpo).get("order", {})


def apuesta(s):
    """Monto por operación: % del saldo actual (interés compuesto)."""
    return max(APUESTA_MIN, PCT * (s or 0))


def mercado(ticker):
    r = requests.get(f"{BASE}/markets/{ticker}", timeout=15)
    return r.json().get("market", {})


# ---------- estado y registro ----------
def _estado(est):
    r = est.setdefault("real", {})
    for k, v in (("pos", {}), ("ops", 0), ("neg", 0), ("pnl", 0.0), ("max_pnl", 0.0), ("dia", ""),
                 ("pnl_dia", 0.0), ("partidos", []), ("parado", ""), ("ensayo", {})):
        r.setdefault(k, v)
    hoy = datetime.now(MIAMI).date().isoformat()
    if r["dia"] != hoy:
        r["dia"], r["pnl_dia"] = hoy, 0.0
    return r


def _log(evento, ticker="", jugador="", contratos="", p_paper="", p_lim="", dinero="", pnl="", sal="", nota=""):
    nuevo = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if nuevo:
            w.writerow(["fecha_utc", "modo", "evento", "ticker", "jugador", "contratos", "precio_paper",
                        "precio_limite", "dinero_usd", "pnl_usd", "saldo_usd", "nota"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), MODO, evento, ticker, jugador,
                    contratos, p_paper, p_lim, dinero, pnl, sal, nota])


def _cerrar(r, mid, g, aviso, texto):
    """Registra el resultado de una operación real cerrada y aplica los frenos."""
    r["pos"].pop(mid, None)
    r["ops"] += 1
    r["pnl"] = round(r["pnl"] + g, 2)
    r["pnl_dia"] = round(r["pnl_dia"] + g, 2)
    r["max_pnl"] = max(r["max_pnl"], r["pnl"])
    if g < 0:
        r["neg"] += 1
    freno = ""
    if r["ops"] >= META_OPS:
        freno = f"meta de {META_OPS} operaciones completada"
    elif r["pnl"] <= r["max_pnl"] - FRENO_DD:
        freno = f"pérdida de ${FRENO_DD:.0f} desde el máximo"
    elif r["neg"] >= MAX_NEG:
        freno = f"{MAX_NEG} operaciones negativas"
    if freno:
        r["parado"] = freno
    aviso(f"{texto}\nResultado: {'+' if g >= 0 else '−'}${abs(g):.2f} · Prueba: {r['ops']}/{META_OPS} ops, "
          f"{'+' if r['pnl'] >= 0 else '−'}${abs(r['pnl']):.2f}"
          + (f"\n⛔ Bot real DETENIDO: {freno}." if freno else ""))


# ---------- ganchos llamados desde tenis_alertas.py ----------
def entrada(est, mid, pos, aviso):
    """Se llama cuando el paper abre una operación (no sombra)."""
    if MODO not in ("ensayo", "real"):
        return
    try:
        r = _estado(est)
        tk, jug, ask = pos["ticker"], pos["jugador"], float(pos["ask"])
        if MODO == "ensayo":
            try:
                s = saldo()
            except Exception as e:
                s = None
                print("Ensayo: no pude leer el saldo:", repr(e)[:120])
            n = max(1, round(apuesta(s if s else 49.06) / (ask + DESLIZ)))
            r["ensayo"][mid] = dict(ticker=tk, jugador=jug, contratos=n, ask=ask, ts=time.time())
            _log("ensayo_compra", tk, jug, n, ask, round(ask + DESLIZ, 2), round(n * ask, 2), "", s,
                 "orden NO enviada")
            aviso(f"🧪 ENSAYO (no se envió): compraría {n} contratos de {jug} a {ask * 100:.0f}¢ "
                  f"(${n * ask:.2f}). Saldo leído: {f'${s:.2f}' if s is not None else 'ERROR (clave)'}")
            return
        motivo = ""
        expuesto = sum(p["costo"] for p in r["pos"].values())
        if r["parado"]:
            motivo = f"bot detenido ({r['parado']})"
        elif mid in r["partidos"]:
            motivo = "ya hubo una orden en este partido"
        elif r["pnl_dia"] - expuesto - apuesta(saldo()) < -TOPE_DIA - 0.01:
            motivo = f"tope de pérdida diaria (${TOPE_DIA:.2f})"
        elif ask + DESLIZ >= 1:
            motivo = "precio inválido"
        if motivo:
            _log("omitida", tk, jug, "", ask, "", "", "", "", motivo)
            return
        lim = round(ask + DESLIZ, 2)
        s0 = saldo()
        n = max(1, round(apuesta(s0) / lim)) if s0 else 0
        if s0 is None or s0 < n * lim + 0.10:
            _log("omitida", tk, jug, n, ask, lim, "", "", s0, "saldo insuficiente")
            aviso(f"⚠️ Bot real: saldo insuficiente (${s0}) para comprar {jug}.")
            return
        r["partidos"] = (r["partidos"] + [mid])[-300:]
        o = _orden(tk, "buy", n, lim)
        time.sleep(1)
        lleno = posicion(tk)
        s1 = saldo()
        costo = round(s0 - s1, 2)
        if lleno <= 0:
            _log("sin_llenar", tk, jug, n, ask, lim, 0, "", s1, f"estado={o.get('status')}")
            aviso(f"ℹ️ Bot real: la orden de {jug} a {lim * 100:.0f}¢ no se llenó (el precio se movió).")
            return
        r["pos"][mid] = dict(ticker=tk, jugador=jug, contratos=lleno, costo=costo, ask_paper=ask, ts=time.time())
        _log("compra", tk, jug, lleno, ask, lim, costo, "", s1, f"pedidos={n} estado={o.get('status')}")
        aviso(f"💵 REAL: compré {lleno} contratos de {jug} por ${costo:.2f} "
              f"(paper {ask * 100:.0f}¢). Saldo: ${s1:.2f}")
    except Exception as e:
        print("Real entrada error:", repr(e))
        _log("error", pos.get("ticker", ""), pos.get("jugador", ""), nota=repr(e)[:200])
        aviso(f"⚠️ Bot real: error al comprar {pos.get('jugador', '')}: {repr(e)[:150]}")


def salida(est, mid, bid, aviso):
    """Se llama cuando el paper vende por valor justo."""
    if MODO not in ("ensayo", "real"):
        return
    try:
        r = _estado(est)
        if MODO == "ensayo":
            p = r["ensayo"].pop(mid, None)
            if p:
                g = round((bid - p["ask"]) * p["contratos"], 2)
                _log("ensayo_venta", p["ticker"], p["jugador"], p["contratos"], bid, round(bid - DESLIZ, 2),
                     round(bid * p["contratos"], 2), g, "", "orden NO enviada")
                aviso(f"🧪 ENSAYO (no se envió): vendería {p['contratos']} de {p['jugador']} a {bid * 100:.0f}¢ "
                      f"(≈{'+' if g >= 0 else '−'}${abs(g):.2f} antes de comisión)")
            return
        if mid in r["pos"]:
            r["pos"][mid]["vender"] = True
            r["pos"][mid]["bid_paper"] = bid
        mantener(est, aviso)
    except Exception as e:
        print("Real salida error:", repr(e))
        _log("error", nota=repr(e)[:200])


def mantener(est, aviso):
    """Cada ciclo: ventas pendientes y liquidaciones de las posiciones reales (gratis, sin Apify)."""
    if MODO not in ("ensayo", "real"):
        return
    r = _estado(est)
    if MODO == "ensayo":
        for mid, p in list(r["ensayo"].items()):
            try:
                mk = mercado(p["ticker"])
            except Exception:
                continue
            if mk.get("result") in ("yes", "no"):
                pago = 1.0 if mk["result"] == "yes" else 0.0
                g = round((pago - p["ask"]) * p["contratos"], 2)
                _log("ensayo_liquidacion", p["ticker"], p["jugador"], p["contratos"], p["ask"], "",
                     pago * p["contratos"], g, "", "simulado")
                r["ensayo"].pop(mid, None)
            elif time.time() - p["ts"] > 24 * 3600:
                r["ensayo"].pop(mid, None)
        return
    for mid, p in list(r["pos"].items()):
        try:
            tk = p["ticker"]
            mk = mercado(tk)
            if mk.get("result") in ("yes", "no"):
                pago = (1.0 if mk["result"] == "yes" else 0.0) * p["contratos"]
                g = round(pago - p["costo"], 2)
                _log("liquidacion", tk, p["jugador"], p["contratos"], "", "", pago, g, "", mk["result"])
                _cerrar(r, mid, g, aviso, f"🏁 REAL: se liquidó {p['jugador']} ({'ganó' if pago else 'perdió'}).")
                continue
            if not p.get("vender"):
                continue
            bid = _num(mk, "yes_bid_dollars")
            if bid is None:
                c = _num(mk, "yes_bid")
                bid = c / 100 if c is not None else None
            if not bid or bid < 0.02:
                continue
            tiene = posicion(tk)
            if tiene <= 0:  # ya no hay contratos (vendidos o liquidados fuera del bot)
                _log("sin_posicion", tk, p["jugador"], nota="la posición ya no existe en Kalshi")
                _cerrar(r, mid, round(p.get("ingreso", 0) - p["costo"], 2), aviso,
                        f"ℹ️ REAL: {p['jugador']} ya no tiene contratos en Kalshi.")
                continue
            lim = round(max(0.01, bid - DESLIZ), 2)
            s0 = saldo()
            o = _orden(tk, "sell", tiene, lim)
            time.sleep(1)
            queda = posicion(tk)
            s1 = saldo()
            p["ingreso"] = round(p.get("ingreso", 0) + (s1 - s0), 2)
            _log("venta", tk, p["jugador"], tiene - queda, p.get("bid_paper", ""), lim, round(s1 - s0, 2), "", s1,
                 f"quedan={queda} estado={o.get('status')}")
            if queda <= 0:
                g = round(p["ingreso"] - p["costo"], 2)
                _cerrar(r, mid, g, aviso, f"💵 REAL: vendí {p['jugador']} a {lim * 100:.0f}¢+ "
                                          f"(paper {p.get('bid_paper', bid) * 100:.0f}¢). Saldo: ${s1:.2f}")
            else:
                p["contratos"] = queda  # se reintenta en el próximo ciclo
        except Exception as e:
            print("Real mantener error:", repr(e))
            _log("error", p.get("ticker", ""), p.get("jugador", ""), nota=repr(e)[:200])


def comprobar(aviso):
    """Al arrancar: verifica la clave leyendo el saldo (una vez por corrida)."""
    if MODO not in ("ensayo", "real"):
        return
    try:
        s = saldo()
        print(f"Kalshi real ({MODO}): saldo ${s:.2f}")
        return s
    except Exception as e:
        print("Kalshi real: no pude leer el saldo:", repr(e))
        _log("error", nota="saldo: " + repr(e)[:120] + " | " + diagnostico_clave())
        aviso(f"⚠️ Bot real ({MODO}): no pude leer el saldo de Kalshi. Revisa la clave. {repr(e)[:120]}")
