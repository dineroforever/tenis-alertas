"""Backtest histórico con precios reales de Kalshi (velas horarias) 60 min antes del salto.

Uso: python -m nba.backtest --desde 2025-11-05 --hasta 2026-06-30
Se puede cortar y retomar: guarda el progreso en data/backtest.json.
"""
import argparse
import os
import math
import time
from datetime import date

from . import common
from . import games as G
from .common import SERIES, candle_quote, kalshi_fee, find_game_markets, iso_to_dt, k_abbr, load, market_side_team, save, telegram
from .model import evaluate, market_prob, project, strike_of
from .report import summarize

STAKE = 10.0
LEAD_MIN = 60
SIDE_FAMS = ("GAME", "SPREAD", "TEAMTOTAL", "1HSPREAD", "1QSPREAD")


def run(desde, hasta, max_games, max_secs):
    t0 = time.time()
    games = G.update()
    bt = load("backtest.json", {"done": [], "trades": [], "no_markets": 0})
    done = set(bt["done"])
    todo = [g for g in games if desde <= date.fromisoformat(g["day"]) <= hasta and g["id"] not in done
            and g.get("line_m") is not None and g.get("line_t")]
    print(f"partidos por procesar: {len(todo)}")
    for i, g in enumerate(todo[:max_games]):
        if time.time() - t0 > max_secs:
            print("límite de tiempo, se guarda y se retoma en la próxima corrida")
            break
        day = date.fromisoformat(g["day"])
        prev = G.before(games, g["tip"])
        proj = project(prev, g["home"], g["away"], day, g["line_m"], g["line_t"])
        mk = find_game_markets(day, g["away"], g["home"])
        if not mk:
            bt["no_markets"] += 1
        hk, ak = k_abbr(g["home"]), k_abbr(g["away"])
        ts = iso_to_dt(g["tip"]).timestamp() - LEAD_MIN * 60
        for fam, ms in mk.items():
            cands = []
            for m in ms:
                strike = strike_of(m)
                if fam != "GAME" and strike is None:
                    continue
                if m.get("result") not in ("yes", "no"):
                    continue
                side_t = market_side_team(m) if fam in SIDE_FAMS else None
                p = market_prob(fam, proj, side_t, strike, hk, ak)
                if p is not None and 0.25 <= p <= 0.75:
                    cands.append((m, p))
            best = None
            for m, p in cands:
                ya, yb = candle_quote(SERIES[fam], m["ticker"], ts)
                ev_ = evaluate(fam, p, ya, yb)
                if ev_ and (best is None or ev_["edge"] > best[1]["edge"]):
                    best = (m, ev_)
            if best:
                m, ev_ = best
                c = max(1, math.floor(STAKE / ev_["price"]))
                win = (m["result"] == "yes") == (ev_["side"] == "SI")
                pnl = (c * (1 - ev_["price"]) if win else -c * ev_["price"])
                pnl -= kalshi_fee(ev_["price"], c)
                bt["trades"].append({"ticker": m["ticker"], "fam": fam, "game": f"{g['away']} @ {g['home']}",
                                     "day": g["day"], "side": ev_["side"], "price": ev_["price"], "p": ev_["p"],
                                     "edge": ev_["edge"], "contracts": c, "won": win, "pnl": round(pnl, 2),
                                     "status": "cerrada"})
        bt["done"].append(g["id"])
        if i % 20 == 0:
            save("backtest.json", bt)
            print(f"{i}/{len(todo)} · ops {len(bt['trades'])}")
    save("backtest.json", bt)
    remaining = len(todo) - len([1 for g in todo if g["id"] in set(bt["done"])])
    rep = summarize(bt["trades"], f"🧪 NBA BACKTEST Kalshi ({desde} a {hasta}) — {len(bt['done'])} partidos"
                                   + (f", faltan {remaining}" if remaining > 0 else ""))
    save("backtest_report.json", {"report": rep})
    with open(os.path.join(common.DATA_DIR, "backtest_report.txt"), "w", encoding="utf-8") as f:
        f.write(rep)
    telegram(rep)
    print(rep)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2025-11-05")
    ap.add_argument("--hasta", default="2026-06-30")
    ap.add_argument("--max-games", type=int, default=5000)
    ap.add_argument("--max-mins", type=int, default=320)
    a = ap.parse_args()
    run(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.max_games, a.max_mins * 60)
