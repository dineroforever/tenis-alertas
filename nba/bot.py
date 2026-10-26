"""Bot NBA en PAPEL para Kalshi: escanea antes del salto inicial, registra señales,
liquida resultados y envía todo a Telegram. No envía órdenes reales."""
import math
from datetime import timedelta

from . import games as G
from .common import (ET, espn_injuries, espn_scoreboard, espn_summary, et_date, find_game_markets,
                     iso_to_dt, k_abbr, kalshi_fee, kalshi_market, load, market_side_team, now_utc,
                     parse_event, parse_line, quotes, save, telegram)
from .model import evaluate, market_prob, project, strike_of
from .report import milestone_report

STAKE = 10.0                # $ ficticios por operación
WINDOW_MIN = (15, 100)      # escanear partidos que empiezan entre 15 y 100 min
DOUBT = ("day-to-day", "questionable", "doubtful", "game-time", "gtd")
FAM_ES = {"GAME": "Ganador", "SPREAD": "Hándicap", "TOTAL": "Total partido", "TEAMTOTAL": "Total equipo",
          "1HSPREAD": "Hándicap 1.ª mitad", "1HTOTAL": "Total 1.ª mitad",
          "1QSPREAD": "Hándicap 1.er cuarto", "1QTOTAL": "Total 1.er cuarto"}


def leaders_by_team(summ):
    out = {}
    for t in (summ or {}).get("leaders", []) or []:
        abbr = (t.get("team") or {}).get("abbreviation")
        names = set()
        for cat in t.get("leaders", []) or []:
            for l in (cat.get("leaders") or [])[:1]:
                n = (l.get("athlete") or {}).get("displayName")
                if n:
                    names.add(n)
        if abbr:
            out[abbr] = names
    return out


def settle(tr, result):
    win = (result == "yes") == (tr["side"] == "SI")
    c, p = tr["contracts"], tr["price"]
    fee = kalshi_fee(p, c)
    tr["pnl"] = round((c * (1 - p) if win else -c * p) - fee, 2)
    tr["won"] = win
    tr["status"] = "cerrada"


def resolve(db):
    msgs = []
    for tr in db["trades"]:
        if tr["status"] != "abierta":
            continue
        m = kalshi_market(tr["ticker"])
        if not m:
            continue
        if m.get("status") in ("finalized", "settled") and m.get("result") in ("yes", "no"):
            settle(tr, m["result"])
            icon = "✅" if tr["won"] else "❌"
            msgs.append(f"{icon} {tr['game']} · {FAM_ES[tr['fam']]} · {tr['label']} ({tr['side']}) → {tr['pnl']:+.2f}$")
    return msgs


def scan(db, games):
    now = now_utc()
    inj = None
    out_msgs = []
    days = {et_date(now), et_date(now + timedelta(hours=3))}
    for d in sorted(days):
        for ev in espn_scoreboard(d):
            e = parse_event(ev)
            if e["state"] != "pre" or e["id"] in db["scanned"]:
                continue
            tip = iso_to_dt(e["date"])
            mins = (tip - now).total_seconds() / 60
            if not (WINDOW_MIN[0] <= mins <= WINDOW_MIN[1]):
                continue
            db["scanned"][e["id"]] = now.isoformat()
            h, a = e["home"]["abbr"], e["away"]["abbr"]
            gname = f"{a} @ {h}"
            line_m, line_t = parse_line(e["odds"], h, a)
            if line_m is None or line_t is None:
                out_msgs.append(f"⚪ {gname}: sin línea DraftKings → no se analiza")
                continue
            # Variable 1: plantilla — si un líder del equipo está en duda, no se opera
            if inj is None:
                inj = espn_injuries()
            lead = leaders_by_team(espn_summary(e["id"]))
            doubt = [n for t in (h, a) for n in lead.get(t, set())
                     if any(x in inj.get(n, "") for x in DOUBT)]
            if doubt:
                out_msgs.append(f"⚪ {gname}: jugador clave en duda ({', '.join(doubt)}) → no se opera")
                continue
            prev = G.before(games, e["date"])
            proj = project(prev, h, a, et_date(tip), line_m, line_t)
            mk = find_game_markets(et_date(tip), a, h)
            hk, ak = k_abbr(h), k_abbr(a)
            sigs = []
            for fam, ms in mk.items():
                best = None
                for m in ms:
                    if m.get("status") not in ("active", "open"):
                        continue
                    strike = strike_of(m)
                    if fam != "GAME" and strike is None:
                        continue
                    side_t = market_side_team(m) if fam in ("GAME", "SPREAD", "TEAMTOTAL", "1HSPREAD", "1QSPREAD") else None
                    p = market_prob(fam, proj, side_t, strike, hk, ak)
                    ya, yb = quotes(m)
                    ev_ = evaluate(fam, p, ya, yb)
                    if ev_ and (best is None or ev_["edge"] > best[1]["edge"]):
                        best = (m, ev_)
                if best:
                    m, ev_ = best
                    c = max(1, math.floor(STAKE / ev_["price"]))
                    tr = {"id": f"{m['ticker']}|{ev_['side']}", "ticker": m["ticker"], "fam": fam,
                          "game": gname, "event_id": e["id"], "tip": e["date"],
                          "label": m.get("yes_sub_title") or m.get("title"), "side": ev_["side"],
                          "price": ev_["price"], "p": ev_["p"], "edge": ev_["edge"], "contracts": c,
                          "status": "abierta", "opened": now.isoformat(),
                          "proj": {k: round(proj[k], 1) for k in ("margin", "total", "h1_total", "q1_total")}}
                    if tr["id"] not in {t["id"] for t in db["trades"]}:
                        db["trades"].append(tr)
                        sigs.append(tr)
            hora = tip.astimezone(ET).strftime("%I:%M %p ET")
            head = (f"🏀 {gname} · {hora}\n"
                    f"Línea DK: {h} {-line_m:+.1f} | Total {line_t:.1f}\n"
                    f"Modelo: margen {h} {proj['margin']:+.1f} | total {proj['total']:.1f} | "
                    f"pace {proj['poss']:.1f}" + (" | B2B " + (h if proj['b2b_home'] else a)
                                                   if proj['b2b_home'] or proj['b2b_away'] else ""))
            if not sigs:
                out_msgs.append(head + "\n⚪ Sin edge → NO APOSTAR")
                continue
            lines = [head]
            for s in sigs:
                ev100 = 100 * (s["p"] / s["price"] - 1)
                lines.append(f"🟢 PAPEL {FAM_ES[s['fam']]}: {s['label']} → {s['side']} a {s['price']*100:.0f}¢ | "
                             f"modelo {s['p']*100:.0f}% | edge {s['edge']*100:+.1f} | EV/$100 {ev100:+.0f}")
            out_msgs.append("\n".join(lines))
    return out_msgs


def main():
    db = load("paper.json", {"trades": [], "scanned": {}, "milestones": []})
    games = G.update(max_secs=900)
    msgs = resolve(db)
    if msgs:
        telegram("🏀 NBA PAPEL — resultados\n" + "\n".join(msgs))
    sm = scan(db, games)
    for m in sm:
        telegram(m)
    rep = milestone_report(db)
    if rep:
        telegram(rep)
    # limpiar escaneados viejos (>3 días)
    cutoff = (now_utc() - timedelta(days=3)).isoformat()
    db["scanned"] = {k: v for k, v in db["scanned"].items() if v > cutoff}
    save("paper.json", db)


if __name__ == "__main__":
    main()
