# Nifty OI Tracker

Desktop app that tracks Nifty 50 option-chain open interest every 5 minutes and combines it with
price action (candle patterns, swing structure, opening range) to flag graded trade ideas.

- `nifty_app.py` – the desktop window (PySide6)
- `nifty_oi_tracker.py` – the engine; also runs from the command line (`--replay`, `--replay-folder`)
- `build_exe.bat` – builds `NiftyOITracker.exe` on Windows (finds or installs Python)
- `run_app.bat` – runs the app straight from Python

See `README.txt` for setup and usage.

*Educational tool only – not trading advice. Market data belongs to NSE.*

## Working with the window

- **Chart:** scroll to zoom, drag to pan, double-click (or Ctrl+0) to reset. Switch 5 / 15-minute candles and
  toggle OI levels, fresh OI, opening range and markers from the bar above the chart.
- **Click a signal** (a marker on the chart, a card in the feed, or a row in the scorecard) to highlight its candle and
  draw its entry, stop and target zones — skipped ideas are shown dashed. Esc clears; Ctrl+↑/↓ steps through signals.
- **Tiles** show an intraday sparkline and flash green/red when the value moves; click one for today's history.
- **Option chain:** double-click a strike to see how its call and put OI built up during the day.
- New live signals carry a NEW badge for two minutes; a thin bar under the header counts down to the next update.
- Shortcuts: F5 start/stop, Ctrl+1…4 tabs, Ctrl+= / Ctrl+- zoom.
