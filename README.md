# Nifty OI Tracker

Desktop app that tracks Nifty 50 option-chain open interest every 5 minutes and combines it with
price action (candle patterns, swing structure, opening range) to flag graded trade ideas.

- `nifty_app.py` – the desktop window (PySide6)
- `nifty_oi_tracker.py` – the engine; also runs from the command line (`--replay`, `--replay-folder`)
- `build_exe.bat` – builds `NiftyOITracker.exe` on Windows (finds or installs Python)
- `run_app.bat` – runs the app straight from Python

See `README.txt` for setup and usage.

*Educational tool only – not trading advice. Market data belongs to NSE.*
