"""Modelo NBA de 4 variables.

1. Plantilla (filtro de lesiones de titulares dudosos)  -> bot.py
2. Línea de referencia (DraftKings vía ESPN)            -> ancla
3. Net Rating ajustado (ORtg/DRtg recientes + local + back-to-back)
4. Pace combinado (posesiones)
"""
from datetime import date

from .common import kalshi_fee, p_over

CFG = {
    "window": 15,          # últimos N partidos para ratings
    "shrink_k": 8,         # partidos "virtuales" del prior
    "hca": 2.0,            # ventaja de local (pts)
    "b2b": 2.0,            # castigo back-to-back (pts de margen)
    "w_model": 0.35,       # peso del modelo propio vs línea DK
    "sd": {"margin": 12.0, "total": 18.0, "team": 11.5,
           "h1_margin": 8.5, "h1_total": 12.0, "q1_margin": 6.5, "q1_total": 8.0},
    "share": {"h1_total": 0.505, "q1_total": 0.252, "h1_margin": 0.52, "q1_margin": 0.27},
    "min_edge": {"GAME": 0.04, "SPREAD": 0.04, "TOTAL": 0.04, "TEAMTOTAL": 0.05,
                 "1HSPREAD": 0.05, "1HTOTAL": 0.05, "1QSPREAD": 0.05, "1QTOTAL": 0.05},
    "max_edge": 0.15,      # edge mayor = precio viejo o error -> se descarta
    "max_spread": 0.05,    # ask - bid máximo (liquidez)
    "price_min": 0.12,
    "price_max": 0.88,
}


def season_of(d):
    """Temporada NBA a la que pertenece una fecha (año de inicio)."""
    return d.year if d.month >= 8 else d.year - 1


def _poss(g, side):
    return g.get(f"{side}poss")


def league_avgs(games):
    pts = poss = n = 0
    for g in games:
        if g.get("hposs") and g.get("aposs"):
            p = (g["hposs"] + g["aposs"]) / 2
            pts += g["hs"] + g["as"]
            poss += 2 * p
            n += 1
    if n < 20:
        return {"ortg": 114.0, "pace": 99.5}
    return {"ortg": 100 * pts / poss, "pace": poss / (2 * n)}


def team_games(games, team):
    out = []
    for g in games:
        if not (g.get("hposs") and g.get("aposs")):
            continue
        p = (g["hposs"] + g["aposs"]) / 2
        if g["home"] == team:
            out.append((g["hs"], g["as"], p))
        elif g["away"] == team:
            out.append((g["as"], g["hs"], p))
    return out


def raw_rating(games, team, window=None):
    tg = team_games(games, team)
    if window:
        tg = tg[-window:]
    if not tg:
        return None
    pf = sum(x[0] for x in tg)
    pa = sum(x[1] for x in tg)
    ps = sum(x[2] for x in tg)
    return {"ortg": 100 * pf / ps, "drtg": 100 * pa / ps, "pace": ps / len(tg), "n": len(tg)}


def ratings(prev_games, team, game_day):
    """Ratings ajustados del equipo antes de game_day.

    prev_games: partidos ya jugados (ordenados por fecha) ANTES de game_day.
    Prior = 50 % temporada anterior + 50 % promedio de liga.
    """
    s = season_of(game_day)
    cur = [g for g in prev_games if season_of(date.fromisoformat(g["day"])) == s]
    last = [g for g in prev_games if season_of(date.fromisoformat(g["day"])) == s - 1]
    lg = league_avgs(cur if len(cur) >= 60 else (last or cur))

    prior = {"ortg": lg["ortg"], "drtg": lg["ortg"], "pace": lg["pace"]}
    lr = raw_rating(last, team)
    if lr and lr["n"] >= 20:
        lgl = league_avgs(last)
        prior = {
            "ortg": lg["ortg"] + 0.5 * (lr["ortg"] - lgl["ortg"]),
            "drtg": lg["ortg"] + 0.5 * (lr["drtg"] - lgl["ortg"]),
            "pace": lg["pace"] + 0.5 * (lr["pace"] - lgl["pace"]),
        }
    r = raw_rating(cur, team, CFG["window"])
    k = CFG["shrink_k"]
    if not r:
        return {**prior, "n": 0}, lg
    n = r["n"]
    mix = lambda a, b: (n * a + k * b) / (n + k)
    return {"ortg": mix(r["ortg"], prior["ortg"]), "drtg": mix(r["drtg"], prior["drtg"]),
            "pace": mix(r["pace"], prior["pace"]), "n": n}, lg


def played_yesterday(prev_games, team, game_day):
    for g in reversed(prev_games[-40:]):
        if (game_day - date.fromisoformat(g["day"])).days == 1 and team in (g["home"], g["away"]):
            return True
    return False


def learn_params(prev_games):
    """Aprende proporciones de mitad/cuarto y desviaciones con datos previos que tengan línea."""
    share = dict(CFG["share"])
    sd = dict(CFG["sd"])
    rows = [g for g in prev_games[-1500:]
            if g.get("line_m") is not None and g.get("line_t") and len(g.get("hq", [])) >= 4
            and len(g.get("aq", [])) >= 4]
    if len(rows) < 200:
        return share, sd

    def slope(xs, ys):
        den = sum(x * x for x in xs)
        return sum(x * y for x, y in zip(xs, ys)) / den if den else None

    lm = [g["line_m"] for g in rows]
    lt = [g["line_t"] for g in rows]
    h1m = [sum(g["hq"][:2]) - sum(g["aq"][:2]) for g in rows]
    q1m = [g["hq"][0] - g["aq"][0] for g in rows]
    h1t = [sum(g["hq"][:2]) + sum(g["aq"][:2]) for g in rows]
    q1t = [g["hq"][0] + g["aq"][0] for g in rows]
    fm = [g["hs"] - g["as"] for g in rows]
    ft = [g["hs"] + g["as"] for g in rows]

    s = slope(lm, h1m)
    if s is not None:
        share["h1_margin"] = min(max(s, 0.40), 0.62)
    s = slope(lm, q1m)
    if s is not None:
        share["q1_margin"] = min(max(s, 0.18), 0.36)
    share["h1_total"] = sum(h1t) / sum(lt)
    share["q1_total"] = sum(q1t) / sum(lt)

    def rsd(actual, pred):
        n = len(actual)
        return (sum((a - p) ** 2 for a, p in zip(actual, pred)) / n) ** 0.5

    sd["margin"] = rsd(fm, lm)
    sd["total"] = rsd(ft, lt)
    sd["h1_margin"] = rsd(h1m, [x * share["h1_margin"] for x in lm])
    sd["q1_margin"] = rsd(q1m, [x * share["q1_margin"] for x in lm])
    sd["h1_total"] = rsd(h1t, [x * share["h1_total"] for x in lt])
    sd["q1_total"] = rsd(q1t, [x * share["q1_total"] for x in lt])
    hs = [g["hs"] for g in rows]
    hp = [(t + m) / 2 for t, m in zip(lt, lm)]
    sd["team"] = rsd(hs, hp)
    # nunca más confiado que el 80 % de la desviación por defecto
    for k, v in CFG["sd"].items():
        sd[k] = max(sd[k], 0.8 * v)
    return share, sd


def project(prev_games, home, away, game_day, line_m=None, line_t=None):
    """Proyección del partido. Devuelve medias y desviaciones."""
    rh, lg = ratings(prev_games, home, game_day)
    ra, _ = ratings(prev_games, away, game_day)
    poss = rh["pace"] * ra["pace"] / lg["pace"]
    ph = poss * (rh["ortg"] * ra["drtg"] / lg["ortg"]) / 100
    pa = poss * (ra["ortg"] * rh["drtg"] / lg["ortg"]) / 100
    ph += CFG["hca"] / 2
    pa -= CFG["hca"] / 2
    b2b_h = played_yesterday(prev_games, home, game_day)
    b2b_a = played_yesterday(prev_games, away, game_day)
    if b2b_h:
        ph -= CFG["b2b"] / 2; pa += CFG["b2b"] / 2
    if b2b_a:
        pa -= CFG["b2b"] / 2; ph += CFG["b2b"] / 2
    model_m, model_t = ph - pa, ph + pa

    w = CFG["w_model"]
    m = model_m if line_m is None else w * model_m + (1 - w) * line_m
    t = model_t if line_t is None else w * model_t + (1 - w) * line_t
    share, sd = learn_params(prev_games)
    return {
        "margin": m, "total": t, "home_pts": (t + m) / 2, "away_pts": (t - m) / 2,
        "h1_margin": m * share["h1_margin"], "h1_total": t * share["h1_total"],
        "q1_margin": m * share["q1_margin"], "q1_total": t * share["q1_total"],
        "model_margin": model_m, "model_total": model_t, "poss": poss,
        "line_m": line_m, "line_t": line_t, "b2b_home": b2b_h, "b2b_away": b2b_a,
        "n_home": rh["n"], "n_away": ra["n"], "sd": sd,
        "net_home": rh["ortg"] - rh["drtg"], "net_away": ra["ortg"] - ra["drtg"],
    }


def market_prob(fam, proj, side_team_k, strike, home_k, away_k):
    """Probabilidad del modelo de que el mercado se resuelva SÍ."""
    sd = proj["sd"]
    sign = 1 if side_team_k == home_k else (-1 if side_team_k == away_k else None)
    if fam == "GAME":
        return p_over(sign * proj["margin"], 0.0, sd["margin"]) if sign else None
    if fam == "SPREAD":
        return p_over(sign * proj["margin"], strike, sd["margin"]) if sign else None
    if fam == "1HSPREAD":
        return p_over(sign * proj["h1_margin"], strike, sd["h1_margin"]) if sign else None
    if fam == "1QSPREAD":
        return p_over(sign * proj["q1_margin"], strike, sd["q1_margin"]) if sign else None
    if fam == "TOTAL":
        return p_over(proj["total"], strike, sd["total"])
    if fam == "1HTOTAL":
        return p_over(proj["h1_total"], strike, sd["h1_total"])
    if fam == "1QTOTAL":
        return p_over(proj["q1_total"], strike, sd["q1_total"])
    if fam == "TEAMTOTAL":
        if not sign:
            return None
        mean = proj["home_pts"] if sign == 1 else proj["away_pts"]
        return p_over(mean, strike, sd["team"])
    return None


def evaluate(fam, p_yes, ya, yb):
    """Mejor lado (SÍ/NO) con edge neto de comisión. Devuelve dict o None."""
    if p_yes is None or ya is None or yb is None or yb <= 0 or ya >= 1:
        return None
    if ya - yb > CFG["max_spread"]:
        return None
    best = None
    for side, price, p in (("SI", ya, p_yes), ("NO", 1 - yb, 1 - p_yes)):
        if not (CFG["price_min"] <= price <= CFG["price_max"]):
            continue
        edge = p - price - kalshi_fee(price)
        if CFG["min_edge"][fam] <= edge <= CFG["max_edge"] and (best is None or edge > best["edge"]):
            best = {"side": side, "price": round(price, 4), "p": round(p, 4), "edge": round(edge, 4)}
    return best


def strike_of(m):
    s = m.get("floor_strike")
    try:
        return float(s) if s is not None else None
    except (TypeError, ValueError):
        return None
