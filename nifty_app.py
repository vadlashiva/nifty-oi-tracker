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
import sys
import threading

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal, QUrl, QRectF, QPointF
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter, QPen,
                           QBrush, QPainterPath, QFontMetrics)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QComboBox,
                               QHBoxLayout, QVBoxLayout, QGridLayout, QSplitter, QTabWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QStyledItemDelegate, QScrollArea, QPlainTextEdit,
                               QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QFileDialog,
                               QDialogButtonBox, QMessageBox, QSystemTrayIcon, QToolButton, QSizePolicy,
                               QAbstractItemView, QStyle)

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
    def __init__(self, label):
        super().__init__()
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(1)
        self.l, self.v, self.s = QLabel(label), QLabel("–"), QLabel("")
        self.l.setObjectName("tl")
        self.v.setObjectName("tv")
        self.s.setObjectName("ts")
        for w in (self.l, self.v, self.s):
            lay.addWidget(w)
        lay.addStretch()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(82)
        self.setMinimumWidth(110)

    def set(self, value, sub="", color=None):
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{color};" if color else "")
        fm = QFontMetrics(self.s.font())
        self.s.setText(fm.elidedText(sub, Qt.ElideRight, max(60, self.width() - 26)))
        self.setToolTip(sub if fm.horizontalAdvance(sub) > self.width() - 26 else "")


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


# --------------------------------------------------------------------------- candle chart
class ChartWidget(QWidget):
    MARKED = ("BREAKDOWN", "BREAKOUT", "REJECTION", "BOUNCE", "REVERSAL UP", "REVERSAL DOWN",
              "OR BREAKOUT", "OR BREAKDOWN")

    def __init__(self):
        super().__init__()
        self.snaps, self.hover = [], None
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.empty_text = "Press Start to begin."

    def set_data(self, snaps):
        self.snaps = snaps
        self.update()

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
        snaps = [s for s in self.snaps if s.get("candle")]
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)
        fm = QFontMetrics(small)
        if not snaps:
            p.setPen(QColor(T["ink2"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return
        candles = [s["candle"] for s in snaps]
        last = snaps[-1]
        lv, pa = last["levels"], last.get("pa") or {}
        levels = [(lv.get("R"), "Max call OI", T["dn"], Qt.DashLine), (lv.get("S"), "Max put OI", T["up"], Qt.DashLine)]
        if lv.get("CE_fresh_near") not in (lv.get("R"),):
            levels.append((lv.get("CE_fresh_near"), "Fresh calls", T["dn"], Qt.DotLine))
        if lv.get("PE_fresh_near") not in (lv.get("S"),):
            levels.append((lv.get("PE_fresh_near"), "Fresh puts", T["up"], Qt.DotLine))
        if pa.get("or_high"):
            levels += [(pa["or_high"], "OR high", T["ink2"], Qt.DashLine), (pa["or_low"], "OR low", T["ink2"], Qt.DashLine)]
        lo, hi = min(c["low"] for c in candles), max(c["high"] for c in candles)
        span = hi - lo
        for v, *_ in levels:
            if v and lo - 0.35 * span - 30 <= v <= hi + 0.35 * span + 30:
                lo, hi = min(lo, v), max(hi, v)
        pad = max(15, (hi - lo) * 0.08)
        lo, hi = lo - pad, hi + pad
        W, H = self.width(), self.height()
        L, R, Tm, B = 62, 128, 14, 28
        n = max(len(candles), 75)
        cw = (W - L - R) / n
        y = lambda v: Tm + (hi - v) / (hi - lo) * (H - Tm - B)

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

        # OI / opening-range levels, labels on the right without overlapping
        used = []
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
            pen = QPen(QColor(col), 1.2, style)
            p.setPen(pen)
            p.drawLine(QPointF(L, y(v)), QPointF(W - R, y(v)))
            ty = y(v)
            while any(abs(ty - u) < 13 for u in used):
                ty += 13
            used.append(ty)
            p.setPen(QColor(col))
            p.drawText(QRectF(W - R + 6, ty - 8, R - 6, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{name} {v:,.0f}")

        # candles
        for i, c in enumerate(candles):
            x = L + (i + 0.5) * cw
            col = QColor(T["up"] if c["close"] >= c["open"] else T["dn"])
            p.setPen(QPen(col, 1.2))
            p.drawLine(QPointF(x, y(c["high"])), QPointF(x, y(c["low"])))
            top, bot = y(max(c["open"], c["close"])), y(min(c["open"], c["close"]))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x - cw * 0.34, top, cw * 0.68, max(1.2, bot - top)), 1, 1)
            if (c["start"].hour, c["start"].minute) == (9, 15) or (c["start"].minute == 0 and c["start"].hour > 9):
                p.setPen(QColor(T["ink2"]))
                p.drawText(QRectF(x - 30, H - B + 6, 60, 16), Qt.AlignCenter, f"{c['start']:%H:%M}")

        # signal markers (with grade for trade ideas)
        idx = {s["time"]: i for i, s in enumerate(snaps)}
        marks = {}
        for s in snaps:
            for sig in s.get("_signals", []):
                if sig["kind"] in self.MARKED and sig["time"] in idx:
                    marks.setdefault(idx[sig["time"]], []).append(sig)
        for i, sigs in marks.items():
            c = candles[i]
            x = L + (i + 0.5) * cw
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
                p.setPen(Qt.NoPen)
                p.setBrush(col if sig.get("plan") else QColor(T["card"]))
                if not sig.get("plan"):
                    p.setPen(QPen(col, 1.4))
                p.drawPath(path)
                if sig.get("plan") and sig.get("grade"):
                    p.setPen(col)
                    p.drawText(QRectF(x + 6, cy - 8, 14, 16), Qt.AlignLeft | Qt.AlignVCenter, sig["grade"])

        # hover crosshair + tooltip
        if self.hover is not None and L <= self.hover.x() <= W - R:
            i = int((self.hover.x() - L) / cw)
            if 0 <= i < len(candles):
                c, s = candles[i], snaps[i]
                x = L + (i + 0.5) * cw
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
                lines = [f"{c['start']:%H:%M}–{c['start'] + dt.timedelta(minutes=5):%H:%M}",
                         f"O {c['open']:,.2f}   H {c['high']:,.2f}",
                         f"L {c['low']:,.2f}   C {c['close']:,.2f}",
                         f"PCR {s['pcr']:.2f}" if s.get("pcr") else "PCR –"]
                for sig in marks.get(i, []) + [x2 for x2 in s.get("_signals", []) if x2["kind"] not in self.MARKED][:3]:
                    lines.append(f"{sig['kind']}" + (f"  · grade {sig['grade']}" if sig.get("plan") else ""))
                bw = max(fm.horizontalAdvance(t) for t in lines) + 20
                bh = len(lines) * 16 + 12
                bx = x + 12 if x + 12 + bw < W - R else x - 12 - bw
                by = max(Tm, min(H - B - bh, hy - bh / 2))
                p.setPen(QPen(QColor(T["line"]), 1))
                p.setBrush(QColor(T["tip"]))
                p.drawRoundedRect(QRectF(bx, by, bw, bh), 8, 8)
                for k, t in enumerate(lines):
                    p.setPen(QColor(T["ink"] if k else T["ink2"]))
                    p.drawText(QRectF(bx + 10, by + 6 + k * 16, bw - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, t)
        p.end()


# --------------------------------------------------------------------------- signal feed
class SignalFeed(QWidget):
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
        self.filter.addItems(["All signals", "Trade ideas only", "Graded A/B only"])
        self.filter.currentIndexChanged.connect(lambda _: self.render())
        head.addWidget(t)
        head.addWidget(self.count)
        head.addStretch()
        head.addWidget(self.filter)
        lay.addLayout(head)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(0, 0, 6, 0)
        self.vbox.setSpacing(8)
        self.vbox.addStretch()
        self.area.setWidget(self.inner)
        lay.addWidget(self.area, 1)
        self.signals = []

    def set_signals(self, signals):
        self.signals = signals
        self.render()

    def render(self):
        pos = self.area.verticalScrollBar().value()
        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(0, 0, 6, 0)
        self.vbox.setSpacing(8)
        self.vbox.addStretch()
        self.area.setWidget(self.inner)                 # the old one is deleted by Qt
        QTimer.singleShot(0, lambda: self.area.verticalScrollBar().setValue(pos))
        f = self.filter.currentIndex()
        sigs = [s for s in self.signals if f == 0 or (s.get("plan") if f == 1 else
                                                      (s.get("plan") and s.get("grade") in ("A", "B")))]
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
        fr = QFrame()
        fr.setObjectName("card")
        col = dir_color(s["direction"])
        fr.setStyleSheet(f"QFrame#card {{ border-left:4px solid {col}; }}")
        lay = QVBoxLayout(fr)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(4)
        badge = ""
        if s.get("grade") and (s.get("plan") or s.get("score") is not None):
            gcol = {"A": T["up"], "B": T["accent"], "C": T["ink2"]}[s["grade"]]
            badge = (f"&nbsp;&nbsp;<span style='color:{gcol}; font-weight:700;'>Grade {s['grade']}"
                     + (f" · {s['score']}" if s.get("score") is not None else "") + "</span>")
        head = QLabel(f"<span style='color:{T['ink2']};'>{s['time']:%H:%M}</span>&nbsp;&nbsp;"
                      f"<b style='color:{col};'>{esc(s['kind'])}</b>{badge}"
                      f"<span style='color:{T['ink2']};'>&nbsp;&nbsp;@ {s['price']:,.2f}</span>")
        lay.addWidget(head)
        det = QLabel(esc(s["text"]))
        det.setWordWrap(True)
        lay.addWidget(det)
        if s.get("trade"):
            tr = QLabel(esc(s["trade"]))
            tr.setWordWrap(True)
            tr.setStyleSheet("font-weight:600;" if s.get("plan") else f"color:{T['ink2']};")
            lay.addWidget(tr)
        if s.get("option"):
            op = QLabel(esc(s["option"]))
            op.setWordWrap(True)
            op.setStyleSheet(f"background:{T['atm']}; border-radius:6px; padding:5px 8px; font-weight:600;")
            lay.addWidget(op)
        r = s.get("result")
        if r:
            rc = T["up"] if r["points"] > 0 else T["dn"] if r["points"] < 0 else T["ink2"]
            when = f" at {r['exit_time']:%H:%M}" if r["exit_time"] else " (running)"
            opt = (f" · option {r['opt_pnl']:+,.1f} Rs/unit{'' if r['opt_real'] else ' est.'}"
                   if r["opt_pnl"] is not None else "")
            res = QLabel(f"<b style='color:{rc};'>{r['status']}</b>{when} · {r['points']:+,.0f} pts{opt}")
            lay.addWidget(res)
        note = QLabel(esc(s["bias_note"]))
        note.setWordWrap(True)
        note.setObjectName("ts")
        lay.addWidget(note)
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
        for key, (w, typ) in self.inputs.items():
            s["tunables"][key] = w.currentText() if typ is str else typ(w.value())
        return s


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    MODES = [("live", "Live (market hours)"), ("replay", "Replay last session"), ("retest", "Re-test a recorded day")]

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker, self.rec, self.next_wait = None, None, None
        self.running_mode, self.had_error = "", False
        self.snaps, self.signals = [], []
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
                                          ("pa", "Price action")]):
            t = Tile(label)
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
        cl.addWidget(self.chart, 1)
        legend = QLabel(f"▲▼ filled = trade idea (letter = grade) · hollow = information only · "
                        f"dashed = OI levels and opening range · hover for details")
        legend.setObjectName("ts")
        cl.addWidget(legend)
        self.tabs.addTab(cw, "Chart")
        self.chain = QTableWidget(0, 7)
        self.chain.setHorizontalHeaderLabels(["Call day chg", "Call 5-min", "Call OI", "Strike", "Put OI",
                                              "Put 5-min", "Put day chg"])
        self.chain.verticalHeader().hide()
        self.chain.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.chain.setSelectionMode(QAbstractItemView.NoSelection)
        self.chain.setShowGrid(False)
        self.chain.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.chain.verticalHeader().setDefaultSectionSize(30)
        self.bar_delegate = BarDelegate(self.chain)
        self.chain.setItemDelegateForColumn(2, self.bar_delegate)
        self.chain.setItemDelegateForColumn(4, self.bar_delegate)
        self.tabs.addTab(self.chain, "Option chain")
        self.tabs.addTab(self.build_scorecard(), "Scorecard")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")
        split.addWidget(self.tabs)
        self.feed = SignalFeed()
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
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)
        g = QHBoxLayout()
        self.sc_tiles = {}
        for key, label in (("ideas", "Trade ideas"), ("won", "Won / closed"), ("pts", "Net Nifty points"),
                           ("opt", "Option P&L (Rs/unit)")):
            t = Tile(label)
            self.sc_tiles[key] = t
            g.addWidget(t)
        v.addLayout(g)
        self.sc_grades = QLabel("")
        self.sc_grades.setObjectName("muted")
        v.addWidget(self.sc_grades)
        self.trades = QTableWidget(0, 10)
        self.trades.setHorizontalHeaderLabels(["Time", "Signal", "Grade", "Entry", "Stop", "Target", "Option",
                                               "Result", "Nifty pts", "Option P&L"])
        self.trades.verticalHeader().hide()
        self.trades.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.trades.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trades.setShowGrid(False)
        hh = self.trades.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)
        self.trades.verticalHeader().setDefaultSectionSize(28)
        v.addWidget(self.trades, 1)
        rule = QLabel("Each idea is followed candle by candle: stop or target on later 5-min candles (stop counted "
                      "first if both fall in one candle); open trades close at 15:30.")
        rule.setObjectName("ts")
        rule.setWordWrap(True)
        v.addWidget(rule)
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
            if left > 0:
                m, s = divmod(int(left), 60)
                h, m = divmod(m, 60)
                eta = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                self.pill_set(f"Live · next update {self.next_wait:%H:%M} (in {eta})", T["up"])
            else:
                self.pill_set("Live · fetching…", T["up"])

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
        self.trades.setRowCount(0)
        if not res:
            self.sc_tiles["ideas"].set("0", "no trade ideas yet")
            for k in ("won", "pts", "opt"):
                self.sc_tiles[k].set("–", "")
            self.sc_grades.setText("")
            return
        sc = engine.scorecard(res)
        self.sc_tiles["ideas"].set(str(sc["trades"]), f"{sc['open']} still running")
        self.sc_tiles["won"].set(f"{sc['wins']} / {sc['closed']}",
                                 f"win rate {sc['win_rate']:.0f}%" if sc["win_rate"] is not None else "")
        self.sc_tiles["pts"].set(f"{sc['points']:+,.0f}", "closed trades",
                                 T["up"] if sc["points"] > 0 else T["dn"] if sc["points"] < 0 else None)
        self.sc_tiles["opt"].set(f"{sc['opt_pnl']:+,.1f}", "× lot size",
                                 T["up"] if sc["opt_pnl"] > 0 else T["dn"] if sc["opt_pnl"] < 0 else None)
        self.sc_grades.setText("   ·   ".join(f"Grade {g}: {n} trades, {w} won, {p:+,.0f} pts"
                                             for g, (n, w, p) in sc["by_grade"].items()))
        self.trades.setRowCount(len(res))
        for r, x in enumerate(reversed(res)):
            sg, p = x["sig"], x["sig"]["plan"]
            rc = T["up"] if x["points"] > 0 else T["dn"] if x["points"] < 0 else T["ink2"]
            cells = [f"{sg['time']:%H:%M}", sg["kind"], sg.get("grade", ""), f"{p['entry']:,.0f}", f"{p['stop']:,.0f}",
                     f"{p['target']:,.0f}", f"{p['strike']:,.0f} {p['opt']} @ {p['prem']:,.1f}",
                     x["status"] + (f" {x['exit_time']:%H:%M}" if x["exit_time"] and "15:30" not in x["status"] else ""),
                     f"{x['points']:+,.0f}", f"{x['opt_pnl']:+,.1f}" if x["opt_pnl"] is not None else "–"]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if col in (7, 8, 9):
                    it.setForeground(QColor(rc))
                if col == 1:
                    it.setForeground(QColor(dir_color(sg["direction"])))
                self.trades.setItem(r, col, it)


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
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
