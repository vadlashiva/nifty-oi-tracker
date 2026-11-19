"""
NIFTY 50 AI-ASSISTED PATTERN SCANNER  (v2) - module used by the "Stock scanner" tab of the
Nifty OI Tracker (nifty_app.v24.py). It can also be run on its own:  python pattern_scanner.py
==========================================
Builds on the swing-point (find_peaks) scanner and adds:

  1. Classic pattern labels from swing highs/lows (trend, breakout, ascending
     triangle, double bottom, tight base, bull flag).
  2. Similarity to a reference chart (default: Cochin Shipyard's base before
     its biggest rally; you can fix the dates in CONFIG).
  3. Cross-stock pattern discovery: a shape that appeared in one stock earlier
     and is now showing up in several other Nifty 50 stocks at the same time.
  4. Breakout probability from historical analogues (k nearest neighbours over
     every past window of every stock), checked out-of-sample: the model only
     "learns" from data before TEST_START and is scored on data after it.
  5. Bullish ranking, ONE "steam after breakout" pick, PNG charts for the top
     10, and an Excel report.

Install:  pip install yfinance pandas numpy matplotlib scipy openpyxl
Output:   <data folder>/stock_scans/<date>/  (Excel, charts, result.json)

Educational / research tool only. Probabilities are historical frequencies,
not forecasts; paper-trade before risking money.
"""

from __future__ import annotations

import io
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import find_peaks
import yfinance as yf

warnings.filterwarnings("ignore")

# ==========================================================================
# CONFIG
# ==========================================================================
PERIOD = "5y"            # history to download
WINDOW = 60              # bars in one "pattern" window (~3 months)
SHAPE_POINTS = 30        # each window is resampled to this many points
LIB_STEP = 3             # step between historical windows in the library
HORIZON = 20             # bars to look ahead for the outcome
TARGET_PCT = 0.08        # "success" = +8% hit ...
STOP_PCT = 0.04          # ... before -4% within HORIZON bars
K_NEIGHBOURS = 40        # analogues used per prediction
TEST_START = "2025-11-01"  # windows ending on/after this are out-of-sample
MOTIF_CORR = 0.90        # shape correlation to count as "same pattern"
MOTIF_MIN_STOCKS = 5     # motif must be live in >= this many other stocks
TOP_N_CHARTS = 10

REFERENCE_SYMBOL = "COCHINSHIP"
# Set both to dates (e.g. "2023-04-01", "2023-06-30") to use a specific base.
# Leave None to auto-pick the 60-bar base just before Cochin's biggest run.
REFERENCE_START = None
REFERENCE_END = None

INDEX_SYMBOL = "^NSEI"

# Fallback list (used only if the live NSE list can't be fetched).
# Check against niftyindices.com after every semi-annual rebalance.
FALLBACK_SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

SCAN_DIR_NAME = "stock_scans"
_log = print            # replaced by run_scan(progress=...)


# ==========================================================================
# DATA
# ==========================================================================
def get_symbols() -> list[str]:
    url = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode()
        syms = pd.read_csv(io.StringIO(raw))["Symbol"].str.strip().tolist()
        if len(syms) >= 45:
            _log(f"Live Nifty 50 list: {len(syms)} symbols")
            return syms
    except Exception as e:
        _log(f"Live list unavailable ({type(e).__name__}); using fallback list")
    return FALLBACK_SYMBOLS


def download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    tickers = [s if s.startswith("^") else f"{s}.NS" for s in symbols]
    _log(f"Downloading {len(tickers)} tickers ({PERIOD}, daily)...")
    raw = yf.download(tickers, period=PERIOD, interval="1d", group_by="ticker",
                      auto_adjust=True, progress=False, threads=True)
    data = {}
    for sym, t in zip(symbols, tickers):
        try:
            df = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            continue
        if len(df) >= WINDOW + HORIZON + 20:
            data[sym] = df
        else:
            _log(f"  skipped {sym}: not enough data")
    _log(f"Loaded {len(data)} / {len(symbols)}")
    return data


# ==========================================================================
# SHAPES
# ==========================================================================
def shape_vector(close: np.ndarray) -> np.ndarray | None:
    """Log-price path resampled to SHAPE_POINTS and z-normalised."""
    y = np.log(close)
    x_new = np.linspace(0, len(y) - 1, SHAPE_POINTS)
    y = np.interp(x_new, np.arange(len(y)), y)
    sd = y.std()
    if sd < 1e-9:
        return None
    return (y - y.mean()) / sd


def outcome(high: np.ndarray, low: np.ndarray, entry: float) -> tuple[int, float]:
    """1 if +TARGET hit before -STOP within the horizon; also max gain."""
    tgt, stp = entry * (1 + TARGET_PCT), entry * (1 - STOP_PCT)
    for h, l in zip(high, low):
        if l <= stp:
            return 0, high.max() / entry - 1
        if h >= tgt:
            return 1, high.max() / entry - 1
    return 0, high.max() / entry - 1


def build_library(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every past WINDOW of every stock + what happened next."""
    rows = []
    for sym, df in data.items():
        c, h, l = df.Close.values, df.High.values, df.Low.values
        idx = df.index
        for end in range(WINDOW, len(df) - HORIZON, LIB_STEP):
            v = shape_vector(c[end - WINDOW:end])
            if v is None:
                continue
            y, gain = outcome(h[end:end + HORIZON], l[end:end + HORIZON], c[end - 1])
            rows.append((sym, idx[end - WINDOW], idx[end - 1], v, y, gain,
                         c[end + HORIZON - 1] / c[end - 1] - 1))
    lib = pd.DataFrame(rows, columns=["symbol", "start", "end", "vec", "hit",
                                      "max_gain", "fwd_ret"])
    _log(f"Pattern library: {len(lib):,} historical windows")
    return lib


def knn_prob(query: np.ndarray, lib_vecs: np.ndarray, lib_hits: np.ndarray,
             k: int = K_NEIGHBOURS) -> tuple[np.ndarray, np.ndarray]:
    """Probability = smoothed hit rate among the k most similar windows."""
    corr = query @ lib_vecs.T / SHAPE_POINTS
    top = np.argpartition(-corr, k, axis=1)[:, :k]
    hits = lib_hits[top]
    base = lib_hits.mean()
    prob = (hits.sum(1) + 2 * base) / (k + 2)       # shrink toward base rate
    return prob, np.take_along_axis(corr, top, 1).mean(1)


# ==========================================================================
# OUT-OF-SAMPLE TEST
# ==========================================================================
def backtest(lib: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    test_start = pd.Timestamp(TEST_START)
    # train windows must have their outcome fully known before test_start
    train = lib[lib.end < test_start - pd.Timedelta(days=int(HORIZON * 1.6))]
    test = lib[lib.end >= test_start]
    if len(train) < 500 or len(test) < 100:
        return {"note": "not enough data for out-of-sample test"}, pd.DataFrame()
    tv, th = np.stack(train.vec), train.hit.values.astype(float)
    p, _ = knn_prob(np.stack(test.vec), tv, th)
    test = test.assign(prob=p)
    base = test.hit.mean()
    test["decile"] = pd.qcut(test.prob.rank(method="first"), 10, labels=False) + 1
    by_dec = test.groupby("decile").agg(windows=("hit", "size"),
                                         pred_prob=("prob", "mean"),
                                         actual_hit_rate=("hit", "mean"),
                                         avg_fwd_ret=("fwd_ret", "mean")).reset_index()
    top = test[test.decile == 10]
    brier = ((test.prob - test.hit) ** 2).mean()
    brier_base = ((base - test.hit) ** 2).mean()
    stats = {
        "train_windows": len(train),
        "test_windows": len(test),
        "test_period": f"{test.end.min().date()} to {test.end.max().date()}",
        "base_hit_rate": round(base, 3),
        "top_decile_hit_rate": round(top.hit.mean(), 3),
        "lift_top_decile": round(top.hit.mean() / base, 2) if base else np.nan,
        "brier_model": round(brier, 4),
        "brier_base_rate": round(brier_base, 4),
        "skill_vs_base_%": round(100 * (1 - brier / brier_base), 2),
    }
    return stats, by_dec


# ==========================================================================
# CLASSIC PATTERNS (swing points)
# ==========================================================================
def analyse_stock(df: pd.DataFrame, idx_close: pd.Series | None) -> dict:
    d = df.tail(250).copy()
    c, h, l, v = d.Close, d.High, d.Low, d.Volume
    prominence = float(c.median()) * 0.01
    highs, _ = find_peaks(h.values, distance=3, prominence=prominence)
    lows, _ = find_peaks(-l.values, distance=3, prominence=prominence)
    labels = []

    trend = "Sideways"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = h.values[highs[-1]] > h.values[highs[-2]]
        hl = l.values[lows[-1]] > l.values[lows[-2]]
        trend = "Uptrend (HH+HL)" if hh and hl else \
                "Downtrend (LH+LL)" if not hh and not hl else "Sideways"

    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    atr = (h - l).rolling(14).mean().iloc[-1]
    last = c.iloc[-1]
    hi20 = h.iloc[-21:-1].max()
    hi55 = h.iloc[-56:-1].max()
    hi250 = h.max()
    vol_ratio = v.iloc[-1] / v.iloc[-21:-1].mean()

    breakout20 = last > hi20
    breakout55 = last > hi55
    if breakout55:
        labels.append("55-day breakout")
    elif breakout20:
        labels.append("20-day breakout")

    # ascending triangle: flat recent highs, rising lows
    rh = [i for i in highs if i >= len(d) - 60]
    rl = [i for i in lows if i >= len(d) - 60]
    if len(rh) >= 2 and len(rl) >= 2:
        hv, lv = h.values[rh], l.values[rl]
        if (hv.max() - hv.min()) / hv.mean() < 0.03 and lv[-1] > lv[0] * 1.02:
            labels.append("Ascending triangle")
    # double bottom: two lows within 3%, price now above the middle peak
    if len(rl) >= 2:
        a, b = rl[-2], rl[-1]
        mid = [i for i in highs if a < i < b]
        if abs(l.values[a] - l.values[b]) / l.values[a] < 0.03 and mid \
                and last > h.values[mid[-1]]:
            labels.append("Double bottom (confirmed)")
    # tight base: last 15 bars range < 1.5 x ATR*sqrt-ish
    rng15 = (h.iloc[-15:].max() - l.iloc[-15:].min()) / last
    if rng15 < 0.06 and last > sma50.iloc[-1]:
        labels.append("Tight base")
    # bull flag: >10% pole in 20 bars before a 5-10 bar shallow pullback
    pole = c.iloc[-15] / c.iloc[-35] - 1
    pull = c.iloc[-1] / c.iloc[-15:].max() - 1
    if pole > 0.10 and -0.06 < pull < -0.01:
        labels.append("Bull flag")

    rs = np.nan
    if idx_close is not None:
        ic = idx_close.reindex(c.index).ffill()
        rs = (c.iloc[-1] / c.iloc[-63]) / (ic.iloc[-1] / ic.iloc[-63]) - 1

    return dict(
        trend=trend, patterns=", ".join(labels) or "-",
        close=round(last, 2), breakout20=breakout20, breakout55=breakout55,
        breakout_level=round(hi55 if breakout55 else hi20, 2),
        ext_from_level=(last / (hi55 if breakout55 else hi20) - 1),
        above_sma50=last > sma50.iloc[-1], sma20_gt_sma50=sma20.iloc[-1] > sma50.iloc[-1],
        vol_ratio=vol_ratio, atr_pct=atr / last, rs_3m=rs,
        pct_from_52w_high=last / hi250 - 1, _highs=highs, _lows=lows,
    )


# ==========================================================================
# REFERENCE (Cochin Shipyard)
# ==========================================================================
def reference_vector(ref: pd.DataFrame) -> tuple[np.ndarray, str]:
    if REFERENCE_START and REFERENCE_END:
        seg = ref.loc[REFERENCE_START:REFERENCE_END].Close.values
        return shape_vector(seg), f"{REFERENCE_START} to {REFERENCE_END} (manual)"
    c = ref.Close.values
    fwd = pd.Series(c).shift(-60) / pd.Series(c) - 1
    best = int(fwd.iloc[WINDOW:-60].idxmax())
    seg = c[best - WINDOW + 1:best + 1]
    lbl = (f"{ref.index[best - WINDOW + 1].date()} to {ref.index[best].date()} "
           f"(base before +{fwd.iloc[best]:.0%} run in 60 bars, auto)")
    return shape_vector(seg), lbl


# ==========================================================================
# CROSS-STOCK MOTIF DISCOVERY
# ==========================================================================
def discover_motifs(lib: pd.DataFrame, current: dict[str, np.ndarray]) -> pd.DataFrame:
    """Historical windows whose shape is now live in >= MOTIF_MIN_STOCKS others."""
    syms = list(current)
    cur = np.stack([current[s] for s in syms])
    hist = lib[lib.end < lib.end.max() - pd.Timedelta(days=120)]  # truly 'earlier'
    hv = np.stack(hist.vec)
    corr = hv @ cur.T / SHAPE_POINTS                      # (n_hist, n_stocks)
    own = np.asarray(hist.symbol, dtype=object)[:, None] == np.asarray(syms, dtype=object)[None, :]
    match = (corr >= MOTIF_CORR) & ~own
    counts = match.sum(1)
    cand = np.where(counts >= MOTIF_MIN_STOCKS)[0]
    if len(cand) == 0:
        return pd.DataFrame()
    cand = cand[np.argsort(-counts[cand])]
    out, used = [], []
    for i in cand:
        r = hist.iloc[i]
        # one motif per source stock per 60-bar stretch, and skip duplicates
        if any(r.symbol == s and abs((r.end - e).days) < 90 for s, e in used):
            continue
        matched = [syms[j] for j in np.where(match[i])[0]]
        if any(len(set(matched) & set(m)) >= 0.8 * len(matched) for m in
               [o["_matched"] for o in out]):
            continue
        used.append((r.symbol, r.end))
        out.append(dict(source=r.symbol, source_start=r.start.date(),
                        source_end=r.end.date(), n_stocks_now=len(matched),
                        stocks_now=", ".join(matched),
                        source_next20_ret=round(r.fwd_ret, 4),
                        source_hit=int(r.hit), _vec=r.vec, _matched=matched))
        if len(out) >= 10:
            break
    return pd.DataFrame(out)


def motif_outcome_stats(motifs: pd.DataFrame, lib: pd.DataFrame) -> pd.DataFrame:
    """For each motif: how did ALL past look-alikes (any stock) perform?"""
    if motifs.empty:
        return motifs
    lv = np.stack(lib.vec)
    rates, rets, ns = [], [], []
    for v in motifs._vec:
        m = (lv @ v / SHAPE_POINTS) >= MOTIF_CORR
        sub = lib[m]
        ns.append(int(m.sum()))
        rates.append(round(sub.hit.mean(), 3) if len(sub) else np.nan)
        rets.append(round(sub.fwd_ret.median(), 4) if len(sub) else np.nan)
    return motifs.assign(past_lookalikes=ns, past_hit_rate=rates,
                         past_median_20d_ret=rets)


# ==========================================================================
# CHARTS
# ==========================================================================
def chart_stock(sym, df, info, path, ref_vec=None):
    d = df.tail(250)
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                  gridspec_kw={"height_ratios": [3.2, 1]})
    ax.plot(d.index, d.Close, color="#1f3b73", lw=1.4, label="Close")
    ax.plot(d.index, d.Close.rolling(20).mean(), color="#e08a1e", lw=1, label="SMA20")
    ax.plot(d.index, d.Close.rolling(50).mean(), color="#7a7a7a", lw=1, label="SMA50")
    hi, lo = info["_highs"], info["_lows"]
    ax.scatter(d.index[hi], d.High.values[hi], marker="v", color="#c0392b", s=22, zorder=3)
    ax.scatter(d.index[lo], d.Low.values[lo], marker="^", color="#1e8449", s=22, zorder=3)
    ax.axhline(info["breakout_level"], ls="--", color="#8e44ad", lw=1,
               label=f"Breakout level {info['breakout_level']}")
    ax.axvspan(d.index[-WINDOW], d.index[-1], color="#f4d03f", alpha=0.12,
               label=f"Pattern window ({WINDOW} bars)")
    ax.set_title(f"{sym}  |  score {info['score']:.0f}  |  P(+{TARGET_PCT:.0%} before "
                 f"-{STOP_PCT:.0%}, {HORIZON}d) = {info['prob']:.0%}  |  {info['patterns']}",
                 fontsize=10)
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(alpha=0.25)
    axv.bar(d.index, d.Volume, color="#95a5a6", width=1)
    axv.set_ylabel("Volume", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def chart_motif(m, data, path):
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(m["_vec"], color="black", lw=3,
            label=f"Source: {m['source']} {m['source_start']}→{m['source_end']}")
    for s in m["_matched"]:
        v = shape_vector(data[s].Close.values[-WINDOW:])
        ax.plot(v, lw=1, alpha=0.7, label=f"{s} (now)")
    ax.set_title(f"Cross-stock motif: live in {m['n_stocks_now']} stocks | "
                 f"past look-alikes hit rate {m.get('past_hit_rate', np.nan)}")
    ax.set_xlabel(f"Window resampled to {SHAPE_POINTS} points (z-normalised)")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# ==========================================================================
# MAIN
# ==========================================================================
def run_scan(root: str | Path = ".", progress=print) -> dict:
    """Run the full scan; save Excel, charts and result.json under root/stock_scans/<date>/."""
    global _log
    _log = progress
    OUT_DIR = Path(root) / SCAN_DIR_NAME / datetime.now().strftime("%Y-%m-%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "charts").mkdir(exist_ok=True)

    symbols = get_symbols()
    data = download(symbols + [REFERENCE_SYMBOL, INDEX_SYMBOL])
    ref = data.pop(REFERENCE_SYMBOL, None)
    idx = data.pop(INDEX_SYMBOL, None)
    idx_close = idx.Close if idx is not None else None

    lib = build_library(data)

    _log("Out-of-sample test of the analogue model...")
    bt_stats, bt_dec = backtest(lib)
    for k, v in bt_stats.items():
        _log(f"  {k}: {v}")

    # current pattern of each stock
    current = {s: shape_vector(df.Close.values[-WINDOW:]) for s, df in data.items()}
    current = {s: v for s, v in current.items() if v is not None}
    syms = list(current)
    q = np.stack([current[s] for s in syms])
    probs, analog_sim = knn_prob(q, np.stack(lib.vec), lib.hit.values.astype(float))

    ref_vec, ref_label = (None, "reference not available")
    if ref is not None:
        ref_vec, ref_label = reference_vector(ref)
    _log(f"Reference pattern: {REFERENCE_SYMBOL} {ref_label}")

    rows = []
    for i, s in enumerate(syms):
        info = analyse_stock(data[s], idx_close)
        info["prob"] = probs[i]
        info["analog_similarity"] = analog_sim[i]
        info["ref_similarity"] = (current[s] @ ref_vec / SHAPE_POINTS) if ref_vec is not None else np.nan
        rows.append((s, info))

    # ---- bullish score (0-100) ----
    # The analogue probability only gets a vote if it beat the base rate
    # out-of-sample (positive Brier skill AND top-decile lift > 1).
    base = lib.hit.mean()
    model_ok = bt_stats.get("skill_vs_base_%", -1) > 0 and bt_stats.get("lift_top_decile", 0) > 1
    prob_weight = 20 if model_ok else 0
    if not model_ok:
        _log("  -> Analogue model did NOT beat the base rate out-of-sample; "
              "its probability is reported but excluded from the score.")
    for s, r in rows:
        score = 0
        score += 20 * (r["trend"].startswith("Uptrend"))
        score += 15 * r["breakout55"] + 8 * (r["breakout20"] and not r["breakout55"])
        score += 10 * r["sma20_gt_sma50"] + 5 * r["above_sma50"]
        score += 10 * min(max((r["vol_ratio"] - 1) / 1.5, 0), 1)
        score += 10 * min(max(r["rs_3m"] / 0.15, 0), 1) if not np.isnan(r["rs_3m"]) else 0
        score += prob_weight * min(max((r["prob"] - base) / 0.15, 0), 1)
        score += 10 * max(r["ref_similarity"], 0) if not np.isnan(r["ref_similarity"]) else 0
        score -= 10 * (r["ext_from_level"] > 0.06)          # chasing penalty
        r["score"] = score

    rank = pd.DataFrame([{"symbol": s, **{k: v for k, v in r.items() if not k.startswith("_")}}
                         for s, r in rows]).sort_values("score", ascending=False)
    rank.insert(1, "rank", range(1, len(rank) + 1))

    # ---- ONE pick with steam after breakout ----
    steam = rank[(rank.breakout20 | rank.breakout55) & (rank.ext_from_level.between(0, 0.05))
                 & (rank.vol_ratio >= 1.3) & (rank.rs_3m > 0) & rank.sma20_gt_sma50]
    pick = steam.sort_values(["prob", "score"], ascending=False).head(1)

    # ---- motifs ----
    _log("Searching for cross-stock patterns...")
    motifs = motif_outcome_stats(discover_motifs(lib, current), lib)

    # ---- charts ----
    info_map = dict(rows)
    for s in rank.symbol.head(TOP_N_CHARTS):
        chart_stock(s, data[s], info_map[s], OUT_DIR / "charts" / f"{s}.png")
    for j, m in motifs.iterrows() if not motifs.empty else []:
        chart_motif(m, data, OUT_DIR / "charts" / f"motif_{j + 1}_{m['source']}.png")

    # ---- report ----
    fmt = rank.copy()
    for c in ["prob", "ext_from_level", "rs_3m", "pct_from_52w_high", "atr_pct"]:
        fmt[c] = (fmt[c] * 100).round(1)
    for c in ["vol_ratio", "analog_similarity", "ref_similarity", "score"]:
        fmt[c] = fmt[c].round(2)
    fmt = fmt.rename(columns={"prob": "breakout_prob_%", "ext_from_level": "ext_above_level_%",
                              "rs_3m": "rs_vs_nifty_3m_%", "pct_from_52w_high": "from_52w_high_%",
                              "atr_pct": "atr_%"})
    xlsx = OUT_DIR / "nifty50_ai_scan.xlsx"
    try:
        with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
            fmt.to_excel(xw, sheet_name="Ranking", index=False)
            (fmt[fmt.symbol.isin(pick.symbol)] if not pick.empty else
             pd.DataFrame({"note": ["No stock passed the steam-after-breakout filter today"]})
             ).to_excel(xw, sheet_name="Top pick", index=False)
            if not motifs.empty:
                motifs.drop(columns=["_vec", "_matched"]).to_excel(xw, sheet_name="Cross-stock motifs", index=False)
            pd.DataFrame(list(bt_stats.items()), columns=["metric", "value"]).to_excel(xw, sheet_name="Backtest", index=False)
            if not bt_dec.empty:
                bt_dec.to_excel(xw, sheet_name="Backtest", index=False, startrow=len(bt_stats) + 3)
            pd.DataFrame({"setting": ["reference", "window", "target/stop/horizon", "k",
                                      "probability used in score"],
                          "value": [f"{REFERENCE_SYMBOL} {ref_label}", WINDOW,
                                    f"+{TARGET_PCT:.0%} / -{STOP_PCT:.0%} / {HORIZON} bars",
                                    K_NEIGHBOURS, "yes" if model_ok else
                                    "no (failed out-of-sample test)"]}).to_excel(xw, sheet_name="Settings", index=False)
    except Exception as e:   # the report is a bonus; never lose the scan over it
        _log(f"Excel report not written ({type(e).__name__}: {e}); results are still shown")
        xlsx = None

    # ---- console summary ----
    _log("\nTOP 10")
    _log(fmt.head(10)[["rank", "symbol", "score", "breakout_prob_%", "trend",
                        "patterns", "ref_similarity"]].to_string(index=False))
    _log("\nSTEAM-AFTER-BREAKOUT PICK: " +
         (pick.symbol.iloc[0] if not pick.empty else "none today"))
    if not motifs.empty:
        _log(f"\nCROSS-STOCK MOTIFS ({len(motifs)})")
        _log(motifs[["source", "source_start", "source_end", "n_stocks_now",
                      "past_hit_rate", "stocks_now"]].to_string(index=False))
    _log(f"\nSaved: {xlsx.resolve() if xlsx else '(no Excel)'}\nCharts: {(OUT_DIR / 'charts').resolve()}")

    result = {
        "as_of": str(max(df.index[-1] for df in data.values()).date()),
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "folder": str(OUT_DIR.resolve()),
        "xlsx": str(xlsx.resolve()) if xlsx else None,
        "reference": f"{REFERENCE_SYMBOL} {ref_label}",
        "model_used_in_score": bool(model_ok),
        "pick": pick.symbol.iloc[0] if not pick.empty else None,
        "ranking": json.loads(fmt.to_json(orient="records")),
        "motifs": [] if motifs.empty else json.loads(
            motifs.drop(columns=["_vec", "_matched"]).to_json(orient="records", date_format="iso")),
        "backtest": {k: (v.item() if hasattr(v, "item") else v) for k, v in bt_stats.items()},
        "backtest_deciles": json.loads(bt_dec.to_json(orient="records")) if not bt_dec.empty else [],
        "settings": {"window": WINDOW, "target": TARGET_PCT, "stop": STOP_PCT, "horizon": HORIZON},
    }
    with open(OUT_DIR / "result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, default=str)
    return result


def load_latest(root: str | Path) -> dict | None:
    """Most recent saved scan (for showing results when the app opens)."""
    base = Path(root) / SCAN_DIR_NAME
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        f = d / "result.json"
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return None


if __name__ == "__main__":
    run_scan(Path(__file__).resolve().parent / "nifty_tracker_data")
