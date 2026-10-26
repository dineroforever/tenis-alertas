"""Utilidades comunes: HTTP, Kalshi, Telegram, matemáticas."""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "nba-paper-bot/1.0"
_ERRORS = {"n": 0}

# Abreviaturas de Kalshi (iguales a las oficiales NBA) -> nombre completo (The Odds API)
TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns", "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors", "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}
NAME_TO_ABBR = {v: k for k, v in TEAMS.items()}
NAME_TO_ABBR["LA Clippers"] = "LAC"


def get_json(url, params=None, tries=4, pause=0.0, headers=None):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=20, headers=headers)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if pause:
                time.sleep(pause)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if _ERRORS["n"] < 15:
                _ERRORS["n"] += 1
                safe = {k: v for k, v in (params or {}).items() if "key" not in k.lower()}
                print(f"[http] fallo {url} {safe} -> {str(e).split('?')[0]}")
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


def ninv(p):
    """Inversa de la normal estándar (bisección)."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if ncdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


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


def parse_suffix(suffix):
    """'26OCT24GSWPHX' -> (date, away, home). Kalshi usa VISITANTE+LOCAL."""
    d = datetime.strptime(suffix[:7].title(), "%y%b%d").date()
    teams = suffix[7:]
    if len(teams) != 6:
        return None
    return d, teams[:3], teams[3:]


def make_suffix(day, away, home):
    return day.strftime("%y%b%d").upper() + away + home


def kalshi_events(series, status=None, max_pages=50):
    out, cur = [], None
    for _ in range(max_pages):
        params = {"series_ticker": series, "limit": 200}
        if status:
            params["status"] = status
        if cur:
            params["cursor"] = cur
        j = get_json(f"{KALSHI}/events", params, pause=0.05) or {}
        evs = j.get("events", [])
        out.extend(evs)
        cur = j.get("cursor")
        if not cur or not evs:
            break
    return out


def kalshi_markets(event_ticker, historical_ok=True):
    """Mercados de un evento. Los liquidados antes del corte de Kalshi están en /historical."""
    j = get_json(f"{KALSHI}/markets", {"event_ticker": event_ticker, "limit": 200}, pause=0.05)
    ms = (j or {}).get("markets", [])
    if not ms and historical_ok:
        j = get_json(f"{KALSHI}/historical/markets", {"event_ticker": event_ticker, "limit": 200}, pause=0.05)
        ms = (j or {}).get("markets", [])
    return ms


def kalshi_market(ticker):
    j = get_json(f"{KALSHI}/markets/{ticker}", pause=0.05)
    return (j or {}).get("market")


def find_game_markets(suffix, families=None):
    found = {}
    for fam, ser in SERIES.items():
        if families and fam not in families:
            continue
        ms = kalshi_markets(f"{ser}-{suffix}")
        if ms:
            found[fam] = ms
    return found


def market_side_team(m):
    """Abreviatura del equipo del lado SÍ (ganador, hándicaps, total de equipo)."""
    suf = m["ticker"].split("-")[-1]
    letters = "".join(ch for ch in suf if ch.isalpha())
    return letters or None


def strike_of(m):
    return fnum(m.get("floor_strike"))


def quotes(m):
    ya = fnum(m.get("yes_ask_dollars"))
    yb = fnum(m.get("yes_bid_dollars"))
    if ya is None and m.get("yes_ask") is not None:
        ya = m["yes_ask"] / 100
    if yb is None and m.get("yes_bid") is not None:
        yb = m["yes_bid"] / 100
    return ya, yb


def tip_time(m):
    """Hora de inicio estimada: Kalshi fija la expiración esperada 3 h después del salto."""
    s = m.get("expected_expiration_time") or m.get("occurrence_datetime")
    return iso_to_dt(s) - timedelta(hours=3) if s else None


def _close(d):
    d = d or {}
    v = d.get("close_dollars", d.get("close"))
    return fnum(v)


def candle_quote(series, ticker, ts):
    """Ask/bid YES de la última vela horaria cerrada antes de ts (epoch)."""
    params = {"start_ts": int(ts - 6 * 3600), "end_ts": int(ts), "period_interval": 60}
    j = get_json(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks", params, pause=0.05, tries=2)
    cs = (j or {}).get("candlesticks") or []
    if not cs:
        j = get_json(f"{KALSHI}/historical/markets/{ticker}/candlesticks", params, pause=0.05, tries=2)
        cs = (j or {}).get("candlesticks") or []
    for c in reversed(cs):
        ya, yb = _close(c.get("yes_ask")), _close(c.get("yes_bid"))
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
            print("telegram error", type(e).__name__)


def now_utc():
    return datetime.now(timezone.utc)


def iso_to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def et_date(dt_utc):
    return dt_utc.astimezone(ET).date()
