"""
Far-OTM option-selling paper test for the Nifty OI Tracker.

Rule tested (one CE and one PE per day):
  * ENTRY: at the first recorded 5-min snapshot at/after ENTRY_TIME, SELL the call beyond the call-OI wall
    (strike above resistance) and the put beyond the put-OI wall (strike below support) whose premium is
    closest to TARGET_ENTRY, within MIN_PREM..MAX_PREM.
  * EXIT:  buy back at BUYBACK (profit), or when the premium reaches STOP_MULT x entry (stop loss),
    or at EXIT_TIME at the last recorded premium.
Uses the real 5-min option prices your tracker recorded (strikes.csv). Nothing is traded.

Limits: prices are only seen every 5 minutes, so a spike that comes and goes inside a candle is missed
(this makes the test a little OPTIMISTIC). Stops are filled at the recorded premium, not exactly at the stop.

Usage:  python otm_short_test.py                 (all recorded Nifty days)
        python otm_short_test.py --buyback 2 --stop-mult 2 --entry 09:30
"""
import argparse
import csv
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "nifty_tracker_data")

TARGET_ENTRY = 5.0     # aim to sell at about Rs 5
MIN_PREM, MAX_PREM = 3.0, 8.0
BUYBACK = 2.0          # buy back here for the profit
STOP_MULT = 2.0        # stop when premium reaches 2 x entry (Rs 10 for a Rs 5 sale)
ENTRY_TIME = "09:30"
EXIT_TIME = "15:15"
LOT_SIZE = 65
LOTS = 1
BROKERAGE_PER_ORDER = 20.0   # Angel One F&O, flat per executed order


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def charges(sell_prem, buy_prem, qty):
    """Approximate round-trip charges (Rs) for selling then buying back an index option."""
    sell_to, buy_to = sell_prem * qty, buy_prem * qty
    brokerage = 2 * BROKERAGE_PER_ORDER
    stt = 0.001 * sell_to                       # 0.1% on the sell side premium
    exch = 0.0003503 * (sell_to + buy_to)       # NSE transaction charge on premium
    sebi = 0.000001 * (sell_to + buy_to)
    stamp = 0.00003 * buy_to
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def load_day(folder):
    snaps, strikes = {}, {}
    with open(os.path.join(folder, "snapshots.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("time"):
                snaps[r["time"]] = r                       # de-duplicates repeated rows
    with open(os.path.join(folder, "strikes.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = fnum(r.get("strike"))
            if r.get("time") and k is not None:
                strikes.setdefault(r["time"], {})[k] = r
    times = sorted(t for t in snaps if t in strikes)
    return snaps, strikes, times


def wall(snap, chain, side):
    """Call wall = biggest call OI above spot; put wall = biggest put OI below spot."""
    key = "resistance" if side == "CE" else "support"
    w = fnum(snap.get(key))
    if w:
        return w
    spot = fnum(snap.get("close")) or 0
    if side == "CE":
        c = {k: fnum(v.get("ce_oi")) or 0 for k, v in chain.items() if k > spot}
    else:
        c = {k: fnum(v.get("pe_oi")) or 0 for k, v in chain.items() if k < spot}
    return max(c, key=c.get) if c else None


def pick(chain, side, w):
    col = side.lower() + "_ltp"
    cands = []
    for k, v in chain.items():
        p = fnum(v.get(col))
        if p is None or not (MIN_PREM <= p <= MAX_PREM):
            continue
        if (side == "CE" and k > w) or (side == "PE" and k < w):
            cands.append((abs(p - TARGET_ENTRY), k, p))
    return min(cands)[1:] if cands else None


def run_day(day, folder):
    snaps, strikes, times = load_day(folder)
    out = []
    entry_t = next((t for t in times if t >= ENTRY_TIME and t < EXIT_TIME), None)
    if not entry_t:
        return out, "no snapshot in the entry window"
    snap, chain = snaps[entry_t], strikes[entry_t]
    expiry = snap.get("expiry") or ""
    try:
        exp_day = dt.datetime.strptime(expiry, "%d-%b-%Y").date() == dt.date.fromisoformat(day)
    except ValueError:
        exp_day = False
    qty = LOT_SIZE * LOTS
    for side in ("CE", "PE"):
        w = wall(snap, chain, side)
        p = pick(chain, side, w) if w else None
        if not p:
            out.append({"day": day, "side": side, "status": "NO TRADE", "note": f"no Rs {MIN_PREM:g}-{MAX_PREM:g} {side} beyond the wall {w}"})
            continue
        k, entry = p
        stop = STOP_MULT * entry
        col = side.lower() + "_ltp"
        status, exit_t, exit_p, worst = "OPEN", entry_t, entry, entry
        for t in times:
            if t <= entry_t:
                continue
            px = fnum(strikes[t].get(k, {}).get(col))
            if px is None:
                continue
            worst = max(worst, px)
            exit_t, exit_p = t, px
            if px <= BUYBACK:
                status, exit_p = "TARGET", BUYBACK
                break
            if px >= stop:
                status = "STOP"
                break
            if t >= EXIT_TIME:
                status = "TIME EXIT"
                break
        if status == "OPEN":
            status = "TIME EXIT" if exit_t >= "15:00" else "DATA ENDED"
        gross = (entry - exit_p) * qty
        ch = charges(entry, exit_p, qty)
        out.append({"day": day, "side": side, "strike": int(k), "expiry_day": "yes" if exp_day else "no",
                    "entry_time": entry_t, "entry": round(entry, 2), "wall": int(w), "spot": snap.get("close"),
                    "exit_time": exit_t, "exit": round(exit_p, 2), "highest": round(worst, 2), "status": status,
                    "gross": round(gross, 0), "charges": round(ch, 0), "net": round(gross - ch, 0)})
    return out, ""


def main():
    global BUYBACK, STOP_MULT, ENTRY_TIME, EXIT_TIME, LOTS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--buyback", type=float, default=BUYBACK)
    ap.add_argument("--stop-mult", type=float, default=STOP_MULT)
    ap.add_argument("--entry", default=ENTRY_TIME)
    ap.add_argument("--exit", default=EXIT_TIME)
    ap.add_argument("--lots", type=int, default=LOTS)
    a = ap.parse_args()
    BUYBACK, STOP_MULT, ENTRY_TIME, EXIT_TIME, LOTS = a.buyback, a.stop_mult, a.entry, a.exit, a.lots

    days = sorted(d for d in os.listdir(a.data)
                  if len(d) == 10 and d[4] == "-" and os.path.isfile(os.path.join(a.data, d, "strikes.csv")))
    rows, skipped = [], []
    for d in days:
        try:
            r, why = run_day(d, os.path.join(a.data, d))
        except Exception as e:
            r, why = [], f"could not read ({e})"
        if why:
            skipped.append((d, why))
        rows.extend(r)

    trades = [r for r in rows if r["status"] not in ("NO TRADE",)]
    print(f"\nFar-OTM short test: sell ~Rs {TARGET_ENTRY:g} beyond the OI wall at {ENTRY_TIME}, buy back Rs {BUYBACK:g}, "
          f"stop {STOP_MULT:g}x entry, exit {EXIT_TIME}, {LOTS} lot x {LOT_SIZE}\n")
    print(f"{'Day':10} {'Side':4} {'Strike':>6} {'Exp?':4} {'In':5} {'Sold':>5} {'Out':5} {'Bought':>6} {'High':>6} {'Result':10} {'Net Rs':>7}")
    for r in rows:
        if r["status"] == "NO TRADE":
            print(f"{r['day']:10} {r['side']:4} {'-':>6} {'':4} {'':5} {'':>5} {'':5} {'':>6} {'':>6} NO TRADE   {'':>7}  {r['note']}")
            continue
        print(f"{r['day']:10} {r['side']:4} {r['strike']:>6} {r['expiry_day']:4} {r['entry_time']:5} {r['entry']:>5.2f} "
              f"{r['exit_time']:5} {r['exit']:>6.2f} {r['highest']:>6.2f} {r['status']:10} {r['net']:>7,.0f}")
    for d, why in skipped:
        print(f"{d:10} skipped: {why}")

    def summary(label, ts):
        if not ts:
            return
        wins = [t for t in ts if t["net"] > 0]
        losses = [t for t in ts if t["net"] <= 0]
        net = sum(t["net"] for t in ts)
        aw = sum(t["net"] for t in wins) / len(wins) if wins else 0
        al = sum(t["net"] for t in losses) / len(losses) if losses else 0
        be = (-al / (aw - al) * 100) if wins and losses else None
        eq, peak, dd = 0, 0, 0
        for t in ts:
            eq += t["net"]; peak = max(peak, eq); dd = min(dd, eq - peak)
        print(f"\n{label}: {len(ts)} trades | won {len(wins)} ({len(wins) / len(ts) * 100:.0f}%) | "
              f"net Rs {net:,.0f} | avg win Rs {aw:,.0f} | avg loss Rs {al:,.0f} | worst Rs {min(t['net'] for t in ts):,.0f} | "
              f"max drawdown Rs {dd:,.0f}" + (f" | break-even win rate {be:.0f}%" if be is not None else ""))

    summary("ALL", trades)
    summary("Expiry days", [t for t in trades if t["expiry_day"] == "yes"])
    summary("Other days", [t for t in trades if t["expiry_day"] == "no"])
    summary("Calls (CE)", [t for t in trades if t["side"] == "CE"])
    summary("Puts (PE)", [t for t in trades if t["side"] == "PE"])

    out = os.path.join(a.data, "otm_short_results.csv")
    keys = ["day", "side", "strike", "expiry_day", "entry_time", "entry", "wall", "spot", "exit_time", "exit",
            "highest", "status", "gross", "charges", "net", "note"]
    try:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nSaved: {out}")
    except OSError as e:
        print(f"\nCould not save the CSV ({e})")
    print("Note: prices are seen every 5 minutes, so quick spikes are missed - real results would be a bit worse.\n")


if __name__ == "__main__":
    main()
