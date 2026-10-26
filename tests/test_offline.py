"""Prueba sin internet: simula ESPN y Kalshi con estructuras reales."""
import os
import random
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from nba import common, games as G, model, bot, backtest  # noqa: E402

random.seed(1)
TEAMS = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS", "HOU", "IND", "LAC", "LAL", "MEM",
         "MIA", "MIL", "MIN", "NO", "NY", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SA", "TOR", "UTAH", "WSH"]
STR = {t: random.gauss(0, 5) for t in TEAMS}
NOW = datetime(2025, 12, 10, 23, 15, tzinfo=timezone.utc)

SB, SUMM, KM, CAND = {}, {}, {}, {}
eid = 1000


def make_game(day, h, a, completed=True, tip_utc=None):
    global eid
    eid += 1
    tip = tip_utc or datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
    exp_m = STR[h] - STR[a] + 2
    m = round(random.gauss(exp_m, 12))
    tot = round(random.gauss(228, 18))
    hs, as_ = (tot + m) // 2, (tot - m) // 2
    hq = [hs // 4] * 3 + [hs - 3 * (hs // 4)]
    aq = [as_ // 4] * 3 + [as_ - 3 * (as_ // 4)]
    line = round(exp_m * 2) / 2
    fav = h if line >= 0 else a
    details = f"{fav} -{abs(line)}"
    comp = {"status": {"type": {"state": "post" if completed else "pre", "completed": completed}},
            "competitors": [
                {"homeAway": "home", "team": {"abbreviation": h, "id": h}, "score": str(hs) if completed else "0",
                 "linescores": [{"value": v} for v in hq] if completed else []},
                {"homeAway": "away", "team": {"abbreviation": a, "id": a}, "score": str(as_) if completed else "0",
                 "linescores": [{"value": v} for v in aq] if completed else []}],
            "odds": [{"details": details, "overUnder": 228.5, "spread": -line}]}
    ev = {"id": str(eid), "date": tip.strftime("%Y-%m-%dT%H:%MZ"), "season": {"type": 2}, "competitions": [comp]}

    def box(t, pts):
        return {"team": {"abbreviation": t}, "statistics": [
            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "40-88"},
            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "18-22"},
            {"name": "offensiveRebounds", "displayValue": "10"},
            {"name": "totalTurnovers", "displayValue": "13"}]}
    SUMM[str(eid)] = {"boxscore": {"teams": [box(h, hs), box(a, as_)]},
                      "pickcenter": [{"details": details, "overUnder": 228.5, "spread": -line}],
                      "leaders": [{"team": {"abbreviation": h}, "leaders": [{"name": "pointsPerGame", "leaders": [
                          {"athlete": {"displayName": f"Estrella {h}"}}]}]}]}
    SB.setdefault(day.strftime("%Y%m%d"), []).append(ev)
    # mercados Kalshi
    hk, ak = common.k_abbr(h), common.k_abbr(a)
    suf = day.strftime("%y%b%d").upper() + ak + hk
    st = "finalized" if completed else "active"

    def mk(ser, tick, fl, yes):
        return {"ticker": f"{ser}-{suf}-{tick}", "floor_strike": fl, "status": st,
                "result": ("yes" if yes else "no") if completed else "", "yes_sub_title": tick,
                "yes_ask_dollars": "0.5000", "yes_bid_dollars": "0.4800"}
    KM[f"KXNBAGAME-{suf}"] = [mk("KXNBAGAME", hk, None, m > 0), mk("KXNBAGAME", ak, None, m < 0)]
    KM[f"KXNBASPREAD-{suf}"] = [mk("KXNBASPREAD", f"{hk}{k}", k - 0.5, m > k - 0.5) for k in range(1, 15, 2)]
    KM[f"KXNBATOTAL-{suf}"] = [mk("KXNBATOTAL", str(k), k - 0.5, tot > k - 0.5) for k in range(210, 250, 3)]
    KM[f"KXNBA1QTOTAL-{suf}"] = [mk("KXNBA1QTOTAL", str(k), k - 0.5, hq[0] + aq[0] > k - 0.5) for k in range(50, 64, 2)]
    return ev


def fake_get_json(url, params=None, tries=4, pause=0.0):
    params = params or {}
    if url.endswith("/scoreboard"):
        return {"events": SB.get(params["dates"], [])}
    if url.endswith("/summary"):
        return SUMM.get(params["event"], {})
    if url.endswith("/injuries"):
        return {"injuries": [{"injuries": [{"status": "Out", "athlete": {"displayName": "Alguien"}}]}]}
    if url.endswith("/markets"):
        return {"markets": KM.get(params["event_ticker"], [])}
    if "/candlesticks" in url:
        q = round(random.uniform(0.3, 0.7), 2)
        return {"candlesticks": [{"yes_ask": {"close_dollars": str(q + 0.01)}, "yes_bid": {"close_dollars": str(q)}}]}
    if "/markets/" in url:
        t = url.rsplit("/", 1)[1]
        for ms in KM.values():
            for m in ms:
                if m["ticker"] == t:
                    return {"market": {**m, "status": "finalized", "result": random.choice(["yes", "no"])}}
    return None


def main():
    tmp = tempfile.mkdtemp()
    common.DATA_DIR = tmp
    common.get_json = fake_get_json
    for mod in (G, bot, backtest):
        for n in ("espn_scoreboard", "espn_summary", "espn_injuries", "kalshi_markets", "kalshi_market"):
            pass
    common.now_utc = lambda: NOW
    G.now_utc = bot.now_utc = lambda: NOW
    G.FIRST_DAY = date(2025, 10, 21)
    d = date(2025, 10, 21)
    while d < date(2025, 12, 10):
        ts = TEAMS[:]
        random.shuffle(ts)
        for i in range(0, 14, 2):
            make_game(d, ts[i], ts[i + 1])
        d += timedelta(days=1)
    # partido de hoy por empezar en 45 min, con precios desviados para forzar señal
    ev = make_game(date(2025, 12, 10), "BOS", "WSH", completed=False, tip_utc=NOW + timedelta(minutes=45))
    suf = "25DEC10WASBOS"
    for m in KM[f"KXNBATOTAL-{suf}"]:
        m["yes_ask_dollars"], m["yes_bid_dollars"] = "0.3000", "0.2800"

    gs = G.update()
    assert len(gs) > 300, len(gs)
    assert all(g["hposs"] for g in gs)
    p = model.project(G.before(gs, ev["date"]), "BOS", "WSH", date(2025, 12, 10), None, None)
    print("proyección sin línea:", {k: round(v, 1) for k, v in p.items() if isinstance(v, float)})
    print("sd aprendidas:", {k: round(v, 1) for k, v in p["sd"].items()})

    db = {"trades": [], "scanned": {}, "milestones": []}
    msgs = bot.scan(db, gs)
    print("\n".join(msgs))
    assert db["trades"], "debería generar señales"
    msgs2 = bot.scan(db, gs)
    assert not msgs2, "no debe repetir partido"
    res = bot.resolve(db)
    print("\n".join(res))
    db["trades"] *= 30
    print(bot.milestone_report(db))

    backtest.run(date(2025, 11, 20), date(2025, 12, 9), 60, 600)
    shutil.rmtree(tmp)
    print("\nOK")


if __name__ == "__main__":
    main()
