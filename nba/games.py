"""Caché de partidos terminados, reconstruidos desde los mercados liquidados de Kalshi.

Puntos de cada equipo = cruce de los mercados de total y hándicap liquidados
(precisión ±1-2 pts, suficiente para ratings). También guarda el total de la
1.ª mitad y del 1.er cuarto cuando Kalshi tuvo esos mercados.
"""
import time

from .common import (SERIES, kalshi_events, kalshi_markets, load, market_side_team, parse_suffix, save,
                     strike_of, tip_time)

FILE = "games.json"


def _bounds(constraints):
    """constraints: lista de (strike, valor_supera_strike: bool). Devuelve estimación central."""
    lo, hi = -1e9, 1e9
    for x, over in constraints:
        if over:
            lo = max(lo, x)
        else:
            hi = min(hi, x)
    if lo > -1e9 and hi < 1e9 and lo <= hi:
        return (lo + hi) / 2
    if lo > -1e9 and hi >= 1e9:
        return lo + 1.5
    if hi < 1e9 and lo <= -1e9:
        return hi - 1.5
    return None


def total_from(ms):
    cons = [(strike_of(m), m["result"] == "yes") for m in ms
            if m.get("result") in ("yes", "no") and strike_of(m) is not None]
    return _bounds(cons)


def margin_from(ms, home, away):
    cons = []
    for m in ms:
        if m.get("result") not in ("yes", "no") or strike_of(m) is None:
            continue
        x, yes, side = strike_of(m), m["result"] == "yes", market_side_team(m)
        if side == home:
            cons.append((x, yes))           # margen_local > x
        elif side == away:
            cons.append((-x, not yes))      # margen_local < -x  <=> no supera -x
    return _bounds(cons)


def _record(suffix, have_1h, have_1q):
    p = parse_suffix(suffix)
    if not p:
        return None
    day, away, home = p
    tot_ms = kalshi_markets(f"{SERIES['TOTAL']}-{suffix}")
    spr_ms = kalshi_markets(f"{SERIES['SPREAD']}-{suffix}")
    total = total_from(tot_ms)
    margin = margin_from(spr_ms, home, away)
    if total is None or margin is None:
        return None
    tip = tip_time(tot_ms[0]) if tot_ms else None
    rec = {"id": suffix, "day": day.isoformat(), "tip": tip.strftime("%Y-%m-%dT%H:%MZ") if tip else day.isoformat(),
           "home": home, "away": away, "hs": round((total + margin) / 2, 1),
           "as": round((total - margin) / 2, 1), "h1t": None, "q1t": None}
    if suffix in have_1h:
        rec["h1t"] = total_from(kalshi_markets(f"{SERIES['1HTOTAL']}-{suffix}"))
    if suffix in have_1q:
        rec["q1t"] = total_from(kalshi_markets(f"{SERIES['1QTOTAL']}-{suffix}"))
    return rec


def update(max_secs=None):
    t0 = time.time()
    db = load(FILE, {"games": [], "skip": []})
    have = {g["id"] for g in db["games"]} | set(db.get("skip", []))
    sufs = lambda ser: {e["event_ticker"].split("-", 1)[1] for e in kalshi_events(ser, "settled")}
    totals = sufs(SERIES["TOTAL"])
    new = sorted(s for s in totals - have if parse_suffix(s))
    if not new:
        print(f"games: sin novedades, total {len(db['games'])}")
        return db["games"]
    have_1h, have_1q = sufs(SERIES["1HTOTAL"]), sufs(SERIES["1QTOTAL"])
    n = 0
    for s in new:
        if max_secs and time.time() - t0 > max_secs:
            print("límite de tiempo en la carga de partidos; se continúa en la próxima corrida")
            break
        r = _record(s, have_1h, have_1q)
        if r:
            db["games"].append(r)
            n += 1
        else:
            db.setdefault("skip", []).append(s)
        if n and n % 100 == 0:
            db["games"].sort(key=lambda g: g["tip"])
            save(FILE, db)
    db["games"].sort(key=lambda g: g["tip"])
    save(FILE, db)
    print(f"games: +{n}, total {len(db['games'])}")
    return db["games"]


def before(games, tip_iso):
    return [g for g in games if g["tip"] < tip_iso]
