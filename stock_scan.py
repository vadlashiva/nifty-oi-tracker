"""Daily F&O stock breakout scan for the Nifty OI Tracker (added 19 Nov 2026).

Scans the Nifty 50 stocks (all of them trade in F&O) on daily closes and looks for one stock "with steam":
a fresh 52-week-high breakout, or a stock within a few per cent of its 52-week high, in an up-trend, with
momentum, and with volume behind the move.  Data: Yahoo Finance daily chart JSON (free, end of day, .NS
symbols); the Nifty 50 list comes from niftyindices.com with a built-in fallback.

    python stock_scan.py              # print the scan and save stock_scan_YYYY-MM-DD.json in the data folder
    python stock_scan.py --data DIR

Educational tool only - not trading advice.  Prices are end-of-day; check live prices before acting.
"""
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time

try:
    import requests
except ImportError:          # the app installs requests; this only matters for a bare Python
    requests = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
NIFTY50_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?range=1y&interval=1d"
# Fallback list (Nifty 50 constituents as of Nov 2026); refreshed from niftyindices.com whenever it is reachable
FALLBACK = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
            "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM",
            "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
            "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
            "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS",
            "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"]

# Rules (kept simple and visible; change here)
NEAR_HIGH_PCT = 3.0       # within this % of the 52-week high counts as "near"
MIN_RET_1M = 0.02         # at least +2% over the last month (21 sessions)
VOL_STEAM = 1.5           # today's volume / 20-day average at or above this = volume behind the move
RSI_STRETCHED = 75        # RSI(14) above this = stretched, prone to a pullback


def _get(url, timeout=12):
    if requests is None:
        raise RuntimeError("the 'requests' package is missing - pip install requests")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:          # network hiccup: retry
            last = str(e)
        time.sleep(1 + attempt)
    raise RuntimeError(f"{last}: {url}")


def nifty50():
    try:
        rows = list(csv.DictReader(io.StringIO(_get(NIFTY50_URL).text)))
        syms = [r["Symbol"].strip() for r in rows if r.get("Symbol")]
        if len(syms) >= 45:
            return syms, "niftyindices.com"
    except Exception:
        pass
    return list(FALLBACK), "built-in list"


def daily(sym):
    """Return (dates, closes, highs, volumes) for the last year, oldest first, without empty bars."""
    j = _get(YAHOO_URL.format(sym=sym.replace("&", "%26"))).json()
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = [(dt.date.fromtimestamp(t), c, h, v) for t, c, h, v in
           zip(res["timestamp"], q["close"], q["high"], q["volume"]) if c is not None and h is not None]
    return [o[0] for o in out], [o[1] for o in out], [o[2] for o in out], [o[3] or 0 for o in out]


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0); losses += max(-d, 0)
    # Wilder smoothing over the whole series for a stable value
    g, l = 0.0, 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up, dn = max(d, 0), max(-d, 0)
        if i <= n:
            g += up / n; l += dn / n
        else:
            g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def analyse(sym):
    d, c, h, v = daily(sym)
    if len(c) < 210:
        return None
    px = c[-1]
    hi_prev = max(h[-253:-1])                       # 52-week high before today
    sma = lambda k: sum(c[-k:]) / k
    ret = lambda k: px / c[-1 - k] - 1
    vol20 = sum(v[-21:-1]) / 20 if sum(v[-21:-1]) else 0
    low10 = min(c[-10:])
    r = dict(symbol=sym, date=d[-1].isoformat(), close=round(px, 2), high52=round(hi_prev, 2),
             from_high_pct=round((px / hi_prev - 1) * 100, 2), ret_1m=round(ret(21), 4), ret_3m=round(ret(63), 4),
             sma20=round(sma(20), 2), sma50=round(sma(50), 2), sma200=round(sma(200), 2), rsi14=round(rsi(c), 1),
             vol_ratio=round(v[-1] / vol20, 2) if vol20 else None, low10=round(low10, 2))
    r["breakout"] = px > hi_prev
    r["near"] = not r["breakout"] and r["from_high_pct"] >= -NEAR_HIGH_PCT
    r["uptrend"] = px > r["sma50"] > r["sma200"]
    r["qualifies"] = (r["breakout"] or r["near"]) and r["uptrend"] and r["ret_1m"] >= MIN_RET_1M
    r["stretched"] = (r["rsi14"] or 0) > RSI_STRETCHED
    r["steam"] = bool(r["vol_ratio"] and r["vol_ratio"] >= VOL_STEAM)
    # score: momentum + closeness to the high + volume, minus a penalty when stretched
    r["score"] = round(100 * r["ret_3m"] + 50 * r["ret_1m"] + 2 * max(-NEAR_HIGH_PCT, r["from_high_pct"])
                       + (5 if r["breakout"] else 0) + (4 if r["steam"] else 0) - (4 if r["stretched"] else 0), 2)
    # levels for a plan (the user decides): trigger at the 52-week high, invalidation below the 20-day average
    # or the 10-day closing low, whichever is higher but still below the trigger
    r["trigger"] = r["high52"]
    inval = max(r["sma20"], r["low10"])
    r["invalidation"] = round(inval if inval < r["trigger"] else min(r["sma20"], r["low10"]), 2)
    return r


def next_month_series(today=None):
    """Stock options expire on the last Tuesday of the month (NSE, from Sept 2025). If this month's expiry is
    under 10 days away, point to next month's series."""
    today = today or dt.date.today()
    def last_tuesday(y, m):
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - dt.timedelta(days=1)
        while d.weekday() != 1:
            d -= dt.timedelta(days=1)
        return d
    e = last_tuesday(today.year, today.month)
    if (e - today).days < 10:
        y, m = (today.year + (today.month == 12), today.month % 12 + 1)
        e = last_tuesday(y, m)
    return e


def run_scan(data_dir=None, progress=None):
    syms, src = nifty50()
    rows, failed = [], []
    for i, s in enumerate(syms):
        if progress:
            progress(i, len(syms), s)
        try:
            r = analyse(s)
            if r:
                rows.append(r)
        except Exception as e:
            failed.append(f"{s}: {e}")
    cands = sorted([r for r in rows if r["qualifies"]], key=lambda r: -r["score"])
    # one name with steam: prefer a breakout with volume that is not stretched, then the best score
    pick = next((r for r in cands if r["breakout"] and r["steam"] and not r["stretched"]), None) or \
        next((r for r in cands if not r["stretched"]), None) or (cands[0] if cands else None)
    out = dict(run_at=dt.datetime.now().isoformat(timespec="seconds"), source=src, scanned=len(rows),
               failed=failed, candidates=cands, pick=pick, series=next_month_series().isoformat(),
               rules=dict(NEAR_HIGH_PCT=NEAR_HIGH_PCT, MIN_RET_1M=MIN_RET_1M, VOL_STEAM=VOL_STEAM,
                          RSI_STRETCHED=RSI_STRETCHED))
    if data_dir:
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, f"stock_scan_{dt.date.today():%Y-%m-%d}.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
        except OSError:
            pass
    return out


def latest_saved(data_dir):
    try:
        names = sorted(n for n in os.listdir(data_dir) if n.startswith("stock_scan_") and n.endswith(".json"))
    except OSError:
        return None
    if not names:
        return None
    try:
        with open(os.path.join(data_dir, names[-1]), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def describe(r, series):
    tag = "BREAKOUT" if r["breakout"] else f"{-r['from_high_pct']:.1f}% below 52-wk high"
    notes = []
    if r["steam"]:
        notes.append(f"volume {r['vol_ratio']:.1f}x the 20-day average")
    if r["stretched"]:
        notes.append(f"RSI {r['rsi14']:.0f}: stretched, pullbacks are common")
    return (f"{r['symbol']}: close {r['close']:,.2f} ({tag}), 52-wk high {r['high52']:,.2f}, "
            f"1m {100 * r['ret_1m']:+.1f}%, 3m {100 * r['ret_3m']:+.1f}%, RSI {r['rsi14']:.0f}. "
            f"Trigger: daily close above {r['trigger']:,.2f}; invalidation: close below {r['invalidation']:,.2f}. "
            f"Options: {dt.date.fromisoformat(series):%b} series (expiry {dt.date.fromisoformat(series):%d %b}). "
            + ("; ".join(notes) + "." if notes else ""))


if __name__ == "__main__":
    dd = sys.argv[sys.argv.index("--data") + 1] if "--data" in sys.argv else None
    res = run_scan(dd, progress=lambda i, n, s: print(f"\r{i + 1}/{n} {s:12s}", end="", flush=True))
    print()
    print(f"Scanned {res['scanned']} stocks ({res['source']}); {len(res['candidates'])} qualify.")
    for r in res["candidates"][:10]:
        print(" ", describe(r, res["series"]))
    if res["pick"]:
        print("\nOne name with steam:", describe(res["pick"], res["series"]))
    else:
        print("\nNo stock meets the rules today.")
    print("\nEducational tool only - not trading advice.")
