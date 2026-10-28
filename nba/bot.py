"""Bot NBA en PAPEL para Kalshi: escanea antes del salto inicial, registra señales,
liquida resultados y envía todo a Telegram. No envía órdenes reales."""
import math
from datetime import timedelta

from . import games as G
from .common import (ET, SERIES, et_date, find_game_markets, kalshi_events, kalshi_fee, kalshi_market,
                     kalshi_markets, load, market_side_team, now_utc, parse_suffix, quotes, save, strike_of,
                     telegram, tip_time)
from .model import SIDE_FAMS, evaluate, kalshi_line_margin, kalshi_line_total, market_prob, project
from .odds import fetch_lines, pick_line
from .report import milestone_report

STAKE = 10.0                # $ ficticios por operación
WINDOW_MIN = (15, 100)      # escanear partidos que empiezan entre 15 y 100 min
FAM_ES = {"GAME": "Ganador", "SPREAD": "Hándicap", "TOTAL": "Total partido", "TEAMTOTAL": "Total equipo",
          "1HSPREAD": "Hándicap 1.ª mitad", "1HTOTAL": "Total 1.ª mitad",
          "1QSPREAD": "Hándicap 1.er cuarto", "1QTOTAL": "Total 1.er cuarto"}
FAM_OFF = {"1HSPREAD", "1HTOTAL", "1QSPREAD", "1QTOTAL"}  # mercados que NO se operan


def mid(m):
    ya, yb = quotes(m)
    if ya is None or yb is None or yb <= 0 or ya - yb > 0.10:
        return None
    return (ya + yb) / 2


def kalshi_implied(mk, home):
    lm = lt = None
    for m in mk.get("GAME", []):
        if market_side_team(m) == home:
            lm = kalshi_line_margin(mid(m))
    lt = kalshi_line_total([(strike_of(m), mid(m)) for m in mk.get("TOTAL", [])])
    return lm, lt


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
        if m and m.get("status") in ("finalized", "settled") and m.get("result") in ("yes", "no"):
            settle(tr, m["result"])
            icon = "✅" if tr["won"] else "❌"
            msgs.append(f"{icon} {tr['game']} · {FAM_ES[tr['fam']]} · {tr['label']} ({tr['side']}) → {tr['pnl']:+.2f}$")
    return msgs


def upcoming(now):
    """Partidos de Kalshi que empiezan dentro de la ventana."""
    out = []
    days = {et_date(now), et_date(now + timedelta(hours=3))}
    for ev in kalshi_events(SERIES["GAME"], "open", max_pages=3):
        suf = ev["event_ticker"].split("-", 1)[1]
        p = parse_suffix(suf)
        if not p or p[0] not in days:
            continue
        ms = kalshi_markets(ev["event_ticker"], historical_ok=False)
        tip = tip_time(ms[0]) if ms else None
        if not tip:
            continue
        mins = (tip - now).total_seconds() / 60
        if WINDOW_MIN[0] <= mins <= WINDOW_MIN[1]:
            out.append((suf, p[0], p[1], p[2], tip))
    return out


def scan(db, games):
    now = now_utc()
    cand = [c for c in upcoming(now) if c[0] not in db["scanned"]]
    if not cand:
        return []
    lines = fetch_lines()
    out_msgs = []
    for suf, day, away, home, tip in cand:
        db["scanned"][suf] = now.isoformat()
        gname = f"{away} @ {home}"
        mk = find_game_markets(suf)
        books = (lines or {}).get((home, away))
        line_m, line_t, alert = pick_line(books)
        src = "Pinnacle" if books and "pinnacle" in books else ("DraftKings" if books else "Kalshi")
        if alert:
            out_msgs.append(f"⚪ {gname}: Pinnacle y DraftKings discrepan (posible baja de último minuto) → no se opera")
            continue
        if line_m is None or line_t is None:
            line_m, line_t = kalshi_implied(mk, home)
        if line_m is None or line_t is None:
            out_msgs.append(f"⚪ {gname}: sin línea de referencia → no se analiza")
            continue
        proj = project(G.before(games, tip.strftime("%Y-%m-%dT%H:%MZ")), home, away, day, line_m, line_t)
        sigs = []
        for fam, ms in mk.items():
            if fam in FAM_OFF:  # desactivados por backtest negativo (1.ª mitad y 1.er cuarto)
                continue
            best = None
            for m in ms:
                if m.get("status") not in ("active", "open"):
                    continue
                strike = strike_of(m)
                if fam != "GAME" and strike is None:
                    continue
                side_t = market_side_team(m) if fam in SIDE_FAMS else None
                p = market_prob(fam, proj, side_t, strike, home, away)
                ya, yb = quotes(m)
                ev_ = evaluate(fam, p, ya, yb)
                if ev_ and (best is None or ev_["edge"] > best[1]["edge"]):
                    best = (m, ev_)
            if best:
                m, ev_ = best
                c = max(1, math.floor(STAKE / ev_["price"]))
                tr = {"id": f"{m['ticker']}|{ev_['side']}", "ticker": m["ticker"], "fam": fam, "game": gname,
                      "tip": tip.isoformat(), "label": m.get("yes_sub_title") or m.get("title"),
                      "side": ev_["side"], "price": ev_["price"], "p": ev_["p"], "edge": ev_["edge"],
                      "contracts": c, "status": "abierta", "opened": now.isoformat(), "line_src": src,
                      "proj": {k: round(proj[k], 1) for k in ("margin", "total", "h1_total", "q1_total")}}
                if tr["id"] not in {t["id"] for t in db["trades"]}:
                    db["trades"].append(tr)
                    sigs.append(tr)
        hora = tip.astimezone(ET).strftime("%I:%M %p ET")
        b2b = [t for t, f in ((home, proj["b2b_home"]), (away, proj["b2b_away"])) if f]
        head = (f"🏀 {gname} · {hora}\n"
                f"Línea {src}: {home} {-line_m:+.1f} | Total {line_t:.1f}\n"
                f"Modelo: margen {home} {proj['margin']:+.1f} | total {proj['total']:.1f}"
                + (f" | B2B {', '.join(b2b)}" if b2b else ""))
        if not sigs:
            out_msgs.append(head + "\n⚪ Sin edge → NO APOSTAR")
            continue
        lines_out = [head]
        for s in sigs:
            ev100 = 100 * (s["p"] / s["price"] - 1)
            lines_out.append(f"🟢 PAPEL {FAM_ES[s['fam']]}: {s['label']} → {s['side']} a {s['price']*100:.0f}¢ | "
                             f"modelo {s['p']*100:.0f}% | edge {s['edge']*100:+.1f} | EV/$100 {ev100:+.0f}")
        out_msgs.append("\n".join(lines_out))
    return out_msgs


def main():
    db = load("paper.json", {"trades": [], "scanned": {}, "milestones": []})
    games = G.update(max_secs=900)
    msgs = resolve(db)
    if msgs:
        telegram("🏀 NBA PAPEL — resultados\n" + "\n".join(msgs))
    for m in scan(db, games):
        telegram(m)
    rep = milestone_report(db)
    if rep:
        telegram(rep)
    cutoff = (now_utc() - timedelta(days=3)).isoformat()
    db["scanned"] = {k: v for k, v in db["scanned"].items() if v > cutoff}
    save("paper.json", db)


if __name__ == "__main__":
    main()
