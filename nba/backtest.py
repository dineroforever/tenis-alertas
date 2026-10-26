"""Backtest histórico con precios reales de Kalshi (velas horarias) 60 min antes del salto.

Como no hay líneas históricas gratis de Pinnacle/DraftKings, la línea de referencia es la
implícita en Kalshi a esa hora: el backtest mide si el modelo propio AGREGA valor sobre el mercado.

Uso: python -m nba.backtest --desde 2025-11-10 --hasta 2026-06-30
Se puede cortar y retomar: guarda el progreso en data/backtest.json.
"""
import argparse
import math
import os
import time
from datetime import date

from . import common
from . import games as G
from .common import SERIES, candle_quote, find_game_markets, iso_to_dt, kalshi_fee, load, market_side_team, save, strike_of, telegram
from .model import SIDE_FAMS, evaluate, kalshi_line_margin, kalshi_line_total, market_prob, project
from .report import summarize

STAKE = 10.0
LEAD_MIN = 60


def cmid(series, ticker, ts):
    ya, yb = candle_quote(series, ticker, ts)
    if ya is None or yb is None or yb <= 0 or ya - yb > 0.10:
        return None
    return (ya + yb) / 2


def run(desde, hasta, max_secs):
    t0 = time.time()
    games = G.update()
    bt = load("backtest.json", {"done": [], "trades": [], "sin_linea": 0})
    done = set(bt["done"])
    todo = [g for g in games if desde <= date.fromisoformat(g["day"]) <= hasta and g["id"] not in done
            and "T" in g["tip"]]
    print(f"partidos por procesar: {len(todo)}")
    for i, g in enumerate(todo):
        if time.time() - t0 > max_secs:
            print("límite de tiempo; se guarda y se retoma en la próxima corrida")
            break
        day, home, away = date.fromisoformat(g["day"]), g["home"], g["away"]
        prev = G.before(games, g["tip"])
        ts = iso_to_dt(g["tip"]).timestamp() - LEAD_MIN * 60
        mk = find_game_markets(g["id"])
        # línea implícita de Kalshi a T-60
        lm = None
        for m in mk.get("GAME", []):
            if market_side_team(m) == home:
                lm = kalshi_line_margin(cmid(SERIES["GAME"], m["ticker"], ts))
        guess = project(prev, home, away, day)["total"]
        near = [m for m in mk.get("TOTAL", []) if strike_of(m) is not None and abs(strike_of(m) - guess) <= 10]
        lt = kalshi_line_total([(strike_of(m), cmid(SERIES["TOTAL"], m["ticker"], ts)) for m in near])
        bt["done"].append(g["id"])
        if lm is None or lt is None:
            bt["sin_linea"] += 1
            continue
        proj = project(prev, home, away, day, lm, lt)
        for fam, ms in mk.items():
            best = None
            for m in ms:
                strike = strike_of(m)
                if (fam != "GAME" and strike is None) or m.get("result") not in ("yes", "no"):
                    continue
                side_t = market_side_team(m) if fam in SIDE_FAMS else None
                p = market_prob(fam, proj, side_t, strike, home, away)
                if p is None or not (0.25 <= p <= 0.75):
                    continue
                ya, yb = candle_quote(SERIES[fam], m["ticker"], ts)
                ev_ = evaluate(fam, p, ya, yb)
                if ev_ and (best is None or ev_["edge"] > best[1]["edge"]):
                    best = (m, ev_)
            if best:
                m, ev_ = best
                c = max(1, math.floor(STAKE / ev_["price"]))
                win = (m["result"] == "yes") == (ev_["side"] == "SI")
                pnl = (c * (1 - ev_["price"]) if win else -c * ev_["price"]) - kalshi_fee(ev_["price"], c)
                bt["trades"].append({"ticker": m["ticker"], "fam": fam, "game": f"{away} @ {home}", "day": g["day"],
                                     "side": ev_["side"], "price": ev_["price"], "p": ev_["p"], "edge": ev_["edge"],
                                     "contracts": c, "won": win, "pnl": round(pnl, 2), "status": "cerrada"})
        if i % 20 == 0:
            save("backtest.json", bt)
            print(f"{i}/{len(todo)} · ops {len(bt['trades'])}")
    save("backtest.json", bt)
    pend = sum(1 for g in todo if g["id"] not in set(bt["done"]))
    rep = summarize(bt["trades"], f"🧪 NBA BACKTEST Kalshi ({desde} a {hasta}) — {len(bt['done'])} partidos"
                                   + (f", faltan {pend} (volver a correr)" if pend else ""))
    rep += f"\nPartidos sin línea implícita (sin liquidez): {bt['sin_linea']}"
    with open(os.path.join(common.DATA_DIR, "backtest_report.txt"), "w", encoding="utf-8") as f:
        f.write(rep)
    telegram(rep)
    print(rep)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2025-11-10")
    ap.add_argument("--hasta", default="2026-06-30")
    ap.add_argument("--max-mins", type=int, default=320)
    a = ap.parse_args()
    run(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.max_mins * 60)
