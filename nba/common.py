"""Utilidades comunes: HTTP, ESPN, Kalshi, Telegram, matemáticas."""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# ESPN -> Kalshi (solo los que difieren)
ESPN_TO_KALSHI = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
KALSHI_TO_ESPN = {v: k for k, v in ESPN_TO_KALSHI.items()}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "nba-paper-bot/1.0"


def k_abbr(espn_abbr):
    return ESPN_TO_KALSHI.get(espn_abbr, espn_abbr)


def get_json(url, params=None, tries=4, pause=0.0):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if pause:
                time.sleep(pause)
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1 + 2 * i)
    return None


# ---------------- archivos ----------------
def load(name, default):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------- matemáticas ----------------
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_over(mean, line, sd):
    """P(X > line) con X ~ Normal(mean, sd)."""
    return 1 - ncdf((line - mean) / sd)


def kalshi_fee(price, contracts=1):
    """Comisión taker de Kalshi en $ (redondeo hacia arriba al centavo)."""
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------- ESPN ----------------
def espn_scoreboard(day):
    """day: date (fecha ET). Devuelve lista de eventos."""
    j = get_json(f"{ESPN}/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 50})
    return (j or {}).get("events", [])


def espn_summary(event_id):
    return get_json(f"{ESPN}/summary", {"event": event_id})


def espn_injuries():
    j = get_json(f"{ESPN}/injuries") or {}
    out = {}
    for team in j.get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {}) or {}
            name = ath.get("displayName")
            if name:
                out[name] = (inj.get("status") or "").lower()
    return out


def parse_event(ev):
    """Resumen simple de un evento ESPN."""
    c = ev["competitions"][0]
    teams = {}
    for t in c["competitors"]:
        teams[t["homeAway"]] = {
            "abbr": t["team"]["abbreviation"],
            "id": t["team"]["id"],
            "score": fnum(t.get("score")),
            "lines": [fnum(l.get("value")) for l in (t.get("linescores") or [])],
        }
    odds = (c.get("odds") or [None])[0]
    return {
        "id": ev["id"],
        "date": ev["date"],
        "state": c["status"]["type"]["state"],  # pre / in / post
        "completed": c["status"]["type"].get("completed", False),
        "home": teams.get("home"),
        "away": teams.get("away"),
        "odds": odds,
        "competition": c,
    }


def parse_line(odds_obj, home_abbr, away_abbr):
    """Devuelve (margen_esperado_local, total) desde un objeto de odds de ESPN."""
    if not odds_obj:
        return None, None
    total = fnum(odds_obj.get("overUnder"))
    details = (odds_obj.get("details") or "").strip()
    margin_home = None
    if details.upper().startswith("EVEN") or details.upper() == "PK":
        margin_home = 0.0
    elif details:
        parts = details.split()
        if len(parts) == 2:
            fav, val = parts[0], fnum(parts[1])
            if val is not None:
                if fav == home_abbr:
                    margin_home = -val
                elif fav == away_abbr:
                    margin_home = val
    if margin_home is None:
        sp = fnum(odds_obj.get("spread"))
        if sp is not None:
            margin_home = -sp
    return margin_home, total


def box_from_summary(summ):
    """Posesiones y puntos por equipo desde el box score de ESPN."""
    out = {}
    try:
        for t in summ["boxscore"]["teams"]:
            st = {s["name"]: s.get("displayValue") for s in t["statistics"]}
            fga = fnum(st["fieldGoalsMade-fieldGoalsAttempted"].split("-")[1])
            fta = fnum(st["freeThrowsMade-freeThrowsAttempted"].split("-")[1])
            orb = fnum(st.get("offensiveRebounds"))
            tov = fnum(st.get("totalTurnovers") or st.get("turnovers"))
            out[t["team"]["abbreviation"]] = fga - orb + tov + 0.44 * fta
    except (KeyError, TypeError, AttributeError, IndexError):
        return None
    return out


# ---------------- Kalshi ----------------
SERIES = {
    "GAME": "KXNBAGAME",
    "SPREAD": "KXNBASPREAD",
    "TOTAL": "KXNBATOTAL",
    "TEAMTOTAL": "KXNBATEAMTOTAL",
    "1HSPREAD": "KXNBA1HSPREAD",
    "1HTOTAL": "KXNBA1HTOTAL",
    "1QSPREAD": "KXNBA1QSPREAD",
    "1QTOTAL": "KXNBA1QTOTAL",
}


def event_suffixes(game_date_et, away_k, home_k):
    d = game_date_et.strftime("%y%b%d").upper()
    return [f"{d}{away_k}{home_k}", f"{d}{home_k}{away_k}"]


def kalshi_markets(event_ticker):
    j = get_json(f"{KALSHI}/markets", {"event_ticker": event_ticker, "limit": 200}, pause=0.05)
    return (j or {}).get("markets", [])


def kalshi_market(ticker):
    j = get_json(f"{KALSHI}/markets/{ticker}", pause=0.05)
    return (j or {}).get("market")


def find_game_markets(game_date_et, away_espn, home_espn):
    """Devuelve {familia: [mercados]} para un partido."""
    away_k, home_k = k_abbr(away_espn), k_abbr(home_espn)
    sufs = event_suffixes(game_date_et, away_k, home_k)
    found = {}
    suffix_ok = None
    for fam, ser in SERIES.items():
        cands = [suffix_ok] if suffix_ok else sufs
        for s in cands:
            ms = kalshi_markets(f"{ser}-{s}")
            if ms:
                found[fam] = ms
                suffix_ok = s
                break
    return found


def market_side_team(m):
    """Para spreads/team totals/ganador: abreviatura Kalshi del equipo del lado YES."""
    suf = m["ticker"].split("-")[-1]
    letters = "".join(ch for ch in suf if ch.isalpha())
    return letters or None


def quotes(m):
    ya = fnum(m.get("yes_ask_dollars"))
    yb = fnum(m.get("yes_bid_dollars"))
    if ya is None and m.get("yes_ask") is not None:
        ya = m["yes_ask"] / 100
    if yb is None and m.get("yes_bid") is not None:
        yb = m["yes_bid"] / 100
    return ya, yb


def candle_quote(series, ticker, ts):
    """Precio ask/bid YES en la última vela horaria cerrada antes de ts (epoch)."""
    j = get_json(
        f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": int(ts - 6 * 3600), "end_ts": int(ts), "period_interval": 60},
        pause=0.05,
    )
    cs = (j or {}).get("candlesticks") or []
    for c in reversed(cs):
        ya = fnum((c.get("yes_ask") or {}).get("close_dollars"))
        yb = fnum((c.get("yes_bid") or {}).get("close_dollars"))
        if ya is not None and yb is not None and 0 < ya < 1:
            return ya, yb
    return None, None


# ---------------- Telegram ----------------
def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram desactivado]\n" + text)
        return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        try:
            SESSION.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": chunk, "disable_web_page_preview": "true"},
                timeout=20,
            )
        except requests.RequestException as e:
            print("telegram error", e)


def now_utc():
    return datetime.now(timezone.utc)


def iso_to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def et_date(dt_utc):
    return dt_utc.astimezone(ET).date()


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)
