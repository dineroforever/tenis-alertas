"""Líneas de Pinnacle y DraftKings vía The Odds API (clave gratuita: ODDS_API_KEY).
Cada consulta de cuotas cuesta 2 créditos (spreads + totals); el plan gratis trae 500/mes."""
import os

from .common import NAME_TO_ABBR, get_json

URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"


def fetch_lines():
    """Devuelve {(local, visitante): {"pinnacle": (margen_local, total), "draftkings": (...)}}."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return None
    j = get_json(URL, {"apiKey": key, "markets": "spreads,totals", "bookmakers": "pinnacle,draftkings",
                       "oddsFormat": "decimal"}, tries=2)
    if j is None:
        return None
    out = {}
    for ev in j:
        h, a = NAME_TO_ABBR.get(ev.get("home_team")), NAME_TO_ABBR.get(ev.get("away_team"))
        if not h or not a:
            continue
        books = {}
        for bk in ev.get("bookmakers", []):
            m_home = t = None
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    if mk["key"] == "spreads" and o.get("name") == ev["home_team"] and o.get("point") is not None:
                        m_home = -float(o["point"])
                    if mk["key"] == "totals" and o.get("name") == "Over" and o.get("point") is not None:
                        t = float(o["point"])
            if m_home is not None and t is not None:
                books[bk["key"]] = (m_home, t)
        if books:
            out[(h, a)] = books
    return out


def pick_line(books):
    """Línea a usar + bandera de discrepancia (posible noticia de plantilla)."""
    if not books:
        return None, None, False
    pin, dk = books.get("pinnacle"), books.get("draftkings")
    ref = pin or dk
    alert = bool(pin and dk and (abs(pin[0] - dk[0]) > 1.5 or abs(pin[1] - dk[1]) > 3))
    return ref[0], ref[1], alert
