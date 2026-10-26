"""Modelo NBA de 4 variables.

1. Plantilla      -> filtro: si Pinnacle y DraftKings discrepan (noticia de lesión), no se opera (bot.py)
2. Línea sharp    -> Pinnacle/DraftKings (The Odds API); sin clave, la línea implícita de Kalshi
3. Rating ajustado-> puntos anotados/recibidos en los últimos 15 partidos + local + back-to-back
4. Ritmo          -> total de puntos esperado (ofensiva de uno vs defensa del otro)
"""
from datetime import date

from .common import kalshi_fee, ncdf, ninv, p_over

CFG = {
    "window": 15,          # últimos N partidos para ratings
    "shrink_k": 8,         # partidos "virtuales" del prior
    "hca": 2.0,            # ventaja de local (pts)
    "b2b": 2.0,            # castigo back-to-back (pts de margen)
    "w_model": 0.35,       # peso del modelo propio vs línea de referencia
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
SIDE_FAMS = ("GAME", "SPREAD", "TEAMTOTAL", "1HSPREAD", "1QSPREAD")


def season_of(d):
    return d.year if d.month >= 8 else d.year - 1


def _day(g):
    return date.fromisoformat(g["day"])


def league_pts(games):
    if len(games) < 20:
        return 114.0
    return sum(g["hs"] + g["as"] for g in games) / (2 * len(games))


def team_rows(games, team):
    out = []
    for g in games:
        if g["home"] == team:
            out.append((g["hs"], g["as"]))
        elif g["away"] == team:
            out.append((g["as"], g["hs"]))
    return out


def ratings(prev_games, team, game_day):
    """Ofensiva/defensa en puntos por partido, con regresión al prior."""
    s = season_of(game_day)
    cur = [g for g in prev_games if season_of(_day(g)) == s]
    last = [g for g in prev_games if season_of(_day(g)) == s - 1]
    lg = league_pts(cur if len(cur) >= 60 else (last or cur))
    prior = {"off": lg, "def": lg}
    lr = team_rows(last, team)
    if len(lr) >= 20:
        lgl = league_pts(last)
        prior = {"off": lg + 0.5 * (sum(x[0] for x in lr) / len(lr) - lgl),
                 "def": lg + 0.5 * (sum(x[1] for x in lr) / len(lr) - lgl)}
    rows = team_rows(cur, team)[-CFG["window"]:]
    n, k = len(rows), CFG["shrink_k"]
    if not n:
        return {**prior, "n": 0}, lg
    off = sum(x[0] for x in rows) / n
    de = sum(x[1] for x in rows) / n
    return {"off": (n * off + k * prior["off"]) / (n + k), "def": (n * de + k * prior["def"]) / (n + k), "n": n}, lg


def played_yesterday(prev_games, team, game_day):
    for g in reversed(prev_games[-40:]):
        if (game_day - _day(g)).days == 1 and team in (g["home"], g["away"]):
            return True
    return False


def learn_shares(prev_games):
    share = dict(CFG["share"])
    h1 = [g for g in prev_games[-1500:] if g.get("h1t")]
    if len(h1) >= 100:
        share["h1_total"] = sum(g["h1t"] for g in h1) / sum(g["hs"] + g["as"] for g in h1)
    q1 = [g for g in prev_games[-1500:] if g.get("q1t")]
    if len(q1) >= 40:
        share["q1_total"] = sum(g["q1t"] for g in q1) / sum(g["hs"] + g["as"] for g in q1)
    return share


def project(prev_games, home, away, game_day, line_m=None, line_t=None):
    rh, lg = ratings(prev_games, home, game_day)
    ra, _ = ratings(prev_games, away, game_day)
    ph = rh["off"] + ra["def"] - lg + CFG["hca"] / 2
    pa = ra["off"] + rh["def"] - lg - CFG["hca"] / 2
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
    share = learn_shares(prev_games)
    return {
        "margin": m, "total": t, "home_pts": (t + m) / 2, "away_pts": (t - m) / 2,
        "h1_margin": m * share["h1_margin"], "h1_total": t * share["h1_total"],
        "q1_margin": m * share["q1_margin"], "q1_total": t * share["q1_total"],
        "model_margin": model_m, "model_total": model_t,
        "line_m": line_m, "line_t": line_t, "b2b_home": b2b_h, "b2b_away": b2b_a,
        "n_home": rh["n"], "n_away": ra["n"], "sd": dict(CFG["sd"]),
    }


def market_prob(fam, proj, side_team, strike, home, away):
    """Probabilidad del modelo de que el mercado se resuelva SÍ."""
    sd = proj["sd"]
    sign = 1 if side_team == home else (-1 if side_team == away else None)
    if fam in SIDE_FAMS and sign is None:
        return None
    if fam == "GAME":
        return p_over(sign * proj["margin"], 0.0, sd["margin"])
    if fam == "SPREAD":
        return p_over(sign * proj["margin"], strike, sd["margin"])
    if fam == "1HSPREAD":
        return p_over(sign * proj["h1_margin"], strike, sd["h1_margin"])
    if fam == "1QSPREAD":
        return p_over(sign * proj["q1_margin"], strike, sd["q1_margin"])
    if fam == "TOTAL":
        return p_over(proj["total"], strike, sd["total"])
    if fam == "1HTOTAL":
        return p_over(proj["h1_total"], strike, sd["h1_total"])
    if fam == "1QTOTAL":
        return p_over(proj["q1_total"], strike, sd["q1_total"])
    if fam == "TEAMTOTAL":
        mean = proj["home_pts"] if sign == 1 else proj["away_pts"]
        return p_over(mean, strike, sd["team"])
    return None


def evaluate(fam, p_yes, ya, yb):
    """Mejor lado (SI/NO) con edge neto de comisión, o None."""
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


# ---------- línea implícita de Kalshi (cuando no hay casas de apuestas) ----------
def kalshi_line_margin(p_home_mid):
    """Margen local implícito a partir del precio medio del ganador."""
    if p_home_mid is None:
        return None
    return CFG["sd"]["margin"] * ninv(p_home_mid)


def kalshi_line_total(points):
    """points: lista (strike, prob_media). Interpola el strike con 50 %."""
    pts = sorted((s, p) for s, p in points if s is not None and p is not None)
    for (s1, p1), (s2, p2) in zip(pts, pts[1:]):
        if p1 >= 0.5 >= p2 and p1 != p2:
            return s1 + (p1 - 0.5) * (s2 - s1) / (p1 - p2)
    return None


__all__ = ["CFG", "project", "market_prob", "evaluate", "kalshi_line_margin", "kalshi_line_total", "ncdf"]
