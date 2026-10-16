"""
Nifty OI Tracker - desktop app (PySide6 / Qt).

Same engine as nifty_oi_tracker.py (keep both files together), with a clean window instead of a console:
live tracking, replay of the last session, re-test of a recorded day, candle chart with OI levels,
signal feed, option chain, scorecard, settings and desktop alerts.

Run:   python nifty_app.py          (or double-click NiftyOITracker.exe after building with build_exe.bat)
Needs: pip install pyside6 requests

Educational tool only - not trading advice.
"""

import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal, QUrl, QRectF, QPointF, QLockFile
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter, QPen,
                           QBrush, QPainterPath, QFontMetrics, QKeySequence, QShortcut)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QComboBox,
                               QHBoxLayout, QVBoxLayout, QGridLayout, QSplitter, QTabWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QStyledItemDelegate, QScrollArea, QPlainTextEdit,
                               QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QFileDialog,
                               QDialogButtonBox, QMessageBox, QSystemTrayIcon, QToolButton, QSizePolicy,
                               QAbstractItemView, QStyle, QProgressBar)

import nifty_oi_tracker as engine

APP_NAME = "Nifty OI Tracker"
VERSION = "1.0"


# --------------------------------------------------------------------------- paths & settings
def app_dir():
    """Folder of the .exe when frozen, else of this script. Data and settings live here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource(name):
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), name)


SETTINGS_PATH = os.path.join(app_dir(), "nifty_tracker_settings.json")

# (engine constant, label, type, min, max, step, help)
TUNABLES = [
    ("BREAK_MIN", "Break distance (pts)", int, 0, 100, 1, "A 5-min close must be this far through a level"),
    ("BREAK_COOLDOWN_MIN", "Break cooldown (min)", int, 0, 120, 5, "No repeat break on the same level inside this"),
    ("LEVEL_STICKY", "Level stickiness (pts)", int, 0, 200, 10, "Keep a level while price is this close to it"),
    ("STOP_BUFFER", "Minimum stop (pts)", int, 5, 200, 5, "Smallest stop distance beyond a level"),
    ("ATR_STOP_MULT", "Stop × average range", float, 0.5, 4.0, 0.1, "Stop distance in average 5-min ranges"),
    ("MIN_RR", "Minimum reward : risk", float, 0.5, 5.0, 0.1, "Trade ideas below this are skipped"),
    ("TARGET_CAP_ATR", "Target cap (× avg range)", float, 1.0, 20.0, 0.5, "Caps far-away targets"),
    ("SCORE_A", "Score for grade A", int, 1, 7, 1, "Confluence score = OI + price action + context"),
    ("SCORE_B", "Score for grade B", int, 0, 7, 1, "Below this the idea is grade C"),
    ("MIN_GRADE", "Lowest grade to trade", str, None, None, None, "Lower grades are shown but not traded"),
    ("REV_LOOKBACK", "Reversal lookback (candles)", int, 3, 20, 1, "Day low/high must be this recent"),
    ("REV_MIN_SWING", "Reversal minimum swing (pts)", int, 10, 300, 5, "Size of the move into the low/high"),
    ("FLOW_THRESHOLD", "OI-flow threshold (contracts)", int, 1000, 500000, 1000, "Total OI change for flow signals"),
    ("VIX_JUMP_PCT", "VIX jump alert (%)", float, 0.5, 20.0, 0.5, "Rise in VIX between candles"),
]

DEFAULTS = {
    "data_dir": os.path.join(app_dir(), "nifty_tracker_data"),
    "theme": "auto", "sound": True, "notify": True, "notify_all": False, "autostart": False,
    "lots": engine.LOTS, "lot_size": engine.LOT_SIZE, "brokerage": engine.BROKERAGE_PER_ORDER,
    "tunables": {k: getattr(engine, k) for k, *_ in TUNABLES},
}


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        s.update({k: v for k, v in saved.items() if k != "tunables"})
        s["tunables"].update({k: v for k, v in saved.get("tunables", {}).items() if k in s["tunables"]})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


def apply_tunables(s):
    for k, v in s["tunables"].items():
        setattr(engine, k, v)
    apply_money(s)


def apply_money(s):
    """Position size and costs used for the 'profit if you had taken every idea' figures."""
    engine.LOTS = max(1, int(s.get("lots", engine.LOTS)))
    engine.LOT_SIZE = max(1, int(s.get("lot_size", engine.LOT_SIZE)))
    engine.BROKERAGE_PER_ORDER = max(0.0, float(s.get("brokerage", engine.BROKERAGE_PER_ORDER)))


def rs(x, sign=True):
    """Rs 1,234 with a sign; Indian digit grouping for lakhs."""
    v = abs(x)
    whole = f"{v:,.0f}"
    if v >= 1e5:
        n = str(int(round(v)))
        head, tail = n[:-3], n[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    pre = ("+" if x > 0 else "−" if x < 0 else "") if sign else ("−" if x < 0 else "")
    return f"{pre}₹{whole}"


# --------------------------------------------------------------------------- theme
LIGHT = dict(bg="#f5f5f3", card="#ffffff", ink="#1b1b1a", ink2="#6b6a65", line="#e4e3df", grid="#eeede9",
             up="#1a9e6e", dn="#d63b3a", warn="#b87a00", accent="#2563eb", accent_ink="#ffffff",
             atm="#eaf1fd", hover="#f0efec", tip="#ffffff")
DARK = dict(bg="#151514", card="#1f1f1d", ink="#f1f1ee", ink2="#a3a29b", line="#34342f", grid="#2a2a27",
            up="#2fbf86", dn="#e66767", warn="#eda100", accent="#5b8def", accent_ink="#0b0b0b",
            atm="#1e2c42", hover="#2a2a27", tip="#262624")
T = dict(LIGHT)


def arrow_path(up=False):
    """Small chevron SVG in the current ink colour (Qt style sheets need an image file for arrows)."""
    import tempfile
    col = T["ink2"].lstrip("#")
    path = os.path.join(tempfile.gettempdir(), f"nifty_arrow_{'up' if up else 'dn'}_{col}.svg")
    if not os.path.exists(path):
        pts = "2,8 6,4 10,8" if up else "2,4 6,8 10,4"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><polyline points="{pts}" '
                    f'fill="none" stroke="#{col}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return path.replace("\\", "/")


def qss():
    return f"""
    QWidget {{ background:{T['bg']}; color:{T['ink']}; font-size:10pt; }}
    QLabel {{ background:transparent; }}
    QMainWindow::separator {{ background:{T['line']}; }}
    QFrame#card, QFrame#tile {{ background:{T['card']}; border:1px solid {T['line']}; border-radius:10px; }}
    QFrame#tile QLabel, QFrame#card QLabel, QFrame#header QLabel {{ background:transparent; }}
    QFrame#header {{ background:{T['card']}; border-bottom:1px solid {T['line']}; }}
    QLabel#title {{ font-size:15pt; font-weight:650; }}
    QLabel#muted, QLabel#tl {{ color:{T['ink2']}; }}
    QLabel#tl {{ font-size:9pt; }}
    QLabel#tv {{ font-size:17pt; font-weight:650; }}
    QLabel#ts {{ color:{T['ink2']}; font-size:9pt; }}
    QLabel#pill {{ border-radius:11px; padding:3px 12px; font-weight:600; background:{T['hover']}; }}
    QPushButton, QToolButton {{ background:{T['card']}; border:1px solid {T['line']}; border-radius:8px;
        padding:6px 14px; }}
    QPushButton:hover, QToolButton:hover {{ background:{T['hover']}; }}
    QToolButton:checkable {{ color:{T['ink2']}; }}
    QToolButton:checked {{ background:{T['atm']}; border:1px solid {T['accent']}; color:{T['ink']}; }}
    QToolButton:disabled {{ color:{T['line']}; }}
    QPushButton#primary {{ background:{T['accent']}; color:{T['accent_ink']}; border:none; font-weight:650;
        padding:7px 22px; }}
    QPushButton#primary:hover {{ background:{T['accent']}; }}
    QPushButton#danger {{ background:{T['dn']}; color:#ffffff; border:none; font-weight:650; padding:7px 22px; }}
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background:{T['card']}; border:1px solid {T['line']};
        border-radius:8px; padding:5px 8px; }}
    QComboBox QAbstractItemView {{ background:{T['card']}; border:1px solid {T['line']};
        selection-background-color:{T['atm']}; selection-color:{T['ink']}; }}
    QTabWidget::pane {{ border:1px solid {T['line']}; border-radius:10px; background:{T['card']}; top:-1px; }}
    QTabBar::tab {{ background:transparent; padding:7px 16px; margin-right:4px; color:{T['ink2']};
        border-bottom:2px solid transparent; }}
    QTabBar::tab:selected {{ color:{T['ink']}; border-bottom:2px solid {T['accent']}; font-weight:600; }}
    QTableWidget {{ background:{T['card']}; border:none; gridline-color:{T['line']};
        selection-background-color:{T['atm']}; selection-color:{T['ink']}; alternate-background-color:{T['card']}; }}
    QHeaderView::section {{ background:{T['card']}; color:{T['ink2']}; border:none;
        border-bottom:1px solid {T['line']}; padding:6px; font-weight:600; font-size:9pt; }}
    QTableCornerButton::section {{ background:{T['card']}; border:none; }}
    QScrollArea {{ border:none; background:transparent; }}
    QScrollArea > QWidget > QWidget {{ background:transparent; }}
    QPlainTextEdit {{ background:{T['card']}; border:none; font-family:Consolas, 'Cascadia Mono', monospace;
        font-size:9pt; }}
    QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{T['line']}; border-radius:4px; min-height:30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height:0; width:0; }}
    QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
    QScrollBar::handle:horizontal {{ background:{T['line']}; border-radius:4px; min-width:30px; }}
    QToolTip {{ background:{T['tip']}; color:{T['ink']}; border:1px solid {T['line']}; padding:6px; }}
    QStatusBar {{ background:{T['card']}; color:{T['ink2']}; border-top:1px solid {T['line']}; }}
    QStatusBar QLabel {{ background:transparent; color:{T['ink2']}; }}
    QCheckBox {{ background:transparent; spacing:8px; }}
    QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {T['ink2']}; border-radius:4px;
        background:{T['card']}; }}
    QCheckBox::indicator:checked {{ background:{T['accent']}; border:1px solid {T['accent']}; }}
    QComboBox::drop-down {{ border:none; width:24px; }}
    QComboBox::down-arrow {{ image:url("{arrow_path()}"); width:10px; height:10px; }}
    QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        border:none; width:18px; background:transparent; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url("{arrow_path(up=True)}"); width:8px; height:8px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url("{arrow_path()}"); width:8px; height:8px; }}
    QTabWidget > QStackedWidget > QWidget, QWidget#page {{ background:{T['card']}; }}
    QWidget#page QLabel {{ background:transparent; }}
    QDialog {{ background:{T['bg']}; }}
    QProgressBar#countdown {{ background:{T['card']}; border:none; max-height:3px; }}
    QProgressBar#countdown::chunk {{ background:{T['up']}; }}
    """


def dir_color(direction):
    return {"bullish": T["up"], "bearish": T["dn"], "caution": T["warn"]}.get(direction, T["ink2"])


def esc(text):
    import html
    return html.escape(str(text))


# --------------------------------------------------------------------------- background work
class Worker(QThread):
    """Runs one engine generator (live / replay / re-test) and turns its events into Qt signals."""
    info = Signal(str)
    error = Signal(str)
    started_rec = Signal(object)
    snap = Signal(object, object)
    waiting = Signal(object)
    done = Signal(object)

    def __init__(self, mode, out, expiry=None, folder=None, sound=True):
        super().__init__()
        self.mode, self.out, self.expiry, self.folder, self.sound = mode, out, expiry, folder, sound
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            if self.mode == "live":
                gen = engine.iter_live(self.out, self.expiry, stop=self.stop_event)
            elif self.mode == "replay":
                gen = engine.iter_replay(self.out, self.expiry)
            else:
                gen = engine.iter_replay_folder(self.folder, self.expiry)
            for ev in gen:
                kind = ev[0]
                if kind == "info":
                    self.info.emit(ev[1])
                elif kind == "error":
                    self.error.emit(ev[1])
                elif kind == "start":
                    self.started_rec.emit(ev[1])
                elif kind == "wait":
                    self.waiting.emit(ev[1])
                elif kind == "snap":
                    snap, sigs = ev[1], ev[2]
                    self.snap.emit(snap, sigs)
                    if self.mode == "live" and self.sound and sigs and not snap.get("_restored"):
                        engine.beep(sigs[0]["kind"])
                elif kind == "done":
                    self.done.emit(ev[1])
                if self.stop_event.is_set() and self.mode != "live":
                    break
        except Exception as e:  # never crash the window
            self.error.emit(f"{type(e).__name__}: {e}")


class ExpiryLoader(QObject):
    """Fetches the expiry list in a daemon thread (never blocks the window or its closing)."""
    loaded = Signal(list)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            items = list(engine.NSEClient().expiries())[:8]
        except Exception:
            items = []
        try:
            self.loaded.emit(items)
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- small widgets
class Tile(QFrame):
    """Number tile with an intraday sparkline. Flashes green/red when the value moves; click for history."""
    clicked = Signal()

    def __init__(self, label, spark=True):
        super().__init__()
        self.setObjectName("tile")
        self.spark = spark
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 16 if spark else 9)
        lay.setSpacing(1)
        self.l, self.v, self.s = QLabel(label), QLabel("–"), QLabel("")
        self.l.setObjectName("tl")
        self.v.setObjectName("tv")
        self.s.setObjectName("ts")
        for w in (self.l, self.v, self.s):
            lay.addWidget(w)
        lay.addStretch()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(92 if spark else 82)
        self.setMinimumWidth(110)
        self.hist, self.flash_col = [], None
        self.flash_timer = QTimer(self)
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(self._unflash)

    def set(self, value, sub="", color=None):
        old = self.v.text()
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{color};" if color else "")
        fm = QFontMetrics(self.s.font())
        self.s.setText(fm.elidedText(sub, Qt.ElideRight, max(60, self.width() - 26)))
        tip = sub if fm.horizontalAdvance(sub) > self.width() - 26 else ""
        if self.hist:
            tip = (tip + "\n" if tip else "") + "Click for today's history"
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor if len(self.hist) > 1 else Qt.ArrowCursor)
        if old not in ("–", value) and value != "–":
            try:
                a, b = float(re.sub(r"[^\d.\-]", "", old)), float(re.sub(r"[^\d.\-]", "", value))
                self.flash_col = T["up"] if b > a else T["dn"] if b < a else T["accent"]
            except ValueError:
                self.flash_col = T["accent"]
            self.flash_timer.start(1600)
            self.update()

    def set_history(self, hist):
        """hist = [(datetime, value or None), ...]"""
        self.hist = hist
        self.update()

    def _unflash(self):
        self.flash_col = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and len(self.hist) > 1:
            self.clicked.emit()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        vals = [v for _, v in self.hist if v is not None]
        if self.spark and len(vals) > 1:
            lo, hi = min(vals), max(vals)
            r = QRectF(12, self.height() - 16, self.width() - 24, 10)
            step = r.width() / (len(vals) - 1)
            path = QPainterPath()
            for i, v in enumerate(vals):
                pt = QPointF(r.left() + i * step, r.bottom() - ((v - lo) / (hi - lo) if hi > lo else 0.5) * r.height())
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            col = QColor(T["up"] if vals[-1] >= vals[0] else T["dn"])
            col.setAlphaF(0.8)
            p.setPen(QPen(col, 1.4))
            p.drawPath(path)
        if self.flash_col:
            p.setPen(QPen(QColor(self.flash_col), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)
        p.end()


class BarDelegate(QStyledItemDelegate):
    """Draws a soft horizontal OI bar behind the number (calls from the right, puts from the left)."""

    def paint(self, p, opt, idx):
        frac = idx.data(Qt.UserRole) or 0
        side = idx.data(Qt.UserRole + 1)
        bg = idx.data(Qt.BackgroundRole)
        if bg:
            p.fillRect(opt.rect, bg)
        r = opt.rect.adjusted(6, 7, -6, -7)
        w = r.width() * max(0.0, min(1.0, frac))
        col = QColor(T["dn"] if side == "ce" else T["up"])
        col.setAlphaF(0.22)
        bar = QRectF(r.right() - w, r.top(), w, r.height()) if side == "ce" else QRectF(r.left(), r.top(), w, r.height())
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(bar, 3, 3)
        p.setPen(QColor(T["ink"]))
        p.drawText(opt.rect.adjusted(10, 0, -10, 0),
                   (Qt.AlignRight if side == "ce" else Qt.AlignLeft) | Qt.AlignVCenter, str(idx.data()))
        p.restore()


# --------------------------------------------------------------------------- signal helpers
def sig_key(s):
    """Stable id for a signal across refreshes (the dicts can be rebuilt by the engine)."""
    return f"{s['time']:%Y%m%d%H%M}|{s['kind']}|{s['price']:.2f}"


def _num(text):
    return float(text.replace(",", ""))


def plan_levels(s):
    """(entry, stop, target, taken) for a signal, also for ideas skipped on reward:risk or grade."""
    p = s.get("plan")
    if p:
        return p["entry"], p["stop"], p["target"], True
    t = s.get("trade") or ""
    mt, ms = re.search(r"target ([\d,]+(?:\.\d+)?)", t), re.search(r"stop ([\d,]+(?:\.\d+)?)", t)
    if mt and ms:
        return s["price"], _num(ms.group(1)), _num(mt.group(1)), False
    return None


# --------------------------------------------------------------------------- candle chart
class ChartWidget(QWidget):
    """Candles + OI levels. Scroll = zoom, drag = pan, double-click = reset,
    click a marker (or a candle with a signal) = select it and draw its entry / stop / target."""
    MARKED = ("BREAKDOWN", "BREAKOUT", "REJECTION", "BOUNCE", "REVERSAL UP", "REVERSAL DOWN",
              "OR BREAKOUT", "OR BREAKDOWN")
    signal_clicked = Signal(object)          # signal dict, or None to clear
    view_changed = Signal()

    def __init__(self):
        super().__init__()
        self.snaps, self.hover = [], None
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.setFocusPolicy(Qt.ClickFocus)
        self.empty_text = "Press Start to begin."
        self.tf = 5                                   # minutes per candle (5 or 15)
        self.view_n, self.view_off = None, 0          # None = fit the whole day; off = candles hidden on the right
        self.sel_key = None
        self.show = {"oi": True, "fresh": True, "or": True, "marks": True}
        self._press, self._hits, self._geom = None, [], None

    # ---- data / state
    def set_data(self, snaps):
        self.snaps = snaps
        self.update()

    def set_timeframe(self, minutes):
        self.tf = minutes
        self.reset_view()

    def set_selected(self, key):
        self.sel_key = key
        self.update()

    def toggle(self, name, on):
        self.show[name] = on
        self.update()

    def reset_view(self):
        self.view_n, self.view_off = None, 0
        self.unsetCursor()
        self.update()
        self.view_changed.emit()

    def zoomed(self):
        return self.view_n is not None

    def bars(self):
        """Candles at the chosen timeframe: list of dicts {c, snap (last), snaps, dur}."""
        snaps = [s for s in self.snaps if s.get("candle")]
        if self.tf == 5:
            return [{"c": s["candle"], "snap": s, "snaps": [s], "dur": 5} for s in snaps]
        out, cur_key = [], None
        for s in snaps:
            c = s["candle"]
            k = ((c["start"].hour * 60 + c["start"].minute) - 555) // self.tf
            if k != cur_key:
                out.append({"c": dict(c), "snap": s, "snaps": [s], "dur": self.tf})
                cur_key = k
            else:
                b = out[-1]
                b["c"]["high"] = max(b["c"]["high"], c["high"])
                b["c"]["low"] = min(b["c"]["low"], c["low"])
                b["c"]["close"] = c["close"]
                b["snap"] = s
                b["snaps"].append(s)
        return out

    def all_signals(self):
        return [sig for s in self.snaps for sig in s.get("_signals", [])]

    def selected_signal(self):
        if not self.sel_key:
            return None
        return next((g for g in self.all_signals() if sig_key(g) == self.sel_key), None)

    def zoom(self, factor, anchor=0.5):
        total = len(self.bars())
        if total < 2:
            return
        start, count = self._window(total)
        new = int(round((self.view_n or total) * factor))
        if new >= total:
            self.reset_view()
            return
        new = max(8, new)
        b = start + anchor * count
        new_start = max(0, min(total - new, int(round(b - anchor * new))))
        self.view_n, self.view_off = new, total - (new_start + new)
        self.setCursor(Qt.OpenHandCursor)
        self.update()
        self.view_changed.emit()

    def center_on(self, key):
        """Scroll a zoomed view so the candle of this signal is visible."""
        if not self.zoomed():
            return
        bars = self.bars()
        i = self._bar_of(bars, key)
        if i is None:
            return
        start, count = self._window(len(bars))
        if not (start <= i < start + count):
            new_start = max(0, min(len(bars) - count, i - count // 2))
            self.view_off = len(bars) - (new_start + count)
            self.update()

    def _window(self, total):
        if self.view_n is None:
            return 0, total
        count = min(self.view_n, total)
        self.view_off = max(0, min(total - count, self.view_off))
        return total - self.view_off - count, count

    @staticmethod
    def _bar_of(bars, key):
        for i, b in enumerate(bars):
            for s in b["snaps"]:
                if any(sig_key(g) == key for g in s.get("_signals", [])):
                    return i
        return None

    # ---- mouse
    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def wheelEvent(self, e):
        if not self._geom:
            return
        L, cw, start, count, slots, W, R = self._geom
        anchor = max(0.0, min(1.0, (e.position().x() - L) / max(1, cw * slots)))
        anchor = min(anchor * slots / max(1, count), 1.0)
        self.zoom(0.8 if e.angleDelta().y() > 0 else 1.25, anchor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = (e.position(), self.view_off)
            if self.zoomed():
                self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        self.hover = e.position()
        if self._press and self.zoomed() and self._geom and e.buttons() & Qt.LeftButton:
            cw = self._geom[1]
            self.view_off = self._press[1] + int((e.position().x() - self._press[0].x()) / max(1, cw))
        elif not self._press:
            over = any(r.contains(e.position()) for r, _ in self._hits)
            self.setCursor(Qt.PointingHandCursor if over else Qt.OpenHandCursor if self.zoomed() else Qt.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self._press:
            return
        moved = (e.position() - self._press[0]).manhattanLength()
        self._press = None
        self.setCursor(Qt.OpenHandCursor if self.zoomed() else Qt.ArrowCursor)
        if moved > 4:
            self.view_changed.emit()
            return
        for r, sig in self._hits:                       # click on a marker
            if r.contains(e.position()):
                self.signal_clicked.emit(sig)
                return
        if self._geom:                                  # click on a candle that carries a signal
            L, cw, start, count, slots, W, R = self._geom
            i = start + int((e.position().x() - L) / cw)
            bars = self.bars()
            if L <= e.position().x() <= W - R and start <= i < start + count:
                sigs = [g for s in bars[i]["snaps"] for g in s.get("_signals", []) if g["kind"] in self.MARKED]
                if sigs:
                    sigs.sort(key=lambda g: (not g.get("plan"), g.get("grade") or "Z"))
                    self.signal_clicked.emit(sigs[0])
                    return
        self.signal_clicked.emit(None)

    def mouseDoubleClickEvent(self, e):
        self.reset_view()

    # ---- painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T["card"]))
        self._hits, self._geom = [], None
        allbars = self.bars()
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)
        fm = QFontMetrics(small)
        if not allbars:
            p.setPen(QColor(T["ink2"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return
        start, count = self._window(len(allbars))
        bars = allbars[start:start + count]
        candles = [b["c"] for b in bars]
        last = allbars[-1]["snap"]
        lv, pa = last["levels"], last.get("pa") or {}
        levels = []
        if self.show["oi"]:
            levels += [(lv.get("R"), "Max call OI", T["dn"], Qt.DashLine), (lv.get("S"), "Max put OI", T["up"], Qt.DashLine)]
        if self.show["fresh"]:
            if lv.get("CE_fresh_near") not in (lv.get("R"),):
                levels.append((lv.get("CE_fresh_near"), "Fresh calls", T["dn"], Qt.DotLine))
            if lv.get("PE_fresh_near") not in (lv.get("S"),):
                levels.append((lv.get("PE_fresh_near"), "Fresh puts", T["up"], Qt.DotLine))
        if self.show["or"] and pa.get("or_high"):
            levels += [(pa["or_high"], "OR high", T["ink2"], Qt.DashLine), (pa["or_low"], "OR low", T["ink2"], Qt.DashLine)]

        sel = self.selected_signal()
        sel_bar = self._bar_of(allbars, self.sel_key) if sel else None
        plan = plan_levels(sel) if sel else None

        lo, hi = min(c["low"] for c in candles), max(c["high"] for c in candles)
        span = hi - lo
        for v, *_ in levels:
            if v and lo - 0.35 * span - 30 <= v <= hi + 0.35 * span + 30:
                lo, hi = min(lo, v), max(hi, v)
        if plan:
            lo, hi = min([lo] + list(plan[:3])), max([hi] + list(plan[:3]))
        pad = max(15, (hi - lo) * 0.08)
        lo, hi = lo - pad, hi + pad
        W, H = self.width(), self.height()
        L, R, Tm, B = 62, 128, 14, 28
        slots = count if self.zoomed() else max(count, 75 * 5 // self.tf)
        cw = (W - L - R) / slots
        self._geom = (L, cw, start, count, slots, W, R)
        y = lambda v: Tm + (hi - v) / (hi - lo) * (H - Tm - B)
        xof = lambda i: L + (i - start + 0.5) * cw       # i = index into allbars

        # grid + price axis
        step = 10 ** math.floor(math.log10(max(1, (hi - lo) / 5)))
        step *= 5 if (hi - lo) / step > 25 else 2 if (hi - lo) / step > 10 else 1
        g = math.ceil(lo / step) * step
        while g <= hi:
            p.setPen(QPen(QColor(T["grid"]), 1))
            p.drawLine(QPointF(L, y(g)), QPointF(W - R, y(g)))
            p.setPen(QColor(T["ink2"]))
            p.drawText(QRectF(0, y(g) - 8, L - 8, 16), Qt.AlignRight | Qt.AlignVCenter, f"{g:,.0f}")
            g += step

        # selected signal: column highlight + entry / stop / target zones to the right of it
        used = []

        def right_label(v, text, col, bold=False):
            ty = y(v)
            while any(abs(ty - u) < 13 for u in used):
                ty += 13
            used.append(ty)
            f2 = QFont(small)
            f2.setBold(bold)
            p.setFont(f2)
            p.setPen(QColor(col))
            p.drawText(QRectF(W - R + 6, ty - 8, R - 6, 16), Qt.AlignLeft | Qt.AlignVCenter, text)
            p.setFont(small)

        if sel_bar is not None and start <= sel_bar < start + count:
            band = QColor(T["accent"])
            band.setAlphaF(0.10)
            p.fillRect(QRectF(xof(sel_bar) - cw / 2, Tm, cw, H - Tm - B), band)
        if plan:
            entry, stop, target, taken = plan
            x0 = max(L, xof(sel_bar) if sel_bar is not None else L)
            for a, b, col in ((entry, target, T["up"] if target > entry else T["dn"]),
                              (entry, stop, T["dn"] if target > entry else T["up"])):
                fill = QColor(col)
                fill.setAlphaF(0.09 if taken else 0.05)
                p.fillRect(QRectF(x0, min(y(a), y(b)), W - R - x0, abs(y(a) - y(b))), fill)
            for v, name, col, style in ((target, "Target", T["up"] if target > entry else T["dn"], Qt.SolidLine),
                                        (stop, "Stop", T["dn"] if target > entry else T["up"], Qt.SolidLine),
                                        (entry, "Entry", T["accent"], Qt.SolidLine)):
                p.setPen(QPen(QColor(col), 1.6, style if taken else Qt.DashDotLine))
                p.drawLine(QPointF(x0, y(v)), QPointF(W - R, y(v)))
                right_label(v, f"{name} {v:,.0f}", col, bold=True)

        # OI / opening-range levels, labels on the right without overlapping
        for v, name, col, style in levels:
            if not v:
                continue
            if not (lo < v < hi):            # off-scale level: small label at the edge it lies beyond
                ty = Tm + 8 if v > hi else H - B - 8
                while any(abs(ty - u) < 13 for u in used):
                    ty += 13 if v > hi else -13
                used.append(ty)
                p.setPen(QColor(col))
                p.drawText(QRectF(W - R + 6, ty - 8, R - 6, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{'↑' if v > hi else '↓'} {name} {v:,.0f}")
                continue
            p.setPen(QPen(QColor(col), 1.2, style))
            p.drawLine(QPointF(L, y(v)), QPointF(W - R, y(v)))
            right_label(v, f"{name} {v:,.0f}", col)

        # candles + time axis (label spacing adapts to zoom)
        interval = next((m for m in (5, 15, 30, 60, 120) if m >= self.tf and m / self.tf * cw >= 56), 120)
        for k, c in enumerate(candles):
            x = L + (k + 0.5) * cw
            col = QColor(T["up"] if c["close"] >= c["open"] else T["dn"])
            p.setPen(QPen(col, 1.2))
            p.drawLine(QPointF(x, y(c["high"])), QPointF(x, y(c["low"])))
            top, bot = y(max(c["open"], c["close"])), y(min(c["open"], c["close"]))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x - cw * 0.34, top, cw * 0.68, max(1.2, bot - top)), 1, 1)
            mins = c["start"].hour * 60 + c["start"].minute
            if (mins - 555) % interval == 0 if interval < 60 else mins % interval == 0 or mins == 555:
                p.setPen(QColor(T["ink2"]))
                p.drawText(QRectF(x - 30, H - B + 6, 60, 16), Qt.AlignCenter, f"{c['start']:%H:%M}")

        # trade exit marker for the selected idea
        if plan and sel and sel.get("result") and sel["result"].get("exit_time"):
            r = sel["result"]
            ex = next((i for i, b in enumerate(allbars) if any(s["time"] == r["exit_time"] for s in b["snaps"])), None)
            if ex is not None and start <= ex < start + count:
                bull = plan[2] > plan[0]
                px = plan[0] + (r["points"] if bull else -r["points"])
                col = QColor(T["up"] if r["points"] > 0 else T["dn"] if r["points"] < 0 else T["ink2"])
                p.setPen(QPen(col, 2))
                p.setBrush(QColor(T["card"]))
                p.drawEllipse(QPointF(xof(ex), y(px)), 5, 5)
                p.drawText(QRectF(xof(ex) + 8, y(px) - 18, 120, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{r['status']} {r['points']:+,.0f}")

        # signal markers (with grade for trade ideas); remembered for clicking
        marks = {}
        for i in range(start, start + count):
            for s in allbars[i]["snaps"]:
                for sig in s.get("_signals", []):
                    if sig["kind"] in self.MARKED:
                        marks.setdefault(i, []).append(sig)
        if self.show["marks"]:
            for i, sigs in marks.items():
                c = allbars[i]["c"]
                x = xof(i)
                for k, sig in enumerate(sigs[:2]):
                    up = sig["direction"] == "bullish"
                    col = QColor(T["up"] if up else T["dn"])
                    cy = y(c["low"]) + 10 + k * 13 if up else y(c["high"]) - 10 - k * 13
                    path = QPainterPath()
                    if up:
                        path.moveTo(x, cy - 5); path.lineTo(x - 5, cy + 4); path.lineTo(x + 5, cy + 4)
                    else:
                        path.moveTo(x, cy + 5); path.lineTo(x - 5, cy - 4); path.lineTo(x + 5, cy - 4)
                    path.closeSubpath()
                    if sig_key(sig) == self.sel_key:
                        p.setPen(QPen(QColor(T["accent"]), 2))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(QPointF(x, cy), 9, 9)
                    p.setPen(Qt.NoPen)
                    p.setBrush(col if sig.get("plan") else QColor(T["card"]))
                    if not sig.get("plan"):
                        p.setPen(QPen(col, 1.4))
                    p.drawPath(path)
                    if sig.get("plan") and sig.get("grade"):
                        p.setPen(col)
                        p.drawText(QRectF(x + 6, cy - 8, 14, 16), Qt.AlignLeft | Qt.AlignVCenter, sig["grade"])
                    self._hits.append((QRectF(x - 8, cy - 8, 16, 16), sig))

        # zoom hint
        if self.zoomed():
            txt = f"{count} of {len(allbars)} candles · double-click to reset"
            p.setPen(QColor(T["ink2"]))
            p.drawText(QRectF(L, 0, W - L - R, Tm + 2), Qt.AlignRight | Qt.AlignVCenter, txt)

        # hover crosshair + tooltip
        if self.hover is not None and self._press is None and L <= self.hover.x() <= W - R:
            i = start + int((self.hover.x() - L) / cw)
            if start <= i < start + count:
                b = allbars[i]
                c, s = b["c"], b["snap"]
                x = xof(i)
                p.setPen(QPen(QColor(T["ink2"]), 1, Qt.DotLine))
                p.drawLine(QPointF(x, Tm), QPointF(x, H - B))
                hy = max(Tm, min(H - B, self.hover.y()))
                p.drawLine(QPointF(L, hy), QPointF(W - R, hy))
                price = hi - (hy - Tm) / (H - Tm - B) * (hi - lo)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(T["ink"]))
                p.drawRoundedRect(QRectF(2, hy - 9, L - 6, 18), 4, 4)
                p.setPen(QColor(T["card"]))
                p.drawText(QRectF(2, hy - 9, L - 6, 18), Qt.AlignCenter, f"{price:,.0f}")
                first = b["snaps"][0]
                d_oi = ""
                prev_i = self.snaps.index(first) - 1 if first in self.snaps else -1
                if prev_i >= 0 and s.get("strikes") and self.snaps[prev_i].get("strikes"):
                    a, z = self.snaps[prev_i]["strikes"], s["strikes"]
                    dce = sum(z[k]["ce_oi"] - a[k]["ce_oi"] for k in z if k in a)
                    dpe = sum(z[k]["pe_oi"] - a[k]["pe_oi"] for k in z if k in a)
                    d_oi = f"OI chg  calls {dce:+,.0f}  puts {dpe:+,.0f}"
                lines = [f"{c['start']:%H:%M}–{c['start'] + dt.timedelta(minutes=b['dur']):%H:%M}",
                         f"O {c['open']:,.2f}   H {c['high']:,.2f}",
                         f"L {c['low']:,.2f}   C {c['close']:,.2f}",
                         f"PCR {s['pcr']:.2f}" if s.get("pcr") else "PCR –"]
                if d_oi:
                    lines.append(d_oi)
                others = [g for x2 in b["snaps"] for g in x2.get("_signals", []) if g["kind"] not in self.MARKED]
                for sig in marks.get(i, []) + others[:3]:
                    lines.append(f"{sig['kind']}" + (f"  · grade {sig['grade']}" if sig.get("plan") else ""))
                if marks.get(i):
                    lines.append("click to show entry / stop / target")
                bw = max(fm.horizontalAdvance(t) for t in lines) + 20
                bh = len(lines) * 16 + 12
                bx = x + 12 if x + 12 + bw < W - R else x - 12 - bw
                by = max(Tm, min(H - B - bh, hy - bh / 2))
                p.setPen(QPen(QColor(T["line"]), 1))
                p.setBrush(QColor(T["tip"]))
                p.drawRoundedRect(QRectF(bx, by, bw, bh), 8, 8)
                for k, t in enumerate(lines):
                    p.setPen(QColor(T["ink"] if 0 < k < len(lines) - (1 if marks.get(i) else 0) else T["ink2"]))
                    p.drawText(QRectF(bx + 10, by + 6 + k * 16, bw - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, t)
        p.end()


# --------------------------------------------------------------------------- small line chart (history pop-ups)
class LineChart(QWidget):
    """Lines over the day with a hover read-out. series = [(name, colour, [(datetime, value), ...]), ...]"""

    def __init__(self, series, fmt="{:,.0f}"):
        super().__init__()
        self.series, self.fmt, self.hover = series, fmt, None
        self.setMouseTracking(True)
        self.setMinimumSize(520, 260)

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseMoveEvent(self, e):
        self.hover = e.position()
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T["card"]))
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)
        fm = QFontMetrics(small)
        pts = [v for _, _, ser in self.series for _, v in ser if v is not None]
        times = sorted({t for _, _, ser in self.series for t, _ in ser})
        if len(times) < 2 or not pts:
            p.setPen(QColor(T["ink2"]))
            p.drawText(self.rect(), Qt.AlignCenter, "Not enough data yet.")
            return
        lo, hi = min(pts), max(pts)
        pad = (hi - lo) * 0.1 or max(1, abs(hi) * 0.01)
        lo, hi = lo - pad, hi + pad
        W, H = self.width(), self.height()
        L, R, Tm, B = 70, 16, 30, 26
        tx = {t: L + i / (len(times) - 1) * (W - L - R) for i, t in enumerate(times)}
        y = lambda v: Tm + (hi - v) / (hi - lo) * (H - Tm - B)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            p.setPen(QPen(QColor(T["grid"]), 1))
            p.drawLine(QPointF(L, y(v)), QPointF(W - R, y(v)))
            p.setPen(QColor(T["ink2"]))
            p.drawText(QRectF(0, y(v) - 8, L - 8, 16), Qt.AlignRight | Qt.AlignVCenter, self.fmt.format(v))
        every = max(1, int(len(times) / max(1, (W - L - R) / 70)))
        for i, t in enumerate(times):
            if i % every == 0:
                p.drawText(QRectF(tx[t] - 30, H - B + 6, 60, 16), Qt.AlignCenter, f"{t:%H:%M}")
        lx = L
        for name, col, ser in self.series:          # legend
            p.setPen(QPen(QColor(col), 3))
            p.drawLine(QPointF(lx, 12), QPointF(lx + 16, 12))
            p.setPen(QColor(T["ink"]))
            p.drawText(QRectF(lx + 22, 4, 200, 16), Qt.AlignLeft | Qt.AlignVCenter, name)
            lx += 40 + fm.horizontalAdvance(name)
        for name, col, ser in self.series:
            path = QPainterPath()
            first = True
            for t, v in ser:
                if v is None:
                    continue
                pt = QPointF(tx[t], y(v))
                path.moveTo(pt) if first else path.lineTo(pt)
                first = False
            p.setPen(QPen(QColor(col), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        if self.hover is not None and L <= self.hover.x() <= W - R:
            t = min(times, key=lambda t: abs(tx[t] - self.hover.x()))
            p.setPen(QPen(QColor(T["ink2"]), 1, Qt.DotLine))
            p.drawLine(QPointF(tx[t], Tm), QPointF(tx[t], H - B))
            lines = [f"{t:%H:%M}"]
            for name, col, ser in self.series:
                v = dict(ser).get(t)
                if v is not None:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(col))
                    p.drawEllipse(QPointF(tx[t], y(v)), 4, 4)
                    lines.append(f"{name}  {self.fmt.format(v)}")
            bw = max(fm.horizontalAdvance(s) for s in lines) + 20
            bh = len(lines) * 16 + 12
            bx = tx[t] + 12 if tx[t] + 12 + bw < W - R else tx[t] - 12 - bw
            p.setPen(QPen(QColor(T["line"]), 1))
            p.setBrush(QColor(T["tip"]))
            p.drawRoundedRect(QRectF(bx, Tm, bw, bh), 8, 8)
            for k, s in enumerate(lines):
                p.setPen(QColor(T["ink2"] if k == 0 else T["ink"]))
                p.drawText(QRectF(bx + 10, Tm + 6 + k * 16, bw - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, s)
        p.end()


class HistoryDialog(QDialog):
    def __init__(self, parent, title, series, fmt="{:,.0f}", note=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 420)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        head = QLabel(title)
        head.setStyleSheet("font-size:12pt; font-weight:650;")
        v.addWidget(head)
        v.addWidget(LineChart(series, fmt), 1)
        if note:
            n = QLabel(note)
            n.setObjectName("ts")
            n.setWordWrap(True)
            v.addWidget(n)


# --------------------------------------------------------------------------- signal feed
class EquityWidget(QWidget):
    """Running total (Rs) of closed trade ideas, one point per exit, with a zero line."""

    def __init__(self):
        super().__init__()
        self.points = []
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.hover = None

    def set_points(self, pts):
        self.points = pts
        self.update()

    def _geom(self):
        vals = [0.0] + [v for _, v in self.points]
        lo, hi = min(vals), max(vals)
        if hi - lo < 1:
            hi, lo = hi + 500, lo - 500
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
        L, R, Tp, B = 78, 34, 12, 24
        w, h = self.width() - L - R, self.height() - Tp - B
        n = len(vals) - 1
        xs = [L + w * (i / max(n, 1)) for i in range(len(vals))]
        y = lambda v: Tp + (hi - v) / (hi - lo) * h
        return vals, xs, y, lo, hi, L, R

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T["card"]))
        if not self.points:
            p.setPen(QColor(T["ink2"]))
            p.drawText(self.rect(), Qt.AlignCenter, "The running total appears after the first closed trade idea")
            return
        vals, xs, y, lo, hi, L, R = self._geom()
        p.setPen(QPen(QColor(T["grid"]), 1))
        p.drawLine(QPointF(L, y(0)), QPointF(self.width() - R, y(0)))
        p.setPen(QColor(T["ink2"]))
        for v in (0, max(vals), min(vals)):
            if v == 0 or abs(v) > 1:
                p.drawText(QRectF(0, y(v) - 9, L - 8, 18), Qt.AlignRight | Qt.AlignVCenter, rs(v) if v else "₹0")
        col = QColor(T["up"] if vals[-1] >= 0 else T["dn"])
        path = QPainterPath(QPointF(xs[0], y(vals[0])))
        for x, v in zip(xs[1:], vals[1:]):
            path.lineTo(QPointF(x, y(v)))
        p.setPen(QPen(col, 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setBrush(col)
        p.setPen(QPen(QColor(T["card"]), 2))
        for x, v in zip(xs[1:], vals[1:]):
            p.drawEllipse(QPointF(x, y(v)), 4.5, 4.5)
        p.setPen(QColor(T["ink2"]))
        for (t, _), x in zip(self.points, xs[1:]):
            p.drawText(QRectF(x - 30, self.height() - 20, 60, 16), Qt.AlignCenter, f"{t:%H:%M}")
        if self.hover is not None:
            t, v = self.points[self.hover]
            x, yy = xs[self.hover + 1], y(v)
            txt = f"{t:%H:%M}  total {rs(v)}"
            fm = QFontMetrics(p.font())
            bw = fm.horizontalAdvance(txt) + 16
            bx = min(max(x - bw / 2, 4), self.width() - bw - 4)
            p.setBrush(QColor(T["tip"]))
            p.setPen(QPen(QColor(T["line"]), 1))
            p.drawRoundedRect(QRectF(bx, max(2, yy - 34), bw, 24), 6, 6)
            p.setPen(QColor(T["ink"]))
            p.drawText(QRectF(bx, max(2, yy - 34), bw, 24), Qt.AlignCenter, txt)

    def mouseMoveEvent(self, e):
        if not self.points:
            return
        vals, xs, *_ = self._geom()
        px = e.position().x()
        i = min(range(1, len(xs)), key=lambda j: abs(xs[j] - px))
        new = i - 1 if abs(xs[i] - px) < 30 else None
        if new != self.hover:
            self.hover = new
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()


class SignalCard(QFrame):
    clicked = Signal(object)

    def __init__(self, sig):
        super().__init__()
        self.sig = sig
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.sig)


class SignalFeed(QWidget):
    selected = Signal(object)            # signal dict or None
    FILTERS = ["All signals", "Trade ideas only", "Graded A/B only", "Bullish only", "Bearish only"]
    NEW_SECONDS = 120

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Signals")
        t.setStyleSheet("font-size:12pt; font-weight:650;")
        self.count = QLabel("")
        self.count.setObjectName("muted")
        self.filter = QComboBox()
        self.filter.addItems(self.FILTERS)
        self.filter.currentIndexChanged.connect(lambda _: self.render())
        head.addWidget(t)
        head.addWidget(self.count)
        head.addStretch()
        head.addWidget(self.filter)
        lay.addLayout(head)
        hint = QLabel("Click a card to see it on the chart · Ctrl+↑/↓ steps through them")
        hint.setObjectName("ts")
        lay.addWidget(hint)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.area.setWidget(self.inner)
        lay.addWidget(self.area, 1)
        self.signals, self.cards, self.sel_key, self.new_at = [], {}, None, {}
        self.new_timer = QTimer(self)
        self.new_timer.setSingleShot(True)
        self.new_timer.timeout.connect(self.render)

    def set_signals(self, signals):
        self.signals = signals
        self.render()

    def mark_new(self, sigs):
        now = time.monotonic()
        for s in sigs:
            self.new_at[sig_key(s)] = now
        self.new_timer.start(self.NEW_SECONDS * 1000 + 500)

    def visible(self):
        f = self.filter.currentIndex()
        keep = [lambda s: True, lambda s: s.get("plan"), lambda s: s.get("plan") and s.get("grade") in ("A", "B"),
                lambda s: s["direction"] == "bullish", lambda s: s["direction"] == "bearish"][f]
        return [s for s in self.signals if keep(s)]

    def set_selected(self, key, scroll=True):
        self.sel_key = key
        for k, fr in self.cards.items():
            self.style_card(fr, k == key)
        if scroll and key in self.cards:
            fr = self.cards[key]
            QTimer.singleShot(0, lambda: self.area.ensureWidgetVisible(fr, 0, 40))

    def style_card(self, fr, on):
        col = dir_color(fr.sig["direction"])
        if on:
            fr.setStyleSheet(f"QFrame#card {{ border:2px solid {T['accent']}; border-left:5px solid {col};"
                             f" background:{T['atm']}; }}")
        else:
            fr.setStyleSheet(f"QFrame#card {{ border-left:4px solid {col}; }}"
                             f" QFrame#card:hover {{ background:{T['hover']}; }}")

    def render(self):
        pos = self.area.verticalScrollBar().value()
        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(0, 0, 6, 0)
        self.vbox.setSpacing(8)
        self.vbox.addStretch()
        self.area.setWidget(self.inner)                 # the old one is deleted by Qt
        self.cards = {}
        QTimer.singleShot(0, lambda: self.area.verticalScrollBar().setValue(pos))
        sigs = self.visible()
        self.count.setText(f"{len(sigs)}" + (f" of {len(self.signals)}" if len(sigs) != len(self.signals) else ""))
        if not sigs:
            lab = QLabel("No signals yet." if not self.signals else "Nothing matches this filter.")
            lab.setObjectName("muted")
            lab.setAlignment(Qt.AlignCenter)
            lab.setMinimumHeight(80)
            self.vbox.insertWidget(0, lab)
            return
        for i, s in enumerate(reversed(sigs[-150:])):
            self.vbox.insertWidget(i, self.card(s))

    def card(self, s):
        key = sig_key(s)
        fr = SignalCard(s)
        fr.clicked.connect(lambda sig: self.selected.emit(None if sig_key(sig) == self.sel_key else sig))
        self.cards[key] = fr
        self.style_card(fr, key == self.sel_key)
        col = dir_color(s["direction"])
        lay = QVBoxLayout(fr)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(4)
        badge = ""
        if s.get("grade") and (s.get("plan") or s.get("score") is not None):
            gcol = {"A": T["up"], "B": T["accent"], "C": T["ink2"]}[s["grade"]]
            badge = (f"&nbsp;&nbsp;<span style='color:{gcol}; font-weight:700;'>Grade {s['grade']}"
                     + (f" · {s['score']}" if s.get("score") is not None else "") + "</span>")
        new = ""
        if time.monotonic() - self.new_at.get(key, -1e9) < self.NEW_SECONDS:
            new = (f"&nbsp;&nbsp;<span style='background:{T['accent']}; color:{T['accent_ink']};"
                   f" font-weight:700;'>&nbsp;NEW&nbsp;</span>")
        head = QLabel(f"<span style='color:{T['ink2']};'>{s['time']:%H:%M}</span>&nbsp;&nbsp;"
                      f"<b style='color:{col};'>{esc(s['kind'])}</b>{badge}"
                      f"<span style='color:{T['ink2']};'>&nbsp;&nbsp;@ {s['price']:,.2f}</span>{new}")
        lay.addWidget(head)
        det = QLabel(str(s["text"]))
        det.setTextFormat(Qt.PlainText)
        det.setWordWrap(True)
        lay.addWidget(det)
        if s.get("trade"):
            tr = QLabel(str(s["trade"]))
            tr.setTextFormat(Qt.PlainText)
            tr.setWordWrap(True)
            tr.setStyleSheet("font-weight:600;" if s.get("plan") else f"color:{T['ink2']};")
            lay.addWidget(tr)
        if s.get("option"):
            op = QLabel(str(s["option"]))
            op.setTextFormat(Qt.PlainText)
            op.setWordWrap(True)
            op.setStyleSheet(f"background:{T['atm']}; border-radius:6px; padding:5px 8px; font-weight:600;")
            lay.addWidget(op)
        r = s.get("result")
        if r:
            rc = T["up"] if r["points"] > 0 else T["dn"] if r["points"] < 0 else T["ink2"]
            when = f" at {r['exit_time']:%H:%M}" if r["exit_time"] else " (running)"
            opt = (f" · option {r['opt_pnl']:+,.1f} Rs/unit{'' if r['opt_real'] else ' est.'}"
                   if r["opt_pnl"] is not None else "")
            m = r.get("money")
            rs_txt = (f" · <b style='color:{T['up'] if m['net'] > 0 else T['dn'] if m['net'] < 0 else T['ink2']};'>"
                      f"{'now ' if r['status'] == 'OPEN' else ''}{rs(m['net'])}</b> on {m['qty']} qty") if m else ""
            res = QLabel(f"<b style='color:{rc};'>{r['status']}</b>{when} · {r['points']:+,.0f} pts{opt}{rs_txt}")
            lay.addWidget(res)
        note = QLabel(str(s["bias_note"]))
        note.setTextFormat(Qt.PlainText)
        note.setWordWrap(True)
        note.setObjectName("ts")
        lay.addWidget(note)
        for w in fr.findChildren(QLabel):               # let clicks on the text reach the card
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        return fr


# --------------------------------------------------------------------------- settings dialog
class SettingsDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.s = json.loads(json.dumps(settings))
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        gen = QWidget()
        fl = QFormLayout(gen)
        fl.setContentsMargins(16, 16, 16, 16)
        fl.setVerticalSpacing(10)
        row = QHBoxLayout()
        self.data_dir = QLineEdit(self.s["data_dir"])
        b = QPushButton("Browse…")
        b.clicked.connect(self.pick_dir)
        row.addWidget(self.data_dir, 1)
        row.addWidget(b)
        fl.addRow("Data folder", row)
        self.theme = QComboBox()
        self.theme.addItems(["auto", "light", "dark"])
        self.theme.setCurrentText(self.s["theme"])
        fl.addRow("Theme", self.theme)
        self.sound = QCheckBox("Beep on new signals (live mode)")
        self.sound.setChecked(self.s["sound"])
        self.notify = QCheckBox("Desktop notification for new trade ideas")
        self.notify.setChecked(self.s["notify"])
        self.notify_all = QCheckBox("…and for every other signal too")
        self.notify_all.setChecked(self.s["notify_all"])
        self.autostart = QCheckBox("Start live tracking when the app opens")
        self.autostart.setChecked(self.s["autostart"])
        for w in (self.sound, self.notify, self.notify_all, self.autostart):
            fl.addRow("", w)
        sep = QLabel("Profit calculation – 'if you had taken every idea'")
        sep.setObjectName("tl")
        fl.addRow(sep)
        self.lots = QSpinBox()
        self.lots.setRange(1, 100)
        self.lots.setValue(int(self.s.get("lots", 1)))
        self.lots.setToolTip("How many lots you would buy for each trade idea")
        fl.addRow("Lots per idea", self.lots)
        self.lot_size = QSpinBox()
        self.lot_size.setRange(1, 1000)
        self.lot_size.setValue(int(self.s.get("lot_size", 65)))
        self.lot_size.setToolTip("Units per Nifty lot (65 from Jan 2026). Change only if NSE revises it.")
        fl.addRow("Lot size (units)", self.lot_size)
        self.brokerage = QDoubleSpinBox()
        self.brokerage.setRange(0, 500)
        self.brokerage.setDecimals(0)
        self.brokerage.setValue(float(self.s.get("brokerage", 20)))
        self.brokerage.setPrefix("₹ ")
        self.brokerage.setToolTip("Your broker's charge per executed order (buy and sell are 2 orders). "
                                  "STT, exchange fees, GST and stamp duty are added automatically.")
        fl.addRow("Brokerage per order", self.brokerage)
        tabs.addTab(gen, "General")

        rules = QWidget()
        rl = QFormLayout(rules)
        rl.setContentsMargins(16, 16, 16, 16)
        rl.setVerticalSpacing(8)
        self.inputs = {}
        for key, label, typ, lo, hi, step, tip in TUNABLES:
            val = self.s["tunables"][key]
            if typ is str:
                w = QComboBox()
                w.addItems(["A", "B", "C"])
                w.setCurrentText(str(val))
            elif typ is int:
                w = QSpinBox()
                w.setRange(lo, hi)
                w.setSingleStep(step)
                w.setValue(int(val))
                w.setGroupSeparatorShown(True)
            else:
                w = QDoubleSpinBox()
                w.setRange(lo, hi)
                w.setSingleStep(step)
                w.setDecimals(1)
                w.setValue(float(val))
            w.setToolTip(tip)
            lab = QLabel(label)
            lab.setToolTip(tip)
            rl.addRow(lab, w)
            self.inputs[key] = (w, typ)
        reset = QPushButton("Reset rules to defaults")
        reset.clicked.connect(self.reset_rules)
        rl.addRow("", reset)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(rules)
        tabs.addTab(sc, "Signal rules")

        note = QLabel("Rule changes apply from the next Start. Re-test a recorded day to see their effect.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        outer.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.data_dir.text())
        if d:
            self.data_dir.setText(d)

    def reset_rules(self):
        for key, (w, typ) in self.inputs.items():
            v = DEFAULTS["tunables"][key]
            w.setCurrentText(str(v)) if typ is str else w.setValue(v)

    def result_settings(self):
        s = self.s
        s["data_dir"] = self.data_dir.text().strip() or DEFAULTS["data_dir"]
        s["theme"] = self.theme.currentText()
        s["sound"], s["notify"] = self.sound.isChecked(), self.notify.isChecked()
        s["notify_all"], s["autostart"] = self.notify_all.isChecked(), self.autostart.isChecked()
        s["lots"], s["lot_size"], s["brokerage"] = self.lots.value(), self.lot_size.value(), self.brokerage.value()
        for key, (w, typ) in self.inputs.items():
            s["tunables"][key] = w.currentText() if typ is str else typ(w.value())
        return s


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    MODES = [("live", "Live (market hours)"), ("replay", "Replay last session"), ("retest", "Re-test a recorded day")]

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        apply_money(self.settings)
        self.worker, self.rec, self.next_wait = None, None, None
        self.running_mode, self.had_error = "", False
        self.snaps, self.signals = [], []
        self.sel_key, self.chain_strikes, self.trade_rows = None, [], []
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 860)
        self.setMinimumSize(1080, 680)
        icon = QIcon(resource("app.ico"))
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(lambda *_: (self.showNormal(), self.activateWindow()))
            self.tray.show()
        self.build()
        self.build_shortcuts()
        self.apply_theme()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.expiry_loader = ExpiryLoader()
        self.expiry_loader.loaded.connect(self.on_expiries)
        self.expiry_loader.start()
        if self.settings["autostart"]:
            QTimer.singleShot(800, self.start)

    # ---- layout
    def build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 10, 18, 10)
        h.setSpacing(10)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        title = QLabel("Nifty 50 · OI Tracker")
        title.setObjectName("title")
        self.subtitle = QLabel("Open interest + price action, every 5 minutes")
        self.subtitle.setObjectName("muted")
        tb.addWidget(title)
        tb.addWidget(self.subtitle)
        h.addLayout(tb)
        h.addSpacing(12)
        self.pill = QLabel("●  Ready")
        self.pill.setObjectName("pill")
        h.addWidget(self.pill)
        h.addStretch()
        self.mode = QComboBox()
        for _, label in self.MODES:
            self.mode.addItem(label)
        self.mode.setToolTip("Live: record real OI every 5 min during market hours\n"
                             "Replay: test the rules on the last session (real prices, estimated OI)\n"
                             "Re-test: run the current rules on a day recorded in live mode (real OI)")
        self.expiry = QComboBox()
        self.expiry.addItem("Nearest expiry")
        self.expiry.setToolTip("Option expiry to track")
        self.expiry.setMinimumWidth(150)
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.toggle)
        self.start_btn.setToolTip("Start / stop (F5)")
        h.addWidget(self.mode)
        h.addWidget(self.expiry)
        h.addWidget(self.start_btn)
        for text, tip, fn in (("Folder", "Open the data folder", self.open_folder),
                              ("Web view", "Open the HTML dashboard in your browser", self.open_html),
                              ("Settings", "Settings and signal rules", self.open_settings)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addWidget(header)
        self.countdown = QProgressBar()
        self.countdown.setObjectName("countdown")
        self.countdown.setTextVisible(False)
        self.countdown.setRange(0, 1000)
        self.countdown.setFixedHeight(3)
        self.countdown.setValue(0)
        v.addWidget(self.countdown)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(16, 14, 16, 12)
        bv.setSpacing(12)
        tiles = QGridLayout()
        tiles.setSpacing(10)
        self.tiles = {}
        for i, (key, label) in enumerate([("nifty", "Nifty (5-min close)"), ("pcr", "PCR"),
                                          ("res", "Resistance · max call OI"), ("sup", "Support · max put OI"),
                                          ("mp", "Max pain"), ("vix", "India VIX"), ("trend", "15-min trend"),
                                          ("pa", "Price action"), ("pnl", "Profit if taken")]):
            t = Tile(label, spark=key not in ("pa", "pnl"))
            t.clicked.connect(lambda k=key: self.show_tile_history(k))
            self.tiles[key] = t
            tiles.addWidget(t, 0, i)
        bv.addLayout(tiles)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        self.tabs = QTabWidget()
        self.chart = ChartWidget()
        cw = QWidget()
        cw.setObjectName("page")
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        for name, text, tip in (("oi", "OI levels", "Max call / put OI lines"),
                                ("fresh", "Fresh OI", "Strikes with the biggest fresh writing"),
                                ("or", "Opening range", "First 15-minute high and low"),
                                ("marks", "Signals", "Signal markers on the candles")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setChecked(True)
            b.toggled.connect(lambda on, n=name: self.chart.toggle(n, on))
            bar.addWidget(b)
        bar.addSpacing(8)
        self.tf = QComboBox()
        self.tf.addItems(["5 min", "15 min"])
        self.tf.setToolTip("Candle size")
        self.tf.currentIndexChanged.connect(lambda i: self.chart.set_timeframe(5 if i == 0 else 15))
        bar.addWidget(self.tf)
        self.reset_zoom = QToolButton()
        self.reset_zoom.setText("Reset zoom")
        self.reset_zoom.setToolTip("Show the whole day (Ctrl+0, or double-click the chart)")
        self.reset_zoom.clicked.connect(self.chart.reset_view)
        self.reset_zoom.setEnabled(False)
        self.chart.view_changed.connect(lambda: self.reset_zoom.setEnabled(self.chart.zoomed()))
        bar.addWidget(self.reset_zoom)
        bar.addStretch()
        cl.addLayout(bar)
        cl.addWidget(self.chart, 1)
        legend = QLabel("▲▼ filled = trade idea (letter = grade) · hollow = information only · "
                        "scroll = zoom · drag = pan · click a marker = entry / stop / target")
        legend.setObjectName("ts")
        cl.addWidget(legend)
        self.tabs.addTab(cw, "Chart")
        self.chain = QTableWidget(0, 7)
        self.chain.setHorizontalHeaderLabels(["Call day chg", "Call 5-min", "Call OI", "Strike", "Put OI",
                                              "Put 5-min", "Put day chg"])
        self.chain.verticalHeader().hide()
        self.chain.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.chain.setSelectionMode(QAbstractItemView.SingleSelection)
        self.chain.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.chain.cellDoubleClicked.connect(self.show_strike_history)
        self.chain.setShowGrid(False)
        self.chain.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.chain.verticalHeader().setDefaultSectionSize(30)
        self.bar_delegate = BarDelegate(self.chain)
        self.chain.setItemDelegateForColumn(2, self.bar_delegate)
        self.chain.setItemDelegateForColumn(4, self.bar_delegate)
        self.tabs.addTab(self.chain, "Option chain")
        self.tabs.addTab(self.build_scorecard(), "Profit && scorecard")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")
        split.addWidget(self.tabs)
        self.feed = SignalFeed()
        self.feed.selected.connect(lambda sig: self.select_signal(sig, scroll_feed=False))
        self.chart.signal_clicked.connect(lambda sig: self.select_signal(sig, show_chart=False))
        split.addWidget(self.feed)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([820, 480])
        bv.addWidget(split, 1)
        v.addWidget(body, 1)

        self.status_folder = QLabel("")
        self.status_upd = QLabel("")
        self.statusBar().addWidget(self.status_folder, 1)
        self.statusBar().addPermanentWidget(self.status_upd)
        self.statusBar().addPermanentWidget(QLabel("Educational tool – not trading advice  "))
        self.status_folder.setText(f"Data folder: {self.settings['data_dir']}")

    def build_scorecard(self):
        w = QWidget()
        w.setObjectName("page")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)
        self.money_note = QLabel("")
        self.money_note.setObjectName("muted")
        self.money_note.setWordWrap(True)
        v.addWidget(self.money_note)
        g = QHBoxLayout()
        self.sc_tiles = {}
        for key, label in (("net", "Net profit today"), ("open", "Open trades (if closed now)"),
                           ("gradeA", "Grade A only"), ("cap", "Money needed"), ("won", "Won / closed")):
            t = Tile(label, spark=False)
            self.sc_tiles[key] = t
            g.addWidget(t)
        v.addLayout(g)
        self.equity = EquityWidget()
        v.addWidget(self.equity)
        self.sc_grades = QLabel("")
        self.sc_grades.setObjectName("muted")
        self.sc_grades.setWordWrap(True)
        v.addWidget(self.sc_grades)
        self.trades = QTableWidget(0, 9)
        self.trades.setHorizontalHeaderLabels(["Time", "Grade", "Trade", "Buy ₹", "Exit ₹", "Result", "Qty",
                                               "Charges", "Net profit"])
        self.trades.verticalHeader().hide()
        self.trades.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.trades.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trades.cellDoubleClicked.connect(self.on_trade_dblclick)
        self.trades.setShowGrid(False)
        hh = self.trades.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.trades.verticalHeader().setDefaultSectionSize(28)
        self.trades.setMinimumHeight(170)
        v.addWidget(self.trades)
        hist_t = QLabel("Earlier days (live runs) – net profit if every idea was taken")
        hist_t.setObjectName("tl")
        v.addWidget(hist_t)
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(["Day", "Trades", "Profitable", "Net profit"])
        self.history.verticalHeader().hide()
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.setShowGrid(False)
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history.verticalHeader().setDefaultSectionSize(26)
        self.history.setMinimumHeight(120)
        v.addWidget(self.history)
        rule = QLabel("Each idea is followed candle by candle: stop or target on later 5-min candles (stop counted "
                      "first if both fall in one candle); open trades close at 15:30. Option exit price is the real "
                      "LTP in live / re-test mode and an estimate in replay. Charges are approximate (brokerage, "
                      "STT, exchange, GST, stamp). Change lots, lot size and brokerage in Settings.")
        rule.setObjectName("ts")
        rule.setWordWrap(True)
        v.addWidget(rule)
        v.addStretch()
        sc.setWidget(inner)
        outer.addWidget(sc)
        return w

    # ---- theme
    def apply_theme(self):
        mode = self.settings["theme"]
        if mode == "auto":
            try:
                dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
            except AttributeError:
                dark = False
        else:
            dark = mode == "dark"
        T.clear()
        T.update(DARK if dark else LIGHT)
        QApplication.instance().setStyleSheet(qss())
        self.refresh_all()

    # ---- actions
    def toggle(self):
        if self.worker and self.worker.isRunning():
            self.stop()
        else:
            self.start()

    def start(self):
        if self.worker and self.worker.isRunning():
            return
        mode = self.MODES[self.mode.currentIndex()][0]
        folder = None
        if mode == "retest":
            folder = QFileDialog.getExistingDirectory(self, "Choose a day recorded in live mode",
                                                      self.settings["data_dir"])
            if not folder:
                return
        apply_tunables(self.settings)
        os.makedirs(self.settings["data_dir"], exist_ok=True)
        expiry = self.expiry.currentText() if self.expiry.currentIndex() > 0 else None
        self.snaps, self.signals, self.rec, self.next_wait = [], [], None, None
        self.select_signal(None)
        self.chart.empty_text = "Connecting to NSE…" if mode != "retest" else "Loading…"
        self.refresh_all()
        self.worker = Worker(mode, self.settings["data_dir"], expiry, folder, self.settings["sound"])
        self.worker.info.connect(lambda t: self.log_line(t))
        self.worker.error.connect(self.on_error)
        self.worker.started_rec.connect(self.on_started)
        self.worker.snap.connect(self.on_snap)
        self.worker.waiting.connect(self.on_wait)
        self.worker.done.connect(self.on_done)
        self.worker.finished.connect(self.on_finished)
        self.running_mode, self.had_error = mode, False
        self.set_running(True)
        self.log_line(f"Started: {dict(self.MODES)[mode]}")
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.pill_set("Stopping…", T["ink2"])

    def set_running(self, on):
        self.start_btn.setText("Stop" if on else "Start")
        self.start_btn.setObjectName("danger" if on else "primary")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)
        self.mode.setEnabled(not on)
        self.expiry.setEnabled(not on)
        if on:
            self.pill_set("Working…", T["accent"])

    def pill_set(self, text, color):
        self.pill.setText(f"●  {text}")
        self.pill.setStyleSheet(f"color:{color};")

    def open_folder(self):
        path = self.rec.folder if self.rec else self.settings["data_dir"]
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_html(self):
        if self.rec and os.path.exists(self.rec.dash_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.rec.dash_path))
        else:
            QMessageBox.information(self, APP_NAME, "The HTML dashboard appears after the first run.")

    def open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self.settings = dlg.result_settings()
            save_settings(self.settings)
            apply_money(self.settings)
            self.status_folder.setText(f"Data folder: {self.settings['data_dir']}")
            self.apply_theme()
            if self.worker and self.worker.isRunning():
                self.log_line("Settings saved - rule changes apply from the next Start.")

    # ---- worker events
    def log_line(self, text):
        self.log.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def on_error(self, text):
        self.log_line("! " + text)
        if not self.snaps:
            self.chart.empty_text = text
            self.chart.update()
        if self.running_mode == "live" and self.snaps:
            return                      # a missed candle while live: keep going, it is in the Log
        self.had_error = True
        low = text.lower()
        if "closed" in low or "holiday" in low or "weekend" in low:
            self.pill_set("Market closed", T["ink2"])
        elif "reach nse" in low:
            self.pill_set("Can't reach NSE – check internet", T["dn"])
        else:
            self.pill_set(text if len(text) < 60 else "Problem – see Log", T["dn"])

    def on_started(self, rec):
        self.rec = rec
        names = {"live": "Live", "replay": "Replay · estimated OI", "retest": "Re-test · real OI"}
        day = f"{rec.day:%a %d %b %Y}" if rec.day else ""
        self.subtitle.setText(" · ".join(x for x in (names.get(rec.mode, ""), day,
                                                     f"expiry {rec.expiry}" if rec.expiry else "") if x))
        self.status_folder.setText(f"Saving to: {rec.folder}")
        self.chart.empty_text = ("Waiting for the first 5-minute candle (09:20)…" if rec.mode == "live"
                                 else "Loading…")
        self.chart.update()

    def on_snap(self, snap, sigs):
        self.snaps.append(snap)
        self.signals.extend(sigs)
        replaying = self.running_mode != "live" or snap.get("_restored")
        if not replaying and sigs:
            self.feed.mark_new(sigs)
        if not replaying or len(self.snaps) % 15 == 0:
            self.refresh_all()
        if replaying:
            return
        for s in sigs:
            self.log_line(f"{s['kind']} @ {s['price']:,.2f} - {s['text']}")
        trade = [s for s in sigs if s.get("plan")]
        if self.settings["notify"] and (trade or (self.settings["notify_all"] and sigs)):
            s = (trade or sigs)[0]
            title = f"{s['kind']}" + (f" · grade {s['grade']}" if s.get("plan") else "") + f" @ {s['price']:,.0f}"
            body = s.get("option") or s.get("trade") or s["text"]
            if self.tray:
                self.tray.showMessage(title, body, QSystemTrayIcon.Information, 10000)
            QApplication.alert(self, 0)

    def on_wait(self, nxt):
        self.next_wait = nxt

    def on_done(self, rec):
        self.rec = rec
        self.refresh_all()
        res = [s["result"] for s in self.signals if s.get("result")]
        self.log_line(f"Finished: {len(self.snaps)} candles, {len(self.signals)} signals, {len(res)} trade ideas.")

    def on_finished(self):
        self.set_running(False)
        self.next_wait = None
        self.refresh_all()
        if not self.had_error:
            done = {"live": "Stopped", "replay": "Replay finished", "retest": "Re-test finished"}
            self.pill_set(done.get(self.running_mode, "Ready"), T["ink2"])

    def tick(self):
        if self.worker and self.worker.isRunning() and self.running_mode == "live" and self.next_wait:
            now = dt.datetime.now(engine.IST)
            left = (self.next_wait + dt.timedelta(seconds=engine.POLL_DELAY_SEC) - now).total_seconds()
            self.countdown.setValue(int(1000 * max(0.0, min(1.0, 1 - left / 300.0))))
            if left > 0:
                m, s = divmod(int(left), 60)
                h, m = divmod(m, 60)
                eta = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                self.pill_set(f"Live · next update {self.next_wait:%H:%M} (in {eta})", T["up"])
            else:
                self.pill_set("Live · fetching…", T["up"])
        elif self.countdown.value():
            self.countdown.setValue(0)

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, APP_NAME, "Tracking is running. Stop it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.worker.stop()
            self.worker.wait(3000)
        if self.tray:
            self.tray.hide()
        e.accept()

    def on_expiries(self, items):
        for x in items:
            self.expiry.addItem(x)

    # ---- interaction
    def build_shortcuts(self):
        keys = [("F5", self.toggle), ("Esc", lambda: self.select_signal(None)),
                ("Ctrl+Up", lambda: self.step_signal(-1)), ("Ctrl+Down", lambda: self.step_signal(1)),
                ("Ctrl+0", self.chart.reset_view), ("Ctrl+=", lambda: self.chart.zoom(0.8, 1.0)),
                ("Ctrl++", lambda: self.chart.zoom(0.8, 1.0)), ("Ctrl+-", lambda: self.chart.zoom(1.25, 1.0))]
        keys += [(f"Ctrl+{i + 1}", lambda i=i: self.tabs.setCurrentIndex(i)) for i in range(self.tabs.count())]
        for seq, fn in keys:
            QShortcut(QKeySequence(seq), self, activated=fn)

    def select_signal(self, sig, scroll_feed=True, show_chart=True):
        """One selection shared by the chart, the feed and the status bar."""
        self.sel_key = sig_key(sig) if sig else None
        self.chart.set_selected(self.sel_key)
        self.feed.set_selected(self.sel_key, scroll=scroll_feed)
        if not sig:
            self.statusBar().clearMessage()
            return
        if show_chart:
            self.tabs.setCurrentIndex(0)
        self.chart.center_on(self.sel_key)
        lv = plan_levels(sig)
        msg = f"{sig['time']:%H:%M} {sig['kind']} @ {sig['price']:,.2f}"
        if lv:
            entry, stop, target, taken = lv
            rr = abs(target - entry) / max(0.01, abs(entry - stop))
            msg += (f"   ·   entry {entry:,.0f}  stop {stop:,.0f}  target {target:,.0f}  (R:R {rr:.1f})"
                    + ("" if taken else "  ·  skipped"))
        self.statusBar().showMessage(msg + "   ·   Esc to clear")

    def step_signal(self, d):
        """Ctrl+Up / Ctrl+Down: previous / next signal shown in the feed (newest is at the top)."""
        sigs = list(reversed(self.feed.visible()))
        if not sigs:
            return
        keys = [sig_key(s) for s in sigs]
        i = keys.index(self.sel_key) + d if self.sel_key in keys else 0
        self.select_signal(sigs[max(0, min(len(sigs) - 1, i))])

    TILE_HIST = {"nifty": ("Nifty 5-min close", "{:,.2f}", lambda s: s["candle"]["close"]),
                 "pcr": ("Put-call ratio", "{:.2f}", lambda s: s.get("pcr")),
                 "res": ("Resistance (max call OI strike)", "{:,.0f}", lambda s: s["levels"].get("R")),
                 "sup": ("Support (max put OI strike)", "{:,.0f}", lambda s: s["levels"].get("S")),
                 "mp": ("Max pain", "{:,.0f}", lambda s: s.get("max_pain")),
                 "vix": ("India VIX", "{:.2f}", lambda s: s.get("vix")),
                 "trend": ("15-min EMA9", "{:,.0f}", lambda s: (s.get("trend15") or {}).get("ema"))}

    def tile_history(self, key):
        if key not in self.TILE_HIST:
            return []
        fn = self.TILE_HIST[key][2]
        out = []
        for s in self.snaps:
            if not s.get("candle"):
                continue
            try:
                out.append((s["time"], fn(s)))
            except (KeyError, TypeError):
                out.append((s["time"], None))
        return out

    def show_tile_history(self, key):
        hist = self.tile_history(key)
        if len(hist) < 2:
            return
        title, fmt, _ = self.TILE_HIST[key]
        series = [(title, T["accent"], hist)]
        if key == "nifty":
            series += [("Max call OI", T["dn"], self.tile_history("res")), ("Max put OI", T["up"], self.tile_history("sup"))]
        self.open_dialog(HistoryDialog(self, f"{title} · today", series, fmt))

    def show_strike_history(self, row, _col=0):
        if not (0 <= row < len(self.chain_strikes)):
            return
        k = self.chain_strikes[row]
        ce = [(s["time"], s["strikes"][k]["ce_oi"]) for s in self.snaps if k in s.get("strikes", {})]
        pe = [(s["time"], s["strikes"][k]["pe_oi"]) for s in self.snaps if k in s.get("strikes", {})]
        self.open_dialog(HistoryDialog(self, f"{k:,.0f} strike · call vs put OI today",
                                       [("Call OI", T["dn"], ce), ("Put OI", T["up"], pe)],
                                       note="Rising call OI = writers defending this strike from above; "
                                            "rising put OI = support building. Hover the lines for exact values."))

    def on_trade_dblclick(self, row, _col):
        if 0 <= row < len(self.trade_rows):
            self.select_signal(self.trade_rows[row])

    def open_dialog(self, dlg):
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    # ---- rendering
    def refresh_all(self):
        if not hasattr(self, "tiles"):
            return
        if self.snaps:
            try:
                engine.evaluate_trades(self.snaps, self.signals)
            except Exception:
                pass
        self.refresh_tiles()
        self.chart.set_data(self.snaps)
        self.refresh_chain()
        self.refresh_scorecard()
        self.feed.set_signals(self.signals)

    def refresh_tiles(self):
        for k, t in self.tiles.items():
            t.set_history(self.tile_history(k))
        if not self.snaps:
            for t in self.tiles.values():
                t.set("–", "")
            return
        last, first = self.snaps[-1], self.snaps[0]
        c, lv = last["candle"], last["levels"]
        chg = c["close"] - first["candle"]["open"]
        self.tiles["nifty"].set(f"{c['close']:,.2f}", f"{chg:+,.2f} since open")
        self.status_upd.setText(f"Last candle {last['time']:%H:%M}   ")
        b = engine.bias(last["pcr"])
        self.tiles["pcr"].set(f"{last['pcr']:.2f}" if last["pcr"] else "–", f"Bias: {b}",
                              {"bullish": T["up"], "bearish": T["dn"]}.get(b))
        self.tiles["res"].set(f"{lv['R']:,.0f}" if lv["R"] else "–",
                              f"Fresh calls {lv['CE_fresh_near']:,.0f}" if lv["CE_fresh_near"] else "")
        self.tiles["sup"].set(f"{lv['S']:,.0f}" if lv["S"] else "–",
                              f"Fresh puts {lv['PE_fresh_near']:,.0f}" if lv["PE_fresh_near"] else "")
        self.tiles["mp"].set(f"{last['max_pain']:,.0f}", f"ATM straddle {last['straddle']:.0f} pts")
        self.tiles["vix"].set(f"{last['vix']:.2f}" if last["vix"] else "–", "")
        tr = (last.get("trend15") or {})
        self.tiles["trend"].set(tr.get("trend", "n/a").upper(),
                                f"EMA9 {tr['ema']:,.0f}" if tr.get("ema") else "from 10:00",
                                {"up": T["up"], "down": T["dn"]}.get(tr.get("trend")))
        pa = last.get("pa") or {}
        bits = ([pa["event"]] if pa.get("event") else []) + [n for n, _ in pa.get("patterns", [])]
        self.tiles["pa"].set(pa.get("struct", "n/a").upper(), ", ".join(bits) or "no pattern",
                             {"up": T["up"], "down": T["dn"]}.get(pa.get("struct")))

    def refresh_chain(self):
        self.chain.setRowCount(0)
        if not self.snaps:
            return
        last = self.snaps[-1]
        prev = self.snaps[-2] if len(self.snaps) > 1 else None
        st = last["strikes"]
        ks = [k for k in sorted(st) if abs(k - last["atm"]) <= 10 * engine.STRIKE_STEP]
        mx = max([st[k]["ce_oi"] for k in ks] + [st[k]["pe_oi"] for k in ks] + [1])
        self.chain_strikes = ks
        self.chain.setRowCount(len(ks))
        atm_bg = QBrush(QColor(T["atm"]))
        for r, k in enumerate(ks):
            v = st[k]
            d5c = v["ce_oi"] - prev["strikes"][k]["ce_oi"] if prev and k in prev["strikes"] else 0
            d5p = v["pe_oi"] - prev["strikes"][k]["pe_oi"] if prev and k in prev["strikes"] else 0
            cells = [f"{v['ce_chg']:+,.0f}", f"{d5c:+,.0f}", f"{v['ce_oi']:,.0f}", f"{k:,.0f}",
                     f"{v['pe_oi']:,.0f}", f"{d5p:+,.0f}", f"{v['pe_chg']:+,.0f}"]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setTextAlignment((Qt.AlignRight if col < 3 else Qt.AlignCenter if col == 3 else Qt.AlignLeft)
                                    | Qt.AlignVCenter)
                if col == 2:
                    it.setData(Qt.UserRole, v["ce_oi"] / mx)
                    it.setData(Qt.UserRole + 1, "ce")
                if col == 4:
                    it.setData(Qt.UserRole, v["pe_oi"] / mx)
                    it.setData(Qt.UserRole + 1, "pe")
                if col == 3:
                    it.setToolTip("Double-click to see how this strike's call and put OI built up today")
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if col in (1, 5):
                    val = d5c if col == 1 else d5p
                    if val:
                        it.setForeground(QColor(T["ink"] if abs(val) < 20000 else T["warn"]))
                if k == last["atm"]:
                    it.setBackground(atm_bg)
                self.chain.setItem(r, col, it)
        for r, k in enumerate(ks):
            if k == last["atm"]:
                self.chain.scrollToItem(self.chain.item(r, 3), QAbstractItemView.PositionAtCenter)

    def refresh_scorecard(self):
        res = [s["result"] for s in self.signals if s.get("result")]
        lots, size = engine.LOTS, engine.LOT_SIZE
        self.money_note.setText(f"If you had bought {lots} lot{'s' if lots > 1 else ''} ({lots * size} units) of every "
                                f"suggested option at the suggested price and sold at its stop, target or 15:30 – "
                                f"after approx. charges (brokerage ₹{engine.BROKERAGE_PER_ORDER:.0f}/order + taxes)."
                                + ("  Replay prices are estimates." if self.rec and self.rec.mode == "replay" else ""))
        self.trades.setRowCount(0)
        self.trade_rows = []
        self.refresh_history()
        col = lambda v: T["up"] if v > 0 else T["dn"] if v < 0 else None
        if not res:
            for k, t in self.sc_tiles.items():
                t.set("–", "no trade ideas yet" if k == "net" else "")
            self.tiles["pnl"].set("–", "no trade ideas yet")
            self.equity.set_points([])
            self.sc_grades.setText("")
            return
        sc, ms = engine.scorecard(res), engine.money_summary(res)
        self.sc_tiles["net"].set(rs(ms["net"]), f"{ms['n_closed']} closed trade{'s' if ms['n_closed'] != 1 else ''}",
                                 col(ms["net"]))
        self.sc_tiles["open"].set(rs(ms["open"]) if ms["n_open"] else "–",
                                  f"{ms['n_open']} running" if ms["n_open"] else "none running",
                                  col(ms["open"]) if ms["n_open"] else None)
        self.sc_tiles["gradeA"].set(rs(ms["grade_a"]), "only A ideas", col(ms["grade_a"]))
        self.sc_tiles["cap"].set(rs(ms["capital"], sign=False), "per trade (max)")
        self.sc_tiles["won"].set(f"{ms['wins']} / {ms['n_closed']}",
                                 f"win rate {ms['wins'] / ms['n_closed'] * 100:.0f}%"
                                 if ms["n_closed"] else "none closed yet")
        total = ms["net"] + ms["open"]
        self.tiles["pnl"].set(rs(total), f"{lots} lot · {ms['n_closed'] + ms['n_open']} ideas",
                              col(total))
        self.equity.set_points(ms["curve"])
        self.sc_grades.setText("   ·   ".join(
            f"Grade {g}: {n} trades, {w} won, {rs(sum(r['money']['net'] for r in res if r.get('money') and r['status'] != 'OPEN' and r['sig'].get('grade') == g))}"
            for g, (n, w, p) in sc["by_grade"].items()))
        self.trade_rows = [x["sig"] for x in reversed(res)]
        self.trades.setRowCount(len(res))
        for r, x in enumerate(reversed(res)):
            sg, p, m = x["sig"], x["sig"]["plan"], x.get("money")
            net = m["net"] if m else 0
            rc = T["up"] if net > 0 else T["dn"] if net < 0 else T["ink2"]
            cells = [f"{sg['time']:%H:%M}", sg.get("grade", ""), f"BUY {p['strike']:,.0f} {p['opt']}",
                     f"{p['prem']:,.1f}", f"{x['exit_prem']:,.1f}" if x.get("exit_prem") is not None else "–",
                     x["status"] + (f" {x['exit_time']:%H:%M}" if x["exit_time"] and "15:30" not in x["status"] else ""),
                     str(m["qty"]) if m else "–", f"₹{m['charges']:,.0f}" if m else "–", rs(net) if m else "–"]
            tip = (f"{sg['kind']} · Nifty entry {p['entry']:,.0f}, stop {p['stop']:,.0f}, target {p['target']:,.0f} · "
                   f"{x['points']:+,.0f} Nifty pts" + (f" · invested {rs(m['buy'], sign=False)}" if m else ""))
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setToolTip(tip if not (x["status"] == "OPEN" and c in (5, 8)) else "Still running – value if you closed it now")
                if c in (3, 4, 6, 7, 8):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c in (5, 8):
                    it.setForeground(QColor(rc))
                if c == 8:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if c == 2:
                    it.setForeground(QColor(dir_color(sg["direction"])))
                self.trades.setItem(r, c, it)

    def refresh_history(self):
        cur = os.path.basename(self.rec.folder) if self.rec else None
        rows = engine.history_pnl(self.settings["data_dir"], exclude=cur)
        self.history.setRowCount(len(rows) + (1 if rows else 0))
        tot = [0, 0, 0.0]
        for r, (d, n, w, net) in enumerate(rows):
            tot = [tot[0] + n, tot[1] + w, tot[2] + net]
            for c, text in enumerate([d, str(n), str(w), rs(net)]):
                it = QTableWidgetItem(text)
                if c == 3:
                    it.setForeground(QColor(T["up"] if net > 0 else T["dn"] if net < 0 else T["ink2"]))
                self.history.setItem(r, c, it)
        if rows:
            for c, text in enumerate(["Total", str(tot[0]), str(tot[1]), rs(tot[2])]):
                it = QTableWidgetItem(text)
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                if c == 3:
                    it.setForeground(QColor(T["up"] if tot[2] > 0 else T["dn"] if tot[2] < 0 else T["ink2"]))
                self.history.setItem(len(rows), c, it)

def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NiftyOITracker.App")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    f = QFont("Segoe UI" if os.name == "nt" else app.font().family(), 10)
    app.setFont(f)
    # only one tracker at a time: two copies writing the same day's CSV files corrupt them
    lock = QLockFile(os.path.join(app_dir(), ".nifty_tracker.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME, "Nifty OI Tracker is already running.\n\n"
                                "Use the open window (check the taskbar and the system tray) - running two copies "
                                "at once damages the day's recorded data.")
        sys.exit(0)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
