"""Estadísticas de resultados (papel y backtest)."""
from collections import defaultdict

MILESTONES = [25, 50, 100, 150, 200, 300, 400, 500]
FAM_ES = {"GAME": "Ganador", "SPREAD": "Hándicap", "TOTAL": "Total partido", "TEAMTOTAL": "Total equipo",
          "1HSPREAD": "Hándicap 1H", "1HTOTAL": "Total 1H", "1QSPREAD": "Hándicap 1Q", "1QTOTAL": "Total 1Q"}


def summarize(trades, title):
    closed = [t for t in trades if t.get("status") == "cerrada"]
    if not closed:
        return f"{title}\nSin operaciones cerradas."
    by = defaultdict(list)
    for t in closed:
        by[t["fam"]].append(t)

    def row(name, ts):
        n = len(ts)
        w = sum(1 for t in ts if t["won"])
        pnl = sum(t["pnl"] for t in ts)
        risk = sum(t["contracts"] * t["price"] for t in ts)
        exp = sum(t["p"] for t in ts)
        roi = 100 * pnl / risk if risk else 0
        icon = "🟢" if roi > 0 else "🔴"
        return (f"{icon} {name}: {n} ops | aciertos {w} ({100*w/n:.0f}%, esperado {100*exp/n:.0f}%) | "
                f"P&L {pnl:+.2f}$ | ROI {roi:+.1f}%")

    lines = [title, row("TOTAL", closed), ""]
    for fam in sorted(by, key=lambda f: -len(by[f])):
        lines.append(row(FAM_ES.get(fam, fam), by[fam]))
    # por tramo de edge
    lines.append("")
    for lo, hi in ((0.04, 0.06), (0.06, 0.09), (0.09, 1)):
        ts = [t for t in closed if lo <= t["edge"] < hi]
        if ts:
            lines.append(row(f"Edge {lo*100:.0f}-{min(hi,0.99)*100:.0f}", ts))
    return "\n".join(lines)


def milestone_report(db):
    n = sum(1 for t in db["trades"] if t.get("status") == "cerrada")
    hit = [m for m in MILESTONES if n >= m and m not in db["milestones"]]
    if not hit:
        return None
    db["milestones"].extend(hit)
    return summarize(db["trades"], f"📊 NBA PAPEL — informe a {max(hit)} operaciones")
