"""Prueba sin internet: simula Kalshi y The Odds API con sus estructuras reales."""
import os
import random
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from nba import backtest, bot, common, games as G, model, odds  # noqa: E402

random.seed(1)
TEAMS = list(common.TEAMS)
STR = {t: random.gauss(0, 5) for t in TEAMS}
NOW = datetime(2025, 12, 10, 23, 15, tzinfo=timezone.utc)
EVENTS, KM = {}, {}


def add_game(day, home, away, completed=True, tip=None):
    tip = tip or datetime(day.year, day.month, day.day, 23, 30, tzinfo=timezone.utc)
    suf = common.make_suffix(day, away, home)
    m = round(random.gauss(STR[home] - STR[away] + 2, 12)) or 1
    tot = round(random.gauss(228, 18))
    st = "finalized" if completed else "active"
    exp = (tip + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def mk(ser, tick, fl, yes):
        return {"ticker": f"{ser}-{suf}-{tick}", "floor_strike": fl, "status": st,
                "result": ("yes" if yes else "no") if completed else "", "yes_sub_title": tick,
                "yes_ask_dollars": "0.5000", "yes_bid_dollars": "0.4800", "expected_expiration_time": exp}
    KM[f"KXNBAGAME-{suf}"] = [mk("KXNBAGAME", home, None, m > 0), mk("KXNBAGAME", away, None, m < 0)]
    KM[f"KXNBASPREAD-{suf}"] = ([mk("KXNBASPREAD", f"{home}{k}", k - 0.5, m > k - 0.5) for k in range(2, 25, 2)]
                                + [mk("KXNBASPREAD", f"{away}{k}", k - 0.5, -m > k - 0.5) for k in range(2, 25, 2)])
    KM[f"KXNBATOTAL-{suf}"] = [mk("KXNBATOTAL", str(k), k - 0.5, tot > k - 0.5) for k in range(195, 265, 3)]
    h1 = round(tot * 0.5 + random.gauss(0, 6))
    KM[f"KXNBA1HTOTAL-{suf}"] = [mk("KXNBA1HTOTAL", str(k), k - 0.5, h1 > k - 0.5) for k in range(95, 135, 2)]
    for ser in ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1HTOTAL"):
        EVENTS.setdefault((ser, "settled" if completed else "open"), []).append({"event_ticker": f"{ser}-{suf}"})
    return suf


def fake_get_json(url, params=None, tries=4, pause=0.0, headers=None):
    params = params or {}
    if url.endswith("/events"):
        evs = EVENTS.get((params["series_ticker"], params.get("status")), [])
        start = int(params.get("cursor") or 0)
        nxt = start + 200
        return {"events": evs[start:nxt], "cursor": str(nxt) if nxt < len(evs) else ""}
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
    if "the-odds-api" in url:
        return [{"home_team": "Boston Celtics", "away_team": "Washington Wizards", "bookmakers": [
            {"key": "pinnacle", "markets": [
                {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "point": -7.5},
                                                {"name": "Washington Wizards", "point": 7.5}]},
                {"key": "totals", "outcomes": [{"name": "Over", "point": 228.5}, {"name": "Under", "point": 228.5}]}]}]}]
    return None


def main():
    tmp = tempfile.mkdtemp()
    common.DATA_DIR = tmp
    common.get_json = odds.get_json = fake_get_json
    G.kalshi_events = bot.kalshi_events = common.kalshi_events
    bot.now_utc = lambda: NOW
    d = date(2025, 10, 21)
    while d < date(2025, 12, 10):
        ts = TEAMS[:]
        random.shuffle(ts)
        for i in range(0, 14, 2):
            add_game(d, ts[i], ts[i + 1])
        d += timedelta(days=1)
    suf = add_game(date(2025, 12, 10), "BOS", "WAS", completed=False, tip=NOW + timedelta(minutes=45))
    for m in KM[f"KXNBATOTAL-{suf}"]:
        if m["floor_strike"] > 240:
            m["yes_ask_dollars"], m["yes_bid_dollars"] = "0.3200", "0.3000"

    gs = G.update()
    assert len(gs) > 300, len(gs)
    g0 = gs[0]
    print("ejemplo partido reconstruido:", g0)
    p = model.project(G.before(gs, "2025-12-10T23:00Z"), "BOS", "WAS", date(2025, 12, 10))
    print("proyección sin línea:", {k: round(v, 1) for k, v in p.items() if isinstance(v, float)})

    os.environ["ODDS_API_KEY"] = "prueba"
    db = {"trades": [], "scanned": {}, "milestones": []}
    msgs = bot.scan(db, gs)
    print("\n".join(msgs))
    assert "Pinnacle" in msgs[0]
    assert not bot.scan(db, gs), "no debe repetir partido"
    del os.environ["ODDS_API_KEY"]
    print("\n".join(bot.resolve(db)))
    db["trades"] = (db["trades"] or [{"fam": "TOTAL", "status": "cerrada", "won": True, "pnl": 5, "contracts": 10,
                                      "price": 0.5, "p": 0.55, "edge": 0.05}]) * 30
    for t in db["trades"]:
        t.setdefault("status", "cerrada")
    print(bot.milestone_report(db))

    backtest.run(date(2025, 11, 20), date(2025, 12, 9), 600)
    shutil.rmtree(tmp)
    print("\nOK")


if __name__ == "__main__":
    main()
