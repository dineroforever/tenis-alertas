"""Caché de partidos terminados (resultados, posesiones, cuartos y línea DK)."""
from datetime import date, timedelta

from .common import (box_from_summary, daterange, espn_scoreboard, espn_summary, et_date,
                     iso_to_dt, load, now_utc, parse_event, parse_line, save)

FIRST_DAY = date(2025, 10, 21)  # inicio temporada 2025-26 (prior para 2026-27)
FILE = "games.json"


def _record(ev):
    e = parse_event(ev)
    if not e["completed"] or not e["home"] or not e["away"]:
        return None
    summ = espn_summary(e["id"]) or {}
    poss = box_from_summary(summ) or {}
    h, a = e["home"]["abbr"], e["away"]["abbr"]
    pc = (summ.get("pickcenter") or [None])[0]
    lm, lt = parse_line(pc, h, a)
    if lm is None:
        lm, lt = parse_line(e["odds"], h, a)
    return {
        "id": e["id"], "day": et_date(iso_to_dt(e["date"])).isoformat(), "tip": e["date"],
        "home": h, "away": a, "hs": e["home"]["score"], "as": e["away"]["score"],
        "hposs": poss.get(h), "aposs": poss.get(a),
        "hq": e["home"]["lines"], "aq": e["away"]["lines"],
        "line_m": lm, "line_t": lt,
    }


def update(max_days=400):
    db = load(FILE, {"last_day": None, "games": []})
    have = {g["id"] for g in db["games"]}
    start = date.fromisoformat(db["last_day"]) - timedelta(days=1) if db["last_day"] else FIRST_DAY
    end = et_date(now_utc()) - timedelta(days=1)
    n_new = 0
    for i, d in enumerate(daterange(start, end)):
        if i >= max_days:
            break
        for ev in espn_scoreboard(d):
            if ev["id"] in have:
                continue
            # descartar All-Star / pretemporada (season type 2=regular, 3=playoffs, 5=play-in)
            st = (ev.get("season") or {}).get("type")
            if st not in (2, 3, 5, None):
                continue
            r = _record(ev)
            if r:
                db["games"].append(r)
                have.add(r["id"])
                n_new += 1
        db["last_day"] = d.isoformat()
        if n_new and n_new % 100 == 0:
            save(FILE, db)
    db["games"].sort(key=lambda g: g["tip"])
    save(FILE, db)
    print(f"games: +{n_new}, total {len(db['games'])}, hasta {db['last_day']}")
    return db["games"]


def before(games, tip_iso):
    return [g for g in games if g["tip"] < tip_iso]
