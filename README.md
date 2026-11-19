# Nifty OI Tracker

Desktop app that tracks Nifty 50 option-chain open interest every 5 minutes and combines it with
price action (candle patterns, swing structure, opening range) to flag graded trade ideas.

**Current version: v24** – run `run_v24.bat` (or build the exe with `build_v24.bat`).

- `nifty_app.v24.py` / `nifty_oi_tracker_v24.py` – current desktop window and engine (Nifty, Bank Nifty and Sensex,
  trade ideas at R:R 1:2, buy and sell option plans, profit scorecard, weekly self-review, contra guard)
- `nifty_app.py` – the earlier desktop window (PySide6)
- `nifty_oi_tracker.py` – the engine; also runs from the command line (`--replay`, `--replay-folder`)
- `build_exe.bat` – builds `NiftyOITracker.exe` on Windows (finds or installs Python)
- `run_app.bat` – runs the earlier app straight from Python
- `otm_short_test.py`, `run_otm_test.bat` – back-test of OTM option-selling ideas on the recorded days

See `README.txt` for setup and usage.

*Educational tool only – not trading advice. Market data belongs to NSE.*

## Stock scan (daily F&O breakout screen)

`stock_scan.py` scans the Nifty 50 (all F&O stocks) on daily closes from Yahoo Finance and narrows to one stock "with steam": a 52-week-high breakout (or a stock within 3% of it) in an up-trend (close > 50-day > 200-day average), up at least 2% over the month, ranked by momentum and volume, with stretched names (RSI > 75) deprioritised. For each stock it shows a trigger (daily close above the 52-week high), an invalidation level and the stock-options series to look at.

- In the app: **Stock scan** button (Ctrl+K). It runs on its own once each weekday after 15:45 and saves `stock_scan_YYYY-MM-DD.json` in the data folder.
- Command line: `python stock_scan.py --data nifty_tracker_data`

Educational screen only, not trading advice.
