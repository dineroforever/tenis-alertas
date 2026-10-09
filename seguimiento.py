"""
Seguimiento de partidos donde entramos -> Telegram.

Fuente del marcador: API pública de Kalshi (live_data), la misma que Kalshi usa
para liquidar. No necesita llave ni gasta Apify.

Qué sigue:
  1) Toda alerta de compra del bot (se agrega sola).
  2) Lo que tú agregues por Telegram:
       /seguir <link o ticker de Kalshi> <jugador> [precio¢] [contratos]
         ej: /seguir https://kalshi.com/markets/.../kxatpchallengermatch-26oct09llamar Martinez 75 20
       /dejar <jugador>     deja de seguir esa posición
       /estado              marcador y P&L de todo lo que sigues, ya
       /ayuda               lista de comandos

Cuándo escribe:
  - al empezar a seguir (confirmación con marcador)
  - ⚡ quiebre de saque
  - 🏁 fin de set
  - ✅/❌ fin del partido (liquidación) con ganancia o pérdida
  La señal 🔴 VENDER por valor justo la sigue enviando tenis_alertas.py.
"""
import re, time
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
MAX_POS = 15


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _get(path, **params):
    r = requests.get(f"{KALSHI}{path}", params=params or None, timeout=15)
    r.raise_for_status()
    return r.json()


def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).strip()


# ───────────── Kalshi ─────────────
def ticker_evento(texto):
    """Acepta link o ticker; devuelve el ticker del evento en mayúsculas."""
    t = texto.strip().rstrip("/").split("?")[0].split("/")[-1].upper()
    if not re.match(r"^KX[A-Z0-9]+-[A-Z0-9]+", t):
        return None
    partes = t.split("-")
    return "-".join(partes[:2])


def mercado_de(evento, jugador):
    """Busca en el evento el mercado del jugador. Devuelve (mercado, rival_nombre)."""
    ev = _get(f"/events/{evento}", with_nested_markets="true")
    mks = ev.get("markets") or ev.get("event", {}).get("markets") or []
    j = _norm(jugador)
    elegido = None
    for mk in mks:
        nom = _norm(mk.get("yes_sub_title", ""))
        if j and (j in nom or nom.endswith(j) or j.split()[-1] in nom.split()):
            elegido = mk
    if not elegido:
        return None, None
    rival = next((mk.get("yes_sub_title", "") for mk in mks if mk is not elegido), "")
    return elegido, rival


def mercado(ticker):
    return _get(f"/markets/{ticker}").get("market", {})


def milestone_id(evento):
    d = _get("/milestones", related_event_ticker=evento, limit=5)
    for m in d.get("milestones", []):
        return m.get("id"), m.get("type")
    return None, None


def marcador_kalshi(mid, mtype):
    d = _get(f"/live_data/{mtype}/milestone/{mid}").get("live_data", {}).get("details", {})
    return d


def leer_score(det, nombre_jugador, c1_nombre_hint=None):
    """
    Convierte live_data de Kalshi a un dict simple visto desde NUESTRO jugador.
    Kalshi no da nombres en details; usamos el orden del título ('A vs B'):
    competitor1 = primero del título.
    """
    r1 = det.get("competitor1_round_scores") or []
    r2 = det.get("competitor2_round_scores") or []
    sets = [(a.get("score", 0), b.get("score", 0)) for a, b in zip(r1, r2)]
    return dict(
        sets=sets,
        pts=(det.get("competitor1_current_round_score"), det.get("competitor2_current_round_score")),
        server_c1=det.get("server") == det.get("competitor1_id") if det.get("server") else None,
        status=det.get("status", ""),
        winner_c1=(det.get("winner") == det.get("competitor1_id")) if det.get("winner") else None,
        sets_c1=det.get("competitor1_overall_score", 0),
        sets_c2=det.get("competitor2_overall_score", 0),
    )


# ───────────── texto ─────────────
def txt_marcador(sc, somos_c1):
    sets = [(a, b) if somos_c1 else (b, a) for a, b in sc["sets"]]
    if len(sets) > 1 and sets[-1] == (0, 0):
        sets = sets[:-1]  # set recién empezado: no mostrar "0-0"
    t = " ".join(f"{a}-{b}" for a, b in sets) or "0-0"
    p1, p2 = sc["pts"]
    if somos_c1 is False:
        p1, p2 = p2, p1
    if p1 not in (None, 0, "0") or p2 not in (None, 0, "0"):
        t += f" ({p1}-{p2})"
    return t


def txt_pos(pos, mk):
    bid = _f(mk.get("yes_bid_dollars"))
    ask = _f(mk.get("yes_ask_dollars"))
    linea = f"Kalshi {pos['jugador']}: bid {bid*100:.0f}¢ / ask {ask*100:.0f}¢" if bid is not None and ask is not None else "Kalshi: sin precio"
    if pos.get("entrada") and bid is not None:
        e = pos["entrada"]
        c = pos.get("contratos") or 0
        g = (bid - e) * c
        linea += f"\nTu entrada {e*100:.0f}¢ → ahora {((bid/e)-1)*100:+.0f}%"
        if c:
            linea += f" (${g:+.2f} con {c} contratos)"
    return linea


def cabecera(pos):
    return f"{pos['jugador']} vs {pos['rival']}"


# ───────────── Telegram (comandos) ─────────────
def leer_comandos(tg_token, tg_chat, est):
    """Lee mensajes nuevos del chat autorizado. Devuelve lista de textos."""
    if not tg_token:
        return []
    off = est.get("tg_offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{tg_token}/getUpdates",
                         params={"offset": off, "timeout": 0}, timeout=15).json()
    except Exception as e:
        print("getUpdates fallo:", repr(e))
        return []
    out = []
    for u in r.get("result", []):
        est["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message") or {}
        if str(msg.get("chat", {}).get("id")) != str(tg_chat):
            continue
        t = (msg.get("text") or "").strip()
        if t.startswith("/"):
            out.append(t)
    return out


AYUDA = ("📋 <b>Comandos de seguimiento</b>\n"
         "/seguir &lt;link Kalshi&gt; &lt;jugador&gt; [precio¢] [contratos]\n"
         "   ej: /seguir https://kalshi.com/markets/.../kxatpchallengermatch-26oct09llamar Martinez 75 20\n"
         "/dejar &lt;jugador&gt;\n/estado\n/ayuda")


def agregar(est, evento, jugador, entrada=None, contratos=None, origen="manual", ticker=None):
    seg = est.setdefault("seguir", {})
    if len(seg) >= MAX_POS:
        return None, "Ya sigo 15 posiciones; usa /dejar para liberar una."
    mk = rival = None
    if ticker:
        ev = _get(f"/events/{evento}", with_nested_markets="true")
        mks = ev.get("markets") or ev.get("event", {}).get("markets") or []
        for m in mks:
            if m.get("ticker") == ticker:
                mk = m
            else:
                rival = m.get("yes_sub_title", "")
    else:
        mk, rival = mercado_de(evento, jugador)
    if not mk:
        return None, f"No encontré a «{jugador}» en {evento}."
    mid, mtype = milestone_id(evento)
    titulo = _get(f"/events/{evento}").get("event", {}).get("title", "")
    primero = titulo.split(" vs ")[0].strip() if " vs " in titulo else ""
    nombre = mk.get("yes_sub_title", jugador)
    somos_c1 = bool(primero) and _norm(primero).split()[-1] in _norm(nombre).split()
    pos = dict(ticker=mk["ticker"], evento=evento, jugador=nombre, rival=rival or "?",
               entrada=entrada, contratos=contratos, origen=origen, mid=mid, mtype=mtype,
               somos_c1=somos_c1, ult=None, ts=time.time())
    seg[mk["ticker"]] = pos
    return pos, None


def procesar_comandos(cmds, est, enviar):
    for c in cmds:
        partes = c.split()
        cmd = partes[0].lower().split("@")[0]
        try:
            if cmd in ("/ayuda", "/start", "/help"):
                enviar(AYUDA)
            elif cmd == "/estado":
                enviar(resumen(est))
            elif cmd == "/dejar" and len(partes) >= 2:
                j = _norm(" ".join(partes[1:]))
                seg = est.setdefault("seguir", {})
                quitar = [k for k, p in seg.items() if j in _norm(p["jugador"])]
                for k in quitar:
                    del seg[k]
                enviar(f"🗑 Dejé de seguir {len(quitar)} posición(es) de «{' '.join(partes[1:])}».")
            elif cmd == "/seguir" and len(partes) >= 3:
                evento = ticker_evento(partes[1])
                if not evento:
                    enviar("No reconocí el link/ticker de Kalshi. Escribe /ayuda.")
                    continue
                nums = [p for p in partes[2:] if re.match(r"^\d+(\.\d+)?$", p)]
                nombre = " ".join(p for p in partes[2:] if p not in nums)
                entrada = contratos = None
                if nums:
                    v = float(nums[0])
                    entrada = v / 100 if v > 1 else v
                if len(nums) > 1:
                    contratos = int(float(nums[1]))
                pos, err = agregar(est, evento, nombre, entrada, contratos)
                if err:
                    enviar("⚠️ " + err)
                else:
                    enviar("👀 <b>Siguiendo</b> " + estado_pos(pos, primera=True))
            else:
                enviar("No entendí el comando. " + AYUDA)
        except Exception as e:
            print("Comando fallo:", c, repr(e))
            enviar(f"⚠️ No pude procesar «{c}» ({type(e).__name__}).")


# ───────────── seguimiento ─────────────
def estado_pos(pos, primera=False):
    mk = mercado(pos["ticker"])
    sc = None
    if pos.get("mid"):
        try:
            sc = leer_score(marcador_kalshi(pos["mid"], pos["mtype"]), pos["jugador"])
        except Exception:
            sc = None
    t = f"<b>{cabecera(pos)}</b>\n"
    if sc and sc["sets"]:
        saca = ""
        if sc["server_c1"] is not None:
            saca_nos = sc["server_c1"] == pos["somos_c1"]
            saca = f" · saca {pos['jugador'] if saca_nos else pos['rival']}"
        t += f"Marcador ({pos['jugador']} primero): <b>{txt_marcador(sc, pos['somos_c1'])}</b>{saca}\n"
    else:
        t += "Marcador: aún no empieza o sin dato\n"
    t += txt_pos(pos, mk)
    if primera:
        pos["ult"] = sc
    return t


def resumen(est):
    seg = est.get("seguir", {})
    if not seg:
        return "No estoy siguiendo ninguna posición. Usa /seguir (ver /ayuda)."
    partes = []
    for pos in list(seg.values()):
        try:
            partes.append(estado_pos(pos))
        except Exception as e:
            partes.append(f"<b>{cabecera(pos)}</b>\nsin datos ({type(e).__name__})")
    return "📊 <b>Tus posiciones</b>\n\n" + "\n\n".join(partes)


def eventos_nuevos(prev, sc):
    """Compara dos marcadores (vistos desde competitor1). Devuelve lista de eventos."""
    out = []
    if not prev or not sc or not sc["sets"]:
        return out
    ps, ns = prev["sets"], sc["sets"]
    # fin de set
    if sc["sets_c1"] + sc["sets_c2"] > prev["sets_c1"] + prev["sets_c2"]:
        out.append(("set", sc["sets_c1"] > prev["sets_c1"]))
        return out
    if len(ps) != len(ns) or not ns:
        return out
    a0, b0 = ps[-1]
    a1, b1 = ns[-1]
    if (a1 + b1) == (a0 + b0) + 1 and prev.get("server_c1") is not None and not (a0 == 6 and b0 == 6):
        gano_c1 = a1 > a0
        if gano_c1 != prev["server_c1"]:
            out.append(("quiebre", gano_c1))
    return out


def ciclo_seguimiento(est, enviar, log_salida=None):
    seg = est.setdefault("seguir", {})
    for tk, pos in list(seg.items()):
        try:
            mk = mercado(tk)
        except Exception as e:
            print("seguimiento mercado fallo", tk, repr(e))
            continue
        res = mk.get("result")
        if res in ("yes", "no") or mk.get("status") in ("settled", "finalized"):
            gano = res == "yes"
            t = ("✅ <b>GANÓ " if gano else "❌ <b>PERDIÓ ") + pos["jugador"].upper() + "</b>\n"
            t += f"{cabecera(pos)}\n"
            if pos.get("ult") and pos["ult"].get("sets"):
                t += f"Final ({pos['jugador']} primero): {txt_marcador(pos['ult'], pos['somos_c1'])}\n"
            if pos.get("entrada"):
                pago = 1.0 if gano else 0.0
                c = pos.get("contratos") or 0
                t += f"Entrada {pos['entrada']*100:.0f}¢ → liquida {pago*100:.0f}¢ ({((pago/pos['entrada'])-1)*100:+.0f}%"
                t += f", ${(pago-pos['entrada'])*c:+.2f})" if c else ")"
            enviar(t)
            del seg[tk]
            continue
        if time.time() - pos.get("ts", 0) > 14 * 3600:
            del seg[tk]
            continue
        if not pos.get("mid"):
            try:
                pos["mid"], pos["mtype"] = milestone_id(pos["evento"])
            except Exception:
                pass
            if not pos.get("mid"):
                continue
        try:
            sc = leer_score(marcador_kalshi(pos["mid"], pos["mtype"]), pos["jugador"])
        except Exception as e:
            print("seguimiento live_data fallo", tk, repr(e))
            continue
        prev = pos.get("ult")
        for tipo, a_favor_c1 in eventos_nuevos(prev, sc):
            nuestro = a_favor_c1 == pos["somos_c1"]
            quien = pos["jugador"] if nuestro else pos["rival"]
            if tipo == "quiebre":
                titulo = f"⚡ QUIEBRE de {quien}" + (" 👍" if nuestro else " 👎")
            else:
                titulo = f"🏁 SET para {quien}" + (" 👍" if nuestro else " 👎")
            saca = ""
            if sc["server_c1"] is not None:
                saca = f" · saca {pos['jugador'] if sc['server_c1'] == pos['somos_c1'] else pos['rival']}"
            enviar(f"{titulo}\n<b>{cabecera(pos)}</b>\n"
                   f"Marcador ({pos['jugador']} primero): <b>{txt_marcador(sc, pos['somos_c1'])}</b>{saca}\n"
                   f"{txt_pos(pos, mk)}")
        if sc and sc["sets"]:
            pos["ult"] = sc
