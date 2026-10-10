"""
Bot de alertas de tenis en vivo -> Telegram (Kalshi).
Envía mensaje SOLO cuando un partido pasa el filtro completo del skill
tennis-live-predictor-pro (versión "casa blanda + modelo").

Fuentes:
  - API pública de Kalshi: precios + marcador y estadísticas de saque en vivo (gratis)
  - Apify crawlstone/tennis-scraper (liveMatches = cuotas de la casa) SOLO cuando Kalshi
    muestra un partido candidato o hay posiciones que vigilar (modo ahorro, tope diario)
  - API pública de Kalshi (precios, volumen, profundidad) — no necesita llave
Variables de entorno (Secrets de GitHub):
  APIFY_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Opcionales: BANKROLL (500), LOOP_MINUTES (28), POLL_SECONDS (120), DRY_RUN (0)
"""
import csv, json, math, os, re, sys, time, unicodedata
from datetime import datetime, timezone
import requests

from tenis_prob import prob
import seguimiento as segui

# ───────────── Parámetros del filtro (skill PRO) ─────────────
MIN_VOL_CONTRATOS = float(os.getenv("MIN_VOL_CONTRATOS", "300000"))  # ≈ $100K en la web de Kalshi
PRECIO_MIN, PRECIO_MAX = 0.20, 0.80
SPREAD_MAX = 0.03
EDGE_MODELO_MIN = 0.05      # P_final − ask
EDGE_CASA_MIN = 0.03        # P_book − ask
EDGE_MODELO_MAX = 0.10      # edge mayor = probable error del modelo (no entrar)
CONTRADICCION_MAX = 0.10    # |P_score − P_book|
MAX_ENTRADAS = 1            # sin reentradas: las 2 reentradas registradas perdieron (−$12.05)
W_BOOK, W_SCORE = 0.40, 0.60  # casa blanda
ANCLA = 80
BANKROLL = float(os.getenv("BANKROLL", "500"))
KELLY_FRAC, TOPE_OP = 0.25, 0.02
FEE_TAKER = 0.07

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXCHALLENGERMATCH",
          "KXWTACHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH"]
FEMENINO = {"KXWTAMATCH", "KXWTACHALLENGERMATCH", "KXITFWMATCH"}
APIFY_ACTOR = "crawlstone~tennis-scraper"

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
# Modo ahorro: Apify cobra ~$0.00736 por llamada. Primero se filtra con Kalshi (gratis)
# y solo se llama a Apify cuando hay un partido candidato o una posición que vigilar.
APIFY_COSTO = float(os.getenv("APIFY_COSTO", "0.00736"))
APIFY_TOPE_DIA = float(os.getenv("APIFY_TOPE_DIA", "0.60"))   # ≈ $18/mes
APIFY_SEG_CADA = float(os.getenv("APIFY_SEG_CADA", "240"))    # revisar posiciones cada 4 min
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
LOG_CSV = os.getenv("LOG_CSV", "senales_tenis.csv")
LOG_SALIDAS = os.getenv("LOG_SALIDAS", "senales_salidas.csv")
LOG_SOMBRA = os.getenv("LOG_SOMBRA", "sombra_entradas.csv")          # señales bloqueadas por las reglas nuevas
LOG_SOMBRA_SAL = os.getenv("LOG_SOMBRA_SAL", "sombra_salidas.csv")  # (seguidas en paper, sin avisar)
LOG_TRAY = os.getenv("LOG_TRAY", "trayectoria_precios.csv")  # máx/mín del bid de cada operación (solo anotación)
LOG_CAND = os.getenv("LOG_CAND", "candidatos.csv")  # toda evaluación del modelo (para calibrarlo)
STATE = os.getenv("STATE_FILE", "estado_alertas.json")

PTS = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4, "AD": 4}


# ───────────── utilidades ─────────────
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).strip()


def apellido_match(full_name, kalshi_name):
    """kalshi usa apellidos ('Sanchez Jover'); apify nombre completo."""
    a, k = norm(full_name), norm(kalshi_name)
    return bool(k) and (a.endswith(k) or k in a.split() or k == a)


def frac_to_dec(f):
    if not f or "/" not in f:
        return None
    n, d = f.split("/")
    return 1 + float(n) / float(d)


def no_vig(dh, da):
    if not dh or not da or dh <= 1 or da <= 1:
        return None
    ph, pa = 1 / dh, 1 / da
    return ph / (ph + pa)


TABLA0 = [(0.50, 0), (0.55, 1), (0.60, 2), (0.65, 3), (0.69, 4), (0.74, 5),
          (0.78, 6), (0.85, 8), (0.90, 10), (0.97, 14)]


def brecha_desde_cuota(p):
    """Tabla 0 (mejor de 3): prob pre-partido -> brecha SPW en pp (signo para el local)."""
    s = 1 if p >= 0.5 else -1
    q = p if p >= 0.5 else 1 - p
    for (p0, g0), (p1, g1) in zip(TABLA0, TABLA0[1:]):
        if q <= p1:
            return s * (g0 + (g1 - g0) * (q - p0) / (p1 - p0))
    return s * 14


def fee(precio, contratos, rate=FEE_TAKER):
    return math.ceil(rate * contratos * precio * (1 - precio) * 100) / 100


# ───────────── datos ─────────────
class SinPresupuesto(Exception):
    """Se alcanzó el tope diario de gasto en Apify."""


class SinSaldo(Exception):
    """Apify respondió 402: no queda saldo en el mes."""


GASTO = {}  # se enlaza a est["apify"] en main()


def gasto_hoy():
    dia = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if GASTO.get("dia") != dia:
        GASTO.clear()
        GASTO.update(dia=dia, usd=0.0, llamadas=0, aviso_tope=False, aviso_saldo=False)
    return GASTO["usd"]


def apify(inp, timeout=120):
    if gasto_hoy() + APIFY_COSTO > APIFY_TOPE_DIA:
        raise SinPresupuesto()
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=inp, timeout=timeout)
    if r.status_code == 402:
        raise SinSaldo()
    r.raise_for_status()
    GASTO["usd"] += APIFY_COSTO
    GASTO["llamadas"] = GASTO.get("llamadas", 0) + 1
    return r.json()


_MILESTONE = {}


def kalshi_live(ev):
    """Marcador y estadísticas en vivo desde Kalshi (gratis). Devuelve 'details' o None."""
    tk = ev["event_ticker"]
    if tk not in _MILESTONE:
        r = requests.get(f"{KALSHI}/milestones", params={"related_event_ticker": tk, "limit": 5}, timeout=15)
        ms = r.json().get("milestones", []) if r.ok else []
        if not ms:
            return None
        _MILESTONE[tk] = (ms[0]["id"], ms[0]["type"])
    mid, mtype = _MILESTONE[tk]
    r = requests.get(f"{KALSHI}/live_data/{mtype}/milestone/{mid}", timeout=15)
    if not r.ok:
        return None
    return r.json().get("live_data", {}).get("details") or None


def precandidato(ev):
    """Filtro gratis con precios de Kalshi: volumen y algún lado con precio/spread válidos."""
    mks = ev.get("markets", [])
    if sum(f(mk.get("volume_fp")) or 0 for mk in mks) < MIN_VOL_CONTRATOS:
        return False
    for mk in mks:
        ask, bid = f(mk.get("yes_ask_dollars")), f(mk.get("yes_bid_dollars"))
        if ask is not None and bid is not None and PRECIO_MIN <= ask <= PRECIO_MAX and ask - bid <= SPREAD_MAX:
            return True
    return False


def momento_kalshi(det):
    """Filtros 1 y 2 con el marcador de Kalshi: en vivo, set a ≤1 game, entre games, sin tiebreak."""
    if not det or det.get("status") != "live":
        return False
    r1, r2 = det.get("competitor1_round_scores") or [], det.get("competitor2_round_scores") or []
    if not r1 or not r2:
        return False
    g1, g2 = r1[-1].get("score", 0) or 0, r2[-1].get("score", 0) or 0
    if abs(g1 - g2) > 1 or (g1 == 6 and g2 == 6):
        return False
    if len(r1) == 1 and g1 + g2 == 0:
        return False
    p1, p2 = det.get("competitor1_current_round_score"), det.get("competitor2_current_round_score")
    return str(p1) in ("0", "None", "") and str(p2) in ("0", "None", "")


def stats_kalshi(det, m, ev):
    """(spw_home, n_home, spw_away, n_away) con las estadísticas de saque de Kalshi, o None."""
    t = (ev or {}).get("title", "")
    if not det or " vs " not in t:
        return None
    home_es_c1 = apellido_match(m["homePlayerName"], t.split(" vs ", 1)[0].strip())

    def spw(s):
        w, l = (s or {}).get("service_points_won"), (s or {}).get("service_points_lost")
        if w is None or l is None or w + l < 10:
            return None
        return w / (w + l), w + l

    x1, x2 = spw(det.get("competitor1_statistics")), spw(det.get("competitor2_statistics"))
    if not x1 or not x2:
        return None
    (sh, nh), (sa, na) = (x1, x2) if home_es_c1 else (x2, x1)
    return sh, nh, sa, na


def kalshi_eventos():
    eventos = []
    for s in SERIES:
        cursor = None
        while True:
            p = {"series_ticker": s, "status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                p["cursor"] = cursor
            r = requests.get(f"{KALSHI}/events", params=p, timeout=20)
            if r.status_code != 200:
                break
            d = r.json()
            for e in d.get("events", []):
                e["_serie"] = s
                eventos.append(e)
            cursor = d.get("cursor")
            if not cursor:
                break
    return eventos


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ───────────── modelo ─────────────
def stats_saque(pbp):
    """Devuelve (spw_home, n_home, spw_away, n_away) del partido de hoy, o None."""
    try:
        allp = next(s for s in pbp["statistics"] if s["period"] == "ALL")
    except (KeyError, StopIteration, TypeError):
        return None
    items = {}
    for g in allp.get("groups", []):
        for it in g.get("items", []):
            items[it["key"]] = it
    sp, rp = items.get("servicePointsScored"), items.get("receiverPointsScored")
    if not sp or not rp:
        return None
    sh_w, sa_w = sp["homeValue"], sp["awayValue"]
    sh_n = sh_w + rp["awayValue"]          # puntos de saque del local
    sa_n = sa_w + rp["homeValue"]
    if sh_n < 10 or sa_n < 10:
        return None
    return sh_w / sh_n, sh_n, sa_w / sa_n, sa_n


def estado(m):
    """sets ganados, games set actual, quién saca (True=local), puntos."""
    sets = m["score"].get("sets", [])
    if not sets:
        return None
    sh = sa = 0
    for st in sets[:-1]:
        if st["home"] > st["away"]:
            sh += 1
        else:
            sa += 1
    cur = sets[-1]
    gh, ga = cur["home"], cur["away"]
    # Quién saca: alterna cada game durante todo el partido (tiebreak = 1 game)
    total = sum(min(st["home"] + st["away"], 13) for st in sets[:-1]) + gh + ga
    fts = m.get("firstToServe")
    if fts not in ("home", "away"):
        return None
    local_saca = (total % 2 == 0) == (fts == "home")
    hp, ap = str(m["score"].get("homePoint", "0")), str(m["score"].get("awayPoint", "0"))
    if gh == 6 and ga == 6:  # tiebreak: puntos numéricos
        ph = int(hp) if hp.isdigit() else 0
        pa = int(ap) if ap.isdigit() else 0
    else:
        ph, pa = PTS.get(hp, 0), PTS.get(ap, 0)
    return sh, sa, gh, ga, local_saca, ph, pa


def marcador(m, lado):
    """Marcador visto desde el jugador recomendado (sus games primero)."""
    a, b = ("home", "away") if lado == "home" else ("away", "home")
    txt = " ".join(f'{st[a]}-{st[b]}' for st in m["score"].get("sets", []))
    pa_, pb_ = m["score"].get(a + "Point"), m["score"].get(b + "Point")
    if pa_ not in (None, "0", 0) or pb_ not in (None, "0", 0):
        txt += f" ({pa_}-{pb_})"
    return txt


def mercados(m, ev):
    """Mapea los mercados Kalshi del evento a (local, visita)."""
    mk_home = mk_away = None
    for mk in ev.get("markets", []):
        nm = mk.get("yes_sub_title", "")
        if apellido_match(m["homePlayerName"], nm.split()[-1] if nm else "") or apellido_match(m["homePlayerName"], nm):
            mk_home = mk
        elif apellido_match(m["awayPlayerName"], nm.split()[-1] if nm else "") or apellido_match(m["awayPlayerName"], nm):
            mk_away = mk
    return mk_home, mk_away


def modelo(m, serie, st, exigir_cuota=True, det=None, ev=None):
    """Calcula (p_final_h, p_score_h, p_book_h, brecha) para el local, o None."""
    sh, sa, gh, ga, local_saca, ph, pa = st
    o = m.get("odds") or {}
    dh, da = f(o.get("home", {}).get("decimal")), f(o.get("away", {}).get("decimal"))
    p_book_h = no_vig(dh, da)
    p_pre_h = no_vig(frac_to_dec(o.get("home", {}).get("initialFractional")),
                     frac_to_dec(o.get("away", {}).get("initialFractional")))
    if exigir_cuota and (p_book_h is None or p_pre_h is None):
        return None
    if p_pre_h is None:
        p_pre_h = 0.5
    # Stats de saque de hoy: primero Kalshi (gratis); si faltan, pointByPoint de Apify
    ss = stats_kalshi(det, m, ev)
    if not ss:
        try:
            pbp = apify({"mode": "pointByPoint", "matchId": m["id"]}, timeout=90)
            ss = stats_saque(pbp[0]) if pbp else None
        except SinSaldo:
            raise
        except Exception:
            ss = None
    if not ss:
        return None
    spw_h, n_h, spw_a, n_a = ss
    fem = serie in FEMENINO or "women" in (m.get("tournamentName") or "").lower()
    spw_tour = 0.56 if fem else 0.64
    brecha_prev = brecha_desde_cuota(p_pre_h) / 100      # sin stats de temporada → solo cuota
    n = (n_h + n_a) / 2
    brecha = (n * (spw_h - spw_a) + ANCLA * brecha_prev) / (n + ANCLA)
    # ph/pa = puntos del local/visita en el game (o tiebreak) en curso
    p_score_h = prob(spw_tour + brecha / 2, spw_tour - brecha / 2,
                     sh, sa, gh, ga, local_saca, ph, pa, 3)
    if p_book_h is None:
        p_final_h = p_score_h
    else:
        p_final_h = W_BOOK * p_book_h + W_SCORE * p_score_h
    return min(max(p_final_h, 0.03), 0.97), p_score_h, p_book_h, brecha


def evaluar(m, ev, serie, permitir_edge_alto=False, det=None):
    """Devuelve dict de señal o None. Solo pasa si cumple TODO el filtro."""
    st = estado(m)
    if not st:
        return None
    sh, sa, gh, ga, local_saca, ph, pa = st
    # Filtro 1: set activo a ≤1 game · Filtro 2: entre games
    if abs(gh - ga) > 1 or gh + ga == 0 and sh + sa == 0 or ph or pa:
        return None
    if gh == 6 and ga == 6:
        return None  # no entrar en tiebreak

    mk_home, mk_away = mercados(m, ev)
    if not mk_home or not mk_away:
        return None
    vol = (f(mk_home.get("volume_fp")) or 0) + (f(mk_away.get("volume_fp")) or 0)
    if vol < MIN_VOL_CONTRATOS:
        return None

    r = modelo(m, serie, st, det=det, ev=ev)
    if not r:
        return None
    p_final_h, p_score_h, p_book_h, brecha = r
    log_candidato(m, ev, st, mk_home, mk_away, r, vol)
    if abs(p_score_h - p_book_h) > CONTRADICCION_MAX:
        return None

    mejor = None
    for lado, mk, P, Pb in (("home", mk_home, p_final_h, p_book_h),
                            ("away", mk_away, 1 - p_final_h, 1 - p_book_h)):
        ask, bid = f(mk.get("yes_ask_dollars")), f(mk.get("yes_bid_dollars"))
        if ask is None or bid is None:
            continue
        if not (PRECIO_MIN <= ask <= PRECIO_MAX) or ask - bid > SPREAD_MAX:
            continue
        if P - ask < EDGE_MODELO_MIN or Pb - ask < EDGE_CASA_MIN:
            continue
        if P - ask > EDGE_MODELO_MAX and not permitir_edge_alto:
            continue
        kelly = (P - ask) / (1 - ask)
        monto = min(KELLY_FRAC * kelly, TOPE_OP) * BANKROLL
        contratos = max(1, int(monto / ask))
        depth = f(mk.get("yes_ask_size_fp")) or 0
        if depth < 3 * contratos:
            continue
        costo = ask + fee(ask, contratos) / contratos
        e = P - ask
        if not mejor or e > mejor["edge"]:
            mejor = dict(lado=lado, mk=mk, P=P, Pb=Pb, ask=ask, bid=bid, edge=e,
                         edge_casa=Pb - ask, ev=100 * (P / costo - 1),
                         monto=round(contratos * ask, 2), contratos=contratos)
    if not mejor:
        return None
    jug = m["homePlayerName"] if mejor["lado"] == "home" else m["awayPlayerName"]
    rival = m["awayPlayerName"] if mejor["lado"] == "home" else m["homePlayerName"]
    saca = m["homePlayerName"] if local_saca else m["awayPlayerName"]
    sets_txt = marcador(m, mejor["lado"])
    mejor.update(jugador=jug, rival=rival, torneo=m.get("tournamentName", ""),
                 superficie=m.get("surface", "?"), sets_txt=sets_txt, saca=saca,
                 p_score=p_score_h if mejor["lado"] == "home" else 1 - p_score_h,
                 brecha=brecha * 100 * (1 if mejor["lado"] == "home" else -1),
                 vol=vol, ticker=mejor["mk"]["ticker"], match_id=m["id"],
                 clave=f'{m["id"]}-{mejor["lado"]}-{sh}{sa}-{gh}{ga}')
    return mejor


# ───────────── salida ─────────────
def mensaje(s):
    pct = s["monto"] / BANKROLL * 100
    return (
        f"🟢 <b>ENTRAR A FAVOR DE: {s['jugador'].upper()}</b>\n"
        f"👉 En Kalshi compra <b>SÍ (Yes) a que gana {s['jugador']}</b>\n\n"
        f"vs {s['rival']} · {s['torneo']} · {s['superficie']}\n"
        f"Marcador ({s['jugador']} primero): <b>{s['sets_txt']}</b> · próximo saque: {s['saca']}\n\n"
        f"1. Prob. modelo: <b>{s['P']*100:.1f}%</b> (calc. {s['p_score']*100:.1f}% · casa {s['Pb']*100:.1f}%)\n"
        f"2. Prob. mercado: {s['ask']*100:.0f}¢ (bid {s['bid']*100:.0f}¢)\n"
        f"3. EDGE: <b>+{s['edge']*100:.1f} pp</b> (casa vs Kalshi +{s['edge_casa']*100:.1f} pp)\n"
        f"4. EV por $100: ${s['ev']:.1f}\n\n"
        f"💵 Monto: <b>${s['monto']:.2f}</b> ({pct:.1f}% bankroll) · {s['contratos']} contratos\n"
        f"Plan: compra límite a {s['ask']*100:.0f}¢ · <b>el bot te avisa cuándo VENDER</b> (valor justo) · sin stop-loss · si ves lesión o MTO, vende tú de inmediato\n"
        f"⚠️ Confirma el marcador en Kalshi antes de comprar (dato de hace segundos).\n"
        f"https://kalshi.com/markets/{s['ticker'].split('-')[0].lower()}/m/{s['ticker'].rsplit('-', 1)[0].lower()}\n"
        f"<i>Confianza MEDIA (sin casa sharp). Estimación, no garantía.</i>"
    )


def telegram(txt):
    """Envía a Telegram. Si falla con formato, reintenta en texto plano. Registra todo."""
    if DRY_RUN or not TG_TOKEN:
        print("---- [DRY RUN] ----\n" + txt)
        return True
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    base = {"chat_id": TG_CHAT, "disable_web_page_preview": True}
    for intento in range(3):
        try:
            r = requests.post(url, json={**base, "text": txt, "parse_mode": "HTML"}, timeout=15)
            if r.ok:
                print("Telegram OK")
                return True
            print("Telegram FALLO (HTML):", r.status_code, r.text[:300])
            plano = re.sub(r"</?[bi]>", "", txt)
            r = requests.post(url, json={**base, "text": plano}, timeout=15)
            if r.ok:
                print("Telegram OK (texto plano)")
                return True
            print("Telegram FALLO (plano):", r.status_code, r.text[:300])
        except Exception as e:
            print("Telegram FALLO (red):", repr(e))
        time.sleep(3)
    return False


def log(s, archivo=None, motivo=""):
    archivo = archivo or LOG_CSV
    nuevo = not os.path.exists(archivo)
    with open(archivo, "a", newline="") as fh:
        w = csv.writer(fh)
        if nuevo:
            w.writerow(["fecha_utc", "match_id", "ticker", "jugador", "rival", "torneo", "marcador",
                        "p_final", "p_score", "p_casa", "ask", "bid", "edge_pp", "ev_100", "monto",
                        "resultado"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), s["match_id"], s["ticker"],
                    s["jugador"], s["rival"], s["torneo"], s["sets_txt"], round(s["P"], 4),
                    round(s["p_score"], 4), round(s["Pb"], 4), s["ask"], s["bid"],
                    round(s["edge"] * 100, 1), round(s["ev"], 1), s["monto"], motivo])


_CAND_VISTOS = set()


def log_candidato(m, ev, st, mk_home, mk_away, r, vol):
    """Anota cada evaluación (una por marcador) para medir después si el modelo
    le gana al precio de Kalshi. No cambia ninguna decisión."""
    sh, sa, gh, ga, local_saca = st[:5]
    clave = f'{m["id"]}-{sh}{sa}-{gh}{ga}'
    if clave in _CAND_VISTOS:
        return
    _CAND_VISTOS.add(clave)
    try:
        nuevo = not os.path.exists(LOG_CAND)
        with open(LOG_CAND, "a", newline="") as fh:
            w = csv.writer(fh)
            if nuevo:
                w.writerow(["fecha_utc", "match_id", "serie", "ticker_local", "ticker_visita", "local", "visita",
                            "sets_local", "sets_visita", "games_local", "games_visita", "saca_local",
                            "p_final_local", "p_calc_local", "p_casa_local", "brecha",
                            "ask_local", "bid_local", "ask_visita", "bid_visita", "volumen"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), m["id"], ev.get("_serie", ""),
                        mk_home.get("ticker"), mk_away.get("ticker"), m["homePlayerName"], m["awayPlayerName"],
                        sh, sa, gh, ga, int(bool(local_saca)),
                        round(r[0], 4), round(r[1], 4), "" if r[2] is None else round(r[2], 4), round(r[3], 4),
                        mk_home.get("yes_ask_dollars"), mk_home.get("yes_bid_dollars"),
                        mk_away.get("yes_ask_dollars"), mk_away.get("yes_bid_dollars"), round(vol)])
    except Exception as e:
        print("No pude anotar candidato:", repr(e))


def cargar_estado():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"abiertas": {}, "entradas": {}}


def guardar_estado(e):
    # los contadores de entradas se reinician cuando no quedan posiciones del partido
    vivos = set(e.get("abiertas", {}))
    e["entradas"] = {k: v for k, v in e.get("entradas", {}).items()
                     if k in vivos or len(e["entradas"]) < 500}
    with open(STATE, "w") as fh:
        json.dump(e, fh)


def emparejar(m, eventos):
    for ev in eventos:
        t = ev.get("title", "")
        if " vs " not in t:
            continue
        a, b = [x.strip() for x in t.split(" vs ", 1)]
        h, w = m["homePlayerName"], m["awayPlayerName"]
        if (apellido_match(h, a) and apellido_match(w, b)) or (apellido_match(h, b) and apellido_match(w, a)):
            return ev
    return None


def mensaje_salida(pos, x):
    g = (x["neto"] - pos["ask"]) * pos["contratos"]
    return (
        f"🔴 <b>VENDER TUS SÍ DE: {pos['jugador'].upper()}</b> (si compraste)\n"
        f"vs {pos['rival']} · Marcador ({pos['jugador']} primero): <b>{x['sets_txt']}</b>\n\n"
        f"Kalshi paga: <b>{x['bid']*100:.0f}¢</b> (neto de comisión {x['neto']*100:.1f}¢)\n"
        f"Vale según el modelo: {x['P']*100:.1f}%\n"
        f"Entrada: {pos['ask']*100:.0f}¢ → resultado {((x['neto']/pos['ask'])-1)*100:+.1f}% (${g:+.2f})\n"
        f"Vende con orden límite al bid ({x['bid']*100:.0f}¢) o 1¢ arriba.\n"
        f"No hay reentrada en este partido."
    )


def seguir(m, ev, pos):
    """Salida por valor justo: vender si bid − comisión ≥ P + 0.5 pp."""
    st = estado(m)
    if not st:
        return None
    mk_home, mk_away = mercados(m, ev)
    mk = mk_home if pos["lado"] == "home" else mk_away
    if not mk:
        return None
    bid = f(mk.get("yes_bid_dollars"))
    if not bid:
        return None
    try:
        det = kalshi_live(ev)
    except Exception:
        det = None
    r = modelo(m, ev["_serie"], st, exigir_cuota=False, det=det, ev=ev)
    if not r:
        return None
    P = r[0] if pos["lado"] == "home" else 1 - r[0]
    neto = bid - fee(bid, pos["contratos"]) / pos["contratos"]
    if neto >= P + 0.005:
        return dict(bid=bid, neto=neto, P=P, sets_txt=marcador(m, pos["lado"]))
    return None


def log_salida(pos, motivo, precio, neto, archivo=None):
    archivo = archivo or LOG_SALIDAS
    nuevo = not os.path.exists(archivo)
    with open(archivo, "a", newline="") as fh:
        w = csv.writer(fh)
        if nuevo:
            w.writerow(["fecha_utc", "match_id", "ticker", "jugador", "entrada", "contratos",
                        "motivo", "precio_salida", "neto_por_contrato", "ganancia_usd", "rend_pct"])
        g = (neto - pos["ask"]) * pos["contratos"]
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), pos["match_id"],
                    pos["ticker"], pos["jugador"], pos["ask"], pos["contratos"], motivo,
                    round(precio, 4), round(neto, 4), round(g, 2), round((neto / pos["ask"] - 1) * 100, 1)])
    log_trayectoria(pos, motivo, precio, "sombra" if archivo == LOG_SOMBRA_SAL else "real")


def anotar_extremos(pos, m, ev):
    """Guarda el bid más alto y más bajo que tuvo la posición (para comparar salidas a futuro)."""
    try:
        mk_home, mk_away = mercados(m, ev)
        mk = mk_home if pos["lado"] == "home" else mk_away
        bid = f(mk.get("yes_bid_dollars")) if mk else None
        if bid is None:
            return
        if bid > pos.get("max_bid", -1):
            pos["max_bid"], pos["t_max"] = bid, round((time.time() - pos["ts"]) / 60)
        pos["min_bid"] = min(pos.get("min_bid", 2), bid)
    except Exception:
        pass


def log_trayectoria(pos, motivo, precio, tipo):
    try:
        nuevo = not os.path.exists(LOG_TRAY)
        with open(LOG_TRAY, "a", newline="") as fh:
            w = csv.writer(fh)
            if nuevo:
                w.writerow(["fecha_utc", "tipo", "ticker", "jugador", "entrada", "contratos", "motivo_salida",
                            "precio_salida", "max_bid", "min_bid", "min_hasta_max", "minutos_abierta",
                            "toco_10pct", "toco_20pct", "toco_30pct"])
            e, mx = pos["ask"], pos.get("max_bid")
            toco = lambda k: "" if mx is None else ("si" if mx >= e * (1 + k) else "no")
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), tipo, pos["ticker"],
                        pos["jugador"], e, pos["contratos"], motivo, round(precio, 4), mx, pos.get("min_bid"),
                        pos.get("t_max"), round((time.time() - pos["ts"]) / 60), toco(.10), toco(.20), toco(.30)])
    except Exception as ex:
        print("No pude anotar trayectoria:", repr(ex))


def liquidacion(ticker):
    try:
        r = requests.get(f"{KALSHI}/markets/{ticker}", timeout=15).json().get("market", {})
        return r.get("result")  # 'yes' / 'no' / ''
    except Exception:
        return None


def ciclo(est):
    eventos = kalshi_eventos()
    abiertas = est.setdefault("abiertas", {})
    entradas = est.setdefault("entradas", {})
    sombra = est.setdefault("sombra", {})
    ent_sombra = est.setdefault("entradas_sombra", {})
    n_ok = n_out = 0

    # 0) Filtro gratis con Kalshi: ¿hay algún partido en el momento justo para entrar?
    cands = {}
    for ev in eventos:
        if not precandidato(ev):
            continue
        try:
            det = kalshi_live(ev)
        except Exception:
            det = None
        if momento_kalshi(det):
            cands[ev["event_ticker"]] = det
    toca_seg = bool(abiertas or sombra) and time.time() - est.get("ult_seg", 0) >= APIFY_SEG_CADA
    if not cands and not toca_seg:
        print(f"{datetime.now(timezone.utc):%H:%M:%S} kalshi={len(eventos)} candidatos=0 "
              f"abiertas={len(abiertas)} sombra_abiertas={len(sombra)} · sin llamar a Apify "
              f"(hoy ${gasto_hoy():.2f} de ${APIFY_TOPE_DIA:.2f})")
        return
    vivos = apify({"mode": "liveMatches", "matchType": ["singles"]})
    est["ult_seg"] = time.time()
    por_id = {m["id"]: m for m in vivos}

    # 1) Seguimiento de posiciones abiertas (salida por valor justo)
    for mid, pos in list(abiertas.items()):
        m = por_id.get(int(mid))
        if not m:
            res = liquidacion(pos["ticker"])
            if res in ("yes", "no"):
                precio = 1.0 if res == "yes" else 0.0
                log_salida(pos, "liquidacion", precio, precio)
                del abiertas[mid]
            elif time.time() - pos["ts"] > 12 * 3600:
                del abiertas[mid]
            continue
        ev = emparejar(m, eventos)
        if not ev:
            continue
        anotar_extremos(pos, m, ev)
        x = seguir(m, ev, pos)
        if x:
            telegram(mensaje_salida(pos, x))
            log_salida(pos, "valor_justo", x["bid"], x["neto"])
            del abiertas[mid]
            n_out += 1

    # 1b) Señales bloqueadas por las reglas nuevas: se siguen en paper, sin avisar
    for mid, pos in list(sombra.items()):
        m = por_id.get(int(mid))
        if not m:
            res = liquidacion(pos["ticker"])
            if res in ("yes", "no"):
                precio = 1.0 if res == "yes" else 0.0
                log_salida(pos, "liquidacion", precio, precio, LOG_SOMBRA_SAL)
                del sombra[mid]
            elif time.time() - pos["ts"] > 12 * 3600:
                del sombra[mid]
            continue
        ev = emparejar(m, eventos)
        if ev:
            anotar_extremos(pos, m, ev)
        x = seguir(m, ev, pos) if ev else None
        if x:
            log_salida(pos, "valor_justo", x["bid"], x["neto"], LOG_SOMBRA_SAL)
            del sombra[mid]
    n_sombra = 0

    # 2) Nuevas entradas
    for m in vivos:
        mid = str(m["id"])
        if mid in abiertas:
            continue
        reentrada = entradas.get(mid, 0) >= MAX_ENTRADAS
        sombra_libre = mid not in sombra and ent_sombra.get(mid, 0) < 3
        if reentrada and not sombra_libre:
            continue
        ev = emparejar(m, eventos)
        if not ev or ev["event_ticker"] not in cands:
            continue
        s = evaluar(m, ev, ev["_serie"], permitir_edge_alto=True, det=cands[ev["event_ticker"]])
        if not s:
            continue
        motivos = (["edge>10"] if s["edge"] > EDGE_MODELO_MAX else []) + (["reentrada"] if reentrada else [])
        if motivos:
            if sombra_libre:
                log(s, LOG_SOMBRA, "+".join(motivos))
                ent_sombra[mid] = ent_sombra.get(mid, 0) + 1
                sombra[mid] = dict(match_id=m["id"], ticker=s["ticker"], lado=s["lado"], ask=s["ask"],
                                   contratos=s["contratos"], jugador=s["jugador"], rival=s["rival"],
                                   ts=time.time())
                n_sombra += 1
            continue
        telegram(mensaje(s))
        log(s)
        entradas[mid] = entradas.get(mid, 0) + 1
        abiertas[mid] = dict(match_id=m["id"], ticker=s["ticker"], lado=s["lado"], ask=s["ask"],
                             contratos=s["contratos"], jugador=s["jugador"], rival=s["rival"],
                             ts=time.time())
        try:  # seguimiento de marcador por Telegram (quiebres, sets, final)
            pos, _ = segui.agregar(est, s["ticker"].rsplit("-", 1)[0], s["jugador"], s["ask"],
                                   s["contratos"], origen="bot", ticker=s["ticker"])
            if pos:
                pos["ult"] = None
        except Exception as e:
            print("No pude agregar al seguimiento:", repr(e))
        n_ok += 1
    print(f"{datetime.now(timezone.utc):%H:%M:%S} vivos={len(vivos)} kalshi={len(eventos)} "
          f"entradas={n_ok} salidas={n_out} abiertas={len(abiertas)} "
          f"bloqueadas={n_sombra} sombra_abiertas={len(sombra)} candidatos={len(cands)} "
          f"apify_hoy=${gasto_hoy():.2f}/{APIFY_TOPE_DIA:.2f}")


def main():
    if not APIFY_TOKEN:
        sys.exit("Falta APIFY_TOKEN")
    minutos = float(os.getenv("LOOP_MINUTES", "28"))
    pausa = float(os.getenv("POLL_SECONDS", "120"))
    fin = time.time() + minutos * 60
    est = cargar_estado()
    global GASTO
    GASTO = est.setdefault("apify", {})
    for pos in est.get("abiertas", {}).values():  # alertas previas al módulo de seguimiento
        if pos["ticker"] not in est.get("seguir", {}):
            try:
                segui.agregar(est, pos["ticker"].rsplit("-", 1)[0], pos["jugador"], pos["ask"],
                              pos["contratos"], origen="bot", ticker=pos["ticker"])
            except Exception as e:
                print("No pude migrar al seguimiento:", repr(e))
    fallos = 0
    if os.getenv("PRUEBA_SENAL") == "1":
        ej = dict(jugador="Jugador Ejemplo", rival="Rival Ejemplo", torneo="PRUEBA", superficie="Dura",
                  sets_txt="6-4 3-3", saca="Rival Ejemplo", P=0.62, p_score=0.64, Pb=0.60, ask=0.55,
                  bid=0.54, edge=0.07, edge_casa=0.05, ev=11.0, monto=9.90, contratos=18,
                  ticker="KXATPMATCH-EJEMPLO-EJE")
        telegram("🧪 <b>EJEMPLO DE SEÑAL — NO OPERAR</b>\n(así se ven las alertas reales)\n\n" + mensaje(ej))
    while True:
        try:  # comandos de Telegram + marcador de posiciones (independiente de Apify)
            segui.procesar_comandos(segui.leer_comandos(TG_TOKEN, TG_CHAT, est), est, telegram)
            segui.ciclo_seguimiento(est, telegram)
        except Exception as e:
            print("Seguimiento error:", repr(e))
        try:
            ciclo(est)
            fallos = 0
        except SinPresupuesto:
            fallos = 0
            print(f"Tope diario de Apify alcanzado (${gasto_hoy():.2f}).")
            if not GASTO.get("aviso_tope"):
                GASTO["aviso_tope"] = True
                telegram(f"💤 Bot de tenis: llegué al tope diario de Apify (${APIFY_TOPE_DIA:.2f}) para no gastar "
                         f"de más. Pauso las señales nuevas hasta las 8 p. m. (Miami), cuando se reinicia el día.")
        except SinSaldo:
            fallos = 0
            print("Apify sin saldo (402).")
            if not GASTO.get("aviso_saldo"):
                GASTO["aviso_saldo"] = True
                telegram("⚠️ Bot de tenis: Apify se quedó sin saldo este mes. Pauso las señales hasta que se "
                         "renueve; los comandos /estado y el seguimiento de marcador siguen funcionando.")
        except Exception as e:
            fallos += 1
            print("Error:", repr(e))
            if fallos == 5:
                telegram("⚠️ Bot de tenis: 5 errores seguidos leyendo datos (Apify/Kalshi). Revisa GitHub Actions.")
        guardar_estado(est)
        if time.time() + pausa > fin:
            break
        time.sleep(pausa)


if __name__ == "__main__":
    main()
