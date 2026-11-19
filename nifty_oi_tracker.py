"""
Nifty 50 OI Tracker - records Nifty + option-chain OI every 5 minutes and flags signals.

Run it before 9:15 AM IST on a trading day:
    python nifty_oi_tracker.py

Every 5 minutes (just after each 5-min candle closes) it:
  1. pulls Nifty 1-minute data from NSE and builds the 5-minute candle
  2. pulls the Nifty option chain (nearest expiry) and India VIX
  3. works out OI support / resistance, PCR, max pain
  4. checks the signal rules and prints + beeps on any signal
  5. appends to CSV files and rewrites dashboard.html (open it in your browser)

Output folder: ./nifty_tracker_data/<YYYY-MM-DD>/
    snapshots.csv  - one row per 5-min candle (price, OI totals, PCR, levels)
    strikes.csv    - OI per strike (ATM +/- 12 strikes) at every snapshot
    signals.csv    - every signal with time, type, details, trade idea
    dashboard.html - live dashboard, auto-refreshes every 60 s

Options:
    --once          take one snapshot now and exit (good for testing)
    --expiry DATE   use a specific expiry, e.g. 06-Oct-2026 (default: nearest)
    --no-beep       silence the alert sound
    --replay        test mode: replay the latest session (real prices, ESTIMATED OI)
    --replay-folder FOLDER
                    re-test the rules offline on a day recorded by live mode (real 5-min OI);
                    writes to FOLDER_retest and leaves the original untouched

Educational tool only - not trading advice. Data belongs to NSE; use it for personal
analysis and keep the polling gentle (this script makes ~4 requests per 5 minutes).
"""

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import sys
import time
import webbrowser

try:
    import requests
except ImportError:  # pragma: no cover
    print("The 'requests' package is missing. Install it with:  pip install requests")
    sys.exit(1)

try:  # Chrome-like TLS handshake; BSE (Akamai) answers HTTP 403 to plain `requests`
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
POLL_DELAY_SEC = 20          # wait this long after a candle closes before fetching
STRIKE_STEP = 50
LEVEL_WINDOW = 600           # look for support/resistance within +/- this many points
FRESH_WINDOW = 300           # look for fresh OI build-up within +/- this many points
TOP_OI_WINDOW = 1500         # "highest OI" strikes are looked for within +/- this many points of spot
FLOW_THRESHOLD = 20000       # contracts: min change in total CE/PE OI to flag an OI-flow signal
PRICE_MOVE_MIN = 10          # points: min 5-min move for OI-flow classification
STOP_BUFFER = 25             # points beyond a level for the stop-loss
LOT_SIZE = 65                # Nifty contract size (units per lot) - NSE revised it to 65 from Jan 2026
LOTS = 1                     # lots per idea for the "if you had taken it" profit in Rs
BROKERAGE_PER_ORDER = 20.0   # Rs per executed order (typical discount broker)
DEFAULT_IV = 13.0            # % used when NSE shows no IV for a strike
VIX_JUMP_PCT = 3.0           # % rise in VIX between snapshots that triggers a caution flag
# Added 16 Nov 2026: BIG MONEY alert - one strike adds or sheds an unusually large number of contracts inside
# one 5-min candle. 30,000 is about the top 10% of candles on the recorded Nifty days (6 Oct - 16 Nov 2026).
BIG_MONEY = 1                # 1 = flag big-player activity at single strikes, 0 = off
BIG_OI_JUMP = 30000          # contracts: min 5-min OI change at ONE strike (Nifty; scaled for the other indices)
BIG_MAX_LISTED = 3           # strikes listed per alert, biggest first
BREAK_MIN = 10               # points: a 5-min close must be at least this far through a level to count as a break
BREAK_COOLDOWN_MIN = 30      # minutes: no repeat BREAKOUT/BREAKDOWN on the same level inside this window
LEVEL_STICKY = 60            # points: keep a support/resistance level while price is within this distance of it
ATR_CANDLES = 6              # 5-min candles used for the average range (stop-loss sizing)
ATR_STOP_MULT = 1.0          # stop = level +/- max(STOP_BUFFER, ATR_STOP_MULT * average range)
MIN_RR = 2.0                 # skip trade ideas whose reward:risk is below this (2 = risk 1 : reward 2)
# Switchable rules added 29 Oct 2026 (1 = on, 0 = off). Set from the back-test on recorded days.
EARLY_TREND = 0              # before the 15-min trend exists (~10:00), use the opening-range break / 5-min structure
RR_OVERRIDE = 0              # let a grade-C idea through when R:R >= STRONG_RR, OI confirms and price action is positive
CONTINUATION = 1             # pullback-free continuation idea after 2 candles of same-side OI flow + structure
BLOCK_CTR_REV = 0            # block a reversal against an opening-range break made in the last REV_BLOCK_MIN minutes
REV_BLOCK_MIN = 30           # minutes
# Added 30 Oct 2026 after reviewing the live log 19-30 Oct (ideas against the PCR bias won 0 of 5;
# one setup often fired two ideas in the same direction, doubling the loss):
BLOCK_AGAINST_PCR = 1        # skip any trade idea (buying or selling) that goes AGAINST the PCR bias
ONE_IDEA_PER_SETUP = 1       # at most one idea per direction per candle, buying and selling together (highest score wins)
CONT_COOLDOWN_MIN = 30       # minutes between continuation ideas in the same direction
DAILY_TARGET = 500           # Rs: the day's profit goal shown on the scorecard
DAILY_LOSS_LIMIT = 1500      # Rs: when the day's P&L (closed + open) reaches -this, the app says stop for the day
STRONG_RR = 2.0              # reward:risk at or above this is labelled "Strong" (2 = 2:1); MIN_RR..STRONG_RR = "OK"
MIN_GRADE = "B"              # trade ideas graded below this are shown but not traded
# Confluence score for every trade idea = OI (0..2) + price action (-2..+3)
#   + context (-3..+3: PCR bias, 15-min trend, global cues x GLOBAL_WEIGHT)
SCORE_A = 5                  # score >= this -> grade A
SCORE_B = 3                  # score >= this -> grade B, below -> C
OR_CANDLES = 3               # opening range = first 3 five-minute candles (09:15-09:30)
OR_LAST_ENTRY = dt.time(13, 30)  # opening-range breakouts only count until this time
SWING_K = 2                  # a swing high/low needs this many candles on each side (fractal)
TARGET_CAP_ATR = 6           # cap the target at this many average 5-min ranges from entry (no 400-pt targets)
NO_NEW_TRADES_AFTER = dt.time(15, 0)   # no fresh trade ideas after this candle close
# Accuracy filters for the option-BUYING ideas (breakout / breakdown / OR / reversal), chosen by back-testing
# the recorded live days 6-23 Oct 2026 (afternoon buy ideas and repeat ideas were the main losers):
BUY_LAST_ENTRY = 1300        # HHMM: no new option-buying idea after this candle close (sell ideas keep 15:00)
MAX_IDEAS_PER_SIDE = 3       # at most this many buying ideas per direction per day
BUY_TARGET_CAP_ATR = 4.0     # buying-idea target capped at this many average 5-min ranges (sell ideas: TARGET_CAP_ATR)
EXPIRY_EVE_MUTE_FROM = dt.time(14, 45)  # on expiry day / the day before, mute OI-flow and PCR signals from here
REV_LOOKBACK = 8             # candles: the day's low/high must have been made within this many candles
REV_MIN_SWING = 60           # points: the move into that low/high must be at least this big
REV_OI_CANDLES = 3           # snapshots used to confirm a reversal with total OI flow
REV_STOP_PAD = 5             # points: reversal stop sits this far beyond the day's low/high (or 0.3 x avg range)
CSV_ENCODING = "utf-8-sig"   # BOM so Excel shows symbols correctly
# Global cues: GIFT Nifty (NSE IX Nifty futures, from NSE's market-status feed) and Nasdaq-100 futures
# (NQ=F from Yahoo Finance; they trade during Indian market hours). Each one that is up / down by at least
# GLOBAL_MOVE_PCT on the day counts as one bullish / bearish vote; the majority sets the global bias, which
# adds GLOBAL_WEIGHT context points to trade ideas that agree with it (and takes them from ideas against it).
GLOBAL_MOVE_PCT = 0.3        # % day change that counts as a clear move
GLOBAL_WEIGHT = 1            # context points for agreeing with / going against the global bias (0 = ignore)
GLOBAL_CACHE_SEC = 240       # fetch the global cues at most this often
NASDAQ_SYMBOL = "NQ=F"       # Nasdaq-100 E-mini futures on Yahoo Finance

BASE = "https://www.nseindia.com"
URL_HOME = BASE + "/option-chain"
URL_EXPIRIES = BASE + "/api/option-chain-contract-info?symbol=NIFTY"
URL_CHAIN = BASE + "/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry={expiry}"
URL_CHART = BASE + "/api/NextApi/apiClient/indexTrackerApi?functionName=getIndexChart&&index=NIFTY%2050&flag=1D"
URL_INDICES = BASE + "/api/allIndices"
URL_HOLIDAYS = BASE + "/api/holiday-master?type=trading"
URL_MARKET_STATUS = BASE + "/api/marketStatus"   # also carries the GIFT Nifty quote ("giftnifty")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": URL_HOME,
}

# --------------------------------------------------------------------------- instruments
# scale = typical index level / Nifty level, used to stretch every point-based rule (break distance,
# stop buffer, level windows...) so one set of settings works for all three indices.
# flow_scale = typical total option OI / Nifty total option OI, used to shrink FLOW_THRESHOLD (a contract
# count) the same way: Bank Nifty carries roughly a fifth of Nifty's OI, so 20,000 becomes 4,000.
# big_scale = typical largest single-strike 5-min OI change / Nifty's, used to shrink BIG_OI_JUMP: measured on
# the recorded days, Bank Nifty's is about a twentieth of Nifty's and Sensex's a little below Nifty's.
INSTRUMENTS = {
    "NIFTY": {"label": "Nifty 50", "short": "Nifty", "exchange": "NSE", "symbol": "NIFTY",
              "chart_index": "NIFTY%2050", "step": 50, "lot": 65, "scale": 1.0, "flow_scale": 1.0, "big_scale": 1.0},
    "BANKNIFTY": {"label": "Bank Nifty", "short": "Bank Nifty", "exchange": "NSE", "symbol": "BANKNIFTY",
                  "chart_index": "NIFTY%20BANK", "step": 100, "lot": 30, "scale": 2.2, "flow_scale": 0.2, "big_scale": 0.05},
    "SENSEX": {"label": "Sensex", "short": "Sensex", "exchange": "BSE", "symbol": "SENSEX",
               "bse_scrip": "1", "yahoo": "%5EBSESN", "step": 100, "lot": 20, "scale": 3.3, "flow_scale": 1.0, "big_scale": 0.85},
}
INSTRUMENT = "NIFTY"
SCALE = 1.0
FLOW_SCALE = 1.0
_BASE_FLOW_THRESHOLD = FLOW_THRESHOLD
BIG_SCALE = 1.0
_BASE_BIG_OI_JUMP = BIG_OI_JUMP
NAME = "Nifty"
_BASE_POINTS = {"LEVEL_WINDOW": LEVEL_WINDOW, "FRESH_WINDOW": FRESH_WINDOW, "PRICE_MOVE_MIN": PRICE_MOVE_MIN,
                "REV_STOP_PAD": REV_STOP_PAD, "STOP_BUFFER": STOP_BUFFER, "BREAK_MIN": BREAK_MIN,
                "LEVEL_STICKY": LEVEL_STICKY, "REV_MIN_SWING": REV_MIN_SWING}
POINT_TUNABLES = ("STOP_BUFFER", "BREAK_MIN", "LEVEL_STICKY", "REV_MIN_SWING")


def set_instrument(key, lot_size=None):
    """Switch the whole engine to NIFTY / BANKNIFTY / SENSEX. Point-based rules are scaled from their
    Nifty values; call apply-tunables code AFTER this (it scales user values the same way)."""
    global INSTRUMENT, SCALE, NAME, STRIKE_STEP, LOT_SIZE, URL_EXPIRIES, URL_CHAIN, URL_CHART
    global FLOW_SCALE, FLOW_THRESHOLD, BIG_SCALE, BIG_OI_JUMP
    ins = INSTRUMENTS[key]
    FLOW_SCALE = ins.get("flow_scale", 1.0)
    FLOW_THRESHOLD = int(round(_BASE_FLOW_THRESHOLD * FLOW_SCALE))
    BIG_SCALE = ins.get("big_scale", 1.0)
    BIG_OI_JUMP = int(round(_BASE_BIG_OI_JUMP * BIG_SCALE))
    INSTRUMENT, SCALE, NAME, STRIKE_STEP = key, ins["scale"], ins["short"], ins["step"]
    LOT_SIZE = int(lot_size or ins["lot"])
    for k, v in _BASE_POINTS.items():
        val = v * SCALE
        globals()[k] = int(round(val)) if isinstance(v, int) else val
    if ins["exchange"] == "NSE":
        URL_EXPIRIES = BASE + f"/api/option-chain-contract-info?symbol={ins['symbol']}"
        URL_CHAIN = BASE + f"/api/option-chain-v3?type=Indices&symbol={ins['symbol']}&expiry={{expiry}}"
        URL_CHART = (BASE + "/api/NextApi/apiClient/indexTrackerApi?functionName=getIndexChart&&index="
                     + ins["chart_index"] + "&flag=1D")


def make_client():
    """Data client for the current instrument (NSE for Nifty / Bank Nifty, BSE for Sensex)."""
    return BSEClient() if INSTRUMENTS[INSTRUMENT]["exchange"] == "BSE" else NSEClient()


# --------------------------------------------------------------------------- data fetch
class NSEClient:
    """Small NSE client: opens the option-chain page once for cookies, then calls the JSON APIs."""

    def __init__(self):
        self.s = None
        self._new_session()

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        try:
            self.s.get(URL_HOME, timeout=15)
        except requests.RequestException as e:
            print(f"  ! could not open NSE home page: {e}")

    def get_json(self, url, tries=3):
        last = None
        for attempt in range(tries):
            try:
                r = self.s.get(url, timeout=20)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    self._new_session()      # cookies expired -> refresh
            except (requests.RequestException, ValueError) as e:
                last = str(e)
                self._new_session()
            time.sleep(2 + attempt * 3)
        raise RuntimeError(f"NSE request failed ({last}): {url}")

    def expiries(self):
        return self.get_json(URL_EXPIRIES)["expiryDates"]

    def chain(self, expiry):
        return self.get_json(URL_CHAIN.format(expiry=expiry))

    def chart(self):
        return self.get_json(URL_CHART)

    def holidays(self):
        data = self.get_json(URL_HOLIDAYS)
        rows = data.get("FO") or data.get("CM") or []
        out = set()
        for r in rows:
            try:
                out.add(dt.datetime.strptime(r["tradingDate"], "%d-%b-%Y").date())
            except (KeyError, ValueError):
                pass
        return out

    def vix(self):
        data = self.get_json(URL_INDICES).get("data", [])
        for row in data:
            if row.get("index") == "INDIA VIX":
                return float(row["last"])
        return None


BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_HEADERS = dict(HEADERS, Referer="https://www.bseindia.com/", Origin="https://www.bseindia.com")


def _pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


class BSEClient:
    """Sensex options from BSE's website API, Sensex 1-minute prices from Yahoo Finance, VIX and holidays
    from NSE. BSE sits behind Akamai, which answers HTTP 403 to Python's plain `requests`, so the BSE calls
    go through curl_cffi (Chrome TLS fingerprint) when it is installed. BSE publishes no expiry list any
    more, so expiries are found by asking the option chain which upcoming Tuesdays/Thursdays have contracts.
    The raw reply is saved to bse_last_reply.json when it cannot be read (send that file to fix it)."""
    DEBUG_PATH = None
    PROBE_WEEKS = 5

    def __init__(self):
        self.s = None
        self._new_session()
        self.web = requests.Session()                  # Yahoo Finance is happy with plain requests
        self.web.headers.update({"User-Agent": HEADERS["User-Agent"]})
        self._nse = None
        self._chain_cache = {}

    def _new_session(self):
        hdr = dict(BSE_HEADERS, Referer="https://www.bseindia.com/derivatives/OptionChain")
        if cffi_requests is not None:
            self.s = cffi_requests.Session(impersonate="chrome")
        else:
            self.s = requests.Session()
        self.s.headers.update(hdr)
        try:
            self.s.get("https://www.bseindia.com/", timeout=15)
        except Exception:
            pass

    def nse(self):
        if self._nse is None:
            self._nse = NSEClient()
        return self._nse

    def get_json(self, url, tries=3):
        last = None
        for attempt in range(tries):
            try:
                r = self.s.get(url, timeout=20)
                body = (r.text or "").strip()
                if r.status_code == 200 and body[:1] in ("{", "["):
                    return json.loads(body)
                last = f"HTTP {r.status_code}" if r.status_code != 200 else "BSE sent a web page, not data"
                if r.status_code in (401, 403):
                    self._new_session()                # fresh cookies before the next try
            except Exception as e:
                last = str(e)
            time.sleep(1 + attempt * 2)
        hint = ("" if cffi_requests is not None or "403" not in str(last) else
                " - BSE blocks plain Python; run:  pip install curl_cffi  and start again")
        raise RuntimeError(f"BSE request failed ({last}){hint}: {url}")

    def _dump(self, data):
        if self.DEBUG_PATH:
            try:
                with open(self.DEBUG_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1, default=str)
            except OSError:
                pass

    @staticmethod
    def _rows(data):
        if isinstance(data, list):
            return data
        for k in ("Table", "Table1", "data", "Data"):
            if isinstance(data.get(k), list) and data[k]:
                return data[k]
        return []

    def _raw_chain(self, expiry):
        """Rows of BSE's DerivOptionChain for one expiry ('DD-Mon-YYYY'). Cached for 20 s."""
        hit = self._chain_cache.get(expiry)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
        scrip = INSTRUMENTS[INSTRUMENT]["bse_scrip"]
        e = dt.datetime.strptime(expiry, "%d-%b-%Y").strftime("%d %b %Y").replace(" ", "+")
        data = self.get_json(f"{BSE_API}/DerivOptionChain/w?scrip_cd={scrip}&ProductType=IO&Expiry={e}")
        self._chain_cache[expiry] = (time.time(), data)
        return data

    def expiries(self):
        today = dt.datetime.now(IST).date()
        cands, d = [], today
        while len(cands) < self.PROBE_WEEKS * 2:
            if d.weekday() in (1, 3):                  # Tuesday / Thursday
                cands.append(d.strftime("%d-%b-%Y"))
            d += dt.timedelta(days=1)
        out, err = [], None
        for c in cands:
            try:
                if [r for r in self._rows(self._raw_chain(c)) if isinstance(r, dict)]:
                    out.append(c)
            except RuntimeError as e:
                err = e
                if not out and "403" in str(e):
                    raise                              # blocked: no point probing every date
            if len(out) >= 3:
                break
        if not out:
            raise err or RuntimeError("BSE lists no live Sensex option expiries in the next few weeks")
        return out

    def chain(self, expiry):
        """BSE option chain, returned in NSE's shape so parse_chain() works unchanged."""
        data = self._raw_chain(expiry)
        rows, spot = [], None
        for r in self._rows(data):
            if not isinstance(r, dict):
                continue
            k = _num(_pick(r, "Strike_Price1", "Strike_Price", "StrikePrice", "strikePrice", "STRIKE_PRICE"))
            if not k:
                continue
            spot = spot or _num(_pick(r, "UlaValue", "UnderlyingValue", "Ula_Value", "underlyingValue")) or None
            rows.append({"strikePrice": k, "CE": {
                "openInterest": _num(_pick(r, "C_Open_Interest", "CE_OpenInterest", "C_OI", "CE_OI")),
                "changeinOpenInterest": _num(_pick(r, "C_Absolute_Change_OI", "CE_ChgInOI", "C_Change_OI", "C_Chg_OI")),
                "lastPrice": _num(_pick(r, "C_Last_Trd_Price", "C_LTP", "CE_LTP", "C_Ltp")),
                "impliedVolatility": _num(_pick(r, "C_IV", "CE_IV")),
                "totalTradedVolume": _num(_pick(r, "C_Vol_Traded", "CE_Volume", "C_Volume"))}, "PE": {
                "openInterest": _num(_pick(r, "Open_Interest", "PE_OpenInterest", "P_Open_Interest", "PE_OI")),
                "changeinOpenInterest": _num(_pick(r, "Absolute_Change_OI", "PE_ChgInOI", "P_Absolute_Change_OI")),
                "lastPrice": _num(_pick(r, "Last_Trd_Price", "PE_LTP", "P_Last_Trd_Price", "P_LTP")),
                "impliedVolatility": _num(_pick(r, "IV", "PE_IV", "P_IV")),
                "totalTradedVolume": _num(_pick(r, "Vol_Traded", "PE_Volume", "P_Vol_Traded"))}})
        if not spot and isinstance(data, dict):
            for k in ("Table1", "Table2"):
                for r in data.get(k) or []:
                    spot = spot or _num(_pick(r, "UlaValue", "UnderlyingValue", "LTP", "CurrValue")) or None
        oi = sum(x["CE"]["openInterest"] + x["PE"]["openInterest"] for x in rows)
        if not rows or not spot or not oi:
            self._dump(data)
            raise RuntimeError("could not read the Sensex option chain from BSE (saved bse_last_reply.json)")
        return {"records": {"data": rows, "underlyingValue": spot,
                            "timestamp": dt.datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S")}}

    def chart(self):
        """Yahoo Finance 1-minute Sensex candles, returned in NSE's grapthData shape: each minute is fed as
        open / high / low / close points stamped hh:mm:59 (IST wall clock), as minute_points() expects."""
        sym = INSTRUMENTS[INSTRUMENT]["yahoo"]
        r = self.web.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d",
                         timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Sensex prices: Yahoo Finance HTTP {r.status_code}")
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        out = []
        for i, ts in enumerate(res.get("timestamp") or []):
            vals = [q[k][i] for k in ("open", "high", "low", "close")]
            if any(v is None for v in vals):
                continue
            t = dt.datetime.fromtimestamp(ts, IST).replace(tzinfo=None, second=59, microsecond=0)
            stamp = int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out += [[stamp, v, "NM"] for v in vals]
        return {"grapthData": out}

    def holidays(self):
        return self.nse().holidays()

    def vix(self):
        return self.nse().vix()


# --------------------------------------------------------------------------- calculations
def minute_points(chart_json):
    """NSE 1-minute ticks -> list of (minute_start_datetime_IST, price) for the normal market."""
    g = chart_json.get("data", chart_json).get("grapthData", [])
    out = []
    for row in g:
        ts, price = row[0], float(row[1])
        kind = row[2] if len(row) > 2 else "NM"
        if kind == "PO":
            continue
        # NSE stamps regular points at the END of each minute (hh:mm:59), IST wall-clock-as-UTC.
        # After ~15:20 it switches to 30-second points that sometimes contain spikes; keep those
        # only when they are close to the previous price.
        raw = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).replace(tzinfo=None)
        if raw.second != 59 and out and abs(price / out[-1][1] - 1) > 0.003:
            continue
        t = (raw - dt.timedelta(seconds=59)).replace(second=0, microsecond=0) if raw.second == 59 \
            else raw.replace(second=0, microsecond=0)
        if t.time() == dt.time(9, 14):          # last pre-market point = the official open
            t = t.replace(minute=15)
        if MARKET_OPEN <= t.time() < MARKET_CLOSE:
            out.append((t, price))
    return out


def five_min_candles(points, n=5):
    """Group (minute, price) into n-min OHLC candles keyed by candle start time (n divides 60)."""
    buckets = {}
    for t, p in points:
        m = (t.hour * 60 + t.minute - 15) // n * n + 15          # NSE session starts 09:15
        b = t.replace(hour=m // 60, minute=m % 60)
        buckets.setdefault(b, []).append(p)
    candles = []
    for b in sorted(buckets):
        v = buckets[b]
        candles.append({"start": b, "open": v[0], "high": max(v), "low": min(v), "close": v[-1]})
    return candles


def parse_chain(chain_json):
    rec = chain_json["records"]
    strikes = {}
    for row in rec["data"]:
        k = float(row["strikePrice"])
        ce, pe = row.get("CE") or {}, row.get("PE") or {}
        strikes[k] = {
            "ce_oi": float(ce.get("openInterest") or 0), "ce_chg": float(ce.get("changeinOpenInterest") or 0),
            "ce_ltp": float(ce.get("lastPrice") or 0), "ce_iv": float(ce.get("impliedVolatility") or 0),
            "ce_vol": float(ce.get("totalTradedVolume") or 0),
            "pe_oi": float(pe.get("openInterest") or 0), "pe_chg": float(pe.get("changeinOpenInterest") or 0),
            "pe_ltp": float(pe.get("lastPrice") or 0), "pe_iv": float(pe.get("impliedVolatility") or 0),
            "pe_vol": float(pe.get("totalTradedVolume") or 0),
        }
    return {"timestamp": rec.get("timestamp"), "spot": float(rec["underlyingValue"]), "strikes": strikes}


def trend_15m(points, upto):
    """15-minute trend from candles that have fully closed by `upto` (naive IST datetime).
    UP   = last close above its 9-period EMA and above the close two candles earlier
    DOWN = last close below the EMA and below the close two candles earlier
    SIDEWAYS otherwise. Needs 3 closed 15-min candles (from 10:00)."""
    c15 = [c for c in five_min_candles(points, 15) if c["start"] + dt.timedelta(minutes=15) <= upto]
    if len(c15) < 3:
        return {"trend": "n/a", "text": "15-min trend: not enough candles yet (from 10:00)"}
    ema, a = c15[0]["close"], 2 / (9 + 1)
    for c in c15[1:]:
        ema = c["close"] * a + ema * (1 - a)
    last, back2 = c15[-1]["close"], c15[-3]["close"]
    if last > ema and last > back2:
        tr = "up"
    elif last < ema and last < back2:
        tr = "down"
    else:
        tr = "sideways"
    return {"trend": tr, "ema": ema, "close": last,
            "text": f"15-min trend {tr.upper()} (close {last:,.0f} vs EMA9 {ema:,.0f})"}


def avg_range(candles, n=None):
    """Average true range of the last n 5-min candles (points). Used to size stops to today's noise."""
    n = n or ATR_CANDLES
    if not candles:
        return None
    trs = []
    for i, c in enumerate(candles[-n:]):
        j = len(candles) - min(n, len(candles)) + i
        pc = candles[j - 1]["close"] if j > 0 else c["open"]
        trs.append(max(c["high"], pc) - min(c["low"], pc))
    return sum(trs) / len(trs)


# --------------------------------------------------------------------------- price action
def candle_patterns(cands, atr):
    """Patterns on the LAST candle of `cands` (uses the two before it). Returns [(name, 'bullish'|'bearish')]."""
    if len(cands) < 2:
        return []
    c, pc = cands[-1], cands[-2]
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    if rng <= 0:
        return []
    body, upper, lower = abs(cl - o), h - max(o, cl), min(o, cl) - l
    big = rng >= 0.6 * (atr or rng)
    out = []
    if big and lower >= 2 * body and lower >= 0.5 * rng and upper <= 0.25 * rng:
        out.append(("hammer / pin bar", "bullish"))
    if big and upper >= 2 * body and upper >= 0.5 * rng and lower <= 0.25 * rng:
        out.append(("shooting star / pin bar", "bearish"))
    pbody = abs(pc["close"] - pc["open"])
    if cl > o and pc["close"] < pc["open"] and cl >= pc["open"] and o <= pc["close"] and body >= pbody:
        out.append(("bullish engulfing", "bullish"))
    if cl < o and pc["close"] > pc["open"] and cl <= pc["open"] and o >= pc["close"] and body >= pbody:
        out.append(("bearish engulfing", "bearish"))
    if rng >= (atr or rng) and body >= 0.6 * rng:
        if cl > o and h - cl <= 0.2 * rng:
            out.append(("strong bull candle", "bullish"))
        if cl < o and cl - l <= 0.2 * rng:
            out.append(("strong bear candle", "bearish"))
    if len(cands) >= 3:
        m = cands[-3]
        if pc["high"] <= m["high"] and pc["low"] >= m["low"]:        # previous candle was an inside bar
            if cl > m["high"]:
                out.append(("inside-bar breakout", "bullish"))
            elif cl < m["low"]:
                out.append(("inside-bar breakdown", "bearish"))
    return out


def swing_points(cands, k=None):
    """Confirmed swing highs/lows (fractals) among all candles BEFORE the last one: [(index, price)]."""
    k = k or SWING_K
    hist = cands[:-1]
    highs, lows = [], []
    for i in range(k, len(hist) - k):
        win = hist[i - k:i + k + 1]
        if all(hist[i]["high"] > x["high"] for j, x in enumerate(win) if j != k):
            highs.append((i, hist[i]["high"]))
        if all(hist[i]["low"] < x["low"] for j, x in enumerate(win) if j != k):
            lows.append((i, hist[i]["low"]))
    return highs, lows


def price_action(cands, atr):
    """Everything the signal rules need about price: patterns, swing structure, structure breaks,
    opening range."""
    pa = {"patterns": candle_patterns(cands, atr), "struct": "n/a", "event": "", "sh": None, "sl": None,
          "or_high": None, "or_low": None}
    if len(cands) >= OR_CANDLES + 1:
        orc = cands[:OR_CANDLES]
        pa["or_high"], pa["or_low"] = max(x["high"] for x in orc), min(x["low"] for x in orc)
    if len(cands) < 2:
        return pa
    highs, lows = swing_points(cands)
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
        lh, ll = highs[-1][1] < highs[-2][1], lows[-1][1] < lows[-2][1]
        pa["struct"] = "up" if (hh and hl) else "down" if (lh and ll) else "range"
    elif highs or lows:
        pa["struct"] = "range"
    pa["sh"] = highs[-1][1] if highs else None
    pa["sl"] = lows[-1][1] if lows else None
    c, pc = cands[-1], cands[-2]
    if pa["sh"] and pc["close"] <= pa["sh"] < c["close"]:
        pa["event"] = "CHoCH up" if pa["struct"] == "down" else "BOS up"
    elif pa["sl"] and pc["close"] >= pa["sl"] > c["close"]:
        pa["event"] = "CHoCH down" if pa["struct"] == "up" else "BOS down"
    return pa


def pa_text(pa):
    bits = [f"structure {pa['struct']}"]
    if pa["event"]:
        bits.append(pa["event"])
    bits += [n for n, _ in pa["patterns"]]
    return ", ".join(bits)


def max_pain(strikes):
    best, best_k = math.inf, None
    ks = sorted(strikes)
    for s in ks:
        pain = sum(strikes[k]["ce_oi"] * max(0, s - k) + strikes[k]["pe_oi"] * max(0, k - s) for k in ks)
        if pain < best:
            best, best_k = pain, s
    return best_k


def oi_levels(strikes, spot, prev_levels=None):
    """Resistance = biggest call OI above spot; support = biggest put OI below spot.
    Fresh levels = biggest day's OI addition near spot."""
    above = [k for k in strikes if spot <= k <= spot + LEVEL_WINDOW]
    below = [k for k in strikes if spot - LEVEL_WINDOW <= k <= spot]
    near = [k for k in strikes if abs(k - spot) <= FRESH_WINDOW]
    R = max(above, key=lambda k: strikes[k]["ce_oi"]) if above else None
    S = max(below, key=lambda k: strikes[k]["pe_oi"]) if below else None
    R2 = max(above, key=lambda k: strikes[k]["ce_chg"]) if above else None
    S2 = max(below, key=lambda k: strikes[k]["pe_chg"]) if below else None
    Rf = max(near, key=lambda k: strikes[k]["ce_chg"]) if near else None
    Sf = max(near, key=lambda k: strikes[k]["pe_chg"]) if near else None
    # hysteresis: keep the previous level unless the new one is clearly (10%+) bigger,
    # so levels don't flicker between two similar strikes every 5 minutes
    # A level that price has only just poked through (within LEVEL_STICKY) is kept too, so support does
    # not jump 500 points to the next big strike every time Nifty dips a few points under a round number.
    if prev_levels:
        pR, pS = prev_levels.get("R"), prev_levels.get("S")
        if pR in strikes and spot - LEVEL_STICKY <= pR <= spot + LEVEL_WINDOW + 100 * SCALE \
                and (R is None or strikes[R]["ce_oi"] < strikes[pR]["ce_oi"] * 1.10):
            R = pR
        if pS in strikes and spot - LEVEL_WINDOW - 100 * SCALE <= pS <= spot + LEVEL_STICKY \
                and (S is None or strikes[S]["pe_oi"] < strikes[pS]["pe_oi"] * 1.10):
            S = pS
    return {"R": R, "S": S, "R_fresh": R2, "S_fresh": S2, "CE_fresh_near": Rf, "PE_fresh_near": Sf}


_GLOBAL_CACHE = {"t": 0.0, "data": None}


def _gift_nifty(nse):
    g = (nse.get_json(URL_MARKET_STATUS) or {}).get("giftnifty") or {}
    last = _num(g.get("LASTPRICE"))
    if not last:
        return None
    return {"last": last, "chg": _num(g.get("DAYCHANGE")), "pct": _num(g.get("PERCHANGE")),
            "ts": g.get("TIMESTMP") or "", "expiry": g.get("EXPIRYDATE") or ""}


def _nasdaq_fut():
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{NASDAQ_SYMBOL}?interval=5m&range=1d",
                     headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Nasdaq futures: Yahoo Finance HTTP {r.status_code}")
    meta = r.json()["chart"]["result"][0]["meta"]
    last = _num(meta.get("regularMarketPrice"))
    prev = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
    if not last or not prev:
        return None
    return {"last": last, "chg": last - prev, "pct": (last / prev - 1) * 100}


def _short_err(e):
    t = str(e)
    if "HTTP 4" in t or "403" in t:
        return "blocked (HTTP 403)"
    if any(w in t for w in ("Max retries", "timed out", "Connection", "NameResolution", "proxy")):
        return "not reachable"
    return t[:60]


def global_bias(cues):
    """Majority of the clear moves (|day change| >= GLOBAL_MOVE_PCT) among GIFT Nifty and Nasdaq futures."""
    votes = 0
    for key in ("gift", "nasdaq"):
        q = (cues or {}).get(key)
        if q and q.get("pct") is not None:
            votes += 1 if q["pct"] >= GLOBAL_MOVE_PCT else -1 if q["pct"] <= -GLOBAL_MOVE_PCT else 0
    return "bullish" if votes > 0 else "bearish" if votes < 0 else "neutral"


def global_cues(client=None):
    """GIFT Nifty and Nasdaq-100 futures, cached for GLOBAL_CACHE_SEC. Never raises: a source that cannot be
    reached is left out (shown as '-') and the bias is worked out from what is left."""
    now = time.time()
    if _GLOBAL_CACHE["data"] is not None and now - _GLOBAL_CACHE["t"] < GLOBAL_CACHE_SEC:
        return _GLOBAL_CACHE["data"]
    out, errs = {"gift": None, "nasdaq": None}, []
    try:
        nse = client.nse() if hasattr(client, "nse") else (client if isinstance(client, NSEClient) else NSEClient())
        out["gift"] = _gift_nifty(nse)
    except Exception as e:                      # noqa: BLE001 - a missing cue must never stop the tracker
        errs.append(f"GIFT Nifty: {_short_err(e)}")
    try:
        out["nasdaq"] = _nasdaq_fut()
    except Exception as e:                      # noqa: BLE001
        errs.append(f"Nasdaq: {_short_err(e)}")
    out["bias"] = global_bias(out)
    out["errors"] = errs
    _GLOBAL_CACHE.update(t=now, data=out)
    return out


def global_text(cues):
    if not cues:
        return "Global cues: n/a"
    f = lambda q: f"{q['pct']:+.2f}%" if q else "-"
    return f"GIFT Nifty {f(cues.get('gift'))} · Nasdaq fut {f(cues.get('nasdaq'))} → {cues.get('bias', 'neutral')}"


def top_oi(strikes, n=3, spot=None):
    """Strikes with the highest call OI and the highest put OI within TOP_OI_WINDOW points of spot (wider than the
    support / resistance window), biggest first: the market's main call and put walls for the expiry. Far-away
    strikes are left out - a big position 2,500 points away (or a bad data row) says nothing about today."""
    win = TOP_OI_WINDOW * SCALE
    ks = [k for k in strikes if spot is None or abs(k - spot) <= win]
    ce = sorted((k for k in ks if strikes[k]["ce_oi"] > 0), key=lambda k: -strikes[k]["ce_oi"])[:n]
    pe = sorted((k for k in ks if strikes[k]["pe_oi"] > 0), key=lambda k: -strikes[k]["pe_oi"])[:n]
    return {"ce": ce, "pe": pe}


def summarize(chain, vix, candle, prev_levels=None):
    st = chain["strikes"]
    tce = sum(v["ce_oi"] for v in st.values())
    tpe = sum(v["pe_oi"] for v in st.values())
    cce = sum(v["ce_chg"] for v in st.values())
    cpe = sum(v["pe_chg"] for v in st.values())
    spot = candle["close"] if candle else chain["spot"]
    atm = min(st, key=lambda k: abs(k - spot))
    return {
        "time": candle["start"] + dt.timedelta(minutes=5) if candle else None,
        "candle": candle, "spot": spot, "chain_ts": chain["timestamp"], "vix": vix,
        "tce": tce, "tpe": tpe, "pcr": tpe / tce if tce else None,
        "chg_ce": cce, "chg_pe": cpe, "chg_pcr": (cpe / cce) if cce > 0 else None,
        "max_pain": max_pain(st), "atm": atm,
        "straddle": st[atm]["ce_ltp"] + st[atm]["pe_ltp"],
        "levels": oi_levels(st, spot, prev_levels), "strikes": st, "top_oi": top_oi(st, spot=spot),
    }


# --------------------------------------------------------------------------- option pricing
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, years, iv_pct, opt):
    """Black-Scholes premium (r = 0). opt = 'CE' or 'PE'."""
    sig = max(iv_pct, 1.0) / 100
    t = max(years, 1 / (365 * 24 * 12))           # floor at 5 minutes
    d1 = (math.log(spot / strike) + 0.5 * sig * sig * t) / (sig * math.sqrt(t))
    d2 = d1 - sig * math.sqrt(t)
    if opt == "CE":
        return spot * _ncdf(d1) - strike * _ncdf(d2)
    return strike * _ncdf(-d2) - spot * _ncdf(-d1)


def years_to_expiry(expiry, now):
    """expiry like '06-Oct-2026' (expires 15:30 IST); now = naive IST datetime."""
    try:
        exp = dt.datetime.strptime(expiry, "%d-%b-%Y").replace(hour=15, minute=30)
    except (TypeError, ValueError):
        return 2 / 365
    return max((exp - now).total_seconds(), 300) / (365 * 24 * 3600)


OPTION_HOLD_MIN = 30         # minutes of time decay assumed when estimating the premium at the stop / target


def implied_iv(prem, spot, strike, years, opt):
    """IV (%) that makes Black-Scholes equal the traded premium, or None if the premium has no time value.
    NSE's listed IV can be far from the LTP near expiry, so option stops and targets are priced from this."""
    intrinsic = max(spot - strike, 0.0) if opt == "CE" else max(strike - spot, 0.0)
    if prem <= intrinsic + 0.01:
        return None
    lo, hi = 1.0, 300.0
    if not bs_price(spot, strike, years, lo, opt) <= prem <= bs_price(spot, strike, years, hi, opt):
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, opt) < prem:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def option_levels(snap, k, opt, prem, entry, stop, target):
    """Estimated premium of strike k if the index reaches the stop / target: (p_stop, p_target).
    Priced at the IV implied by the actual premium, after OPTION_HOLD_MIN minutes of decay."""
    yrs = years_to_expiry(snap.get("expiry"), snap["time"])
    solved = implied_iv(prem, entry, k, yrs, opt)
    if solved is not None:
        later = max(yrs - OPTION_HOLD_MIN / (365 * 24 * 60), yrs / 2)
        return (max(0.05, bs_price(stop, k, later, solved, opt)),
                max(0.05, bs_price(target, k, later, solved, opt)))
    iv = snap["strikes"].get(k, {}).get(opt.lower() + "_iv") or DEFAULT_IV
    model_now = bs_price(entry, k, yrs, iv, opt)
    return (max(0.05, prem + bs_price(stop, k, yrs, iv, opt) - model_now),
            max(0.05, prem + bs_price(target, k, yrs, iv, opt) - model_now))


def leg_rr(prem, p_stop, p_target, short=False):
    """Reward:risk of one option leg from its own prices; None when the stop is not a loss."""
    risk = p_stop - prem if short else prem - p_stop
    reward = prem - p_target if short else p_target - prem
    return max(reward, 0.0) / risk if risk > 0 else None


def leg_at_spot(prem, entry, k, opt, sig_time, expiry, spot, stop, target, now, short=False):
    """One option leg re-priced for a spot price the user typed: estimated premium there, premium at the
    stop / target and the leg's reward:risk. Uses the IV implied by the idea's own premium."""
    iv = implied_iv(prem, entry, k, years_to_expiry(expiry, sig_time), opt) or DEFAULT_IV
    yrs = years_to_expiry(expiry, now)
    if now < sig_time or yrs * 365 * 24 * 3600 <= 300:      # an older idea, or expired: use the idea's own time
        yrs = years_to_expiry(expiry, sig_time)

    def after_move(level):
        """Premium at `level`: a closer stop or target is reached sooner, so it gets less time decay."""
        share = min(1.0, abs(level - spot) / max(1.0, abs(level - entry)))
        later = max(yrs - share * OPTION_HOLD_MIN / (365 * 24 * 60), yrs / 2)
        return max(0.05, bs_price(level, k, later, iv, opt))

    p_now = max(0.05, bs_price(spot, k, yrs, iv, opt))
    p_stop, p_tgt = after_move(stop), after_move(target)
    return {"strike": k, "opt": opt, "prem": p_now, "p_stop": p_stop, "p_target": p_tgt,
            "rr": leg_rr(p_now, p_stop, p_tgt, short)}


def check_spot(plan, sig_time, spot, now=None):
    """Risk and reward of a trade idea if entered at `spot`, using the idea's own stop and target.
    Returns {'ok': False, 'msg': ...} when the price is outside the stop-target range."""
    now = now or dt.datetime.now(IST).replace(tzinfo=None)
    short = plan["dir"] == "bearish"
    stop, target = plan["stop"], plan["target"]
    risk = stop - spot if short else spot - stop
    reward = spot - target if short else target - spot
    if risk <= 0:
        return {"ok": False, "msg": f"{spot:,.0f} is past the stop ({stop:,.0f}): this idea no longer applies."}
    if reward <= 0:
        return {"ok": False, "msg": f"{spot:,.0f} is past the target ({target:,.0f}): nothing left to gain."}
    out = {"ok": True, "risk": risk, "reward": reward, "rr": reward / risk, "buy": None, "sell": None}
    args = (sig_time, plan.get("expiry"), spot, stop, target, now)
    if not plan.get("sell_only"):
        out["buy"] = leg_at_spot(plan["prem"], plan["entry"], plan["strike"], plan["opt"], *args)
    sh = plan.get("short")
    if sh:
        out["sell"] = leg_at_spot(sh["prem"], plan["entry"], sh["strike"], sh["opt"], *args, short=True)
    return out


def filter_legs(plan):
    """Keep only the option legs that reach MIN_RR on their own prices. False when neither does."""
    buy_ok = not plan.get("sell_only") and \
        (leg_rr(plan["prem"], plan["p_stop"], plan["p_target"]) or 0) >= MIN_RR
    sh = plan.get("short")
    sell_ok = bool(sh) and (leg_rr(sh["prem"], sh["p_stop"], sh["p_target"], short=True) or 0) >= MIN_RR
    if not sell_ok:
        plan["short"] = None
    if sell_ok and not buy_ok:
        plan["sell_only"] = True
    return buy_ok or sell_ok


def rr_label(rr):
    """'Strong' at STRONG_RR or better, 'OK' from MIN_RR up to STRONG_RR, 'Skip' below MIN_RR."""
    if rr is None:
        return ""
    return "Strong" if rr >= STRONG_RR else "OK" if rr >= MIN_RR else "Skip"


def better_entry(direction, stop, target, rr=None):
    """Entry price at which reward:risk would reach `rr` (default MIN_RR) with the same stop and target.
    Long: (T - E) = rr * (E - S)  ->  E = (T + rr*S) / (1 + rr).  Short is the same formula."""
    rr = MIN_RR if rr is None else rr
    return round((target + rr * stop) / (1 + rr))


def is_expiry_day(snap):
    """True when the tracked (nearest) expiry is the snapshot's own trading day."""
    try:
        return dt.datetime.strptime(snap.get("expiry") or "", "%d-%b-%Y").date() == snap["time"].date()
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def view_for(snap, expiry):
    """The snapshot as seen from `expiry`: OI levels stay on the tracked chain, but option prices come from
    `expiry` (its real chain when loaded, otherwise a Black-Scholes estimate with this chain's IVs)."""
    if not expiry or expiry == snap.get("expiry"):
        return snap
    v = dict(snap)
    v["expiry"] = expiry
    if expiry == snap.get("next_expiry") and snap.get("next_strikes"):
        v["strikes"] = snap["next_strikes"]
    else:
        v["live"] = False
    return v


def trade_view(snap):
    """On expiry day, option ideas are quoted on NEXT week's contract: today's options lose their whole
    time value by 15:30, so buying them on an OI signal is mostly a bet against the clock."""
    if is_expiry_day(snap) and snap.get("next_expiry"):
        return view_for(snap, snap["next_expiry"])
    return snap


EXPIRY_DAY_NOTE = " · EXPIRY DAY: next week's contract, today's expires 15:30 (avoid buying today's options)"


def option_idea(snap, opt, entry, stop, target):
    """Turn an index trade (entry/stop/target in Nifty points) into an option-buying idea at the
    ATM strike: premium now, premium if the stop is hit, premium at the target (Black-Scholes using
    the strike's own IV; live mode anchors on the real LTP)."""
    rolled = trade_view(snap) is not snap
    snap = trade_view(snap)
    st = snap["strikes"]
    k = min(st, key=lambda x: abs(x - entry))
    v = st[k]
    side = opt.lower()
    iv = v.get(side + "_iv") or DEFAULT_IV
    yrs = years_to_expiry(snap.get("expiry"), snap["time"])
    model_now = bs_price(entry, k, yrs, iv, opt)
    ltp = v.get(side + "_ltp") or 0
    live = snap.get("live", False) and ltp > 0
    prem = ltp if live else model_now
    p_stop, p_tgt = option_levels(snap, k, opt, prem, entry, stop, target)
    exp = (snap.get("expiry") or "")[:6]
    days = yrs * 365
    warn = EXPIRY_DAY_NOTE if rolled else (" · expiry very near: premium decays fast, exit same day" if days < 1.5 else "")
    est = "" if live else " (est.)"
    return (f"BUY {INSTRUMENT} {exp} {k:,.0f} {opt} @ ~Rs {prem:,.1f}{est} | "
            f"SL Rs {p_stop:,.1f} ({NAME} {stop:,.0f}) | Target Rs {p_tgt:,.1f} ({NAME} {target:,.0f}){warn}")


def option_premium(snap, k, opt):
    """Premium of strike k at this snapshot: real LTP in live mode, Black-Scholes estimate in replay."""
    v = snap["strikes"].get(k, {})
    side = opt.lower()
    ltp = v.get(side + "_ltp") or 0
    if snap.get("live", False) and ltp > 0:
        return ltp, True
    iv = v.get(side + "_iv") or DEFAULT_IV
    return bs_price(snap["candle"]["close"], k, years_to_expiry(snap.get("expiry"), snap["time"]), iv, opt), False


SELL_COOLDOWN_MIN = 45       # minutes: one OI-wall sell idea per level inside this window
SELL_NEAR_ATR = 1.5          # the candle must come within this many average ranges of the OI wall
SHORT_MARGIN_PCT = 9.0       # rough margin to sell one option, % of index value x quantity (check your broker)


def short_plan(snap, direction, entry, stop, target, strike=None):
    """Option-selling version of the same idea: bullish -> SELL a put, bearish -> SELL a call, one strike
    out of the money (or `strike`). Premium now, and the estimated premium if the index reaches the stop
    (you buy back higher = loss) or the target (you buy back lower = profit)."""
    opt = "PE" if direction == "bullish" else "CE"
    snap = trade_view(snap)
    st = snap["strikes"]
    atm = min(st, key=lambda x: abs(x - entry))
    want = strike if strike is not None else (atm - STRIKE_STEP if opt == "PE" else atm + STRIKE_STEP)
    k = want if want in st else atm
    prem, real = option_premium(snap, k, opt)
    p_stop, p_tgt = option_levels(snap, k, opt, prem, entry, stop, target)
    return {"strike": k, "opt": opt, "prem": prem, "prem_real": real, "p_stop": p_stop, "p_target": p_tgt,
            "margin_per_unit": entry * SHORT_MARGIN_PCT / 100}


def trade_plan(snap, direction, entry, stop, target):
    snap = trade_view(snap)
    opt = "PE" if direction == "bearish" else "CE"
    k = min(snap["strikes"], key=lambda x: abs(x - entry))
    prem, real = option_premium(snap, k, opt)
    p_stop, p_tgt = option_levels(snap, k, opt, prem, entry, stop, target)
    plan = {"dir": direction, "entry": entry, "stop": stop, "target": target, "strike": k, "opt": opt,
            "prem": prem, "prem_real": real, "expiry": snap.get("expiry"),
            "p_stop": p_stop, "p_target": p_tgt}
    try:
        plan["short"] = short_plan(snap, direction, entry, stop, target)
    except (ValueError, ZeroDivisionError, KeyError):
        plan["short"] = None
    return plan


def money_short(entry_prem, exit_prem, lots=None, lot_size=None):
    """Rs result of SELLING `lots` x `lot_size` units at entry_prem and buying back at exit_prem."""
    qty = (lots or LOTS) * (lot_size or LOT_SIZE)
    sell, buy = entry_prem * qty, max(0.0, exit_prem) * qty
    ch = trade_charges(buy, sell)
    return {"qty": qty, "buy": buy, "sell": sell, "gross": sell - buy, "charges": ch, "net": sell - buy - ch}


def _skip_to_watch(s):
    """A skipped idea is not traded, but its plan is kept as 'watch only' so its outcome can still be followed
    and compared with the ideas that were taken. It never counts in the profit totals or the scorecard."""
    if s.get("plan"):
        s["watch"] = s["plan"]
    s["option"], s["plan"] = "", None


def evaluate_trades(snaps, signals):
    """Results of the trade ideas. Skipped ideas are followed the same way (sig["watch_result"]) but are
    not part of the returned results."""
    results = _follow_ideas(snaps, signals, "plan", "result")
    _follow_ideas(snaps, signals, "watch", "watch_result")
    return results


def watch_summary(signals):
    """Today's skipped ideas: how many were followed and how they would have ended."""
    rs = [s["watch_result"] for s in signals if s.get("watch_result")]
    done = [r for r in rs if r["status"] != "OPEN"]
    nets = [r["money"]["net"] for r in done if r.get("money")]
    return {"n": len(rs), "closed": len(done), "open": len(rs) - len(done),
            "wins": sum(1 for r in done if r["points"] > 0), "points": sum(r["points"] for r in done),
            "net": sum(nets) if nets else None}


def watch_history(data_dir, exclude=None):
    """Totals of the skipped ideas over previous LIVE days (watch.csv in each YYYY-MM-DD folder)."""
    out = {"days": 0, "n": 0, "wins": 0, "points": 0.0, "net": 0.0}
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for d in names:
        if d == exclude or len(d) != 10 or not d[:4].isdigit():
            continue
        f = os.path.join(data_dir, d, "watch.csv")
        if not os.path.exists(f):
            continue
        try:
            with open(f, newline="", encoding=CSV_ENCODING) as fh:
                recs = [r for r in csv.DictReader(fh) if r.get("result") not in (None, "", "OPEN")]
        except (OSError, ValueError):
            continue
        if not recs:
            continue
        out["days"] += 1
        for r in recs:
            try:
                pts = float(r["nifty_points"])
            except (KeyError, TypeError, ValueError):
                continue
            out["n"] += 1
            out["wins"] += pts > 0
            out["points"] += pts
            try:
                out["net"] += float(r.get("net_rs") or 0)
            except ValueError:
                pass
    return out


def _follow_ideas(snaps, signals, key, attach):
    """Follow every trade idea through the later 5-min candles.
    Short: stop hit if a candle HIGH reaches the stop, target hit if a candle LOW reaches the target.
    Long: the mirror. If both happen inside one candle we assume the STOP came first (conservative).
    Trades still running at 15:30 are closed at the last price."""
    results = []
    for sig in signals:
        p = sig.get(key)
        if not p:
            continue
        after = [x for x in snaps if x["time"] > sig["time"]]
        short = p["dir"] == "bearish"
        status, exit_px, exit_snap, best, worst = "OPEN", None, None, 0.0, 0.0
        for x in after:
            c = x["candle"]
            fav = (p["entry"] - c["low"]) if short else (c["high"] - p["entry"])
            adv = (c["high"] - p["entry"]) if short else (p["entry"] - c["low"])
            best, worst = max(best, fav), max(worst, adv)
            hit_stop = c["high"] >= p["stop"] if short else c["low"] <= p["stop"]
            hit_tgt = c["low"] <= p["target"] if short else c["high"] >= p["target"]
            if hit_stop:
                status, exit_px, exit_snap = "STOP HIT", p["stop"], x
                break
            if hit_tgt:
                status, exit_px, exit_snap = "TARGET HIT", p["target"], x
                break
        last = snaps[-1]
        if status == "OPEN" and after and last["time"].time() >= dt.time(15, 30):
            status, exit_px, exit_snap = "CLOSED 15:30", last["candle"]["close"], last
        ref_px = exit_px if exit_px is not None else (after[-1]["candle"]["close"] if after else p["entry"])
        pts = (p["entry"] - ref_px) if short else (ref_px - p["entry"])
        ref_snap = exit_snap or (after[-1] if after else None)
        if ref_snap is not None:
            ref_snap = view_for(ref_snap, p.get("expiry"))
        opt_pnl = None
        if ref_snap is not None:
            if exit_snap is not None and status in ("STOP HIT", "TARGET HIT") and not ref_snap.get("live"):
                # replay: price the option at the exact stop/target level
                v = ref_snap["strikes"].get(p["strike"], {})
                iv = v.get(p["opt"].lower() + "_iv") or DEFAULT_IV
                px = bs_price(exit_px, p["strike"], years_to_expiry(ref_snap.get("expiry"), ref_snap["time"]),
                              iv, p["opt"])
            else:
                px, _ = option_premium(ref_snap, p["strike"], p["opt"])
            opt_pnl = px - p["prem"]
        sh, short_exit = p.get("short"), None
        if sh and ref_snap is not None:
            if exit_snap is not None and status in ("STOP HIT", "TARGET HIT") and not ref_snap.get("live"):
                v = ref_snap["strikes"].get(sh["strike"], {})
                iv = v.get(sh["opt"].lower() + "_iv") or DEFAULT_IV
                short_exit = bs_price(exit_px, sh["strike"],
                                      years_to_expiry(ref_snap.get("expiry"), ref_snap["time"]), iv, sh["opt"])
            else:
                short_exit, _ = option_premium(ref_snap, sh["strike"], sh["opt"])
        results.append({"sig": sig, "status": status, "exit_time": exit_snap["time"] if exit_snap else None,
                        "points": pts, "best": best, "worst": worst, "opt_pnl": opt_pnl,
                        "opt_real": p["prem_real"],
                        "exit_prem": (p["prem"] + opt_pnl) if opt_pnl is not None else None,
                        "money": money(p["prem"], p["prem"] + opt_pnl)
                        if opt_pnl is not None and not p.get("sell_only") else None,
                        "short_exit_prem": short_exit,
                        "short_money": money_short(sh["prem"], short_exit) if short_exit is not None else None})
        sig[attach] = results[-1]
    return results


def trade_charges(buy_value, sell_value):
    """Approximate Indian option-buying charges for one round trip (buy + sell), in Rs."""
    brokerage = 2 * BROKERAGE_PER_ORDER
    stt = 0.001 * sell_value                       # 0.1% on sell-side premium
    exch = 0.0003503 * (buy_value + sell_value)    # NSE transaction charges
    sebi = 10e-7 * (buy_value + sell_value)        # Rs 10 per crore
    stamp = 0.00003 * buy_value                    # 0.003% on the buy side
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def money(entry_prem, exit_prem, lots=None, lot_size=None):
    """Rs result of buying `lots` x `lot_size` units at entry_prem and selling at exit_prem."""
    qty = (lots or LOTS) * (lot_size or LOT_SIZE)
    buy, sell = entry_prem * qty, max(0.0, exit_prem) * qty
    ch = trade_charges(buy, sell)
    return {"qty": qty, "buy": buy, "sell": sell, "gross": sell - buy, "charges": ch, "net": sell - buy - ch}


def money_summary(results):
    """Totals in Rs for the 'if you had taken every idea' view."""
    rs = [r for r in results if r.get("money")]
    closed = [r for r in rs if r["status"] != "OPEN"]
    opened = [r for r in rs if r["status"] == "OPEN"]
    curve, cum = [], 0.0
    for r in sorted(closed, key=lambda r: r["exit_time"] or r["sig"]["time"]):
        cum += r["money"]["net"]
        curve.append((r["exit_time"] or r["sig"]["time"], cum))
    return {"net": sum(r["money"]["net"] for r in closed),
            "open": sum(r["money"]["net"] for r in opened),
            "n_closed": len(closed), "n_open": len(opened),
            "wins": sum(1 for r in closed if r["money"]["net"] > 0),
            "grade_a": sum(r["money"]["net"] for r in closed if r["sig"].get("grade") == "A"),
            "charges": sum(r["money"]["charges"] for r in closed),
            "capital": max((r["money"]["buy"] for r in rs), default=0.0),
            "best": max((r["money"]["net"] for r in rs), default=0.0),
            "worst": min((r["money"]["net"] for r in rs), default=0.0),
            "curve": curve}


def history_pnl(data_dir, exclude=None):
    """Net Rs per previous LIVE day (folders named YYYY-MM-DD; replay/re-test folders are skipped).
    Older files without Rs columns are converted from option P&L per unit at the current lot settings."""
    rows = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return rows
    for d in names:
        if d == exclude or len(d) != 10 or not d[:4].isdigit() or not os.path.isdir(os.path.join(data_dir, d)):
            continue
        f = os.path.join(data_dir, d, "results.csv")
        if not os.path.exists(f):
            continue
        try:
            with open(f, newline="", encoding=CSV_ENCODING) as fh:
                recs = list(csv.DictReader(fh))
        except (OSError, ValueError):
            continue
        n = w = 0
        net = 0.0
        for r in recs:
            if r.get("result") in (None, "", "OPEN"):
                continue
            try:
                entry, unit = float(r["option_entry"]), float(r["option_pnl_per_unit"])
            except (KeyError, TypeError, ValueError):
                continue
            v = money(entry, entry + unit)["net"]
            n, w, net = n + 1, w + (v > 0), net + v
        if n:
            rows.append((d, n, w, net))
    return rows


def scorecard(results):
    done = [r for r in results if r["status"] != "OPEN"]
    wins = [r for r in done if r["points"] > 0]
    by_grade = {}
    for g in "ABC":
        rs = [r for r in done if r["sig"].get("grade") == g]
        if rs:
            by_grade[g] = (len(rs), sum(1 for r in rs if r["points"] > 0), sum(r["points"] for r in rs))
    rrs = [r["sig"]["plan"].get("rr") for r in results
           if r.get("sig") and r["sig"].get("plan") and r["sig"]["plan"].get("rr")]
    avg_rr = sum(rrs) / len(rrs) if rrs else None
    return {"avg_rr": avg_rr, "breakeven_wr": (100 / (1 + avg_rr)) if avg_rr else None,
            "trades": len(results), "closed": len(done), "open": len(results) - len(done), "wins": len(wins),
            "win_rate": (len(wins) / len(done) * 100) if done else None,
            "points": sum(r["points"] for r in done),
            "opt_pnl": sum(r["opt_pnl"] for r in done if r["opt_pnl"] is not None),
            "by_grade": by_grade}


def bias(pcr):
    if pcr is None:
        return "neutral"
    if pcr < 0.7:
        return "bearish"
    if pcr > 1.2:
        return "bullish"
    return "neutral"


# --------------------------------------------------------------------------- outlook (next 30 minutes)
# A probability read, NOT a prediction: five simple votes on where price is leaning, a likely range, and a
# running record of how often the call was right - so the number on screen is what it has actually earned.
OUTLOOK_CANDLES = 6          # how far ahead the outlook looks (6 five-minute candles = 30 minutes)
OUTLOOK_MIN_SCORE = 3        # net votes (of 5) needed for an UP / DOWN call; below this it says SIDEWAYS
OUTLOOK_BAND = 1.5           # likely range = close +/- this x average 5-min range x sqrt(candles ahead)
OUTLOOK_FLOW_CANDLES = 3     # OI-flow vote: put writing minus call writing over this many snapshots
OUTLOOK_CACHE = "outlook_history.json"


def outlook(snaps, i=None):
    """Where the index is leaning for the next OUTLOOK_CANDLES candles, from snaps[i] (default: the last).
    Votes (+1 up / -1 down / 0): 15-min trend, 5-min swing structure, price vs the opening range,
    recent OI flow (puts written minus calls written), and the day's change-in-OI PCR.
    Returns {dir: 'up'|'down'|'sideways', score, votes: [(name, vote, text)], low, high, band, time}."""
    if not snaps:
        return None
    i = len(snaps) - 1 if i is None else i
    s = snaps[i]
    spot, atr = s.get("spot"), s.get("atr")
    if spot is None or not atr:
        return None
    votes = []
    tr = (s.get("trend15") or {}).get("trend")
    votes.append(("15-min trend", {"up": 1, "down": -1}.get(tr, 0), (tr or "n/a").upper()))
    pa = s.get("pa") or price_action(s.get("candles") or [], atr)
    votes.append(("Structure", {"up": 1, "down": -1}.get(pa.get("struct"), 0), str(pa.get("struct", "n/a")).upper()))
    v, txt = 0, "inside / not set"
    if pa.get("or_high") is not None:
        if spot > pa["or_high"]:
            v, txt = 1, f"above {pa['or_high']:,.0f}"
        elif spot < pa["or_low"]:
            v, txt = -1, f"below {pa['or_low']:,.0f}"
        else:
            txt = f"inside {pa['or_low']:,.0f}-{pa['or_high']:,.0f}"
    votes.append(("Opening range", v, txt))
    v, txt = 0, "not enough snapshots"
    if i >= OUTLOOK_FLOW_CANDLES:
        p = snaps[i - OUTLOOK_FLOW_CANDLES]
        net = (s["chg_pe"] - p["chg_pe"]) - (s["chg_ce"] - p["chg_ce"])
        v = 1 if net > FLOW_THRESHOLD else -1 if net < -FLOW_THRESHOLD else 0
        txt = f"puts minus calls {net:+,.0f} in {OUTLOOK_FLOW_CANDLES * 5} min"
    votes.append(("OI flow", v, txt))
    cp = s.get("chg_pcr")
    votes.append(("Change-in-OI PCR", 0 if cp is None else 1 if cp > 1.2 else -1 if cp < 0.8 else 0,
                  "n/a" if cp is None else f"{cp:.2f}"))
    score = sum(x[1] for x in votes)
    d = "up" if score >= OUTLOOK_MIN_SCORE else "down" if score <= -OUTLOOK_MIN_SCORE else "sideways"
    band = OUTLOOK_BAND * atr * math.sqrt(OUTLOOK_CANDLES)
    return {"dir": d, "score": score, "votes": votes, "low": spot - band, "high": spot + band, "band": band,
            "spot": spot, "time": s.get("time")}


def outlook_scorecard(snaps):
    """Check every past outlook of the session against what price did over the next OUTLOOK_CANDLES candles.
    A call is right when the close that many candles later is beyond the close it was made at, in the called
    direction. in_band counts candles whose whole next-30-minute high/low stayed inside the likely range."""
    out = {"up": [0, 0], "down": [0, 0], "resolved": 0, "in_band": 0, "rose": 0, "last": None}
    h = OUTLOOK_CANDLES
    for i in range(len(snaps) - h):
        a, b = snaps[i], snaps[i + h]
        try:
            if (b["time"] - a["time"]).total_seconds() != h * 300:
                continue                       # a gap in the recording: no fair 30-minute comparison
        except (TypeError, KeyError):
            continue
        o = outlook(snaps, i)
        if not o:
            continue
        nxt = [x["candle"] for x in snaps[i + 1:i + h + 1] if x.get("candle")]
        if not nxt:
            continue
        out["resolved"] += 1
        out["rose"] += 1 if b["spot"] > a["spot"] else 0
        if max(c["high"] for c in nxt) <= o["high"] and min(c["low"] for c in nxt) >= o["low"]:
            out["in_band"] += 1
        if o["dir"] == "sideways":
            continue
        move = (b["spot"] - a["spot"]) * (1 if o["dir"] == "up" else -1)
        out[o["dir"]][0] += 1
        out[o["dir"]][1] += 1 if move > 0 else 0
        out["last"] = {"time": a["time"], "dir": o["dir"], "move": move, "hit": move > 0}
    out["calls"] = out["up"][0] + out["down"][0]
    out["hits"] = out["up"][1] + out["down"][1]
    return out


def outlook_history(data_dir, exclude=None):
    """The outlook's record over every day recorded in live mode (today left out via `exclude`). Each day is
    scored once and remembered in OUTLOOK_CACHE inside the data folder; the cache is thrown away whenever the
    outlook settings change. Returns totals {days, calls, hits, up, down, resolved, in_band}."""
    key = f"v2/{OUTLOOK_CANDLES}/{OUTLOOK_MIN_SCORE}/{OUTLOOK_BAND}/{OUTLOOK_FLOW_CANDLES}/{FLOW_THRESHOLD}"
    path = os.path.join(data_dir, OUTLOOK_CACHE)
    cache = {}
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    if cache.get("key") != key:
        cache = {"key": key, "days": {}}
    changed = False
    for src in recorded_live_days(data_dir):
        day = os.path.basename(src)
        if day == exclude or day in cache["days"]:
            continue
        try:
            snaps = [s for s, _ in replay_loaded_day(load_recorded_day(src), signals=False)]
            sc = outlook_scorecard(snaps)
        except Exception:
            continue
        cache["days"][day] = {k: sc[k] for k in ("up", "down", "resolved", "in_band", "rose")}
        changed = True
    if changed:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except OSError:
            pass
    tot = {"days": 0, "up": [0, 0], "down": [0, 0], "resolved": 0, "in_band": 0, "rose": 0}
    for day, r in cache["days"].items():
        if day == exclude:
            continue
        tot["days"] += 1
        for k in ("up", "down"):
            tot[k][0] += r[k][0]
            tot[k][1] += r[k][1]
        tot["resolved"] += r["resolved"]
        tot["in_band"] += r["in_band"]
        tot["rose"] += r.get("rose", 0)
    tot["calls"] = tot["up"][0] + tot["down"][0]
    tot["hits"] = tot["up"][1] + tot["down"][1]
    return tot


def outlook_text(o, today=None, hist=None):
    """Plain-text explanation of an outlook (tile tooltip / console)."""
    if not o:
        return "Outlook: needs a few candles first."
    pct = lambda h, n: f"{h}/{n} right ({h / n:.0%})" if n else "no calls yet"
    lines = [f"Outlook for the next {OUTLOOK_CANDLES * 5} minutes: {o['dir'].upper()} (net {o['score']:+d} of 5 votes; "
             f"a call needs {OUTLOOK_MIN_SCORE})",
             f"Likely range {o['low']:,.0f} - {o['high']:,.0f} (close +/- {o['band']:,.0f})", ""]
    lines += [f"  {'+' if v > 0 else '-' if v < 0 else '0'}  {name}: {txt}" for name, v, txt in o["votes"]]
    if today:
        lines += ["", f"Today: {pct(today['hits'], today['calls'])}"
                  + (f"; range held {today['in_band']}/{today['resolved']}" if today["resolved"] else "")]
    if hist and hist.get("days"):
        lines += [f"Last {hist['days']} recorded days: UP calls {pct(*reversed(hist['up']))}, "
                  f"DOWN calls {pct(*reversed(hist['down']))}",
                  f"Range held {hist['in_band']}/{hist['resolved']} times"
                  + (f" ({hist['in_band'] / hist['resolved']:.0%})" if hist["resolved"] else "")]
        if hist["resolved"]:
            up = hist["rose"] / hist["resolved"]
            lines += [f"To compare: price was higher 30 minutes later in {up:.0%} of ALL windows on those days, "
                      f"lower or flat in {1 - up:.0%}"]
    lines += ["", "A lean based on how these days went, not a prediction. It will be wrong often."]
    return "\n".join(lines)


def fmt_k(x):
    return f"{x:,.0f}"


# --------------------------------------------------------------------------- big-player activity
# What a large OI change at one strike most likely is, read from the option's own premium over the same
# 5 minutes: (OI up, premium up) = buyers in control; (OI up, premium down) = writers (sellers) in control;
# (OI down, premium up) = writers buying back; (OI down, premium down) = buyers selling out.
# The exchange does not publish WHO traded, so this is an inference from size, not an identification.
_BIG_READ = {
    ("ce", True, True): ("call BUYING", "bullish"), ("ce", True, False): ("call WRITING", "bearish"),
    ("ce", False, True): ("call SHORT COVERING", "bullish"), ("ce", False, False): ("call LONG UNWINDING", "bearish"),
    ("pe", True, True): ("put BUYING", "bearish"), ("pe", True, False): ("put WRITING", "bullish"),
    ("pe", False, True): ("put SHORT COVERING", "bearish"), ("pe", False, False): ("put LONG UNWINDING", "bullish"),
}


def big_money(st_prev, st, spot, move=0.0):
    """Strikes within TOP_OI_WINDOW of spot whose call or put OI changed by BIG_OI_JUMP contracts or more
    since the previous snapshot, biggest first. Each item says what the change most likely was."""
    out = []
    win = TOP_OI_WINDOW * SCALE
    for k, v in st.items():
        p = st_prev.get(k)
        if p is None or abs(k - spot) > win:
            continue
        for side in ("ce", "pe"):
            d = v[side + "_oi"] - p[side + "_oi"]
            if abs(d) < BIG_OI_JUMP or p[side + "_oi"] <= 0 and v[side + "_oi"] <= 0:
                continue
            l0, l1 = p.get(side + "_ltp") or 0, v.get(side + "_ltp") or 0
            if l0 > 0 and l1 > 0 and l0 != l1:
                prem_up = l1 > l0
            else:                              # no usable premium: a call gains when the index rises, a put when it falls
                prem_up = (move > 0) if side == "ce" else (move < 0)
            what, direction = _BIG_READ[(side, d > 0, prem_up)]
            dv = (v.get(side + "_vol") or 0) - (p.get(side + "_vol") or 0)
            out.append({"strike": k, "side": side, "d_oi": d, "oi": v[side + "_oi"],
                        "pct": d / p[side + "_oi"] * 100 if p[side + "_oi"] > 0 else None,
                        "ltp0": l0, "ltp1": l1, "what": what, "direction": direction,
                        "vol": dv if dv > 0 else None,
                        "crore": abs(d) * LOT_SIZE * (l1 or l0) / 1e7})
    return sorted(out, key=lambda e: -abs(e["d_oi"]))


def big_money_text(e):
    s = f'{e["strike"]:,.0f} {e["side"].upper()} OI {e["d_oi"]:+,.0f}'
    if e["pct"] is not None:
        s += f' ({e["pct"]:+.0f}%)'
    if e["ltp0"] and e["ltp1"]:
        s += f', premium {e["ltp0"]:,.1f} -> {e["ltp1"]:,.1f}'
    if e["crore"] >= 0.1:
        s += f', ~Rs {e["crore"]:,.1f} cr'
    if e["vol"]:
        s += f', volume {e["vol"]:,.0f}'
    return s + f': {e["what"]}'


# --------------------------------------------------------------------------- signal rules
def _days_to_expiry(snap):
    return years_to_expiry(snap.get("expiry"), snap["time"]) * 365 if snap.get("time") else 99


def check_signals(prev, cur):
    """Compare the previous snapshot with the current one and return a list of signal dicts.
    Levels are taken from the PREVIOUS snapshot so a signal only uses information known before
    the candle closed.

    Noise filters (see constants at the top):
      * a break needs a close at least BREAK_MIN points through the level, and the same level can
        only trigger one break per BREAK_COOLDOWN_MIN minutes (stops 22,500 ping-pong);
      * stops are sized to the day's average range, and ideas below MIN_RR reward:risk are dropped;
      * no new trade ideas after NO_NEW_TRADES_AFTER;
      * on expiry day / the day before, OI-flow and PCR signals are muted from EXPIRY_EVE_MUTE_FROM
        (that OI change is mostly positions being squared off, not direction);
      * in replay with ESTIMATED OI the per-candle OI-flow signals are skipped (straight-line OI says
        nothing about any single 5 minutes);
      * if one candle produces both bullish and bearish signals, they are all marked MIXED and any
        trade idea from that candle is downgraded to grade C."""
    sigs = []
    # carry the break log forward even when we return early
    cur["_break_log"] = dict((prev or {}).get("_break_log", {}))
    cur["_rev_done"] = dict((prev or {}).get("_rev_done", {}))
    cur["_sell_log"] = dict((prev or {}).get("_sell_log", {}))
    cur["_ideas_taken"] = dict((prev or {}).get("_ideas_taken", {}))
    cur["pa"] = price_action(cur.get("candles") or ([cur["candle"]] if cur.get("candle") else []), cur.get("atr"))
    cur["_tot_hist"] = ((prev or {}).get("_tot_hist", []) + [(cur["tce"], cur["tpe"])])[-(REV_OI_CANDLES + 1):]
    if cur.get("candle"):
        pdh, pdl = (prev or {}).get("day_high"), (prev or {}).get("day_low")
        cur["day_high"] = max(cur["candle"]["high"], pdh) if pdh else cur["candle"]["high"]
        cur["day_low"] = min(cur["candle"]["low"], pdl) if pdl else cur["candle"]["low"]
    if prev is None or cur["candle"] is None or prev["candle"] is None:
        return sigs
    c, pc = cur["candle"], prev["candle"]
    lv, st_prev, st = prev["levels"], prev["strikes"], cur["strikes"]
    b = bias(cur["pcr"])
    t_close = cur["time"].time()
    estimated = cur.get("oi_estimated", False)
    late = t_close > NO_NEW_TRADES_AFTER
    expiry_eve = _days_to_expiry(cur) < 1.5 and t_close >= EXPIRY_EVE_MUTE_FROM
    atr = cur.get("atr") or 0
    stop_dist = max(STOP_BUFFER, ATR_STOP_MULT * atr)

    def d_oi(k, side):
        if k not in st or k not in st_prev:
            return 0.0
        return st[k][side + "_oi"] - st_prev[k][side + "_oi"]

    pa = cur["pa"]

    def pa_score(direction, kind, ref=None):
        """Price-action points for a trade in `direction` (-2..+3) and a short explanation."""
        bull = direction == "bullish"
        want, opp = ("bullish", "bearish") if bull else ("bearish", "bullish")
        pts, why = 0, []
        same = [n for n, d in pa["patterns"] if d == want]
        against = [n for n, d in pa["patterns"] if d == opp]
        if same:
            pts += 1
            why.append(" + ".join(same))
        if against:
            pts -= 1
            why.append("AGAINST: " + " + ".join(against))
        ev_ok = pa["event"] in (("BOS up", "CHoCH up") if bull else ("BOS down", "CHoCH down"))
        if ev_ok:
            pts += 1
            why.append(pa["event"])
        elif pa["struct"] == ("up" if bull else "down"):
            pts += 1
            why.append("structure " + ("HH/HL" if bull else "LH/LL"))
        elif pa["struct"] == ("down" if bull else "up") and not kind.startswith("REVERSAL"):
            pts -= 1
            why.append("structure against (" + ("LH/LL" if bull else "HH/HL") + ")")
        tol = max(8 * SCALE, 0.5 * (atr or 0))
        if kind in ("BREAKOUT", "BREAKDOWN", "OR BREAKOUT", "OR BREAKDOWN"):
            lvls = [x for x in ((pa["sh"], pa["or_high"]) if bull else (pa["sl"], pa["or_low"])) if x]
            cleared = [x for x in lvls if (c["close"] > x if bull else c["close"] < x)]
            if cleared:
                pts += 1
                why.append(("cleared swing/OR high " if bull else "cleared swing/OR low ")
                           + f"{(max if bull else min)(cleared):,.0f}")
        else:
            ref = ref if ref is not None else (c["low"] if bull else c["high"])
            sup = [(pa["sl"] if bull else pa["sh"], "swing " + ("low" if bull else "high")),
                   (pa["or_low"] if bull else pa["or_high"], "OR " + ("low" if bull else "high")),
                   (round(ref, -2), "round number")]
            near = [f"{name} {v:,.0f}" for v, name in sup if v and abs(ref - v) <= tol]
            if near:
                pts += 1
                why.append("at " + near[0])
        return max(-2, min(3, pts)), ", ".join(why) or "nothing notable"

    def early_trend():
        """Trend before the 15-min trend exists: a close beyond the opening range decides, else the
        5-min swing structure, else 'sideways'."""
        oh, ol = pa.get("or_high"), pa.get("or_low")
        if oh and c["close"] > oh + BREAK_MIN:
            return "up"
        if ol and c["close"] < ol - BREAK_MIN:
            return "down"
        return {"up": "up", "down": "down"}.get(pa.get("struct"), "sideways")

    def add(kind, direction, text, trade=None, level=None, option="", plan=None, oi_conf=None, ref=None):
        agree = (direction == "bearish" and b == "bearish") or (direction == "bullish" and b == "bullish")
        against = (direction == "bearish" and b == "bullish") or (direction == "bullish" and b == "bearish")
        tag = "with PCR bias" if agree else ("AGAINST PCR bias" if against else "PCR neutral")
        tr = (cur.get("trend15") or {}).get("trend", "n/a")
        early = ""
        if EARLY_TREND and tr in ("n/a", None):
            tr, early = early_trend(), " (early: OR/structure)"
        t_with = (direction == "bearish" and tr == "down") or (direction == "bullish" and tr == "up")
        t_against = (direction == "bearish" and tr == "up") or (direction == "bullish" and tr == "down")
        grade, score, p_pts = "", None, None
        if direction in ("bearish", "bullish"):
            tag += " | " + ("with 15-min trend" if t_with else "AGAINST 15-min trend" if t_against
                            else "15-min trend sideways" if tr == "sideways" else "15-min trend n/a") + early
            gb = (cur.get("global") or {}).get("bias", "neutral")
            g_with = gb == direction
            g_against = gb in ("bullish", "bearish") and gb != direction
            if cur.get("global"):
                tag += " | " + ("with global cues" if g_with else "AGAINST global cues" if g_against
                                else "global cues neutral")
            p_pts, p_why = pa_score(direction, kind, ref)
            tag += f" | PA: {p_why}"
            if oi_conf is not None:
                # reversals fight the 15-min trend by design, so the trend is left out of their context
                ctx = (1 if agree else -1 if against else 0)
                if not kind.startswith("REVERSAL"):
                    ctx += (1 if t_with else -1 if t_against else 0)
                ctx += GLOBAL_WEIGHT * (1 if g_with else -1 if g_against else 0)
                score = oi_conf + p_pts + ctx
                grade = "A" if score >= SCORE_A else "B" if score >= SCORE_B else "C"
                tag += f" | score {score} = OI {oi_conf} + PA {p_pts:+d} + context {ctx:+d}"
        if estimated:
            tag += " | OI estimated"
        sigs.append({"time": cur["time"], "kind": kind, "direction": direction, "text": text,
                     "trade": trade or "", "level": level, "bias_note": tag, "price": c["close"],
                     "option": option, "grade": grade, "score": score, "plan": plan,
                     "oi_conf": oi_conf, "pa_pts": p_pts})

    key_levels = sorted({v for v in lv.values() if v is not None})

    def next_below(x):
        """nearest strike below x whose put OI is at least 60% of the biggest put OI nearby"""
        cands = [k for k in st_prev if x - LEVEL_WINDOW <= k < x - 40 * SCALE]
        if not cands:
            return round(x - 100 * SCALE, -1)
        top = max(st_prev[k]["pe_oi"] for k in cands)
        return max(k for k in cands if st_prev[k]["pe_oi"] >= 0.6 * top)

    def next_above(x):
        """nearest strike above x whose call OI is at least 60% of the biggest call OI nearby"""
        cands = [k for k in st_prev if x + 40 * SCALE < k <= x + LEVEL_WINDOW]
        if not cands:
            return round(x + 100 * SCALE, -1)
        top = max(st_prev[k]["ce_oi"] for k in cands)
        return min(k for k in cands if st_prev[k]["ce_oi"] >= 0.6 * top)

    def cooled(k):
        last = cur["_break_log"].get(k)
        return last is None or (cur["time"] - last).total_seconds() >= BREAK_COOLDOWN_MIN * 60

    def idea(direction, k, entry, stp, tgt, conf):
        """Return (trade text, option text, plan) or a reason why there is no trade.
        OI confirmation is no longer a hard gate: it is part of the confluence score (see add())."""
        if late:
            return f"No new trades after {NO_NEW_TRADES_AFTER:%H:%M}", "", None
        if expiry_eve:
            return f"No new trades after {EXPIRY_EVE_MUTE_FROM:%H:%M} this close to expiry", "", None
        capped = ""
        cap = min(TARGET_CAP_ATR, BUY_TARGET_CAP_ATR)
        if atr and abs(tgt - entry) > cap * atr:
            tgt = round(entry - cap * atr) if direction == "bearish" else round(entry + cap * atr)
            capped = ", target capped to today's range"
        risk, reward = abs(entry - stp), abs(tgt - entry)
        rr = reward / risk if risk else 0
        if rr < MIN_RR:
            be = better_entry(direction, stp, tgt)
            be_strong = better_entry(direction, stp, tgt, STRONG_RR)
            way = "pullback" if direction == "bullish" else "bounce"
            return (f"Skip: R:R 1:{int(rr * 10) / 10:.1f} below 1:{MIN_RR:g} (target {tgt:,.0f}, stop {stp:,.0f}). "
                    f"Wait for a {way} to ~{be:,.0f} for 1:{MIN_RR:g} (~{be_strong:,.0f} for 1:{STRONG_RR:g})",
                    "", None)
        side, opt = ("short", "PE") if direction == "bearish" else ("long", "CE")
        lbl = rr_label(rr)
        txt = (f"{NAME} {side}: entry ~{entry:,.0f}, stop {stp:,.0f}, target {tgt:,.0f} "
               f"(risk {risk:.0f} / reward {reward:.0f} pts, R:R 1:{int(rr * 10) / 10:.1f} {lbl}{capped})")
        plan = trade_plan(cur, direction, entry, stp, tgt)
        plan["rr"], plan["rr_label"] = round(rr, 2), lbl
        if not filter_legs(plan):
            return (f"Skip: {NAME} R:R 1:{int(rr * 10) / 10:.1f}, but neither option reaches 1:{MIN_RR:g} "
                    f"(target {tgt:,.0f}, stop {stp:,.0f})", "", None)
        if plan.get("sell_only"):
            sh = plan["short"]
            return txt, (f"SELL {INSTRUMENT} {(plan.get('expiry') or '')[:6]} {sh['strike']:,.0f} {sh['opt']} "
                         f"@ ~Rs {sh['prem']:,.1f} | buy back ~Rs {sh['p_stop']:,.1f} ({NAME} {stp:,.0f}) | "
                         f"target ~Rs {sh['p_target']:,.1f}"), plan
        return txt, option_idea(cur, opt, entry, stp, tgt), plan

    # 1 & 2: breakdown / breakout of an OI level on a 5-min CLOSE at least BREAK_MIN beyond it,
    #        confirmed by OI change at that strike
    for k in key_levels:
        if pc["close"] >= k - BREAK_MIN and c["close"] < k - BREAK_MIN and cooled(k):
            cur["_break_log"][k] = cur["time"]
            dpe, dce = d_oi(k, "pe"), d_oi(k, "ce")
            conf = (1 if dpe < 0 else 0) + (1 if dce > 0 else 0)
            strength = ["unconfirmed", "confirmed", "strongly confirmed"][conf]
            stp = round(k + stop_dist)
            tgt = next_below(c["close"])
            trade, option, plan = idea("bearish", k, c["close"], stp, tgt, conf)
            add("BREAKDOWN", "bearish",
                f"5-min close {c['close']:,.2f} below OI level {k:,.0f} ({strength}: "
                f"put OI {dpe:+,.0f}, call OI {dce:+,.0f} at {k:,.0f})", trade, k, option, plan, oi_conf=conf)
        if pc["close"] <= k + BREAK_MIN and c["close"] > k + BREAK_MIN and cooled(k):
            cur["_break_log"][k] = cur["time"]
            dpe, dce = d_oi(k, "pe"), d_oi(k, "ce")
            conf = (1 if dce < 0 else 0) + (1 if dpe > 0 else 0)
            strength = ["unconfirmed", "confirmed", "strongly confirmed"][conf]
            stp = round(k - stop_dist)
            tgt = next_above(c["close"])
            trade, option, plan = idea("bullish", k, c["close"], stp, tgt, conf)
            add("BREAKOUT", "bullish",
                f"5-min close {c['close']:,.2f} above OI level {k:,.0f} ({strength}: "
                f"call OI {dce:+,.0f}, put OI {dpe:+,.0f} at {k:,.0f})", trade, k, option, plan, oi_conf=conf)

    # 2b: REVERSAL at the day's low / high (catches V-turns that happen away from any OI level).
    #     Bullish: the day's low was made in the last REV_LOOKBACK candles after a fall of REV_MIN_SWING+
    #     points, and this candle closes green, above the previous close AND above the high of the
    #     low candle. Confirmed by total OI over the last REV_OI_CANDLES snapshots: put OI rising
    #     (put writing) and/or call OI falling (short covering). Bearish is the mirror at the day's high.
    #     Graded on OI, not on the 15-min trend (which always disagrees at a turn): A = both, B = one.
    rc = (cur.get("candles") or [])[-(REV_LOOKBACK + 13):]
    th = cur["_tot_hist"]
    if len(rc) >= 4 and len(th) >= 2 and cur.get("day_low") is not None:
        d_ce_n, d_pe_n = th[-1][0] - th[0][0], th[-1][1] - th[0][1]
        window = rc[-(REV_LOOKBACK + 1):-1]                # candles before this one
        pad = max(REV_STOP_PAD, 0.3 * atr)
        # -- bullish reversal from the day's low
        lo_c = min(window, key=lambda x: x["low"])
        before = [x for x in rc if x["start"] < lo_c["start"]]
        if (lo_c["low"] <= cur["day_low"] and c["low"] > lo_c["low"] and before
                and max(x["high"] for x in before) - lo_c["low"] >= REV_MIN_SWING
                and c["close"] > c["open"] and c["close"] > pc["close"] and c["close"] > lo_c["high"]
                and cur["_rev_done"].get("low") != lo_c["low"]):
            conf = (1 if d_pe_n > 0 else 0) + (1 if d_ce_n < 0 else 0)
            cur["_rev_done"]["low"] = lo_c["low"]
            stp = round(lo_c["low"] - pad)
            tgt = max(next_above(c["close"]), round(c["close"] + 2 * (c["close"] - stp)))   # OI level or 2R
            trade, option, plan = idea("bullish", None, c["close"], stp, tgt, conf)
            add("REVERSAL UP", "bullish",
                f"Day low {lo_c['low']:,.2f} at {lo_c['start'] + dt.timedelta(minutes=5):%H:%M}, now closed "
                f"{c['close']:,.2f} above that candle's high ({['no OI support', 'OI confirmed', 'OI strongly confirmed'][conf]}: "
                f"total put OI {d_pe_n:+,.0f}, call OI {d_ce_n:+,.0f} over {len(th) - 1} candles)",
                trade, lo_c["low"], option, plan, oi_conf=conf, ref=lo_c["low"])
        # -- bearish reversal from the day's high
        hi_c = max(window, key=lambda x: x["high"])
        before = [x for x in rc if x["start"] < hi_c["start"]]
        if (hi_c["high"] >= cur["day_high"] and c["high"] < hi_c["high"] and before
                and hi_c["high"] - min(x["low"] for x in before) >= REV_MIN_SWING
                and c["close"] < c["open"] and c["close"] < pc["close"] and c["close"] < hi_c["low"]
                and cur["_rev_done"].get("high") != hi_c["high"]):
            conf = (1 if d_ce_n > 0 else 0) + (1 if d_pe_n < 0 else 0)
            cur["_rev_done"]["high"] = hi_c["high"]
            stp = round(hi_c["high"] + pad)
            tgt = min(next_below(c["close"]), round(c["close"] - 2 * (stp - c["close"])))   # OI level or 2R
            trade, option, plan = idea("bearish", None, c["close"], stp, tgt, conf)
            add("REVERSAL DOWN", "bearish",
                f"Day high {hi_c['high']:,.2f} at {hi_c['start'] + dt.timedelta(minutes=5):%H:%M}, now closed "
                f"{c['close']:,.2f} below that candle's low ({['no OI support', 'OI confirmed', 'OI strongly confirmed'][conf]}: "
                f"total call OI {d_ce_n:+,.0f}, put OI {d_pe_n:+,.0f} over {len(th) - 1} candles)",
                trade, hi_c["high"], option, plan, oi_conf=conf, ref=hi_c["high"])

    # 2c: OPENING-RANGE BREAKOUT / BREAKDOWN (price action level), confirmed by total OI flow.
    #     A 5-min close BREAK_MIN+ beyond the 09:15-09:30 high/low, once per side per day, until OR_LAST_ENTRY.
    orh, orl = pa["or_high"], pa["or_low"]
    if orh and t_close <= OR_LAST_ENTRY and len(th) >= 2:
        d_ce_n, d_pe_n = th[-1][0] - th[0][0], th[-1][1] - th[0][1]
        if not cur["_rev_done"].get("orh") and pc["close"] <= orh + BREAK_MIN < c["close"]:
            cur["_rev_done"]["orh"] = True
            cur["_rev_done"]["orh_t"] = cur["time"]
            conf = (1 if d_pe_n > 0 else 0) + (1 if d_ce_n < 0 else 0)
            stp = round(orh - stop_dist)
            trade, option, plan = idea("bullish", None, c["close"], stp, next_above(c["close"]), conf)
            add("OR BREAKOUT", "bullish",
                f"5-min close {c['close']:,.2f} above the opening-range high {orh:,.2f} "
                f"(total put OI {d_pe_n:+,.0f}, call OI {d_ce_n:+,.0f} over {len(th) - 1} candles)",
                trade, orh, option, plan, oi_conf=conf)
        if not cur["_rev_done"].get("orl") and pc["close"] >= orl - BREAK_MIN > c["close"]:
            cur["_rev_done"]["orl"] = True
            cur["_rev_done"]["orl_t"] = cur["time"]
            conf = (1 if d_ce_n > 0 else 0) + (1 if d_pe_n < 0 else 0)
            stp = round(orl + stop_dist)
            trade, option, plan = idea("bearish", None, c["close"], stp, next_below(c["close"]), conf)
            add("OR BREAKDOWN", "bearish",
                f"5-min close {c['close']:,.2f} below the opening-range low {orl:,.2f} "
                f"(total call OI {d_ce_n:+,.0f}, put OI {d_pe_n:+,.0f} over {len(th) - 1} candles)",
                trade, orl, option, plan, oi_conf=conf)

    # 3: rejection at resistance
    R = lv.get("R")
    for Rk in {R, lv.get("CE_fresh_near")}:
        if Rk and Rk > pc["close"] and c["high"] >= Rk - 10 * SCALE and c["close"] < Rk - 15 * SCALE and c["close"] < c["open"]:
            add("REJECTION", "bearish",
                f"Tested resistance {Rk:,.0f} (high {c['high']:,.2f}) and closed red at {c['close']:,.2f}; "
                f"call OI there {d_oi(Rk, 'ce'):+,.0f}", f"Resistance holding - avoid longs below {Rk:,.0f}", Rk)

    # 4: bounce at support
    S = lv.get("S")
    for Sk in {S, lv.get("PE_fresh_near")}:
        if Sk and Sk < pc["close"] and c["low"] <= Sk + 10 * SCALE and c["close"] > Sk + 15 * SCALE and c["close"] > c["open"]:
            add("BOUNCE", "bullish",
                f"Tested support {Sk:,.0f} (low {c['low']:,.2f}) and closed green at {c['close']:,.2f}; "
                f"put OI there {d_oi(Sk, 'pe'):+,.0f}", f"Support holding - avoid shorts above {Sk:,.0f}", Sk)

    # 4b: option-selling ideas at the OI walls - SELL CE under the max-call-OI strike while it caps the
    #     price, SELL PE above the max-put-OI strike while it holds. Index stop just beyond the wall;
    #     the profit comes mostly from the premium shrinking, so the index target is modest.
    def sell_idea(opt, wall):
        bear = opt == "CE"
        key = f"{opt}{wall:.0f}"
        last = cur["_sell_log"].get(key)
        if last is not None and (cur["time"] - last).total_seconds() < SELL_COOLDOWN_MIN * 60:
            return
        near = max(LEVEL_STICKY, SELL_NEAR_ATR * atr)
        rng = max(c["high"] - c["low"], 0.01)
        if bear:
            ok = (c["close"] < wall and c["high"] < wall + BREAK_MIN and wall - c["high"] <= near
                  and (c["close"] < c["open"] or (c["high"] - c["close"]) >= 0.4 * rng))
        else:
            ok = (c["close"] > wall and c["low"] > wall - BREAK_MIN and c["low"] - wall <= near
                  and (c["close"] > c["open"] or (c["close"] - c["low"]) >= 0.4 * rng))
        if not ok:
            return
        cur["_sell_log"][key] = cur["time"]
        side = "ce" if bear else "pe"
        d_wall = d_oi(wall, side)
        conf = 1 + (1 if d_wall > 0 else 0)          # the biggest OI wall, plus writers still adding
        entry = c["close"]
        stp = round(wall + stop_dist) if bear else round(wall - stop_dist)
        risk = abs(stp - entry)
        tgt = round(entry - 1.5 * risk) if bear else round(entry + 1.5 * risk)
        if atr and abs(tgt - entry) > TARGET_CAP_ATR * atr:
            tgt = round(entry - TARGET_CAP_ATR * atr) if bear else round(entry + TARGET_CAP_ATR * atr)
        rr = abs(tgt - entry) / risk if risk else 0
        kind = "SELL CE" if bear else "SELL PE"
        where = (f"Stayed under the call-OI wall {wall:,.0f} (high {c['high']:,.2f}, close {c['close']:,.2f})" if bear
                 else f"Held above the put-OI wall {wall:,.0f} (low {c['low']:,.2f}, close {c['close']:,.2f})")
        text = f"{where}; {'call' if bear else 'put'} OI there {d_wall:+,.0f} - writers defending it"
        if late or expiry_eve:
            add(kind, "bearish" if bear else "bullish", text,
                f"No new trades after {NO_NEW_TRADES_AFTER:%H:%M}" if late else
                f"No new trades after {EXPIRY_EVE_MUTE_FROM:%H:%M} this close to expiry", wall, oi_conf=conf)
            return
        if rr < MIN_RR:
            be = better_entry("bearish" if bear else "bullish", stp, tgt)
            add(kind, "bearish" if bear else "bullish", text,
                f"Skip: R:R 1:{int(rr * 10) / 10:.1f} below 1:{MIN_RR:g} (stop {stp:,.0f}, target {tgt:,.0f}); "
                f"better entry ~{be:,.0f}", wall, oi_conf=conf)
            return
        plan = trade_plan(cur, "bearish" if bear else "bullish", entry, stp, tgt)
        plan["sell_only"] = True
        plan["rr"], plan["rr_label"] = round(rr, 2), rr_label(rr)
        try:
            plan["short"] = short_plan(cur, "bearish" if bear else "bullish", entry, stp, tgt, strike=wall)
        except (ValueError, ZeroDivisionError, KeyError):
            return
        if not filter_legs(plan):
            add(kind, "bearish" if bear else "bullish", text,
                f"Skip: {NAME} R:R 1:{int(rr * 10) / 10:.1f}, but the option does not reach 1:{MIN_RR:g}",
                wall, oi_conf=conf)
            return
        sh = plan["short"]
        exp = (plan.get("expiry") or "")[:6]
        trade = (f"Sell {wall:,.0f} {opt}: buy back if {NAME} reaches {stp:,.0f}; "
                 f"aim {NAME} {tgt:,.0f} or keep ~half the premium (risk {risk:.0f} pts, R:R 1:{int(rr * 10) / 10:.1f} {rr_label(rr)})")
        option = (f"SELL {INSTRUMENT} {exp} {sh['strike']:,.0f} {opt} @ ~Rs {sh['prem']:,.1f} | "
                  f"buy back ~Rs {sh['p_stop']:,.1f} ({NAME} {stp:,.0f}) | target ~Rs {sh['p_target']:,.1f}")
        add(kind, "bearish" if bear else "bullish", text, trade, wall, option, plan, oi_conf=conf)

    if R and not any(x["kind"] in ("BREAKOUT", "OR BREAKOUT") for x in sigs):
        sell_idea("CE", R)
    if S and not any(x["kind"] in ("BREAKDOWN", "OR BREAKDOWN") for x in sigs):
        sell_idea("PE", S)

    # 5: OI flow over the last 5 minutes (skipped with estimated OI and near expiry close)
    move = c["close"] - pc["close"]
    dce_t, dpe_t = cur["tce"] - prev["tce"], cur["tpe"] - prev["tpe"]
    if abs(move) >= PRICE_MOVE_MIN and not estimated and not expiry_eve:
        if move > 0 and dce_t <= -FLOW_THRESHOLD:
            add("SHORT COVERING", "bullish", f"{NAME} {move:+.0f} pts while total call OI {dce_t:+,.0f}",
                "Call writers exiting - upside can accelerate")
        if move > 0 and dpe_t >= FLOW_THRESHOLD:
            add("PUT WRITING", "bullish", f"{NAME} {move:+.0f} pts with total put OI {dpe_t:+,.0f}",
                "Fresh support being built")
        if move < 0 and dpe_t <= -FLOW_THRESHOLD:
            add("PUT UNWINDING", "bearish", f"{NAME} {move:+.0f} pts while total put OI {dpe_t:+,.0f}",
                "Put writers exiting - supports weakening")
        if move < 0 and dce_t >= FLOW_THRESHOLD:
            add("CALL WRITING", "bearish", f"{NAME} {move:+.0f} pts with total call OI {dce_t:+,.0f}",
                "Fresh resistance being built")

    # 5b: CONTINUATION - the same-side OI flow on this candle AND the previous one, with price structure
    #     agreeing, after the opening range. Entry at the close, stop below the last two candles' low
    #     (or the ATR stop if wider), target the next OI level.
    flow_bull = {x["kind"] for x in sigs if x["kind"] in ("PUT WRITING", "SHORT COVERING")}
    flow_bear = {x["kind"] for x in sigs if x["kind"] in ("CALL WRITING", "PUT UNWINDING")}
    cur["_flow"] = {"bullish": sorted(flow_bull), "bearish": sorted(flow_bear)}
    if CONTINUATION and not estimated and t_close >= dt.time(9, 45):
        pf = (prev.get("_flow") or {})
        for d, now_k, prev_k in (("bullish", flow_bull, pf.get("bullish")), ("bearish", flow_bear, pf.get("bearish"))):
            if not now_k or not prev_k:
                continue
            bull = d == "bullish"
            tr_now = (cur.get("trend15") or {}).get("trend", "n/a")
            if tr_now in ("n/a", None):
                tr_now = early_trend()
            ev = pa.get("event") or ""
            struct_ok = (pa.get("struct") == ("up" if bull else "down")
                         or ev in (("BOS up", "CHoCH up") if bull else ("BOS down", "CHoCH down")))
            if tr_now != ("up" if bull else "down") or not struct_ok:
                continue
            if any(x.get("plan") and x["direction"] == d for x in sigs):
                continue
            key = "cont_" + d
            last = cur["_rev_done"].get(key)
            if last is not None and (cur["time"] - last).total_seconds() < CONT_COOLDOWN_MIN * 60:
                continue
            cur["_rev_done"][key] = cur["time"]
            entry = c["close"]
            if bull:
                stp = round(min(entry - stop_dist, min(c["low"], pc["low"]) - 5 * SCALE))
                tgt = next_above(entry)
            else:
                stp = round(max(entry + stop_dist, max(c["high"], pc["high"]) + 5 * SCALE))
                tgt = next_below(entry)
            both = set(now_k) | set(prev_k)
            conf = 2 if len(both) >= 2 else 1
            trade, option, plan = idea(d, None, entry, stp, tgt, conf)
            add("CONTINUATION UP" if bull else "CONTINUATION DOWN", d,
                f"Two candles of {'/'.join(sorted(both)).lower()} with structure {'up' if bull else 'down'} "
                f"({ev or pa.get('struct')})", trade, None, option, plan, oi_conf=conf)

    # 6: PCR regime change
    if not expiry_eve:
        for th in (0.7, 1.2):
            if prev["pcr"] and cur["pcr"]:
                if prev["pcr"] < th <= cur["pcr"]:
                    add("PCR UP", "bullish", f"PCR crossed above {th} ({prev['pcr']:.3f} -> {cur['pcr']:.3f})")
                if prev["pcr"] >= th > cur["pcr"]:
                    add("PCR DOWN", "bearish", f"PCR crossed below {th} ({prev['pcr']:.3f} -> {cur['pcr']:.3f})")

    # 7: VIX jump
    if prev["vix"] and cur["vix"] and (cur["vix"] / prev["vix"] - 1) * 100 >= VIX_JUMP_PCT:
        add("VIX JUMP", "caution", f"India VIX {prev['vix']:.2f} -> {cur['vix']:.2f} "
            f"({(cur['vix'] / prev['vix'] - 1) * 100:+.1f}%)", "Volatility rising - widen stops or reduce size")

    # 8: level shifts
    for key, name in (("R", "Resistance (max call OI)"), ("S", "Support (max put OI)")):
        a, z = prev["levels"].get(key), cur["levels"].get(key)
        if a and z and a != z:
            add("LEVEL SHIFT", "bearish" if z < a else "bullish", f"{name} moved {a:,.0f} -> {z:,.0f}")

    # 8b: the highest-OI strike of the whole chain moved (call wall or put wall). The new strike must hold
    #     5%+ more OI than the last ANNOUNCED one, so two near-equal strikes do not flip-flop every candle,
    #     while a gradual take-over across several candles is still reported once.
    st_now = cur.get("strikes") or {}
    ct = top_oi(st_now, spot=c["close"])
    ann = dict(prev.get("top_ann") or {k: (v[0] if v else None)
                                       for k, v in top_oi(prev.get("strikes") or {}, spot=pc["close"]).items()})
    for side, name in (("ce", "Highest call OI"), ("pe", "Highest put OI")):
        a, z = ann.get(side), (ct[side][0] if ct[side] else None)
        if a is None or z is None:
            ann[side] = a or z
            continue
        if a != z and (a not in st_now or st_now[z][side + "_oi"] >= st_now[a][side + "_oi"] * 1.05):
            add("MAX OI SHIFT", "bullish" if z > a else "bearish",
                f"{name} strike moved {a:,.0f} -> {z:,.0f} ({st_now[z][side + '_oi']:,.0f} contracts)",
                "Call wall moving up - room above" if side == "ce" and z > a else
                "Call wall moving down - ceiling lowering" if side == "ce" else
                "Put wall moving up - floor rising" if z > a else "Put wall moving down - support giving way")
            ann[side] = z
    cur["top_ann"] = ann

    # 8c: BIG MONEY - an unusually large OI change at one strike inside this candle (information only: it
    #     carries no trade idea and does not count as a conflict). Skipped with estimated OI and near expiry
    #     close, where the change is mostly positions being squared off.
    if BIG_MONEY and not estimated and not expiry_eve:
        big = big_money(st_prev, st, c["close"], move)
        cur["big_money"] = big
        notes = {"bullish": "Large players adding on the bullish side - watch for follow-through, not a trade idea by itself",
                 "bearish": "Large players adding on the bearish side - watch for follow-through, not a trade idea by itself"}
        for d in ("bullish", "bearish"):
            ev = [e for e in big if e["direction"] == d]
            if ev:
                more = f" (+{len(ev) - BIG_MAX_LISTED} more)" if len(ev) > BIG_MAX_LISTED else ""
                add("BIG MONEY", d, "; ".join(big_money_text(e) for e in ev[:BIG_MAX_LISTED]) + more, notes[d])

    # 9: conflicting signals in one candle -> mark MIXED, downgrade any trade idea to C
    #    (level shifts are informational and do not count as a conflict)
    dirs = {s["direction"] for s in sigs if s["kind"] not in ("LEVEL SHIFT", "MAX OI SHIFT", "BIG MONEY")}
    if {"bullish", "bearish"} <= dirs:
        for s in sigs:
            s["bias_note"] += " | MIXED signals this candle"
            if s.get("plan"):
                s["grade"] = "C"
    if BLOCK_CTR_REV:
        for s in sigs:
            if not s.get("plan") or not s["kind"].startswith("REVERSAL"):
                continue
            key = "orh_t" if s["direction"] == "bearish" else "orl_t"
            t_or = cur["_rev_done"].get(key)
            if t_or is not None and 0 <= (cur["time"] - t_or).total_seconds() <= REV_BLOCK_MIN * 60:
                s["trade"] = (f"Skip: against the opening-range {'breakout' if key == 'orh_t' else 'breakdown'} "
                              f"at {t_or:%H:%M} - was: {s['trade']}")
                _skip_to_watch(s)
    if BLOCK_AGAINST_PCR:
        for s in sigs:
            if s.get("plan") and s.get("bias_note", "").startswith("AGAINST PCR bias"):
                s["trade"] = f"Skip: against the PCR bias (these lost 5 of 5 in the 19-30 Oct log) - was: {s['trade']}"
                _skip_to_watch(s)
    for s in sigs:
        if (RR_OVERRIDE and s.get("plan") and s.get("grade") and s["grade"] > MIN_GRADE
                and "MIXED" not in s["bias_note"] and (s["plan"].get("rr") or 0) >= STRONG_RR
                and (s.get("oi_conf") or 0) >= 1 and (s.get("pa_pts") or 0) >= 1):
            s["bias_note"] += f" | R:R override (1:{s['plan']['rr']:.1f} >= 1:{STRONG_RR:g}, OI + PA confirm)"
            s["grade"] = MIN_GRADE
    for s in sigs:
        if s.get("plan") and s.get("grade") and s["grade"] > MIN_GRADE:
            why = ("mixed signals" if "MIXED" in s["bias_note"]
                   else f"score {s.get('score')} below {SCORE_B} - not enough OI + price-action confluence")
            s["trade"] = f"Skip: grade {s['grade']} ({why}) - was: {s['trade']}"
            _skip_to_watch(s)
    # 10: accuracy filters for option-BUYING ideas (sell ideas are left alone):
    #     no new buying ideas after BUY_LAST_ENTRY, one idea per direction per candle (highest score wins),
    #     and at most MAX_IDEAS_PER_SIDE per direction per day.
    last_buy = dt.time(int(BUY_LAST_ENTRY) // 100, int(BUY_LAST_ENTRY) % 100)
    taken_now = set()
    for s in sorted([x for x in sigs if x.get("plan") and not x["plan"].get("sell_only")],
                    key=lambda x: -(x.get("score") or 0)):
        d = s["direction"]
        why = None
        if t_close > last_buy:
            why = f"no new buying ideas after {last_buy:%H:%M} (afternoon breakouts mostly failed in testing)"
        elif d in taken_now:
            why = "another idea in the same direction on this candle"
        elif cur["_ideas_taken"].get(d, 0) >= MAX_IDEAS_PER_SIDE:
            why = f"already {MAX_IDEAS_PER_SIDE} {'long' if d == 'bullish' else 'short'} ideas today"
        if why:
            s["trade"] = f"Skip: {why} - was: {s['trade']}"
            _skip_to_watch(s)
        else:
            taken_now.add(d)
            cur["_ideas_taken"][d] = cur["_ideas_taken"].get(d, 0) + 1
    # 11: one idea per setup - a buying idea and a selling idea in the same direction on the same candle
    #     are the same bet twice; keep only the higher-scoring one.
    if ONE_IDEA_PER_SETUP:
        kept = set()
        for s in sorted([x for x in sigs if x.get("plan")], key=lambda x: -(x.get("score") or 0)):
            if s["direction"] in kept:
                s["trade"] = f"Skip: same setup already has an idea in this direction on this candle - was: {s['trade']}"
                _skip_to_watch(s)
            else:
                kept.add(s["direction"])
    for s in sigs:
        if s.get("option") and s.get("grade"):
            s["option"] = f"[Grade {s['grade']}] " + s["option"]
    return sigs


# --------------------------------------------------------------------------- output
def _global_cols(g):
    g = g or {}
    gi, nq = g.get("gift") or {}, g.get("nasdaq") or {}
    r = lambda v, n: round(v, n) if isinstance(v, (int, float)) else ""
    return [r(gi.get("last"), 2), r(gi.get("pct"), 2), r(nq.get("last"), 2), r(nq.get("pct"), 2),
            g.get("bias", "") if g else ""]


class Recorder:
    SNAP_COLS = ["time", "open", "high", "low", "close", "vix", "total_ce_oi", "total_pe_oi", "pcr",
                 "day_chg_ce_oi", "day_chg_pe_oi", "chg_pcr", "max_pain", "atm", "atm_straddle",
                 "resistance", "support", "fresh_call_strike", "fresh_put_strike", "chain_timestamp", "bias",
                 "trend_15m", "expiry", "avg_range", "structure", "pa_event", "patterns",
                 "gift_nifty", "gift_pct", "nasdaq_fut", "nasdaq_pct", "global_bias"]

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.snap_path = os.path.join(folder, "snapshots.csv")
        self.strike_path = os.path.join(folder, "strikes.csv")
        self.sig_path = os.path.join(folder, "signals.csv")
        self.dash_path = os.path.join(folder, "dashboard.html")
        self.snaps, self.signals = [], []
        self.status = ""
        self.mode, self.day, self.expiry = "live", None, None
        self.autosave = True

    @staticmethod
    def _append(path, header, row):
        for attempt in range(6):
            try:
                new = not os.path.exists(path)
                with open(path, "a", newline="", encoding=CSV_ENCODING) as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(header)
                    w.writerow(row)
                return
            except PermissionError:
                time.sleep(0.5)            # file open in Excel / being scanned - try again
        print(f"  ! could not write {os.path.basename(path)} (is it open in Excel? close it)")

    def record(self, snap, sigs):
        c = snap["candle"]
        lv = snap["levels"]
        t = snap["time"].strftime("%H:%M")
        self._append(self.snap_path, self.SNAP_COLS, [
            t, c["open"], c["high"], c["low"], c["close"], snap["vix"], int(snap["tce"]), int(snap["tpe"]),
            round(snap["pcr"], 3) if snap["pcr"] else "", int(snap["chg_ce"]), int(snap["chg_pe"]),
            round(snap["chg_pcr"], 3) if snap["chg_pcr"] else "", snap["max_pain"], snap["atm"],
            round(snap["straddle"], 2), lv["R"], lv["S"], lv["CE_fresh_near"], lv["PE_fresh_near"],
            snap["chain_ts"], bias(snap["pcr"]), (snap.get("trend15") or {}).get("trend", ""),
            snap.get("expiry") or "", round(snap["atr"], 1) if snap.get("atr") else "",
            (snap.get("pa") or {}).get("struct", ""), (snap.get("pa") or {}).get("event", ""),
            "; ".join(n for n, _ in (snap.get("pa") or {}).get("patterns", []))]
            + _global_cols(snap.get("global")))
        for k in sorted(snap["strikes"]):
            if abs(k - snap["atm"]) <= 12 * STRIKE_STEP:
                v = snap["strikes"][k]
                self._append(self.strike_path, ["time", "strike", "ce_oi", "ce_day_chg", "ce_ltp", "ce_iv",
                                                "pe_oi", "pe_day_chg", "pe_ltp", "pe_iv"],
                             [t, k, int(v["ce_oi"]), int(v["ce_chg"]), v["ce_ltp"], v["ce_iv"],
                              int(v["pe_oi"]), int(v["pe_chg"]), v["pe_ltp"], v["pe_iv"]])
        for s in sigs:
            self._append(self.sig_path, ["time", "signal", "direction", "grade", "price", "details", "trade_idea",
                                          "option_trade", "bias_note"],
                         [t, s["kind"], s["direction"], s.get("grade", ""), s["price"], s["text"], s["trade"],
                          s.get("option", ""), s["bias_note"]])
        self.snaps.append(snap)
        self.signals.extend(sigs)
        if self.autosave:
            self.write_dashboard()

    # ---- dashboard -----------------------------------------------------------------
    def write_dashboard(self):
        """Never let a locked file stop the tracker: write to a temp file, then swap it in, with retries."""
        try:
            self._write_dashboard()
        except Exception as e:  # keep tracking whatever happens
            print(f"  ! dashboard/results not updated this time ({e}); will retry next candle")

    @staticmethod
    def _safe_write(path, text, encoding="utf-8"):
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding=encoding) as f:
            f.write(text)
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.3)
        raise PermissionError(f"{os.path.basename(path)} is locked by another program")

    def _write_dashboard(self):
        import io
        results = evaluate_trades(self.snaps, self.signals) if self.snaps else []
        buf = io.StringIO()
        if True:
            w = csv.writer(buf)
            w.writerow(["signal_time", "signal", "grade", "nifty_entry", "stop", "target", "option", "option_entry",
                        "result", "exit_time", "nifty_points", "best_move", "worst_move", "option_pnl_per_unit",
                        "option_prices", "option_exit", "quantity", "invested_rs", "gross_rs", "charges_rs",
                        "net_rs"])
            for r in results:
                p, sg = r["sig"]["plan"], r["sig"]
                if p.get("sell_only") and p.get("short"):
                    sh, sm = p["short"], r.get("short_money")
                    w.writerow([f'{sg["time"]:%H:%M}', sg["kind"], sg.get("grade", ""), round(p["entry"], 2),
                                p["stop"], p["target"], f'SELL {sh["strike"]:.0f} {sh["opt"]}', round(sh["prem"], 2),
                                r["status"], f'{r["exit_time"]:%H:%M}' if r["exit_time"] else "",
                                round(r["points"], 2), round(r["best"], 2), round(r["worst"], 2), "",
                                "real LTP" if sh.get("prem_real") else "estimated",
                                round(r["short_exit_prem"], 2) if r.get("short_exit_prem") is not None else "",
                                *([sm["qty"], round(sm["sell"], 2), round(sm["gross"], 2), round(sm["charges"], 2),
                                   round(sm["net"], 2)] if sm else ["", "", "", "", ""])])
                    continue
                w.writerow([f'{sg["time"]:%H:%M}', sg["kind"], sg.get("grade", ""), round(p["entry"], 2), p["stop"],
                            p["target"], f'{p["strike"]:.0f} {p["opt"]}', round(p["prem"], 2), r["status"],
                            f'{r["exit_time"]:%H:%M}' if r["exit_time"] else "", round(r["points"], 2),
                            round(r["best"], 2), round(r["worst"], 2),
                            round(r["opt_pnl"], 2) if r["opt_pnl"] is not None else "",
                            "real LTP" if r["opt_real"] else "estimated",
                            round(r["exit_prem"], 2) if r.get("exit_prem") is not None else "",
                            *([r["money"]["qty"], round(r["money"]["buy"], 2), round(r["money"]["gross"], 2),
                               round(r["money"]["charges"], 2), round(r["money"]["net"], 2)]
                              if r.get("money") else ["", "", "", "", ""])])
        self._safe_write(os.path.join(self.folder, "results.csv"), buf.getvalue(), CSV_ENCODING)
        # skipped ideas, followed for comparison only (never part of results.csv or the profit totals)
        wbuf = io.StringIO()
        ww = csv.writer(wbuf)
        ww.writerow(["signal_time", "signal", "grade", "score", "direction", "skip_reason", "nifty_entry", "stop",
                     "target", "result", "exit_time", "nifty_points", "best_move", "worst_move", "net_rs"])
        for sg in self.signals:
            r, p = sg.get("watch_result"), sg.get("watch")
            if not r or not p:
                continue
            why = str(sg.get("trade", "")).split(" - was:")[0]
            m = r.get("money")
            ww.writerow([f'{sg["time"]:%H:%M}', sg["kind"], sg.get("grade", ""),
                         sg.get("score") if sg.get("score") is not None else "", p.get("dir", ""), why,
                         round(p["entry"], 2), p["stop"], p["target"], r["status"],
                         f'{r["exit_time"]:%H:%M}' if r["exit_time"] else "", round(r["points"], 2),
                         round(r["best"], 2), round(r["worst"], 2), round(m["net"], 2) if m else ""])
        self._safe_write(os.path.join(self.folder, "watch.csv"), wbuf.getvalue(), CSV_ENCODING)
        self._safe_write(self.dash_path, render_dashboard(self.snaps, self.signals, self.status))


def trade_tag(sig):
    """'BUY CE 22,550' / 'SELL PE 22,000' for a trade idea, or None if the signal is not a trade."""
    p = sig.get("plan")
    if not p:
        return None
    sh = p.get("short")
    if p.get("sell_only") and sh:
        return f"SELL {sh['opt']} {sh['strike']:,.0f}"
    if p.get("opt") and p.get("strike"):
        return f"BUY {p['opt']} {p['strike']:,.0f}"
    return "BUY" if p.get("dir") == "bullish" else "SELL"


def exit_tag(sig):
    """(label, exit price, profit sign) for a trade idea that has been closed, else None."""
    p, r = sig.get("plan"), sig.get("result")
    if not p or not r or not r.get("exit_time"):
        return None
    m = r.get("short_money") if p.get("sell_only") else r.get("money")
    word = {"TARGET HIT": "TARGET", "STOP HIT": "STOP", "CLOSED 15:30": "15:30 EXIT"}.get(r["status"], r["status"])
    if m and m.get("net") is not None:
        val, txt = m["net"], f"{word} {'+' if m['net'] >= 0 else '-'}Rs {abs(m['net']):,.0f}"
    else:
        val, txt = r["points"], f"{word} {r['points']:+,.0f} pts"
    px = p["entry"] - r["points"] if p["dir"] == "bearish" else p["entry"] + r["points"]
    return txt, px, (1 if val > 0 else -1 if val < 0 else 0)


def _svg_candles(snaps):
    candles = [s["candle"] for s in snaps if s["candle"]]
    if not candles:
        return "<p class='muted'>Waiting for the first 5-minute candle...</p>"
    last = snaps[-1]
    lv = last["levels"]
    levels = [(lv.get("R"), "Max call OI", "var(--dn)"), (lv.get("S"), "Max put OI", "var(--up)")]
    pa = last.get("pa") or {}
    if pa.get("or_high"):
        levels += [(pa["or_high"], "OR high", "var(--ink2)"), (pa["or_low"], "OR low", "var(--ink2)")]
    if lv.get("CE_fresh_near") not in (lv.get("R"),):
        levels.append((lv.get("CE_fresh_near"), "Fresh calls", "var(--dn)"))
    if lv.get("PE_fresh_near") not in (lv.get("S"),):
        levels.append((lv.get("PE_fresh_near"), "Fresh puts", "var(--up)"))
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    for v, _, _ in levels:
        if v and abs(v - last["spot"]) < 400:
            lo, hi = min(lo, v), max(hi, v)
    pad = max(20, (hi - lo) * 0.08)
    lo, hi = lo - pad, hi + pad
    W, H, L, Rm, T, B = 980, 380, 64, 130, 12, 28
    n = max(len(candles), 75)  # full day = 75 candles
    cw = (W - L - Rm) / n
    y = lambda p: T + (hi - p) / (hi - lo) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{NAME} 5-minute candles">']
    step = 10 ** math.floor(math.log10(max(1, (hi - lo) / 5)))
    step = step * (5 if (hi - lo) / step > 25 else 2 if (hi - lo) / step > 10 else 1)
    g = math.ceil(lo / step) * step
    while g <= hi:
        out.append(f'<line x1="{L}" x2="{W - Rm}" y1="{y(g):.1f}" y2="{y(g):.1f}" class="grid"/>'
                   f'<text x="{L - 8}" y="{y(g) + 4:.1f}" class="ax" text-anchor="end">{g:,.0f}</text>')
        g += step
    for v, name, col in levels:
        if v and lo < v < hi:
            out.append(f'<line x1="{L}" x2="{W - Rm}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{col}" '
                       f'stroke-dasharray="5 4" stroke-width="1.2"/>'
                       f'<text x="{W - Rm + 6}" y="{y(v) + 4:.1f}" class="lv" fill="{col}">{name} {v:,.0f}</text>')
    for i, c in enumerate(candles):
        x = L + (i + 0.5) * cw
        col = "var(--up)" if c["close"] >= c["open"] else "var(--dn)"
        top, bot = y(max(c["open"], c["close"])), y(min(c["open"], c["close"]))
        tip = (f'{c["start"]:%H:%M}  O {c["open"]:,.2f}  H {c["high"]:,.2f}  '
               f'L {c["low"]:,.2f}  C {c["close"]:,.2f}')
        out.append(f'<g><title>{tip}</title><line x1="{x:.1f}" x2="{x:.1f}" y1="{y(c["high"]):.1f}" '
                   f'y2="{y(c["low"]):.1f}" stroke="{col}" stroke-width="1.2"/>'
                   f'<rect x="{x - cw * 0.34:.1f}" y="{top:.1f}" width="{cw * 0.68:.1f}" '
                   f'height="{max(1.2, bot - top):.1f}" fill="{col}" rx="1"/></g>')
        if (c["start"].hour, c["start"].minute) == (9, 15) or (c["start"].minute == 0 and c["start"].hour > 9):
            out.append(f'<text x="{x:.1f}" y="{H - 8}" class="ax" text-anchor="middle">{c["start"]:%H:%M}</text>')
    for s in [s for s in _sig_positions(snaps)]:
        i, sig = s
        x = L + (i + 0.5) * cw
        c = candles[i]
        if sig["direction"] == "bearish":
            out.append(f'<text x="{x:.1f}" y="{y(c["high"]) - 6:.1f}" class="mk" fill="var(--dn)" '
                       f'text-anchor="middle"><title>{html.escape(sig["kind"])}</title>▼</text>')
        elif sig["direction"] == "bullish":
            out.append(f'<text x="{x:.1f}" y="{y(c["low"]) + 14:.1f}" class="mk" fill="var(--up)" '
                       f'text-anchor="middle"><title>{html.escape(sig["kind"])}</title>▲</text>')
    # trade ideas: "BUY CE 22,550" at the entry, a dotted line to the exit and the result there
    bar_of = {s["time"]: i for i, s in enumerate([s for s in snaps if s["candle"]])}
    for i, sig in _sig_positions(snaps):
        tag = trade_tag(sig)
        if not tag:
            continue
        x, c = L + (i + 0.5) * cw, candles[i]
        up = sig["direction"] == "bullish"
        col = "var(--up)" if up else "var(--dn)"
        ty = y(c["low"]) + 32 if up else y(c["high"]) - 22
        tw = 7.2 * len(tag) + 12
        out.append(f'<g><title>{html.escape(sig.get("option") or sig.get("trade") or "")}</title>'
                   f'<rect x="{x - tw / 2:.1f}" y="{ty - 12:.1f}" width="{tw:.1f}" height="17" rx="4" fill="{col}"/>'
                   f'<text x="{x:.1f}" y="{ty + 1:.1f}" class="tag" text-anchor="middle">{tag}</text></g>')
        ex = exit_tag(sig)
        if ex and ex[0] and sig["result"]["exit_time"] in bar_of:
            j = bar_of[sig["result"]["exit_time"]]
            x2, (txt, px, sign) = L + (j + 0.5) * cw, ex
            ecol = "var(--up)" if sign > 0 else "var(--dn)" if sign < 0 else "var(--ink2)"
            out.append(f'<line x1="{x:.1f}" y1="{y(sig["plan"]["entry"]):.1f}" x2="{x2:.1f}" y2="{y(px):.1f}" '
                       f'stroke="{ecol}" stroke-width="1.2" stroke-dasharray="2 3"/>'
                       f'<circle cx="{x2:.1f}" cy="{y(px):.1f}" r="4.5" fill="var(--card)" stroke="{ecol}" stroke-width="2"/>'
                       f'<text x="{x2 + 8:.1f}" y="{y(px) + 4:.1f}" class="ex" fill="{ecol}">{html.escape(txt)}</text>')
    out.append("</svg>")
    return "".join(out)


def _sig_positions(snaps):
    idx = {s["time"]: i for i, s in enumerate([s for s in snaps if s["candle"]])}
    res = []
    for s in snaps:
        for sig in s.get("_signals", []):
            if sig["kind"] in ("BREAKDOWN", "BREAKOUT", "REJECTION", "BOUNCE", "REVERSAL UP", "REVERSAL DOWN",
                               "OR BREAKOUT", "OR BREAKDOWN", "SELL CE", "SELL PE") \
                    and sig["time"] in idx:
                res.append((idx[sig["time"]], sig))
    return res


def _tr_cls(snap):
    return {"up": "bullish", "down": "bearish"}.get((snap.get("trend15") or {}).get("trend"), "")


def _result_html(sig):
    r = sig.get("result")
    if not r:
        return ""
    cls = "bullish" if r["points"] > 0 else "bearish" if r["points"] < 0 else ""
    when = f' at {r["exit_time"]:%H:%M}' if r["exit_time"] else " (running)"
    opt = ""
    if r["opt_pnl"] is not None:
        opt = f'<div class="muted">Option {r["opt_pnl"]:+,.1f} Rs/unit{"" if r["opt_real"] else " est."}</div>'
    return (f'<b class="{cls}">{r["status"]}</b>{when}<div>{r["points"]:+,.0f} Nifty pts</div>'
            f'<div class="muted">best +{r["best"]:,.0f} / worst -{r["worst"]:,.0f}</div>{opt}')


def _money_line_html(signals):
    res = [s["result"] for s in signals if s.get("result")]
    if not res:
        return ""
    ms = money_summary(res)
    col = "bullish" if ms["net"] > 0 else "bearish" if ms["net"] < 0 else ""
    open_txt = f', open trades Rs {ms["open"]:+,.0f}' if ms["n_open"] else ""
    return (f'<p style="margin:0 0 10px;font-size:15px">If you had taken every idea ({LOTS} lot = '
            f'{LOTS * LOT_SIZE} qty): <b class="{col}">Rs {ms["net"]:+,.0f}</b> net after approx. charges '
            f'({ms["n_closed"]} closed, {ms["wins"]} profitable{open_txt}) '
            f'· Grade A only Rs {ms["grade_a"]:+,.0f}</p>')


def _score_html(signals):
    res = [s["result"] for s in signals if s.get("result")]
    if not res:
        return "<p class='muted'>No trade ideas yet. Each BREAKDOWN/BREAKOUT idea is followed candle by candle " \
               "until its stop or target is hit (or 15:30).</p>"
    sc = scorecard(res)
    wr = f'{sc["win_rate"]:.0f}%' if sc["win_rate"] is not None else "-"
    g = " · ".join(f'Grade {k}: {n} trades, {w} won, {p:+,.0f} pts' for k, (n, w, p) in sc["by_grade"].items())
    return (f'<div class="tiles" style="margin-top:0">'
            f'<div class="tile"><div class="tl">Trade ideas</div><div class="tv">{sc["trades"]}</div>'
            f'<div class="ts">{sc["open"]} still running</div></div>'
            f'<div class="tile"><div class="tl">Won / closed</div><div class="tv">{sc["wins"]} / {sc["closed"]}</div>'
            f'<div class="ts">win rate {wr}</div></div>'
            f'<div class="tile"><div class="tl">Net Nifty points</div><div class="tv">{sc["points"]:+,.0f}</div>'
            f'<div class="ts">closed trades</div></div>'
            f'<div class="tile"><div class="tl">Option P&amp;L</div><div class="tv">{sc["opt_pnl"]:+,.1f}</div>'
            f'<div class="ts">Rs per unit (x lot size)</div></div></div>'
            f'<p class="muted" style="margin:10px 0 0">{g or ""}</p>'
            f'<p class="muted" style="margin:6px 0 0">Rule: stop or target checked on each later 5-min candle; '
            f'if both fall in one candle the stop is counted first. Open trades are closed at 15:30.</p>')


def _opt_html(sig):
    return f'<div class="opt">{html.escape(sig["option"])}</div>' if sig.get("option") else ""


def render_dashboard(snaps, signals, status=""):
    last = snaps[-1] if snaps else None
    now = dt.datetime.now(IST).strftime("%d %b %Y %H:%M:%S")

    def tile(label, value, sub=""):
        return (f'<div class="tile"><div class="tl">{label}</div><div class="tv">{value}</div>'
                f'<div class="ts">{sub}</div></div>')

    tiles = ""
    if last:
        c = last["candle"]
        first = snaps[0]["candle"]
        chg = c["close"] - first["open"]
        b = bias(last["pcr"])
        lv = last["levels"]
        tiles = "".join([
            tile(f"{NAME} (5-min close)", f'{c["close"]:,.2f}', f'{chg:+,.2f} since open · {last["time"]:%H:%M}'),
            tile("PCR", f'{last["pcr"]:.2f}' if last["pcr"] else "-", f'Bias: <b class="{b}">{b}</b>'),
            tile("Today's change PCR", f'{last["chg_pcr"]:.2f}' if last["chg_pcr"] else "-",
                 "new puts ÷ new calls"),
            tile("Resistance (max call OI)", f'{lv["R"]:,.0f}' if lv["R"] else "-",
                 f'Fresh calls: {lv["CE_fresh_near"]:,.0f}' if lv["CE_fresh_near"] else ""),
            tile("Support (max put OI)", f'{lv["S"]:,.0f}' if lv["S"] else "-",
                 f'Fresh puts: {lv["PE_fresh_near"]:,.0f}' if lv["PE_fresh_near"] else ""),
            tile("Highest OI (call / put)",
                 " / ".join(f"{t[0]:,.0f}" if t else "-" for t in ((last.get("top_oi") or top_oi(last["strikes"], spot=last["spot"]))[x]
                                                                      for x in ("ce", "pe"))),
                 "whole chain · " + " · ".join(f"{x.upper()} " + ", ".join(f"{k:,.0f}" for k in
                                     (last.get("top_oi") or top_oi(last["strikes"], spot=last["spot"]))[x][1:]) for x in ("ce", "pe"))),
            tile("Max pain", f'{last["max_pain"]:,.0f}', f'ATM straddle {last["straddle"]:.0f} pts'),
            tile("India VIX", f'{last["vix"]:.2f}' if last["vix"] else "-", ""),
            tile("Global cues", f'<span class="{(last.get("global") or {}).get("bias", "")}">'
                 f'{(last.get("global") or {}).get("bias", "n/a").upper()}</span>',
                 html.escape(global_text(last.get("global")).replace("Global cues: ", "").rsplit(" → ", 1)[0])),
            tile("Price action", f'<span class="{ {"up": "bullish", "down": "bearish"}.get((last.get("pa") or {}).get("struct"), "") }">'
                 f'{(last.get("pa") or {}).get("struct", "n/a").upper()}</span>',
                 html.escape(", ".join([x for x in [(last.get("pa") or {}).get("event", "")] if x]
                                       + [n for n, _ in (last.get("pa") or {}).get("patterns", [])]) or "no pattern")),
            tile("15-min trend", f'<span class="{_tr_cls(last)}">{(last.get("trend15") or {}).get("trend", "n/a").upper()}</span>',
                 f'EMA9 {last["trend15"]["ema"]:,.0f}' if (last.get("trend15") or {}).get("ema") else "from 10:00"),
        ])

    sig_rows = "".join(
        f'<tr class="{s["direction"]}"><td>{s["time"]:%H:%M}</td><td><b>{html.escape(s["kind"])}</b></td>'
        f'<td>{s["price"]:,.2f}</td><td>{html.escape(s["text"])}</td><td>{html.escape(s["trade"])}{_opt_html(s)}</td>'
        f'<td>{html.escape(s["bias_note"])}</td><td>{_result_html(s)}</td></tr>' for s in reversed(signals)) or \
        '<tr><td colspan="7" class="muted">No signals yet.</td></tr>'

    strike_rows = ""
    if last:
        prev = snaps[-2] if len(snaps) > 1 else None
        st = last["strikes"]
        ks = [k for k in sorted(st) if abs(k - last["atm"]) <= 8 * STRIKE_STEP]
        mx = max([st[k]["ce_oi"] for k in ks] + [st[k]["pe_oi"] for k in ks] + [1])
        for k in ks:
            v = st[k]
            d5c = v["ce_oi"] - prev["strikes"][k]["ce_oi"] if prev and k in prev["strikes"] else 0
            d5p = v["pe_oi"] - prev["strikes"][k]["pe_oi"] if prev and k in prev["strikes"] else 0
            atm = ' class="atm"' if k == last["atm"] else ""
            strike_rows += (
                f'<tr{atm}><td class="num">{fmt_k(v["ce_chg"])}</td><td class="num">{d5c:+,.0f}</td>'
                f'<td class="bar l"><span style="width:{v["ce_oi"] / mx * 100:.0f}%"></span>{fmt_k(v["ce_oi"])}</td>'
                f'<td class="k">{k:,.0f}</td>'
                f'<td class="bar r"><span style="width:{v["pe_oi"] / mx * 100:.0f}%"></span>{fmt_k(v["pe_oi"])}</td>'
                f'<td class="num">{d5p:+,.0f}</td><td class="num">{fmt_k(v["pe_chg"])}</td></tr>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>{INSTRUMENTS[INSTRUMENT]['label']} OI Tracker</title>
<style>
:root{{--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--line:#e6e5e1;--up:#1a9e6e;--dn:#d63b3a;--warn:#c98500;--atm:#eef4fd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1a1a19;--card:#232322;--ink:#fff;--ink2:#c3c2b7;--line:#3a3a38;--up:#2fbf86;--dn:#e66767;--warn:#eda100;--atm:#22324a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px 40px}}h1{{font-size:22px;margin:0}}h2{{font-size:15px;margin:26px 0 10px}}
.muted{{color:var(--ink2)}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin-top:14px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}.tl{{font-size:12px;color:var(--ink2)}}
.tv{{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums}}.ts{{font-size:12px;color:var(--ink2)}}
.bearish{{color:var(--dn)}}.bullish{{color:var(--up)}}.caution{{color:var(--warn)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow-x:auto}}
svg{{width:100%;height:auto;min-width:640px}}.grid{{stroke:var(--line)}}.ax{{fill:var(--ink2);font-size:11px}}.lv{{font-size:11px}}.mk{{font-size:12px}}.tag{{font-size:11px;font-weight:700;fill:#fff}}.ex{{font-size:11px;font-weight:700;paint-order:stroke;stroke:var(--card);stroke-width:3px}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:12px;color:var(--ink2);font-weight:600}}tr.bearish td:nth-child(2){{color:var(--dn)}}tr.bullish td:nth-child(2){{color:var(--up)}}tr.caution td:nth-child(2){{color:var(--warn)}}
td.num{{text-align:right}}.card table{{min-width:720px}}td.k{{text-align:center;font-weight:650}}tr.atm td{{background:var(--atm)}}
td.bar{{position:relative;min-width:110px;text-align:right}}td.bar span{{position:absolute;top:6px;bottom:6px;opacity:.22;border-radius:3px}}
td.bar.l span{{right:8px;background:var(--dn)}}td.bar.r span{{left:8px;background:var(--up)}}td.bar.r{{text-align:left}}
.opt{{margin-top:4px;padding:4px 8px;border-radius:6px;background:var(--atm);color:var(--ink);font-weight:650}}
.status{{margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--atm);font-weight:600}}
footer{{margin-top:24px;font-size:12px;color:var(--ink2)}}
</style></head><body><main>
<h1>{INSTRUMENTS[INSTRUMENT]['label']} · OI Tracker</h1>
<div class="muted">Updated {now} IST · page refreshes every 60 s · {len(snaps)} snapshots · data: {INSTRUMENTS[INSTRUMENT]['exchange']} India</div>
{f'<div class="status">{html.escape(status)}</div>' if status else ''}
<div class="tiles">{tiles}</div>
<h2>Scorecard - did the suggestions work?</h2><div class="card">{_money_line_html(signals)}{_score_html(signals)}</div>
<h2>5-minute candles with OI levels</h2><div class="card">{_svg_candles(snaps)}</div>
<h2>Signals (newest first)</h2><div class="card"><table><thead><tr><th>Time</th><th>Signal</th><th>Price</th>
<th>Details</th><th>Trade idea</th><th>Bias</th><th>Result</th></tr></thead><tbody>{sig_rows}</tbody></table></div>
<h2>Option chain near the money (nearest expiry)</h2><div class="card"><table><thead><tr><th class="num">Call day chg</th>
<th class="num">Call 5-min chg</th><th style="text-align:right">Call OI</th><th style="text-align:center">Strike</th><th>Put OI</th>
<th class="num">Put 5-min chg</th><th class="num">Put day chg</th></tr></thead><tbody>{strike_rows}</tbody></table></div>
<footer>Signals: BREAKDOWN/BREAKOUT = 5-min close through an OI level, confirmed when OI at that strike shifts the same way ·
REJECTION/BOUNCE = level tested and held · REVERSAL UP/DOWN = turn at the day's low/high · OR BREAKOUT/BREAKDOWN = close beyond the 09:15-09:30 range ·
Trade ideas are graded by a confluence score = OI (0..2) + price action (-2..+3: candle pattern, swing structure / BOS / CHoCH,
location) + context (PCR bias, 15-min trend, global cues): A {SCORE_A}+, B {SCORE_B}+ · SHORT COVERING / PUT WRITING / PUT UNWINDING / CALL WRITING = 5-min OI flow ·
Filters: break needs a close {BREAK_MIN}+ pts through the level, one break per level per {BREAK_COOLDOWN_MIN} min,
stop sized to the day's average range, min risk:reward 1:{MIN_RR}, grade {MIN_GRADE} or better (score {SCORE_B}+), no new trades after
{NO_NEW_TRADES_AFTER:%H:%M} ({EXPIRY_EVE_MUTE_FROM:%H:%M} near expiry) · Educational tool, not trading advice.</footer>
</main></body></html>"""


# --------------------------------------------------------------------------- main loop
def print_scorecard(rec):
    res = evaluate_trades(rec.snaps, rec.signals) if rec.snaps else []
    if not res:
        print("No trade ideas today.")
        return
    print("\nRESULTS OF TODAY'S TRADE IDEAS")
    for r in res:
        sg, p = r["sig"], r["sig"]["plan"]
        when = f' at {r["exit_time"]:%H:%M}' if r["exit_time"] else ""
        opt = f' | option {r["opt_pnl"]:+.1f} Rs/unit' if r["opt_pnl"] is not None else ""
        rs = f' | net Rs {r["money"]["net"]:+,.0f} on {r["money"]["qty"]} qty' if r.get("money") else ""
        print(f'  {sg["time"]:%H:%M} [Grade {sg.get("grade", "-")}] {sg["kind"]} {p["strike"]:.0f} {p["opt"]}: '
              f'{r["status"]}{when} -> {r["points"]:+.0f} Nifty pts{opt}{rs}')
    sc = scorecard(res)
    wr = f'{sc["win_rate"]:.0f}%' if sc["win_rate"] is not None else "-"
    ms = money_summary(res)
    print(f'  Total: {sc["wins"]}/{sc["closed"]} won ({wr}), net {sc["points"]:+.0f} Nifty pts, '
          f'net profit Rs {ms["net"]:+,.0f} ({LOTS} lot x {LOT_SIZE}, after approx. charges). Details: results.csv')


def beep(kind):
    try:
        import winsound
        if kind in ("BREAKDOWN", "BREAKOUT"):
            for _ in range(3):
                winsound.Beep(1200, 180)
        elif kind == "BIG MONEY":
            winsound.Beep(600, 120)
            winsound.Beep(1500, 120)
            winsound.Beep(600, 120)
        else:
            winsound.Beep(900, 150)
    except Exception:
        print("\a", end="", flush=True)


COLORS = {"bearish": "\033[91m", "bullish": "\033[92m", "caution": "\033[93m"}


def _lvl(v):
    return f"{v:,.0f}" if v else "-"


def print_snapshot(snap, sigs):
    c, lv = snap["candle"], snap["levels"]
    pcr = f'{snap["pcr"]:.2f}' if snap["pcr"] else "-"
    print(f'[{snap["time"]:%H:%M}] Nifty {c["close"]:,.2f} (O {c["open"]:,.0f} H {c["high"]:,.0f} '
          f'L {c["low"]:,.0f}) | PCR {pcr} {bias(snap["pcr"])} | R {_lvl(lv["R"])} S {_lvl(lv["S"])} | '
          f'MaxPain {snap["max_pain"]:,.0f} | VIX {snap["vix"] or 0:.2f} | '
          f'15m {(snap.get("trend15") or {}).get("trend", "n/a").upper()}')
    for s in sigs:
        col = COLORS.get(s["direction"], "")
        print(f'   {col}>>> {s["kind"]}\033[0m  {s["text"]}')
        if s["trade"]:
            print(f'       {s["trade"]}  {s["bias_note"]}')
        if s.get("option"):
            print(f'       \033[1m>>> {s["option"]}\033[0m')


def next_expiry_after(client, expiry):
    """The first listed expiry after `expiry` (list cached on the client), or None."""
    try:
        lst = getattr(client, "_expiry_list", None)
        if not lst:
            lst = client.expiries()
            client._expiry_list = lst
        cur = dt.datetime.strptime(expiry, "%d-%b-%Y")
        later = sorted(d for d in (dt.datetime.strptime(x, "%d-%b-%Y") for x in lst) if d > cur)
        return later[0].strftime("%d-%b-%Y") if later else None
    except Exception:
        return None


def take_snapshot(client, expiry, candle_start=None, prev_levels=None):
    chart = client.chart()
    pts = minute_points(chart)
    candles = five_min_candles(pts)
    if candle_start is not None:
        candles = [c for c in candles if c["start"] <= candle_start]
    candle = candles[-1] if candles else None
    chain = parse_chain(client.chain(expiry))
    try:
        vix = client.vix()
    except RuntimeError:
        vix = None
    if candle is None:
        return None
    snap = summarize(chain, vix, candle, prev_levels)
    snap["expiry"], snap["live"] = expiry, True
    snap["next_expiry"] = next_expiry_after(client, expiry)
    if snap["next_expiry"] and is_expiry_day(snap):
        try:
            snap["next_strikes"] = parse_chain(client.chain(snap["next_expiry"]))["strikes"]
        except Exception:
            pass                      # fall back to an estimate priced from today's IVs
    snap["global"] = global_cues(client)
    snap["trend15"] = trend_15m(pts, candle["start"] + dt.timedelta(minutes=5))
    snap["atr"] = avg_range(candles)
    snap["candles"] = candles
    return snap


def _clear_folder(folder):
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass


def _parse_hhmm(text):
    """'09:20', '9:20', '9.2' or '09.20' (Excel mangles HH:MM into numbers) -> (h, m)."""
    t = str(text).strip().replace(".", ":")
    h, _, m = t.partition(":")
    return int(h), int((m or "0").ljust(2, "0")[:2])


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Every mode is a generator of events, so the console version and the desktop app share one engine:
#   ("info", text) · ("start", recorder) · ("snap", snap, signals) · ("wait", next_candle_close)
#   ("error", text) · ("done", recorder)
def iter_replay(out, expiry=None, client=None):
    """Replay the latest trading session candle by candle through the same signal rules.
    Prices are real 1-minute NSE data. NSE does not publish intraday OI history, so OI at each
    candle is ESTIMATED by moving in a straight line from the morning OI (close OI minus the
    day's change) to the closing OI. VIX is held at its latest value."""
    yield ("info", "Downloading the latest session from NSE ...")
    client = client or make_client()
    expiry = expiry or client.expiries()[0]
    pts = minute_points(client.chart())
    candles = five_min_candles(pts)
    final = parse_chain(client.chain(expiry))
    try:
        vix = client.vix()
    except RuntimeError:
        vix = None
    if not candles:
        yield ("error", "No intraday data available from NSE right now.")
        return
    try:
        day = dt.datetime.strptime(final["timestamp"][:11], "%d-%b-%Y").date()
    except (TypeError, ValueError):
        day = dt.datetime.now(IST).date()
    folder = os.path.join(out, f"{day.isoformat()}_replay")
    _clear_folder(folder)
    rec = Recorder(folder)
    rec.autosave = False                      # write the dashboard once at the end (faster)
    rec.status = (f"REPLAY of {day:%d %b %Y} - prices are real NSE data; OI between the open and the "
                  f"close is estimated (NSE has no intraday OI history), so OI-flow signals are skipped. "
                  f"For a real-OI test, re-test a recorded live day.")
    rec.mode, rec.day, rec.expiry = "replay", day, expiry
    nxt_exp = next_expiry_after(client, expiry)
    yield ("info", f"Replaying {day:%d %b %Y}: {len(candles)} five-minute candles, expiry {expiry}")
    yield ("start", rec)
    prev = None
    for i, c in enumerate(candles):
        end = c["start"] + dt.timedelta(minutes=5)
        w = min(1.0, max(0.0, ((end.hour * 60 + end.minute) - (9 * 60 + 15)) / 375))
        strikes = {}
        for k, v in final["strikes"].items():
            ce0, pe0 = max(0.0, v["ce_oi"] - v["ce_chg"]), max(0.0, v["pe_oi"] - v["pe_chg"])
            strikes[k] = dict(v, ce_oi=ce0 + (v["ce_oi"] - ce0) * w, pe_oi=pe0 + (v["pe_oi"] - pe0) * w,
                              ce_chg=v["ce_chg"] * w, pe_chg=v["pe_chg"] * w)
        chain = {"timestamp": f"replay {end:%H:%M}", "spot": c["close"], "strikes": strikes}
        snap = summarize(chain, vix, c, prev["levels"] if prev else None)
        snap["expiry"], snap["live"], snap["oi_estimated"] = expiry, False, True
        snap["next_expiry"] = nxt_exp
        snap["trend15"] = trend_15m(pts, end)
        snap["atr"] = avg_range(candles[:i + 1])
        snap["candles"] = candles[:i + 1]
        sigs = check_signals(prev, snap)
        snap["_signals"] = sigs
        rec.record(snap, sigs)
        yield ("snap", snap, sigs)
        prev = snap
    rec.write_dashboard()
    yield ("done", rec)


def load_recorded_day(src, expiry=None):
    """Parse a day recorded by live mode (snapshots.csv + strikes.csv) once, into plain per-candle inputs
    that can be replayed through the rules many times (see replay_loaded_day)."""
    snap_p, strike_p = os.path.join(src, "snapshots.csv"), os.path.join(src, "strikes.csv")
    try:
        day = dt.date.fromisoformat(os.path.basename(src)[:10])
    except ValueError:
        day = dt.datetime.now(IST).date()

    def read(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    rows = read(snap_p)
    # A day started by an older version has a shorter header; values a newer version appended after it land
    # under the key None - give them back their column names (SNAP_COLS order) so restarts keep them.
    for r in rows:
        extra = r.pop(None, None)
        if extra:
            known = [k for k in r if k is not None]
            for name, val in zip(Recorder.SNAP_COLS[len(known):], extra):
                r.setdefault(name, val)
    by_time = {}
    for r in read(strike_p):
        # skip damaged lines (e.g. two trackers once wrote to the same file at the same time)
        try:
            key, strike = _parse_hhmm(r["time"]), float(r["strike"])
        except (TypeError, ValueError, AttributeError):
            continue
        if r.get("pe_iv") is None:
            continue
        by_time.setdefault(key, {})[strike] = {
            "ce_oi": _f(r["ce_oi"], 0), "ce_chg": _f(r["ce_day_chg"], 0), "ce_ltp": _f(r["ce_ltp"], 0),
            "ce_iv": _f(r["ce_iv"], 0), "ce_vol": 0.0,
            "pe_oi": _f(r["pe_oi"], 0), "pe_chg": _f(r["pe_day_chg"], 0), "pe_ltp": _f(r["pe_ltp"], 0),
            "pe_iv": _f(r["pe_iv"], 0), "pe_vol": 0.0}
    items, seen = [], set()
    for r in rows:
        try:
            h, m = _parse_hhmm(r["time"])
        except (TypeError, ValueError, AttributeError):
            continue
        if (h, m) in seen:                   # duplicate candle row
            continue
        seen.add((h, m))
        end = dt.datetime.combine(day, dt.time(h, m))
        c = {"start": end - dt.timedelta(minutes=5), "open": _f(r["open"]), "high": _f(r["high"]),
             "low": _f(r["low"]), "close": _f(r["close"])}
        strikes = by_time.get((h, m))
        if not strikes or c["close"] is None:
            continue
        items.append({"end": end, "candle": c, "strikes": strikes, "row": r,
                      "expiry": expiry or r.get("expiry") or None})
    return {"day": day, "src": src, "items": items}


def replay_loaded_day(loaded, signals=True):
    """Run the CURRENT signal rules over a day from load_recorded_day. Yields (snap, signals).
    signals=False rebuilds the snapshots only (price action included) without running the signal rules."""
    candles, pts, prev = [], [], None
    for it in loaded["items"]:
        c, r, end = dict(it["candle"]), it["row"], it["end"]
        start = c["start"]
        candles.append(c)
        # feed O/H/L/C as minute points so the 15-min trend uses the same code as live mode
        pts += [(start, c["open"]), (start + dt.timedelta(minutes=1), c["high"]),
                (start + dt.timedelta(minutes=2), c["low"]), (start + dt.timedelta(minutes=4), c["close"])]
        chain = {"timestamp": r.get("chain_timestamp"), "spot": c["close"], "strikes": it["strikes"]}
        snap = summarize(chain, _f(r.get("vix")), c, prev["levels"] if prev else None)
        # strikes.csv only keeps ATM +/- 12 strikes, so take whole-chain totals from snapshots.csv
        for key, col in (("tce", "total_ce_oi"), ("tpe", "total_pe_oi"), ("chg_ce", "day_chg_ce_oi"),
                         ("chg_pe", "day_chg_pe_oi"), ("max_pain", "max_pain")):
            v = _f(r.get(col))
            if v is not None:
                snap[key] = v
        snap["pcr"] = snap["tpe"] / snap["tce"] if snap["tce"] else None
        snap["chg_pcr"] = snap["chg_pe"] / snap["chg_ce"] if snap["chg_ce"] > 0 else None
        snap["expiry"] = it["expiry"]
        snap["live"] = True                  # recorded LTPs are real
        gp, npct = _f(r.get("gift_pct")), _f(r.get("nasdaq_pct"))
        if gp is not None or npct is not None:   # days recorded before global cues existed stay neutral
            g = {"gift": {"last": _f(r.get("gift_nifty")), "pct": gp} if gp is not None else None,
                 "nasdaq": {"last": _f(r.get("nasdaq_fut")), "pct": npct} if npct is not None else None}
            g["bias"] = global_bias(g)
            snap["global"] = g
        snap["trend15"] = trend_15m(pts, end)
        snap["atr"] = avg_range(candles)
        snap["candles"] = list(candles)
        if signals:
            sigs = check_signals(prev, snap)
        else:
            sigs = []
            snap["pa"] = price_action(snap["candles"], snap["atr"])
        snap["_signals"] = sigs
        yield snap, sigs
        prev = snap


def rebuild_snaps(src, expiry=None):
    """Rebuild the snapshots of a day recorded by live mode (snapshots.csv + strikes.csv) and run the
    CURRENT signal rules over them. Yields (snap, signals). Writes nothing."""
    yield from replay_loaded_day(load_recorded_day(src, expiry))


# --------------------------------------------------------------------------- weekly rule review
# Candidate values tried for each rule. Point values are in Nifty points (the app scales them for
# Bank Nifty / Sensex through its apply function).
REVIEW_GRID = {
    "BUY_LAST_ENTRY": [1130, 1200, 1230, 1300, 1330, 1400, 1500],
    "MAX_IDEAS_PER_SIDE": [1, 2, 3, 4, 6],
    "BUY_TARGET_CAP_ATR": [3.0, 4.0, 5.0, 6.0],
    "ATR_STOP_MULT": [0.8, 1.0, 1.3, 1.6],
    "MIN_RR": [2.0, 2.5, 3.0],
    "MIN_GRADE": ["A", "B", "C"],
    "BREAK_MIN": [5, 10, 15],
    "EARLY_TREND": [0, 1], "RR_OVERRIDE": [0, 1], "CONTINUATION": [0, 1], "BLOCK_CTR_REV": [0, 1],
    "BLOCK_AGAINST_PCR": [0, 1], "ONE_IDEA_PER_SETUP": [0, 1],
}
REVIEW_LABELS = {
    "BUY_LAST_ENTRY": "Last buying idea at", "MAX_IDEAS_PER_SIDE": "Max buying ideas per side / day",
    "BUY_TARGET_CAP_ATR": "Buying target cap (× avg range)", "ATR_STOP_MULT": "Stop × average range",
    "MIN_RR": "Minimum reward for 1 risk", "MIN_GRADE": "Lowest grade to trade", "BREAK_MIN": "Break distance (pts)",
    "EARLY_TREND": "Early-session trend (0/1)", "RR_OVERRIDE": "Strong R:R override (0/1)",
    "CONTINUATION": "Continuation ideas (0/1)", "BLOCK_CTR_REV": "Block counter-OR reversals (0/1)",
    "BLOCK_AGAINST_PCR": "Block ideas against PCR bias (0/1)", "ONE_IDEA_PER_SETUP": "One idea per setup (0/1)",
}
REVIEW_MIN_DAYS = 8          # need at least this many recorded live days before suggesting anything
REVIEW_MIN_GAIN = 0.05       # a change must improve the tuning days by 5% of their net (and at least Rs 500)
REVIEW_MIN_TRADES = 6        # and the changed rules must still produce at least this many ideas


def review_fmt(key, v):
    if key == "BUY_LAST_ENTRY":
        return f"{int(v) // 100:02d}:{int(v) % 100:02d}"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def recorded_live_days(data_dir):
    """Day folders (YYYY-MM-DD) recorded in live mode, oldest first."""
    out = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for d in names:
        p = os.path.join(data_dir, d)
        if (len(d) == 10 and d[:4].isdigit() and os.path.exists(os.path.join(p, "snapshots.csv"))
                and os.path.exists(os.path.join(p, "strikes.csv"))):
            out.append(p)
    return out


def _idea_net(r):
    """Rs result of one idea as the app shows it: the option buy, or the option sell for sell-only ideas."""
    p = r["sig"]["plan"]
    m = r.get("short_money") if p.get("sell_only") else r.get("money")
    return m["net"] if m else 0.0


def _eval_rules(loaded_days):
    """Run the current rules over pre-loaded days; per-day list of (net Rs, n ideas, n profitable)."""
    per_day = []
    for ld in loaded_days:
        snaps, sigs = [], []
        for snap, s in replay_loaded_day(ld):
            snaps.append(snap)
            sigs.extend(s)
        res = evaluate_trades(snaps, sigs) if snaps else []
        nets = [_idea_net(r) for r in res]
        per_day.append((sum(nets), len(nets), sum(1 for x in nets if x > 0)))
    return per_day


def _summ(per_day):
    net = sum(x[0] for x in per_day)
    n = sum(x[1] for x in per_day)
    w = sum(x[2] for x in per_day)
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in per_day:
        cum += x[0]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"net": net, "n": n, "wins": w, "win_rate": (w / n * 100) if n else None, "max_dd": dd,
            "per_trade": net / n if n else 0.0}


def review_rules(data_dir, current, apply_fn=None, progress=None, grid=None):
    """Weekly rule review. Re-tests every recorded live day with the current rules and with one-at-a-time
    alternatives from REVIEW_GRID, then a combination of the helpful ones.

    Guarding against over-fitting: the days are split by date - the older ~2/3 are used to PICK changes
    ("tuning days"), the newest ~1/3 ("unseen days") only to CHECK them. A change is suggested only if it
    improves the tuning days by a clear margin AND does not make the unseen days worse AND still leaves
    enough ideas. Nothing is changed here - the caller decides whether to apply `suggested`.

    current : dict of the rule values now in use (same keys as REVIEW_GRID, plus anything else)
    apply_fn: callable(dict) that loads a full set of rule values into the engine (default: setattr)
    progress: callable(done, total, text)"""
    grid = grid or REVIEW_GRID
    apply_fn = apply_fn or (lambda t: [setattr(sys.modules[__name__], k, v) for k, v in t.items()])
    days = recorded_live_days(data_dir)
    if len(days) < REVIEW_MIN_DAYS:
        return {"ok": False, "reason": f"Only {len(days)} recorded live day(s) - the review needs at least "
                                       f"{REVIEW_MIN_DAYS}. Keep the app running in live mode on trading days.",
                "days": [os.path.basename(d) for d in days]}
    n_test = max(3, round(len(days) / 3))
    loaded = []
    for i, d in enumerate(days):
        if progress:
            progress(0, 1, f"Loading {os.path.basename(d)} ({i + 1}/{len(days)})")
        try:
            loaded.append(load_recorded_day(d))
        except (OSError, ValueError, KeyError):
            continue
    train, test = loaded[:-n_test], loaded[-n_test:]
    configs = [dict(current)]
    singles = []
    for k, vals in grid.items():
        for v in vals:
            if k in current and v != current[k]:
                singles.append((k, v))
    total = 1 + len(singles) + 1
    done = [0]

    def run(cfg, label):
        if progress:
            progress(done[0], total, label)
        apply_fn(cfg)
        a, b = _eval_rules(train), _eval_rules(test)
        done[0] += 1
        return {"cfg": cfg, "train": _summ(a), "test": _summ(b), "all": _summ(a + b)}

    try:
        base = run(dict(current), "Current rules")
        rows = []
        for k, v in singles:
            cfg = dict(current, **{k: v})
            r = run(cfg, f"Trying {REVIEW_LABELS.get(k, k)} = {review_fmt(k, v)}")
            r["key"], r["value"] = k, v
            rows.append(r)
        # "clearly better" = REVIEW_MIN_GAIN of the tuning days' net, and at least Rs 500 (one lot)
        margin = max(500.0, REVIEW_MIN_GAIN * abs(base["train"]["net"]))

        def good(r):
            return (r["train"]["net"] >= base["train"]["net"] + margin
                    and r["test"]["net"] >= base["test"]["net"]
                    and r["all"]["n"] >= REVIEW_MIN_TRADES)

        best_per_key = {}
        for r in rows:
            if good(r) and (r["key"] not in best_per_key
                            or r["train"]["net"] > best_per_key[r["key"]]["train"]["net"]):
                best_per_key[r["key"]] = r
        suggestion = None
        if best_per_key:
            combo_cfg = dict(current, **{k: r["value"] for k, r in best_per_key.items()})
            combo = run(combo_cfg, "Combining the helpful changes")
            single_best = max(best_per_key.values(), key=lambda r: r["train"]["net"] + r["test"]["net"])
            if len(best_per_key) > 1 and good(combo) and (combo["train"]["net"] + combo["test"]["net"]
                                                           >= single_best["train"]["net"] + single_best["test"]["net"]):
                suggestion = combo
            else:
                suggestion = single_best
        else:
            done[0] += 1
    finally:
        apply_fn(dict(current))              # always leave the engine on the user's rules
    if progress:
        progress(total, total, "Done")
    changes = []
    if suggestion:
        for k in grid:
            if k in current and suggestion["cfg"].get(k) != current[k]:
                changes.append((k, current[k], suggestion["cfg"][k]))
    return {"ok": True, "days": [ld["day"].isoformat() for ld in loaded],
            "train_days": [ld["day"].isoformat() for ld in train], "test_days": [ld["day"].isoformat() for ld in test],
            "base": base, "suggestion": suggestion, "changes": changes,
            "tried": sorted(rows, key=lambda r: -(r["train"]["net"] + r["test"]["net"]))}


def review_report_text(rep, lots, lot_size):
    """Plain-text version of a review, saved next to the data."""
    L = [f"Rule review - {dt.datetime.now(IST):%d %b %Y %H:%M}",
         f"Position: {lots} lot x {lot_size}. Every idea taken at the suggested price, net of approx. charges.", ""]
    if not rep.get("ok"):
        return "\n".join(L + [rep.get("reason", "")])
    L.append(f"Days used: {len(rep['days'])}  (tuning: {rep['train_days'][0]} .. {rep['train_days'][-1]}, "
             f"unseen check: {rep['test_days'][0]} .. {rep['test_days'][-1]})")

    def line(name, r):
        a = r["all"]
        wr = f"{a['win_rate']:.0f}%" if a["win_rate"] is not None else "-"
        return (f"{name:<12} ideas {a['n']:>3}  win {wr:>4}  net Rs {a['net']:>+10,.0f}  "
                f"(unseen days Rs {r['test']['net']:+,.0f})  worst dip Rs {a['max_dd']:,.0f}")

    L.append(line("Current", rep["base"]))
    if rep["suggestion"]:
        L.append(line("Suggested", rep["suggestion"]))
        L.append("")
        L.append("Suggested changes:")
        for k, old, new in rep["changes"]:
            L.append(f"  {REVIEW_LABELS.get(k, k)}: {review_fmt(k, old)} -> {review_fmt(k, new)}")
    else:
        L.append("")
        L.append("No change suggested: no alternative was clearly better on the tuning days without "
                 "getting worse on the unseen days. Keep the current rules.")
    L.append("")
    L.append("All single changes tried (best first):")
    for r in rep["tried"]:
        a = r["all"]
        L.append(f"  {REVIEW_LABELS.get(r['key'], r['key'])} = {review_fmt(r['key'], r['value']):<6} "
                 f"ideas {a['n']:>3}  net Rs {a['net']:>+10,.0f}  tuning {r['train']['net']:>+9,.0f}  "
                 f"unseen {r['test']['net']:>+9,.0f}")
    return "\n".join(L)


def iter_replay_folder(src, expiry=None):
    """Re-run the signal rules on a day recorded by LIVE mode (real 5-min OI), with no internet.
    Writes a fresh copy to <folder>_retest so the original recording is never touched.
    Use this to test rule changes on real data."""
    src = os.path.abspath(src.rstrip("/\\"))
    if not (os.path.exists(os.path.join(src, "snapshots.csv")) and os.path.exists(os.path.join(src, "strikes.csv"))):
        yield ("error", f"Need snapshots.csv and strikes.csv in {src}")
        return
    try:
        day = dt.date.fromisoformat(os.path.basename(src)[:10])
    except ValueError:
        day = dt.datetime.now(IST).date()
    if os.path.basename(src).endswith("_replay"):
        yield ("info", "Note: this folder is an estimated-OI replay; its OI is not real. Use a live-mode folder.")
    out = src + "_retest"
    _clear_folder(out)
    rec = Recorder(out)
    rec.autosave = False
    rec.status = f"RE-TEST of the live recording for {day:%d %b %Y} (real 5-min OI) with the current rules."
    rec.mode, rec.day, rec.expiry = "retest", day, expiry
    yield ("info", f"Re-testing {day:%d %b %Y} from {src} (real OI)")
    yield ("start", rec)
    for snap, sigs in rebuild_snaps(src, expiry):
        rec.record(snap, sigs)
        yield ("snap", snap, sigs)
    if not rec.snaps:
        yield ("error", "No usable snapshots found.")
        return
    if not any(s.get("expiry") for s in rec.snaps):
        yield ("info", "No expiry in the recording - set the expiry for accurate option estimates.")
    rec.write_dashboard()
    yield ("done", rec)


def market_closed_reason(client, today, now):
    if today.weekday() >= 5:
        return "Today is a weekend - the market is closed."
    try:
        if today in client.holidays():
            return "Today is an NSE trading holiday."
    except Exception:
        pass
    if now.time() >= dt.time(15, 31):
        return "The market has closed for today (15:30). Start again before 9:15 AM on the next trading day."
    return None


def iter_live(out, expiry=None, client=None, stop=None, once=False):
    """Live mode: one snapshot just after every 5-minute candle closes, until 15:30.
    `stop` is a threading.Event; setting it ends the loop within a second."""
    import threading
    stop = stop or threading.Event()
    exch = INSTRUMENTS[INSTRUMENT]["exchange"]
    yield ("info", f"Connecting to {exch} ...")
    try:
        client = client or make_client()
        expiry = expiry or client.expiries()[0]
    except Exception as e:
        yield ("error", f"Could not reach {exch} ({e}). Check your internet connection and try again.")
        return
    now = dt.datetime.now(IST)
    today = now.date()
    folder = os.path.join(out, today.isoformat())
    if not once:
        reason = market_closed_reason(client, today, now)
        if reason:
            snap_p, strike_p = os.path.join(folder, "snapshots.csv"), os.path.join(folder, "strikes.csv")
            if os.path.exists(snap_p) and os.path.exists(strike_p):
                # market closed but today was recorded: show the day (chart, ideas, option charts)
                rec = Recorder(folder)
                rec.mode, rec.day, rec.expiry = "live", today, expiry
                rec.status = "Market closed - showing today's recorded session."
                yield ("start", rec)
                n_old = 0
                for snap, sigs in rebuild_snaps(folder, expiry):
                    snap["_restored"] = True
                    rec.snaps.append(snap)
                    rec.signals.extend(sigs)
                    n_old += 1
                    yield ("snap", snap, sigs)
                yield ("info", f"{reason} Loaded today's {n_old} recorded snapshots.")
                yield ("done", rec)
                return
            yield ("error", reason)
            return
    rec = Recorder(folder)
    rec.mode, rec.day, rec.expiry = "live", today, expiry
    rec.status = "Tracker started - first update after the 09:20 candle closes." \
        if now.time() < dt.time(9, 20) else "Tracker started - first update at the next 5-minute candle."
    yield ("info", f"Connected. Expiry {expiry}. Saving to {os.path.abspath(folder)}")
    yield ("start", rec)
    prev = None
    if not once and os.path.exists(rec.snap_path) and os.path.exists(rec.strike_path):
        # restarted during the day: reload what was already recorded (new rows are appended)
        n_old = 0
        for snap, sigs in rebuild_snaps(folder, expiry):
            snap["_restored"] = True
            rec.snaps.append(snap)
            rec.signals.extend(sigs)
            prev = snap
            n_old += 1
            yield ("snap", snap, sigs)
        if n_old:
            yield ("info", f"Restored {n_old} snapshots already recorded today.")
    if once:
        snap = take_snapshot(client, expiry)
        if snap:
            snap["_signals"] = check_signals(None, snap)
            rec.record(snap, [])
            yield ("snap", snap, [])
        yield ("done", rec)
        return
    rec.write_dashboard()
    while not stop.is_set():
        now = dt.datetime.now(IST)
        if now.time() >= dt.time(15, 31):
            rec.status = "Market closed - tracker finished for today."
            if rec.snaps:
                rec.write_dashboard()
            break
        base = now.replace(second=0, microsecond=0)                 # next 5-min boundary
        nxt = base + dt.timedelta(minutes=5 - base.minute % 5)
        first = now.replace(hour=9, minute=20, second=0, microsecond=0)
        if nxt < first:
            nxt = first
        yield ("wait", nxt)
        wake = nxt + dt.timedelta(seconds=POLL_DELAY_SEC)
        # wait in short slices against the wall clock: one long wait stalls for as long as the computer sleeps
        stopped = False
        while True:
            left = (wake - dt.datetime.now(IST)).total_seconds()
            if left <= 0:
                break
            if stop.wait(min(left, 5.0)):
                stopped = True
                break
        if stopped:
            break
        late = (dt.datetime.now(IST) - wake).total_seconds()
        if late > 240:                # woke far past this candle (computer was asleep): its option chain is gone
            yield ("info", f"! Missed the {nxt:%H:%M} candle by {late / 60:.0f} min (was the computer asleep?) - "
                           "carrying on from the next one")
            continue
        candle_start = (nxt - dt.timedelta(minutes=5)).replace(tzinfo=None)
        try:
            snap = take_snapshot(client, expiry, candle_start, prev["levels"] if prev else None)
        except Exception as e:  # keep running whatever happens
            yield ("error", f"Snapshot failed at {dt.datetime.now(IST):%H:%M:%S}: {e} - will retry next candle")
            continue
        if stop.is_set():             # stopped while downloading: record nothing more (another recorder may own
            break                     # this index by now)
        if not snap:
            continue
        sigs = check_signals(prev, snap)
        snap["_signals"] = sigs
        nxt_upd = (nxt + dt.timedelta(minutes=5)).strftime("%H:%M")
        rec.status = ("Market closed - final snapshot recorded." if nxt.time() >= dt.time(15, 30)
                      else f"Running - next update after {nxt_upd} IST.")
        if is_expiry_day(snap):
            rec.status += (" EXPIRY DAY - today's options expire at 15:30; trade ideas use "
                           f"{snap.get('next_expiry') or 'the next expiry'}.")
        rec.record(snap, sigs)
        yield ("snap", snap, sigs)
        prev = snap
    yield ("done", rec)


def _console(events, args, beeps=False, open_browser=True):
    """Drive any iter_* generator from the command line."""
    rec = None
    for ev in events:
        kind = ev[0]
        if kind == "info":
            print(ev[1])
        elif kind == "error":
            print("\n  ! " + ev[1])
        elif kind == "start":
            rec = ev[1]
            print()
            if open_browser and rec.mode == "live" and not args.no_browser:
                webbrowser.open("file:///" + os.path.abspath(rec.dash_path).replace("\\", "/"))
                print("Dashboard opened in your browser (it refreshes itself every minute).")
        elif kind == "wait":
            print(f"  waiting for the {ev[1]:%H:%M} candle ...", end="\r", flush=True)
        elif kind == "snap":
            print(" " * 60, end="\r")
            print_snapshot(ev[1], ev[2])
            if beeps and ev[2] and not args.no_beep:
                beep(ev[2][0]["kind"])
        elif kind == "done":
            rec = ev[1]
            print(f"\nFinished: {len(rec.signals)} signals. Files in {os.path.abspath(rec.folder)}")
            print_scorecard(rec)
            if rec.mode != "live" and not args.no_browser:
                webbrowser.open("file:///" + os.path.abspath(rec.dash_path).replace("\\", "/"))


def replay(args):
    print("=" * 64 + "\n  NIFTY 50 OI TRACKER  -  REPLAY / TEST MODE\n" + "=" * 64)
    _console(iter_replay(args.out, args.expiry), args)


def replay_folder(args):
    _console(iter_replay_folder(args.replay_folder, args.expiry), args)


def run(args):
    if args.replay_folder:
        return replay_folder(args)
    if args.replay:
        return replay(args)
    print("=" * 64 + "\n  NIFTY 50 OI TRACKER  -  keep this window open during market hours\n" + "=" * 64)
    _console(iter_live(args.out, args.expiry, once=args.once), args, beeps=True)


def main():
    global LOTS, LOT_SIZE, BROKERAGE_PER_ORDER
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    p = argparse.ArgumentParser(description="Nifty 50 OI tracker (5-minute)")
    p.add_argument("--once", action="store_true", help="take one snapshot and exit")
    p.add_argument("--replay", action="store_true",
                   help="test mode: replay the latest trading session candle by candle")
    p.add_argument("--replay-folder", metavar="FOLDER",
                   help="re-test the rules on a day recorded by live mode, e.g. nifty_tracker_data/2026-10-05")
    p.add_argument("--expiry", help="expiry date like 06-Oct-2026 (default: nearest)")
    p.add_argument("--no-beep", action="store_true")
    p.add_argument("--lots", type=int, default=LOTS, help="lots per idea for the profit in Rs (default 1)")
    p.add_argument("--lot-size", type=int, default=LOT_SIZE, help="units per Nifty lot (default 65)")
    p.add_argument("--brokerage", type=float, default=BROKERAGE_PER_ORDER, help="Rs per order (default 20)")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty_tracker_data"),
                   help="output folder (default: nifty_tracker_data next to this script)")
    args = p.parse_args()
    LOTS, LOT_SIZE, BROKERAGE_PER_ORDER = max(1, args.lots), max(1, args.lot_size), max(0.0, args.brokerage)
    run(args)


if __name__ == "__main__":
    main()
