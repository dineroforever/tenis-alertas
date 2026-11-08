"""
Variables extra SOLO PARA PAPER (pedido de Roberto, 8-nov-2026): cansancio, estado psicológico y momentum.
No cambian ninguna decisión del bot ni del dinero real: solo se anotan en extras_senales.csv junto a cada
señal (paper y sombra) para medir después, con resultados reales, si mejoran el modelo.

Fuente: API pública de Kalshi (gratis, sin Apify).
  Cansancio
    - pts_partido: puntos jugados en este partido hasta ahora (carga de hoy)
    - prev_n_72h / prev_pts_72h: partidos y puntos jugados por cada jugador en las últimas 72 h
    - horas_desde_prev: horas desde el final de su partido anterior
  Psicología / momentum
    - mom10: puntos ganados de los últimos 10 (jugador − rival)
    - racha_max: máximo de games seguidos ganados (jugador − rival)
    - remontada: 1 si el jugador perdió el 1.er set y ganó el 2.º
    - bp_conv: % de break points convertidos (jugador y rival) — sangre fría en puntos clave
    - df_pct: dobles faltas / puntos de saque (nervios)
    - prev_remonto: 1 si ganó su partido anterior viniendo de un set abajo (confianza)
  Otras
    - cabeza_serie: si el jugador/rival es cabeza de serie
p_ajustada = p_modelo + ajustes pequeños (tope ±5 pp). Los pesos son una hipótesis inicial sin validar:
el objetivo es comparar después p_modelo vs p_ajustada contra los resultados.
"""
import csv, os, time
from datetime import datetime, timezone
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
LOG_EXTRAS = os.getenv("LOG_EXTRAS", "extras_senales.csv")
VENTANA_H = 72
_CACHE = {}  # serie -> (ts, eventos liquidados)
_LD = {}     # event_ticker -> live_data details (partidos terminados no cambian)
CAB = ["fecha_utc", "tipo", "match_id", "ticker", "jugador", "rival", "marcador", "ask", "p_modelo",
       "p_ajustada", "ajuste_pp", "pasaria_ajustado",
       "pts_partido", "j_prev_n_72h", "r_prev_n_72h", "j_prev_pts_72h", "r_prev_pts_72h",
       "j_horas_desde_prev", "r_horas_desde_prev",
       "mom10", "racha_max", "remontada", "j_bp_conv", "r_bp_conv", "j_df_pct", "r_df_pct",
       "j_prev_remonto", "r_prev_remonto", "j_cabeza_serie", "r_cabeza_serie",
       "aj_momentum", "aj_cansancio", "aj_psico"]


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def liquidados(serie):
    """Eventos liquidados de la serie que cerraron en las últimas 72 h (caché 15 min)."""
    c = _CACHE.get(serie)
    if c and time.time() - c[0] < 900:
        return c[1]
    lim = time.time() - VENTANA_H * 3600
    out, cursor = [], None
    for _ in range(5):
        p = {"series_ticker": serie, "status": "settled", "with_nested_markets": "true", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = _get(f"{KALSHI}/events", p)
        evs = d.get("events", [])
        viejo = False
        for e in evs:
            ct = max([_ts(m.get("close_time")) or 0 for m in e.get("markets", [])] or [0])
            if ct < lim:
                viejo = True
                continue
            e["_cierre"] = ct
            out.append(e)
        cursor = d.get("cursor")
        if viejo or not cursor or not evs:
            break
    _CACHE[serie] = (time.time(), out)
    return out


def live_data(event_ticker):
    if event_ticker in _LD:
        return _LD[event_ticker]
    ms = _get(f"{KALSHI}/milestones", {"related_event_ticker": event_ticker, "limit": 5}).get("milestones", [])
    det = None
    if ms:
        d = _get(f"{KALSHI}/live_data/{ms[0]['type']}/milestone/{ms[0]['id']}")
        det = (d.get("live_data") or {}).get("details")
    _LD[event_ticker] = det
    return det


def _lado(det, cid):
    """'competitor1' / 'competitor2' según el id del jugador, o None."""
    if not det or not cid:
        return None
    if det.get("competitor1_id") == cid:
        return "competitor1"
    if det.get("competitor2_id") == cid:
        return "competitor2"
    return None


def _pts(det):
    s1, s2 = det.get("competitor1_statistics") or {}, det.get("competitor2_statistics") or {}
    return (s1.get("points_won") or 0) + (s2.get("points_won") or 0)


def _remonto(det, lado):
    """1 si este lado perdió el 1.er set y terminó/va ganando el 2.º."""
    r = det.get(f"{lado}_round_scores") or []
    o = det.get(("competitor2" if lado == "competitor1" else "competitor1") + "_round_scores") or []
    if len(r) < 2 or len(o) < 2:
        return 0
    perdio1 = (r[0].get("score") or 0) < (o[0].get("score") or 0)
    gano2 = (r[1].get("score") or 0) > (o[1].get("score") or 0)
    return int(perdio1 and gano2)


def previos(cid, serie, ev_actual):
    """(n_partidos, puntos, horas_desde_el_último, remontó_en_el_anterior) en las últimas 72 h."""
    n = pts = 0
    ult, rem = None, ""
    for e in liquidados(serie):
        if e.get("event_ticker") == ev_actual:
            continue
        if not any((m.get("custom_strike") or {}).get("tennis_competitor") == cid for m in e.get("markets", [])):
            continue
        det = live_data(e["event_ticker"])
        n += 1
        if det:
            pts += _pts(det)
        if ult is None or e["_cierre"] > ult[0]:
            lado = _lado(det, cid)
            ult = (e["_cierre"], _remonto(det, lado) if det and lado else "")
    if ult:
        rem = ult[1]
    horas = round((time.time() - ult[0]) / 3600, 1) if ult else ""
    return n, pts, horas, rem


def _pct(a, b):
    return round(a / b, 3) if b else ""


def calcular(s, ev, det):
    """Diccionario de variables extra para la señal s (lado elegido = s['ticker'])."""
    mk = next((m for m in ev.get("markets", []) if m.get("ticker") == s["ticker"]), {})
    otro = next((m for m in ev.get("markets", []) if m.get("ticker") != s["ticker"]), {})
    cj = (mk.get("custom_strike") or {}).get("tennis_competitor")
    cr = (otro.get("custom_strike") or {}).get("tennis_competitor")
    lj, lr = _lado(det, cj), _lado(det, cr)
    x = {"pts_partido": _pts(det) if det else ""}
    if det and lj and lr:
        sj, sr = det.get(f"{lj}_statistics") or {}, det.get(f"{lr}_statistics") or {}
        x["mom10"] = (sj.get("points_won_from_last_10") or 0) - (sr.get("points_won_from_last_10") or 0)
        x["racha_max"] = (sj.get("max_games_in_a_row") or 0) - (sr.get("max_games_in_a_row") or 0)
        x["remontada"] = _remonto(det, lj)
        x["j_bp_conv"] = _pct(sj.get("breakpoints_won") or 0, sj.get("total_breakpoints") or 0)
        x["r_bp_conv"] = _pct(sr.get("breakpoints_won") or 0, sr.get("total_breakpoints") or 0)
        nj = (sj.get("service_points_won") or 0) + (sj.get("service_points_lost") or 0)
        nr = (sr.get("service_points_won") or 0) + (sr.get("service_points_lost") or 0)
        x["j_df_pct"] = _pct(sj.get("double_faults") or 0, nj)
        x["r_df_pct"] = _pct(sr.get("double_faults") or 0, nr)
        x["j_cabeza_serie"] = int(bool(det.get(f"{lj}_seed")))
        x["r_cabeza_serie"] = int(bool(det.get(f"{lr}_seed")))
    serie = ev.get("_serie", "")
    for pref, cid in (("j", cj), ("r", cr)):
        if not cid or not serie:
            continue
        try:
            n, pts, horas, rem = previos(cid, serie, ev.get("event_ticker"))
        except Exception as e:
            print("Extras: no pude leer partidos previos:", repr(e)[:120])
            continue
        x[f"{pref}_prev_n_72h"], x[f"{pref}_prev_pts_72h"] = n, pts
        x[f"{pref}_horas_desde_prev"], x[f"{pref}_prev_remonto"] = horas, rem

    # Ajustes (hipótesis inicial, a validar con resultados)
    aj_mom = max(-0.02, min(0.02, 0.004 * (x.get("mom10") or 0)))
    carga_j = (x.get("j_prev_pts_72h") or 0)
    carga_r = (x.get("r_prev_pts_72h") or 0)
    aj_can = max(-0.02, min(0.02, -0.01 * (carga_j - carga_r) / 60))
    hj, hr = x.get("j_horas_desde_prev"), x.get("r_horas_desde_prev")
    if hj != "" and hj is not None and hj < 20 and (hr in ("", None) or hr >= 20):
        aj_can -= 0.01  # jugó hace menos de 20 h y el rival descansó
    elif hr != "" and hr is not None and hr < 20 and (hj in ("", None) or hj >= 20):
        aj_can += 0.01
    aj_psi = (0.01 * (x.get("remontada") or 0) + 0.005 * (x.get("j_prev_remonto") or 0)
              - 0.005 * (x.get("r_prev_remonto") or 0))
    if (x.get("j_df_pct") or 0) > 0.08:
        aj_psi -= 0.005
    if (x.get("r_df_pct") or 0) > 0.08:
        aj_psi += 0.005
    total = max(-0.05, min(0.05, aj_mom + aj_can + aj_psi))
    p_aj = min(max(s["P"] + total, 0.03), 0.97)
    x.update(aj_momentum=round(aj_mom, 4), aj_cansancio=round(aj_can, 4), aj_psico=round(aj_psi, 4),
             p_ajustada=round(p_aj, 4), ajuste_pp=round(total * 100, 2),
             pasaria_ajustado=int(p_aj - s["ask"] >= 0.05))
    return x


def registrar(s, ev, det, tipo):
    """Anota las variables extra de una señal. Nunca rompe el bot."""
    try:
        x = calcular(s, ev, det)
        base = {"fecha_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tipo": tipo,
                "match_id": s.get("match_id"), "ticker": s.get("ticker"), "jugador": s.get("jugador"),
                "rival": s.get("rival"), "marcador": s.get("sets_txt"), "ask": s.get("ask"),
                "p_modelo": round(s["P"], 4)}
        base.update(x)
        nuevo = not os.path.exists(LOG_EXTRAS)
        with open(LOG_EXTRAS, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CAB, extrasaction="ignore")
            if nuevo:
                w.writeheader()
            w.writerow(base)
    except Exception as e:
        print("Extras: no pude registrar:", repr(e)[:160])
