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
