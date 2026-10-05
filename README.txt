NIFTY OI TRACKER - desktop app
==============================

SET UP (once)
  1. Copy this whole folder into E:\nifty_tracker (your existing nifty_tracker_data folder
     will be picked up automatically, because the app keeps its data next to itself).
  2. Double-click build_exe.bat. It finds Python (or offers to install it), installs what it
     needs and creates NiftyOITracker.exe.
     (Needs Python 3.10+ on PATH and an internet connection, one time only.)
  3. Double-click NiftyOITracker.exe. Right-click it > Send to > Desktop to make a shortcut.

  Prefer not to build? Double-click run_app.bat instead - same app, runs from Python.

USING IT
  Mode      Live (market hours)      - start before 9:15; records real OI every 5 minutes
            Replay last session      - test the rules on the last session (real prices, ESTIMATED OI)
            Re-test a recorded day   - pick a day folder recorded in Live mode; current rules, real OI
  Start     begins the chosen mode; Stop ends it. If you restart Live during the day, the
            candles already recorded today are reloaded.
  Chart     5-min candles, OI levels, opening range. Filled triangle = trade idea (letter = grade),
            hollow = information only. Hover a candle for details.
  Signals   newest first; filter to trade ideas or grade A/B only.
  Settings  data folder, light/dark theme, beep, desktop notifications, auto-start, and every
            signal-rule threshold (break distance, stops, reward:risk, grade scores ...).
  Folder    opens the day's CSV files.   Web view - opens the HTML dashboard in your browser.

FILES
  nifty_app.py          the window (PySide6)
  nifty_oi_tracker.py   the engine - also still works on its own from the command line
  nifty_tracker_settings.json   created when you save Settings

Educational tool only - not trading advice. Data belongs to NSE; keep polling gentle.
