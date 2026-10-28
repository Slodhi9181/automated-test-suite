# -*- coding: utf-8 -*-
"""
panels.py — Comprehensive instrument control panels.
Each panel exposes the full SCPI feature set of its instrument.
"""

import json
import time
from collections import deque
from functools import partial
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QDoubleSpinBox,
    QSpinBox, QCheckBox, QGroupBox, QTabWidget,
    QTextEdit, QLineEdit, QMessageBox, QSplitter,
    QSizePolicy, QFrame, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QScrollArea, QStackedWidget
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QTextCursor, QFont, QColor

from workers import (
    open_resource, InstrumentWorker,
    use_instrument, use_driver,
    shared_resource, shared_driver,
)
from test_automation import (
    ADDR, GWGPP4323, GWInstekGDM8261A, WT5000Driver, ITechFixed,
    MDO34Driver, _dmm_configure, _dmm_measure,
    ScpiError, check_error, write_checked,
)
from enphase_equipment.multi_meter.agilent   import Agilent34401A
from ui_helpers import CURVE_COLORS, _btn, StatusLabel
from app_logger import get_logger, APP_DIR

_log = get_logger("panels")


# ─────────────────────────────────────────────────────────────────────────────
# Background command runner with busy-state management
# ─────────────────────────────────────────────────────────────────────────────

def _run_panel_cmd(fn, status_lbl, buttons, msg):
    """Disable buttons, show progress, run fn in worker, restore on done.

    A 30-second watchdog re-enables buttons if the worker hangs, so the
    panel never stays permanently locked.
    """
    for b in buttons:
        b.setEnabled(False)
    status_lbl.info(f"⏳  {msg}")

    _fired = [False]   # guard: only the first of (result, watchdog) wins

    w = InstrumentWorker(fn)

    def _cb(val, err, sl=status_lbl, bl=buttons):
        if _fired[0]:
            return
        _fired[0] = True
        for b in bl:
            b.setEnabled(True)
        if err:
            sl.err(err)
        else:
            sl.ok(str(val) if val is not None else "Done")
        sl.clear_after(5000)

    w.result.connect(_cb)

    # Watchdog: if no result after 30 s, unlock the panel and warn
    QTimer.singleShot(
        30_000,
        lambda: _cb(None, "⚠  No response — check instrument connection / address")
    )

    w.start()
    return w


# ─────────────────────────────────────────────────────────────────────────────
# Real-time multi-channel plot panel
# ─────────────────────────────────────────────────────────────────────────────

class PlotPanel(QWidget):
    """
    Real-time line plot supporting N channels.

    measure_fn: callable → dict {channel_label: float}
    All channels are plotted on the same axis (or separate if split=True).
    """

    def __init__(self, channels: list, measure_fn=None,
                 y_label="Value", split=False, parent=None):
        super().__init__(parent)
        self.channels    = channels      # list of str labels
        self.measure_fn  = measure_fn    # set later via set_measure_fn()
        self._data       = {ch: deque(maxlen=2000) for ch in channels}
        self._times      = {ch: deque(maxlen=2000) for ch in channels}
        self._t0         = time.time()
        self._timer      = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._busy       = False
        self._worker     = None
        self._build(y_label, split)

    def set_measure_fn(self, fn):
        self.measure_fn = fn

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self, y_label, split):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._log_lines = 0

        # Value display row (one per channel, or just the single value)
        val_row = QHBoxLayout()
        self._val_labels = {}
        for ch in self.channels:
            box = QGroupBox(ch)
            bl  = QVBoxLayout(box)
            lbl = QLabel("---")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                "font-family:'Courier New'; font-size:20pt;"
                "color:#a6e3a1; font-weight:bold;"
            )
            bl.addWidget(lbl)
            self._val_labels[ch] = lbl
            val_row.addWidget(box)
        lay.addLayout(val_row)

        # Controls
        ctrl = QHBoxLayout()
        self.btn_single = _btn("Measure", "blue")
        self.btn_cont   = QPushButton("▶  Start")
        self.btn_cont.setCheckable(True)
        self.btn_clear  = _btn("Clear")
        self.interval   = QSpinBox()
        self.interval.setRange(100, 30000); self.interval.setValue(1000)
        self.interval.setSuffix(" ms"); self.interval.setFixedWidth(110)
        self.maxpts     = QSpinBox()
        self.maxpts.setRange(10, 5000); self.maxpts.setValue(50)
        self.maxpts.setPrefix("pts:"); self.maxpts.setFixedWidth(100)

        self.btn_single.clicked.connect(self.measure_once)
        self.btn_cont.clicked.connect(self._toggle_cont)
        self.btn_clear.clicked.connect(self.clear)
        self.interval.valueChanged.connect(
            lambda v: self._timer.setInterval(v) if self._timer.isActive() else None)
        self.maxpts.valueChanged.connect(
            lambda v: [d.__init__(maxlen=v) for d in self._data.values()])

        self.btn_clr_log = _btn("Clear Log")
        self.btn_clr_log.clicked.connect(self._clear_log)
        ctrl.addWidget(self.btn_single)
        ctrl.addWidget(self.btn_cont)
        ctrl.addWidget(self.btn_clear)
        ctrl.addStretch()
        ctrl.addWidget(QLabel("Interval:"))
        ctrl.addWidget(self.interval)
        ctrl.addWidget(self.maxpts)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.btn_clr_log)
        lay.addLayout(ctrl)

        # Axis controls
        axis_row = QHBoxLayout()
        self.auto_y = QCheckBox("Auto Y")
        self.auto_y.setChecked(True)
        self.auto_y.toggled.connect(self._on_auto_y)
        self.y_min = QDoubleSpinBox(); self.y_min.setRange(-1e9, 1e9)
        self.y_max = QDoubleSpinBox(); self.y_max.setRange(-1e9, 1e9)
        self.y_max.setValue(10); self.y_min.setEnabled(False); self.y_max.setEnabled(False)
        self.y_min.editingFinished.connect(self._apply_range)
        self.y_max.editingFinished.connect(self._apply_range)
        axis_row.addWidget(self.auto_y)
        axis_row.addSpacing(8)
        axis_row.addWidget(QLabel("Y-Min:")); axis_row.addWidget(self.y_min)
        axis_row.addSpacing(6)
        axis_row.addWidget(QLabel("Y-Max:")); axis_row.addWidget(self.y_max)
        axis_row.addStretch()
        lay.addLayout(axis_row)

        # Plot(s)
        # Diagnostic log
        self._diag_log = QTextEdit()
        self._diag_log.setReadOnly(True)
        self._diag_log.setMinimumHeight(64)
        self._diag_log.setMaximumHeight(120)
        self._diag_log.setStyleSheet(
            "font-family:'Courier New'; font-size:8pt; background:#0d0d14;"
            "color:#a6adc8; border:1px solid #313244;")

        if split and len(self.channels) > 1:
            glw = pg.GraphicsLayoutWidget()
            self._plots  = {}
            self._curves = {}
            for i, ch in enumerate(self.channels):
                p = glw.addPlot(row=i, col=0)
                p.setLabel('left', ch)
                p.showGrid(x=True, y=True, alpha=0.25)
                if i > 0:
                    p.setXLink(self._plots[self.channels[0]])
                self._plots[ch]  = p
                self._curves[ch] = p.plot(pen=pg.mkPen(CURVE_COLORS[i], width=2))
            lay.addWidget(glw)
            self._glw = glw
        else:
            self._pw = pg.PlotWidget(
                axisItems={'bottom': pg.DateAxisItem(orientation='bottom')})
            self._pw.showGrid(x=True, y=True, alpha=0.25)
            self._pw.setLabel('bottom', 'Time')
            self._pw.setLabel('left', y_label)
            self._curves = {}
            legend = self._pw.addLegend(offset=(10, 10))
            for i, ch in enumerate(self.channels):
                self._curves[ch] = self._pw.plot(
                    pen=pg.mkPen(CURVE_COLORS[i % len(CURVE_COLORS)], width=2),
                    name=ch
                )
            lay.addWidget(self._pw)
        lay.addWidget(self._diag_log)

    # ── Measurement ──────────────────────────────────────────────────────────

    def measure_once(self):
        if self._busy or self.measure_fn is None:
            return
        self._busy = True
        self.btn_single.setEnabled(False)
        w = InstrumentWorker(self.measure_fn)
        w.result.connect(self._on_result)
        self._worker = w
        w.start()

    def _tick(self):
        if not self._busy:
            self.measure_once()

    def _log(self, msg: str, error: bool = False):
        ts = __import__('time').strftime("%H:%M:%S")
        colour = "#f38ba8" if error else "#a6e3a1"
        self._diag_log.append(
            f'<span style="color:{colour}">[{ts}] {msg}</span>')
        sb = self._diag_log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._log_lines += 1
        if self._log_lines > 500:
            self._diag_log.clear()
            self._log_lines = 0

    def _clear_log(self):
        self._diag_log.clear()
        self._log_lines = 0

    def _on_result(self, val, err):
        self._busy = False
        self.btn_single.setEnabled(True)
        if err:
            for lbl in self._val_labels.values():
                lbl.setText("ERR")
                lbl.setStyleSheet("font-family:'Courier New'; font-size:20pt;"
                                  "color:#f38ba8; font-weight:bold;")
            self._log(str(err), error=True)
            return   # loop continues — timer still running

        if val is None:
            return   # skipped tick (skip_if_busy)

        # val can be a float (single channel) or dict
        if isinstance(val, (int, float)):
            val = {self.channels[0]: float(val)}
        if not isinstance(val, dict):
            return

        t = time.time()   # absolute Unix timestamp — DateAxisItem formats it
        parts = []
        for ch, v in val.items():
            if ch not in self._data:
                continue
            self._data[ch].append(v)
            self._times[ch].append(t)
            lbl = self._val_labels.get(ch)
            if lbl:
                lbl.setText(f"{v:.6g}")
                lbl.setStyleSheet("font-family:'Courier New'; font-size:20pt;"
                                  "color:#a6e3a1; font-weight:bold;")
            self._curves[ch].setData(list(self._times[ch]), list(self._data[ch]))
            parts.append(f"{ch}={v:.6g}")
        if parts:
            self._log("  ".join(parts))

        if self.auto_y.isChecked():
            if hasattr(self, '_pw'):
                self._pw.enableAutoRange(axis='y')

    def _toggle_cont(self, checked):
        if checked:
            self._t0 = time.time()
            self._timer.start(self.interval.value())
            self.btn_cont.setText("⏹  Stop")
            self.btn_cont.setStyleSheet(
                "background:#f38ba8; color:#1e1e2e; font-weight:bold;")
        else:
            self._timer.stop()
            self.btn_cont.setText("▶  Start")
            self.btn_cont.setStyleSheet("")

    def clear(self):
        self._t0 = time.time()
        for ch in self.channels:
            self._data[ch].clear()
            self._times[ch].clear()
            self._curves[ch].setData([], [])

    def _on_auto_y(self, checked):
        self.y_min.setEnabled(not checked)
        self.y_max.setEnabled(not checked)
        if checked and hasattr(self, '_pw'):
            self._pw.enableAutoRange(axis='y')
        elif not checked:
            self._apply_range()

    def _apply_range(self):
        if not self.auto_y.isChecked() and hasattr(self, '_pw'):
            self._pw.setYRange(self.y_min.value(), self.y_max.value())


# ─────────────────────────────────────────────────────────────────────────────
# SCPI Terminal  (universal raw command console)
# ─────────────────────────────────────────────────────────────────────────────

class ScpiTerminal(QWidget):
    def __init__(self, default_key='gdm', parent=None):
        super().__init__(parent)
        self._key = default_key
        self._history = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.inst_sel = QComboBox()
        for k, lbl in [('itech','ITECH IT6005C'),('gpp','GPP-4323'),
                        ('gdm','GDM-8261A'),('dmm','Keysight 34461A'),
                        ('mdo','MDO34'),('wt5000','Yokogawa WT5000')]:
            self.inst_sel.addItem(lbl, k)
        self.inst_sel.setCurrentText("GDM-8261A")
        top.addWidget(QLabel("Instrument:")); top.addWidget(self.inst_sel)
        top.addStretch()
        lay.addLayout(top)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(180)
        lay.addWidget(self.output)

        cmd_row = QHBoxLayout()
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("SCPI command  (e.g.  *IDN?  or  CONF:VOLT:DC)")
        self.cmd_input.returnPressed.connect(self._send_query)
        btn_q = _btn("Query (?)", "blue")
        btn_w = _btn("Write")
        btn_clr = _btn("Clear")
        btn_q.clicked.connect(self._send_query)
        btn_w.clicked.connect(self._send_write)
        btn_clr.clicked.connect(self.output.clear)
        cmd_row.addWidget(self.cmd_input)
        cmd_row.addWidget(btn_q)
        cmd_row.addWidget(btn_w)
        cmd_row.addWidget(btn_clr)
        lay.addLayout(cmd_row)

    def _instrument_key(self):
        return self.inst_sel.currentData()

    def _send_query(self):
        cmd = self.cmd_input.text().strip()
        if not cmd:
            return
        key = self._instrument_key()
        def fn():
            rm, res = open_resource(key, timeout=5000)
            resp = res.query(cmd).strip()
            res.close(); rm.close()
            return resp
        w = InstrumentWorker(fn)
        w.result.connect(lambda v, e: self._show(cmd, v, e, query=True))
        w.start(); self._w = w

    def _send_write(self):
        cmd = self.cmd_input.text().strip()
        if not cmd:
            return
        key = self._instrument_key()
        def fn():
            rm, res = open_resource(key, timeout=5000)
            res.write(cmd)
            res.close(); rm.close()
            return "OK"
        w = InstrumentWorker(fn)
        w.result.connect(lambda v, e: self._show(cmd, v, e, query=False))
        w.start(); self._w = w

    def _show(self, cmd, val, err, query):
        key = self._instrument_key()
        arrow = "→" if query else "↗"
        if err:
            self.output.append(f'<span style="color:#f38ba8">[{key}] {arrow} {cmd}<br>ERROR: {err}</span>')
        else:
            self.output.append(f'<span style="color:#89b4fa">[{key}] {arrow} {cmd}</span>')
            if query and val:
                self.output.append(f'<span style="color:#a6e3a1">← {val}</span>')
        self.output.moveCursor(QTextCursor.MoveOperation.End)


# ─────────────────────────────────────────────────────────────────────────────
# ITECH IT6005C-80-150  (Bi-directional DC Power Supply)
# ─────────────────────────────────────────────────────────────────────────────

class ItechPanel(QWidget):
    """
    Full control of ITECH IT6005C:
    • Output voltage / current / mode (SOURCE or SINK)
    • Output ON / OFF
    • OVP and OCP protection levels
    • Measure actual V, I, P (computed) — all plottable
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn = shared_driver(
            'itech', lambda: ITechFixed(ADDR['itech']))
        lay  = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_control(), "Control")
        tabs.addTab(self._build_plot(),    "Plot  V / I / P")
        lay.addWidget(tabs)

    # ── Control tab ──────────────────────────────────────────────────────────

    def _build_control(self):
        w = QWidget(); root = QVBoxLayout(w)

        # ── Output settings ──────────────────────────────────────
        out = QGroupBox("Output Settings")
        g   = QGridLayout(out)

        g.addWidget(QLabel("Voltage (V):"), 0, 0)
        self.volt = QDoubleSpinBox(); self.volt.setRange(0, 80); self.volt.setValue(12)
        self.volt.setSingleStep(0.1); self.volt.setDecimals(3)
        g.addWidget(self.volt, 0, 1)

        g.addWidget(QLabel("Current (A):"), 1, 0)
        self.curr = QDoubleSpinBox(); self.curr.setRange(0, 150); self.curr.setValue(5)
        self.curr.setSingleStep(0.1); self.curr.setDecimals(3)
        g.addWidget(self.curr, 1, 1)

        g.addWidget(QLabel("Mode:"), 2, 0)
        self.mode = QComboBox(); self.mode.addItems(["SOURCE", "SINK"])
        g.addWidget(self.mode, 2, 1)

        b_set = _btn("Apply Settings", "blue")
        b_on  = _btn("Output ON",  "green")
        b_off = _btn("Output OFF", "red")
        b_idn = _btn("Read IDN")
        b_rst = _btn("Reset (*RST)")
        self._ctrl_btns = [b_set, b_on, b_off, b_idn, b_rst]

        b_set.clicked.connect(self._apply)
        b_on.clicked.connect(lambda: self._ctl(lambda d: d.on()  or "Output ON",  "Output ON…"))
        b_off.clicked.connect(lambda: self._ctl(lambda d: d.off() or "Output OFF", "Output OFF…"))
        b_idn.clicked.connect(lambda: self._ctl(lambda d: d.id,   "Reading IDN…"))
        b_rst.clicked.connect(lambda: self._ctl(lambda d: (d.reset() or "Reset OK"), "Resetting…"))

        g.addWidget(b_set, 3, 0, 1, 2)
        g.addWidget(b_on,  4, 0); g.addWidget(b_off,  4, 1)
        g.addWidget(b_idn, 5, 0); g.addWidget(b_rst,  5, 1)
        root.addWidget(out)

        # ── Protection settings ──────────────────────────────────
        prot = QGroupBox("Protection (OVP / OCP)")
        pg   = QGridLayout(prot)
        pg.addWidget(QLabel("OVP Level (V):"), 0, 0)
        self.ovp_level = QDoubleSpinBox(); self.ovp_level.setRange(0, 85); self.ovp_level.setValue(82)
        pg.addWidget(self.ovp_level, 0, 1)
        self.ovp_en = QCheckBox("Enable OVP"); self.ovp_en.setChecked(False)
        pg.addWidget(self.ovp_en, 0, 2)

        pg.addWidget(QLabel("OCP Level (A):"), 1, 0)
        self.ocp_level = QDoubleSpinBox(); self.ocp_level.setRange(0, 155); self.ocp_level.setValue(155)
        pg.addWidget(self.ocp_level, 1, 1)
        self.ocp_en = QCheckBox("Enable OCP"); self.ocp_en.setChecked(False)
        pg.addWidget(self.ocp_en, 1, 2)

        b_prot = _btn("Apply Protection", "orange")
        b_prot.clicked.connect(self._apply_protection)
        pg.addWidget(b_prot, 2, 0, 1, 3)
        root.addWidget(prot)

        self.status = StatusLabel(""); root.addWidget(self.status)
        root.addStretch()
        return w

    def _build_plot(self):
        self._plot = PlotPanel(
            channels=["Voltage (V)", "Current (A)", "Power (W)"],
            y_label="", split=True
        )
        self._plot.set_measure_fn(self._read_all)
        return self._plot

    # ── Workers ──────────────────────────────────────────────────────────────

    def _ctl(self, fn, msg="Sending command…"):
        conn = self._conn
        def job():
            result = conn.run(fn)
            return result[1] if isinstance(result, tuple) else (result or "Done")
        self._w = _run_panel_cmd(job, self.status, self._ctrl_btns, msg)

    def _apply(self):
        v = self.volt.value(); c = self.curr.value(); m = self.mode.currentText()
        conn = self._conn
        def job():
            def _fn(drv):
                drv.set_volts(v)
                signed = c if m.upper() == "SOURCE" else -abs(c)
                drv.set_amps(signed)
                v_rb = float(drv.ask("VOLT?").split()[-1])
                c_rb = float(drv.ask("CURR?").split()[-1])
                return (f"Confirmed: {v_rb:.3f} V / {abs(c_rb):.3f} A ({m})"
                        f" — click Output ON to enable")
            return conn.run(_fn)
        self._w = _run_panel_cmd(job, self.status, self._ctrl_btns,
                                 f"Applying {v:.3f} V / {c:.3f} A ({m})…")

    def _apply_protection(self):
        ovp_v = self.ovp_level.value(); ovp_e = self.ovp_en.isChecked()
        ocp_a = self.ocp_level.value(); ocp_e = self.ocp_en.isChecked()
        conn = self._conn
        def job():
            def _fn(drv):
                drv.write(f":VOLT:PROT {ovp_v:.3f}")
                drv.write(f":VOLT:PROT:STAT {'ON' if ovp_e else 'OFF'}")
                drv.write(f":CURR:PROT {ocp_a:.3f}")
                drv.write(f":CURR:PROT:STAT {'ON' if ocp_e else 'OFF'}")
                return (f"OVP={ovp_v:.3f}V ({'ON' if ovp_e else 'OFF'}),"
                        f" OCP={ocp_a:.3f}A ({'ON' if ocp_e else 'OFF'})")
            return conn.run(_fn)
        self._w = _run_panel_cmd(job, self.status, self._ctrl_btns, "Applying protection settings…")

    def _read_all(self):
        def fn(drv):
            data = drv.measure()   # returns FloatDict {dc_volts, dc_amps}
            v = float(data['dc_volts'])
            i = float(data['dc_amps'])
            return {"Voltage (V)": v, "Current (A)": i, "Power (W)": v * i}
        return self._conn.run(fn, skip_if_busy=True)



# ─────────────────────────────────────────────────────────────────────────────
# GW Instek GPP-4323  (4-Channel DC PSU)
# ─────────────────────────────────────────────────────────────────────────────

class GppPanel(QWidget):
    """
    Full GPP-4323 control panel (GPP-Series User Manual Rev G, 2024):
    • CH 1–4 tabs    : V/I setpoints, OVP/OCP, output ON/OFF, live V/I/P + CC indicator
    • Overview       : all-channel grid — set and measure all 4 channels at once
    • Protection     : OVP/OCP for all 4 channels in one compact view
    • System         : tracking mode, ALL ON/OFF, beeper, display, save/recall, remote
    • Plot           : real-time V/I/P trends for all channels
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn = shared_resource('gpp')
        self._w    = None   # keeps last worker alive
        lay  = QVBoxLayout(self)
        tabs = QTabWidget()

        self._ch_panels = {}
        for ch in range(1, 5):
            p = self._make_ch_panel(ch)
            self._ch_panels[ch] = p
            tabs.addTab(p, f"CH {ch}")

        tabs.addTab(self._build_overview_tab(),   "Overview")
        tabs.addTab(self._build_protection_tab(), "Protection")
        tabs.addTab(self._build_system_tab(),     "System")
        tabs.addTab(self._build_all_plot(),       "Plot")
        lay.addWidget(tabs)

    # ── Shared runner ────────────────────────────────────────────────────────

    def _gpp_run(self, fn, msg, status, buttons):
        """Run fn(raw_resource) in background; fn wraps result in GWGPP4323."""
        conn = self._conn
        def job():
            r = conn.run(fn)
            if isinstance(r, tuple): return r[1]
            return str(r) if r is not None else "Done"
        self._w = _run_panel_cmd(job, status, buttons, msg)

    # ── Per-channel tab ──────────────────────────────────────────────────────

    def _make_ch_panel(self, ch):
        w   = QWidget()
        lay = QVBoxLayout(w)
        has_load = ch in (1, 2)

        # ── Output Control (unified: source + load mode) ──────────────────
        ctrl = QGroupBox(f"Channel {ch} — Output Control"
                         + (" (Source / Load)" if has_load else ""))
        g    = QGridLayout(ctrl)

        # Mode selector (CH1/CH2 only)
        mode_cb = None
        if has_load:
            g.addWidget(QLabel("Mode:"), 0, 0)
            mode_cb = QComboBox()
            mode_cb.addItems(["Source", "Load CV", "Load CC", "Load CR"])
            mode_cb.setFixedWidth(140)
            g.addWidget(mode_cb, 0, 1)

        # Labels that update dynamically based on mode
        v_label = QLabel("Voltage (V):")
        i_label = QLabel("Current (A):")
        r_label = QLabel("Resistance (\u03a9):")

        row_v = 1 if has_load else 0
        g.addWidget(v_label, row_v, 0)
        v_sp = QDoubleSpinBox(); v_sp.setRange(0, 32.0); v_sp.setValue(5.0)
        v_sp.setSingleStep(0.1); v_sp.setDecimals(3)
        g.addWidget(v_sp, row_v, 1)

        g.addWidget(i_label, row_v + 1, 0)
        c_sp = QDoubleSpinBox(); c_sp.setRange(0, 3.2); c_sp.setValue(1.0)
        c_sp.setSingleStep(0.01); c_sp.setDecimals(3)
        g.addWidget(c_sp, row_v + 1, 1)

        # Resistance field (only for CH1/CH2 Load CR)
        r_sp = None
        if has_load:
            g.addWidget(r_label, row_v + 2, 0)
            r_sp = QDoubleSpinBox(); r_sp.setRange(0, 10000.0); r_sp.setValue(10.0)
            r_sp.setDecimals(2); r_sp.setSingleStep(1.0)
            g.addWidget(r_sp, row_v + 2, 1)
            r_label.setVisible(False)
            r_sp.setVisible(False)

        # CC indicator
        cc_lbl = QLabel("CV")
        cc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cc_lbl.setFixedWidth(44)
        cc_lbl.setStyleSheet(
            "font-family:'Courier New'; font-weight:bold; font-size:10pt;"
            "color:#a6e3a1; background:#1a3d1a; border:1px solid #45475a;"
            "border-radius:4px; padding:2px 6px;")
        g.addWidget(cc_lbl, row_v, 2, 2, 1)

        # Buttons
        btn_row_start = (row_v + 3) if has_load else (row_v + 2)
        b_set = _btn("Apply", "blue")
        b_on  = _btn(f"CH{ch} ON",  "green")
        b_off = _btn(f"CH{ch} OFF", "red")
        b_meas = _btn("Measure V/I/P")
        b_query = _btn("Query Mode") if has_load else _btn("Read IDN")
        status = StatusLabel("")
        btns   = [b_set, b_on, b_off, b_meas, b_query]

        g.addWidget(b_set,   btn_row_start, 0, 1, 2)
        g.addWidget(b_on,    btn_row_start + 1, 0)
        g.addWidget(b_off,   btn_row_start + 1, 1)
        g.addWidget(b_meas,  btn_row_start + 2, 0, 1, 2)
        g.addWidget(b_query, btn_row_start + 3, 0, 1, 2)
        g.addWidget(status,  btn_row_start + 4, 0, 1, 3)
        lay.addWidget(ctrl)

        # ── Mode-change handler (CH1/CH2) ─────────────────────────────────
        if has_load:
            def _on_mode_changed(idx, vl=v_label, il=i_label, rl=r_label,
                                 vs=v_sp, cs=c_sp, rs=r_sp):
                if idx == 0:     # Source
                    vl.setText("Voltage (V):")
                    il.setText("Current Limit (A):")
                    vs.setVisible(True); vl.setVisible(True)
                    cs.setVisible(True); il.setVisible(True)
                    rl.setVisible(False); rs.setVisible(False)
                elif idx == 1:   # Load CV
                    vl.setText("CV Voltage (V):")
                    il.setText("Current Limit (A):")
                    vs.setVisible(True); vl.setVisible(True)
                    cs.setVisible(True); il.setVisible(True)
                    rl.setVisible(False); rs.setVisible(False)
                elif idx == 2:   # Load CC
                    vl.setText("Voltage Limit (V):")
                    il.setText("CC Current (A):")
                    vs.setVisible(True); vl.setVisible(True)
                    cs.setVisible(True); il.setVisible(True)
                    rl.setVisible(False); rs.setVisible(False)
                elif idx == 3:   # Load CR
                    vl.setText("Voltage Limit (V):")
                    il.setText("Current Limit (A):")
                    rl.setText("CR Resistance (\u03a9):")
                    vs.setVisible(True); vl.setVisible(True)
                    cs.setVisible(True); il.setVisible(True)
                    rl.setVisible(True); rs.setVisible(True)
            mode_cb.currentIndexChanged.connect(_on_mode_changed)
            _on_mode_changed(0)

        # ── Measured readback ─────────────────────────────────────────────
        meas_grp = QGroupBox("Measured")
        mg = QGridLayout(meas_grp)
        v_lbl = QLabel("---"); i_lbl = QLabel("---"); p_lbl = QLabel("---")
        _LS = ("font-family:'Courier New'; font-size:13pt; font-weight:bold;"
               "background:#181825; border:1px solid #45475a;"
               "border-radius:4px; padding:4px 8px;")
        v_lbl.setStyleSheet(_LS + "color:#a6e3a1;")
        i_lbl.setStyleSheet(_LS + "color:#89b4fa;")
        p_lbl.setStyleSheet(_LS + "color:#fab387;")
        for col, (lbl_text, lbl_w) in enumerate(
                [("V:", v_lbl), ("A:", i_lbl), ("W:", p_lbl)]):
            mg.addWidget(QLabel(lbl_text), 0, col * 2)
            mg.addWidget(lbl_w,            0, col * 2 + 1)
        lay.addWidget(meas_grp)

        # ── OVP / OCP ─────────────────────────────────────────────────────
        prot = QGroupBox("Protection")
        pg   = QGridLayout(prot)
        pg.addWidget(QLabel("OVP (V):"), 0, 0)
        ovp_sp = QDoubleSpinBox(); ovp_sp.setRange(0, 35.0)
        ovp_sp.setValue(33.6); ovp_sp.setDecimals(3); ovp_sp.setSingleStep(0.1)
        pg.addWidget(ovp_sp, 0, 1)
        ovp_en = QCheckBox("Enable OVP"); ovp_en.setChecked(False)
        pg.addWidget(ovp_en, 0, 2)
        pg.addWidget(QLabel("OCP (A):"), 1, 0)
        ocp_sp = QDoubleSpinBox(); ocp_sp.setRange(0, 3.5)
        ocp_sp.setValue(3.36); ocp_sp.setDecimals(3); ocp_sp.setSingleStep(0.01)
        pg.addWidget(ocp_sp, 1, 1)
        ocp_en = QCheckBox("Enable OCP"); ocp_en.setChecked(False)
        pg.addWidget(ocp_en, 1, 2)
        b_prot  = _btn("Apply Protection", "orange")
        prot_st = StatusLabel("")
        pg.addWidget(b_prot, 2, 0, 1, 3); pg.addWidget(prot_st, 3, 0, 1, 3)
        lay.addWidget(prot)

        # ── Wire button actions ───────────────────────────────────────────
        def _apply_unified(ch=ch, vs=v_sp, cs=c_sp, rs=r_sp,
                           mcb=mode_cb, st=status, bl=btns):
            v = vs.value(); c = cs.value()
            mode_idx = mcb.currentIndex() if mcb else 0
            mode_name = ["Source", "Load CV", "Load CC", "Load CR"][mode_idx]
            r_val = rs.value() if rs else 0

            def fn(res):
                d = GWGPP4323(res)
                if mode_idx == 0:
                    # Source mode: clear load, set V/I
                    if ch in (1, 2):
                        d.set_load_mode(ch, 'OFF')
                    d.set_channel(ch, v, c)
                    return f"CH{ch} Source: V={v:.3f}  I={c:.3f}"
                elif mode_idx == 1:
                    # Load CV: set voltage setpoint, current limit
                    d.set_load_mode(ch, 'CV')
                    d.set_voltage(ch, v)
                    d.set_current(ch, c)
                    return f"CH{ch} Load CV: V={v:.3f}  I_lim={c:.3f}"
                elif mode_idx == 2:
                    # Load CC: set current setpoint, voltage limit
                    d.set_load_mode(ch, 'CC')
                    d.set_voltage(ch, v)
                    d.set_current(ch, c)
                    return f"CH{ch} Load CC: I={c:.3f}  V_lim={v:.3f}"
                elif mode_idx == 3:
                    # Load CR: set resistance, V/I limits
                    d.set_load_mode(ch, 'CR')
                    d.set_load_resistance(ch, r_val)
                    d.set_voltage(ch, v)
                    d.set_current(ch, c)
                    return f"CH{ch} Load CR: R={r_val:.2f}\u03a9  V_lim={v:.3f}  I_lim={c:.3f}"
            self._gpp_run(fn, f"CH{ch} {mode_name}…", st, bl)

        def _measure_ch(ch=ch, vl=v_lbl, il=i_lbl, pl=p_lbl,
                        ccl=cc_lbl, st=status, bl=btns):
            conn = self._conn
            def job():
                def fn(res):
                    d = GWGPP4323(res)
                    m  = d.measure_all(ch)
                    cc = d.get_cc_state(ch)
                    mode = d.get_mode(ch) if ch in (1, 2) else "CV"
                    return (m, cc, mode)
                return conn.run(fn)
            def done(val, err):
                for b in bl: b.setEnabled(True)
                if err:   st.err(err); return
                if val:
                    m, cc, mode = val
                    vl.setText(f"{m['V']:.4f} V")
                    il.setText(f"{m['I']:.4f} A")
                    pl.setText(f"{m['P']:.4f} W")
                    ccl.setText(mode if mode else ("CC" if cc else "CV"))
                    is_load = mode and mode.upper() not in ('CV', 'CC', '')
                    ccl.setStyleSheet(
                        "font-family:'Courier New'; font-weight:bold; font-size:10pt;"
                        + ("color:#f38ba8; background:#3d1a1a;" if cc
                           else "color:#a6e3a1; background:#1a3d1a;")
                        + " border:1px solid #45475a; border-radius:4px; padding:2px 6px;")
                    st.ok(f"CH{ch} measured ({mode})"); st.clear_after(4000)
            for b in bl: b.setEnabled(False)
            st.info("\u23f3  Measuring…")
            mw = InstrumentWorker(job); mw.result.connect(done)
            mw.start(); self._w = mw

        def _query_mode_or_idn(ch=ch, mcb=mode_cb, st=status, bl=btns):
            if has_load:
                self._gpp_run(
                    lambda res: f"CH{ch} mode: {GWGPP4323(res).get_mode(ch)}",
                    f"Query CH{ch} mode…", st, bl)
            else:
                self._gpp_run(
                    lambda res: GWGPP4323(res).identify(), "IDN…", st, bl)

        def _apply_prot(ch=ch, ovps=ovp_sp, ocps=ocp_sp,
                        ove=ovp_en, oce=ocp_en, st=prot_st, bp=b_prot):
            ov, oc  = ovps.value(), ocps.value()
            oven, ocen = ove.isChecked(), oce.isChecked()
            def fn(res):
                d = GWGPP4323(res)
                d.set_ovp(ch, ov);  d.enable_ovp(ch, oven)
                d.set_ocp(ch, oc);  d.enable_ocp(ch, ocen)
                return (f"CH{ch} OVP={ov:.3f}V({'ON' if oven else 'OFF'}),"
                        f" OCP={oc:.3f}A({'ON' if ocen else 'OFF'})")
            self._gpp_run(fn, f"CH{ch} protection…", st, [bp])

        b_set.clicked.connect(lambda: _apply_unified())
        b_on.clicked.connect(lambda _chk, _ch=ch, st=status, bl=btns: self._gpp_run(
            lambda res: GWGPP4323(res).output_on(_ch) or f"CH{_ch} ON",
            f"CH{ch} ON…", st, bl))
        b_off.clicked.connect(lambda _chk, _ch=ch, st=status, bl=btns: self._gpp_run(
            lambda res: GWGPP4323(res).output_off(_ch) or f"CH{_ch} OFF",
            f"CH{ch} OFF…", st, bl))
        b_meas.clicked.connect(lambda: _measure_ch())
        b_query.clicked.connect(lambda: _query_mode_or_idn())
        b_prot.clicked.connect(lambda: _apply_prot())
        lay.addStretch()

        w._v_sp = v_sp;   w._c_sp   = c_sp
        w._mode_cb = mode_cb
        w._r_sp = r_sp
        w._ovp_sp = ovp_sp; w._ocp_sp = ocp_sp
        w._ovp_en = ovp_en; w._ocp_en = ocp_en
        return w

    # ── Overview tab ─────────────────────────────────────────────────────────

    def _build_overview_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        btn_row = QHBoxLayout()
        b_all_on   = _btn("ALL ON",      "green"); b_all_on.setMinimumHeight(34)
        b_all_off  = _btn("ALL OFF",     "red");   b_all_off.setMinimumHeight(34)
        b_meas_all = _btn("Measure All", "blue");  b_meas_all.setMinimumHeight(34)
        b_idn      = _btn("IDN")
        for b in [b_all_on, b_all_off, b_meas_all, b_idn]: btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        g = QGridLayout()
        for col, hdr in enumerate(["CH", "Mode", "Out", "V Set", "I Set",
                                    "Apply", "V Meas", "I Meas", "P Meas", "State"]):
            lb = QLabel(hdr); lb.setStyleSheet("font-weight:bold;"); g.addWidget(lb, 0, col)

        self._ov_mode  = {}
        self._ov_vset  = {}; self._ov_iset   = {}
        self._ov_vmeas = {}; self._ov_imeas  = {}; self._ov_pmeas = {}
        self._ov_cc    = {}; self._ov_out    = {}

        _LS_V = ("font-family:'Courier New'; font-size:11pt; font-weight:bold;"
                 "color:#a6e3a1; background:#181825; border:1px solid #45475a;"
                 "border-radius:3px; padding:2px 6px;")
        _LS_I = _LS_V.replace("#a6e3a1", "#89b4fa")
        _LS_P = _LS_V.replace("#a6e3a1", "#fab387")

        for row, ch in enumerate(range(1, 5), start=1):
            g.addWidget(QLabel(f"CH{ch}"), row, 0)

            # Mode combo for CH1/CH2; fixed label for CH3/CH4
            if ch in (1, 2):
                mode_cb = QComboBox()
                mode_cb.addItems(["Source", "Load CV", "Load CC", "Load CR"])
                mode_cb.setFixedWidth(100)
                mode_cb.currentIndexChanged.connect(
                    lambda idx, _ch=ch: self._on_ov_mode_changed(_ch, idx))
                self._ov_mode[ch] = mode_cb
                g.addWidget(mode_cb, row, 1)
            else:
                src_lbl = QLabel("Source")
                src_lbl.setStyleSheet("color:#a6adc8; font-style:italic;")
                g.addWidget(src_lbl, row, 1)

            out_cb = QCheckBox(); out_cb.setChecked(True)
            self._ov_out[ch] = out_cb; g.addWidget(out_cb, row, 2)

            vs = QDoubleSpinBox(); vs.setRange(0, 32.0); vs.setValue(5.0)
            vs.setDecimals(3); vs.setSingleStep(0.1)
            self._ov_vset[ch] = vs; g.addWidget(vs, row, 3)

            cs = QDoubleSpinBox(); cs.setRange(0, 3.2); cs.setValue(1.0)
            cs.setDecimals(3); cs.setSingleStep(0.01)
            self._ov_iset[ch] = cs; g.addWidget(cs, row, 4)

            b_ch = _btn(f"Apply CH{ch}", "blue")
            def _apply_ch(ch=ch, b=b_ch):
                v  = self._ov_vset[ch].value()
                c  = self._ov_iset[ch].value()
                en = self._ov_out[ch].isChecked()
                mode_str = 'source'
                if ch in (1, 2):
                    mode_str = ['source', 'cv', 'cc', 'cr'][
                        self._ov_mode[ch].currentIndex()]
                _mode_map = {'cv': 'CV', 'cc': 'CC', 'cr': 'CR'}
                scpi_mode = _mode_map.get(mode_str)
                def fn(res, _v=v, _c=c, _en=en, _sm=scpi_mode, _ch=ch):
                    d = GWGPP4323(res)
                    if _sm:
                        d.set_load_mode(_ch, _sm)
                        if _sm == 'CR':
                            d.set_load_resistance(_ch, _v)
                        else:
                            d.set_voltage(_ch, _v)
                        d.set_current(_ch, _c)
                    else:
                        if _ch in (1, 2):
                            d.set_load_mode(_ch, 'OFF')
                        d.set_channel(_ch, _v, _c)
                    d.enable_channel(_ch, _en)
                    lbl = f"Load {_sm}" if _sm else "Source"
                    return f"CH{_ch}: {lbl} V={_v:.3f} I={_c:.3f} ({'ON' if _en else 'OFF'})"
                self._gpp_run(fn, f"CH{ch}\u2026", self.ov_status, [b])
            b_ch.clicked.connect(_apply_ch)
            g.addWidget(b_ch, row, 5)

            for col, (store, sty) in enumerate(
                    [(self._ov_vmeas, _LS_V),
                     (self._ov_imeas, _LS_I),
                     (self._ov_pmeas, _LS_P)], start=6):
                lbl = QLabel("---"); lbl.setStyleSheet(sty)
                store[ch] = lbl; g.addWidget(lbl, row, col)

            cc_lbl = QLabel("CV")
            cc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cc_lbl.setStyleSheet(
                "font-family:'Courier New'; font-weight:bold; font-size:10pt;"
                "color:#a6e3a1; background:#1a3d1a; border:1px solid #45475a;"
                "border-radius:3px; padding:2px 6px;")
            self._ov_cc[ch] = cc_lbl; g.addWidget(cc_lbl, row, 9)

        lay.addLayout(g)
        self.ov_status = StatusLabel(""); lay.addWidget(self.ov_status)
        lay.addStretch()

        def _all_on():
            def fn(res): GWGPP4323(res).all_outputs_on(); return "All channels ON"
            self._gpp_run(fn, "All ON\u2026", self.ov_status, [b_all_on])

        def _all_off():
            def fn(res): GWGPP4323(res).all_outputs_off(); return "All channels OFF"
            self._gpp_run(fn, "All OFF\u2026", self.ov_status, [b_all_off])

        def _meas_all():
            conn = self._conn
            def job():
                def fn(res):
                    d = GWGPP4323(res)
                    data = {}
                    for ch in range(1, 5):
                        m = d.measure_all(ch)
                        m['cc'] = d.get_cc_state(ch)
                        data[ch] = m
                    return data
                return conn.run(fn)
            def done(val, err):
                b_meas_all.setEnabled(True)
                if err: self.ov_status.err(err); return
                if val:
                    for ch, m in val.items():
                        self._ov_vmeas[ch].setText(f"{m['V']:.4f} V")
                        self._ov_imeas[ch].setText(f"{m['I']:.4f} A")
                        self._ov_pmeas[ch].setText(f"{m['P']:.4f} W")
                        cc = m.get('cc', False)
                        self._ov_cc[ch].setText("CC" if cc else "CV")
                        self._ov_cc[ch].setStyleSheet(
                            "font-family:'Courier New'; font-weight:bold; font-size:10pt;"
                            + ("color:#f38ba8; background:#3d1a1a;" if cc
                               else "color:#a6e3a1; background:#1a3d1a;")
                            + " border:1px solid #45475a; border-radius:3px; padding:2px 6px;")
                    self.ov_status.ok("All channels measured")
                    self.ov_status.clear_after(5000)
            b_meas_all.setEnabled(False)
            self.ov_status.info("\u23f3  Measuring all\u2026")
            mw = InstrumentWorker(job); mw.result.connect(done)
            mw.start(); self._w = mw

        b_all_on.clicked.connect(_all_on)
        b_all_off.clicked.connect(_all_off)
        b_meas_all.clicked.connect(_meas_all)
        b_idn.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).identify(), "IDN\u2026", self.ov_status, [b_idn]))
        return w

    def _on_ov_mode_changed(self, ch, idx):
        """Update V spinbox suffix/range in overview when mode changes."""
        vs = self._ov_vset[ch]
        if idx == 3:  # CR
            vs.setSuffix(" \u03a9"); vs.setRange(0.1, 10000); vs.setDecimals(2)
        else:
            vs.setSuffix(" V"); vs.setRange(0, 32.0); vs.setDecimals(3)

    # ── Protection tab ───────────────────────────────────────────────────────

    def _build_protection_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)
        grp = QGroupBox("Over-Voltage / Over-Current Protection — All Channels")
        g   = QGridLayout(grp)

        for col, hdr in enumerate(["CH", "OVP Level (V)", "OVP On",
                                    "OCP Level (A)", "OCP On", "Apply"]):
            lb = QLabel(hdr); lb.setStyleSheet("font-weight:bold;"); g.addWidget(lb, 0, col)

        self._prot_ovp_sp = {}; self._prot_ovp_en = {}
        self._prot_ocp_sp = {}; self._prot_ocp_en = {}

        for row, ch in enumerate(range(1, 5), start=1):
            g.addWidget(QLabel(f"CH{ch}"), row, 0)

            ovp_sp = QDoubleSpinBox(); ovp_sp.setRange(0, 35.0)
            ovp_sp.setValue(33.6); ovp_sp.setDecimals(3)
            self._prot_ovp_sp[ch] = ovp_sp; g.addWidget(ovp_sp, row, 1)

            ovp_en = QCheckBox(); ovp_en.setChecked(False)
            self._prot_ovp_en[ch] = ovp_en; g.addWidget(ovp_en, row, 2)

            ocp_sp = QDoubleSpinBox(); ocp_sp.setRange(0, 3.5)
            ocp_sp.setValue(3.36); ocp_sp.setDecimals(3)
            self._prot_ocp_sp[ch] = ocp_sp; g.addWidget(ocp_sp, row, 3)

            ocp_en = QCheckBox(); ocp_en.setChecked(False)
            self._prot_ocp_en[ch] = ocp_en; g.addWidget(ocp_en, row, 4)

            b_ch = _btn("Apply", "orange")
            def _apply(ch=ch, b=b_ch):
                ov   = self._prot_ovp_sp[ch].value()
                oven = self._prot_ovp_en[ch].isChecked()
                oc   = self._prot_ocp_sp[ch].value()
                ocen = self._prot_ocp_en[ch].isChecked()
                def fn(res):
                    d = GWGPP4323(res)
                    d.set_ovp(ch, ov); d.enable_ovp(ch, oven)
                    d.set_ocp(ch, oc); d.enable_ocp(ch, ocen)
                    return f"CH{ch} protection applied"
                self._gpp_run(fn, f"CH{ch} prot…", self.prot_status, [b])
            b_ch.clicked.connect(_apply)
            g.addWidget(b_ch, row, 5)

        lay.addWidget(grp)

        b_all_prot = _btn("Apply All Channels", "orange"); b_all_prot.setMinimumHeight(32)
        self.prot_status = StatusLabel("")
        pr = QHBoxLayout(); pr.addWidget(b_all_prot); pr.addWidget(self.prot_status)
        lay.addLayout(pr)

        def _apply_all():
            cfg = {ch: (self._prot_ovp_sp[ch].value(),
                        self._prot_ovp_en[ch].isChecked(),
                        self._prot_ocp_sp[ch].value(),
                        self._prot_ocp_en[ch].isChecked())
                   for ch in range(1, 5)}
            def fn(res):
                d = GWGPP4323(res)
                for ch, (ov, oven, oc, ocen) in cfg.items():
                    d.set_ovp(ch, ov); d.enable_ovp(ch, oven)
                    d.set_ocp(ch, oc); d.enable_ocp(ch, ocen)
                return "All channels protection applied"
            self._gpp_run(fn, "Applying all protection…", self.prot_status, [b_all_prot])

        b_all_prot.clicked.connect(_apply_all)
        lay.addStretch()
        return w

    def get_protection_config(self) -> dict:
        """Return current OVP/OCP config from the Protection tab (used by sweep/sequence runners)."""
        return {
            ch: {
                'ovp':    self._prot_ovp_sp[ch].value(),
                'ovp_en': self._prot_ovp_en[ch].isChecked(),
                'ocp':    self._prot_ocp_sp[ch].value(),
                'ocp_en': self._prot_ocp_en[ch].isChecked(),
            }
            for ch in range(1, 5)
        }

    # ── System tab ───────────────────────────────────────────────────────────

    def _build_system_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        # ── Tracking mode ─────────────────────────────────────────────────
        trk = QGroupBox("CH1/CH2 Tracking Mode")
        tg  = QGridLayout(trk)
        self.sys_tracking = QComboBox()
        self.sys_tracking.addItems(["Independent", "Series (2× voltage)", "Parallel (2× current)"])
        b_track = _btn("Apply Tracking", "blue")
        self.sys_track_st = StatusLabel("")
        tg.addWidget(QLabel("Mode:"), 0, 0); tg.addWidget(self.sys_tracking, 0, 1)
        tg.addWidget(b_track, 0, 2); tg.addWidget(self.sys_track_st, 1, 0, 1, 3)
        def _apply_track():
            mode = ["independent", "series", "parallel"][self.sys_tracking.currentIndex()]
            def fn(res): GWGPP4323(res).set_tracking_mode(mode); return f"Tracking: {mode}"
            self._gpp_run(fn, "Setting tracking…", self.sys_track_st, [b_track])
        b_track.clicked.connect(_apply_track)
        lay.addWidget(trk)

        # ── All ON / OFF ──────────────────────────────────────────────────
        out_grp = QGroupBox("All Channels Output")
        og = QHBoxLayout(out_grp)
        b_allon  = _btn("ALL ON",  "green"); b_allon.setMinimumHeight(36)
        b_alloff = _btn("ALL OFF", "red");   b_alloff.setMinimumHeight(36)
        self.sys_out_st = StatusLabel("")
        og.addWidget(b_allon); og.addWidget(b_alloff); og.addWidget(self.sys_out_st)
        b_allon.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).all_outputs_on() or "All ON",
            "All ON…", self.sys_out_st, [b_allon]))
        b_alloff.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).all_outputs_off() or "All OFF",
            "All OFF…", self.sys_out_st, [b_alloff]))
        lay.addWidget(out_grp)

        # ── Beeper ────────────────────────────────────────────────────────
        beep_grp = QGroupBox("Beeper")
        bg = QHBoxLayout(beep_grp)
        self.beeper_en = QCheckBox("Beeper ON"); self.beeper_en.setChecked(True)
        b_beep = _btn("Apply", "blue")
        self.sys_beep_st = StatusLabel("")
        bg.addWidget(self.beeper_en); bg.addWidget(b_beep); bg.addWidget(self.sys_beep_st)
        def _apply_beep():
            en = self.beeper_en.isChecked()
            def fn(res): GWGPP4323(res).set_beeper(en); return f"Beeper {'ON' if en else 'OFF'}"
            self._gpp_run(fn, "Setting beeper…", self.sys_beep_st, [b_beep])
        b_beep.clicked.connect(_apply_beep)
        lay.addWidget(beep_grp)

        # ── Display ───────────────────────────────────────────────────────
        disp_grp = QGroupBox("Display")
        dg = QGridLayout(disp_grp)
        self.disp_en = QCheckBox("Display ON"); self.disp_en.setChecked(True)
        dg.addWidget(self.disp_en, 0, 0)
        dg.addWidget(QLabel("Brightness:"), 0, 1)
        self.disp_bright = QComboBox()
        self.disp_bright.addItems(GWGPP4323.BRIGHTNESS_LEVELS); self.disp_bright.setCurrentIndex(2)
        dg.addWidget(self.disp_bright, 0, 2)
        dg.addWidget(QLabel("Type (1–7):"), 0, 3)
        self.disp_type = QSpinBox(); self.disp_type.setRange(1, 7); self.disp_type.setValue(1)
        dg.addWidget(self.disp_type, 0, 4)
        b_disp = _btn("Apply Display", "blue"); self.sys_disp_st = StatusLabel("")
        dg.addWidget(b_disp, 1, 0, 1, 3); dg.addWidget(self.sys_disp_st, 1, 3, 1, 2)
        def _apply_disp():
            en = self.disp_en.isChecked()
            br = self.disp_bright.currentText(); dt = self.disp_type.value()
            def fn(res):
                d = GWGPP4323(res)
                d.set_display(en); d.set_brightness(br); d.set_display_type(dt)
                return f"Display={'ON' if en else 'OFF'}, {br}, type {dt}"
            self._gpp_run(fn, "Setting display…", self.sys_disp_st, [b_disp])
        b_disp.clicked.connect(_apply_disp)
        lay.addWidget(disp_grp)

        # ── Save / Recall ─────────────────────────────────────────────────
        mem_grp = QGroupBox("Memory Save / Recall  (slots 0–9)")
        mg = QGridLayout(mem_grp)
        mg.addWidget(QLabel("Slot:"), 0, 0)
        self.mem_slot = QSpinBox(); self.mem_slot.setRange(0, 9); self.mem_slot.setValue(0)
        mg.addWidget(self.mem_slot, 0, 1)
        b_save   = _btn("Save State",   "blue")
        b_recall = _btn("Recall State", "orange")
        self.sys_mem_st = StatusLabel("")
        mg.addWidget(b_save, 0, 2); mg.addWidget(b_recall, 0, 3)
        mg.addWidget(self.sys_mem_st, 1, 0, 1, 4)
        b_save.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).save_state(self.mem_slot.value()) or
                        f"State saved to slot {self.mem_slot.value()}",
            "Saving state…", self.sys_mem_st, [b_save]))
        b_recall.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).recall_state(self.mem_slot.value()) or
                        f"State recalled from slot {self.mem_slot.value()}",
            "Recalling state…", self.sys_mem_st, [b_recall]))
        lay.addWidget(mem_grp)

        # ── System commands ───────────────────────────────────────────────
        cmd_grp = QGroupBox("System Commands")
        cg = QHBoxLayout(cmd_grp)
        b_idn = _btn("IDN?"); b_rst = _btn("*RST", "red")
        b_err = _btn("ERR?"); b_ver = _btn("VER?")
        b_rem = _btn("Remote"); b_loc = _btn("Local")
        self.sys_cmd_st = StatusLabel("")
        for b in [b_idn, b_rst, b_err, b_ver, b_rem, b_loc]: cg.addWidget(b)
        cg.addWidget(self.sys_cmd_st)
        b_idn.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).identify(), "IDN…", self.sys_cmd_st, [b_idn]))
        b_rst.clicked.connect(lambda: self._gpp_run(
            lambda res: (GWGPP4323(res).reset() or "Reset OK"),
            "RST…", self.sys_cmd_st, [b_rst]))
        b_err.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).get_error(), "ERR?…", self.sys_cmd_st, [b_err]))
        b_ver.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).get_version(), "VER?…", self.sys_cmd_st, [b_ver]))
        b_rem.clicked.connect(lambda: self._gpp_run(
            lambda res: (GWGPP4323(res).set_remote() or "Remote"),
            "Remote…", self.sys_cmd_st, [b_rem]))
        b_loc.clicked.connect(lambda: self._gpp_run(
            lambda res: (GWGPP4323(res).set_local() or "Local"),
            "Local…", self.sys_cmd_st, [b_loc]))
        lay.addWidget(cmd_grp)
        lay.addStretch()
        return w

    # ── Plot tab ─────────────────────────────────────────────────────────────

    def _build_all_plot(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        b_alloff = _btn("All Channels OFF", "red"); b_alloff.setMinimumHeight(36)
        b_alloff.clicked.connect(lambda: self._gpp_run(
            lambda res: GWGPP4323(res).all_outputs_off() or "All OFF",
            "All OFF…", self.all_status, [b_alloff]))
        lay.addWidget(b_alloff)

        sel_row = QHBoxLayout()
        self.plot_qty = QComboBox()
        self.plot_qty.addItems(["Voltage (V)", "Current (A)", "Power (W)"])
        sel_row.addWidget(QLabel("Plot:")); sel_row.addWidget(self.plot_qty)
        sel_row.addStretch()
        lay.addLayout(sel_row)

        self._all_plot = PlotPanel(
            channels=["CH1", "CH2", "CH3", "CH4"], y_label="All channels")

        def _meas_all(conn=self._conn):
            qty = self.plot_qty.currentText()
            key = {'Voltage (V)': 'V', 'Current (A)': 'I', 'Power (W)': 'P'}[qty]
            def fn(res):
                d = GWGPP4323(res)
                return {f"CH{ch}": d.measure_all(ch)[key] for ch in range(1, 5)}
            return conn.run(fn, skip_if_busy=True)

        self._all_plot.set_measure_fn(_meas_all)
        lay.addWidget(self._all_plot)
        self.all_status = StatusLabel(""); lay.addWidget(self.all_status)
        return w



# ─────────────────────────────────────────────────────────────────────────────
# GW Instek GDM-8261A  — Front-panel style control
# ─────────────────────────────────────────────────────────────────────────────

_GDM_FUNCS = [
    ("V DC",     "VOLT:DC",  ["AUTO","0.1","1","10","100","1000"]),
    ("V AC",     "VOLT:AC",  ["AUTO","0.1","1","10","100","750"]),
    ("A DC",     "CURR:DC",  ["AUTO","100e-6","1e-3","10e-3","100e-3","1","10"]),
    ("A AC",     "CURR:AC",  ["AUTO","1e-3","10e-3","100e-3","1","10"]),
    ("2W Ω",     "RES",      ["AUTO","100","1e3","10e3","100e3","1e6","10e6","100e6"]),
    ("4W Ω",     "FRES",     ["AUTO","100","1e3","10e3","100e3","1e6","10e6","100e6"]),
    ("Hz",       "FREQ",     ["AUTO"]),
    ("Period",   "PER",      ["AUTO"]),
    ("Diode",    "DIOD",     ["AUTO"]),
    ("Cont",     "CONT",     ["AUTO"]),
    ("Temp TC",  "TEMP:TCO", ["AUTO"]),
    ("Temp RTD", "TEMP:RTD", ["AUTO"]),
    ("Temp FRTD","TEMP:FRTD",["AUTO"]),
]

_GDM_FUNC2 = ["OFF","VOLT:DC","VOLT:AC","CURR:DC","CURR:AC","RES","FRES","FREQ","PER"]

_GDM_LCD_STYLE = (
    "font-family:'Courier New'; font-size:34pt; font-weight:bold;"
    "color:#a6e3a1; background:#0d0d14;"
    "border:2px solid #313244; border-radius:8px; padding:6px 12px;"
)
_GDM_LCD_UNIT = (
    "font-family:'Courier New'; font-size:14pt; color:#6c7086;"
    "background:transparent;"
)
_GDM_LCD2_STYLE = (
    "font-family:'Courier New'; font-size:20pt; font-weight:bold;"
    "color:#89b4fa; background:#0d0d14;"
    "border:1px solid #313244; border-radius:6px; padding:4px 10px;"
)


def _func_btn(label: str) -> QPushButton:
    b = QPushButton(label)
    b.setCheckable(True)
    b.setFixedHeight(34)
    b.setStyleSheet(
        "QPushButton{background:#313244;color:#cdd6f4;border:1px solid #45475a;"
        "border-radius:4px;font-weight:bold;padding:0 6px;}"
        "QPushButton:checked{background:#89b4fa;color:#1e1e2e;}"
        "QPushButton:hover{background:#45475a;}"
    )
    return b


class GdmPanel(QWidget):
    """
    GW Instek GDM-8261A front-panel style control panel.

    • Dual 7-segment-style LCD displays (primary + secondary)
    • One-click function buttons mirroring physical front panel
    • Range / Rate / AC-bandwidth / Auto-range
    • Math: REL/NULL, Stats (Min/Max/Avg/StdDev), dBm, dB, MX+B
    • Secondary display function selector
    • Digital + Analog filter controls
    • Live continuous reading with configurable interval
    • Real-time plot tab
    • IDN / Reset / Remote / Local
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gdm_conn      = shared_resource('gdm')
        self._last_func     = None
        self._measuring     = False   # guard: skip tick if previous not done
        self._live_timer    = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._w             = None
        self._diag_timer    = None   # removed

        lay  = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        tabs = QTabWidget()
        tabs.addTab(self._build_front_panel(), "Front Panel")
        tabs.addTab(self._build_advanced(),    "Advanced / Filters")
        tabs.addTab(self._build_plot_tab(),    "Real-Time Plot")
        lay.addWidget(tabs)

    # ─── helpers ────────────────────────────────────────────────────────────

    def _run(self, fn):
        return self._gdm_conn.run(fn)

    def _drv(self, res):
        return GWInstekGDM8261A(res)

    def _current_func(self) -> str:
        for i, (_, code, _) in enumerate(_GDM_FUNCS):
            if self._func_btns[i].isChecked():
                return code
        return "VOLT:DC"

    def _current_ranges(self) -> list:
        for _, code, ranges in _GDM_FUNCS:
            if code == self._current_func():
                return ranges
        return ["AUTO"]

    def _fmt(self, v: float) -> str:
        if v != v:
            return "------"
        return f"{v:+.6g}"

    # ─── Front panel tab ────────────────────────────────────────────────────

    def _build_front_panel(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setSpacing(6)

        # ── top toolbar ──────────────────────────────────────────────────
        tb = QHBoxLayout()
        self._b_idn    = _btn("IDN?")
        self._b_remote = _btn("Remote", "blue")
        self._b_local  = _btn("Local")
        self._b_reset  = _btn("Reset", "red")
        self._b_beeper = QPushButton("🔔 Beeper")
        self._b_beeper.setCheckable(True)
        self._b_beeper.setChecked(True)
        self._b_beeper.setFixedHeight(30)
        self._b_idn.clicked.connect(self._do_idn)
        self._b_remote.clicked.connect(self._do_remote)
        self._b_local.clicked.connect(self._do_local)
        self._b_reset.clicked.connect(self._do_reset)
        self._b_beeper.toggled.connect(self._do_beeper)
        for b in (self._b_idn, self._b_remote, self._b_local, self._b_reset,
                  self._b_beeper):
            tb.addWidget(b)
        tb.addStretch()
        root.addLayout(tb)

        # ── LCD displays ─────────────────────────────────────────────────
        lcd_box = QGroupBox()
        lcd_box.setStyleSheet("QGroupBox{background:#0d0d14;border:2px solid #313244;"
                               "border-radius:10px;}")
        lcd_lay = QVBoxLayout(lcd_box)

        # Primary display row
        p1 = QHBoxLayout()
        self._disp_primary = QLabel("+0.000000")
        self._disp_primary.setAlignment(Qt.AlignmentFlag.AlignRight |
                                         Qt.AlignmentFlag.AlignVCenter)
        self._disp_primary.setStyleSheet(_GDM_LCD_STYLE)
        self._disp_unit = QLabel("V DC")
        self._disp_unit.setStyleSheet(_GDM_LCD_UNIT)
        self._disp_unit.setFixedWidth(80)
        p1.addWidget(self._disp_primary, stretch=1)
        p1.addWidget(self._disp_unit)
        lcd_lay.addLayout(p1)

        # Secondary display row
        p2 = QHBoxLayout()
        self._disp_secondary = QLabel("— — —")
        self._disp_secondary.setAlignment(Qt.AlignmentFlag.AlignRight |
                                           Qt.AlignmentFlag.AlignVCenter)
        self._disp_secondary.setStyleSheet(_GDM_LCD2_STYLE)
        self._disp_unit2 = QLabel("(2nd OFF)")
        self._disp_unit2.setStyleSheet(_GDM_LCD_UNIT)
        self._disp_unit2.setFixedWidth(80)
        p2.addStretch()
        p2.addWidget(self._disp_secondary, stretch=0)
        p2.addWidget(self._disp_unit2)
        lcd_lay.addLayout(p2)
        root.addWidget(lcd_box)

        # ── Function buttons ─────────────────────────────────────────────
        func_box = QGroupBox("Function")
        func_lay = QGridLayout(func_box)
        func_lay.setSpacing(4)
        self._func_btns = []
        self._func_btn_group = []
        cols = 7
        for i, (lbl, code, _) in enumerate(_GDM_FUNCS):
            b = _func_btn(lbl)
            b.setProperty("func_code", code)
            b.clicked.connect(lambda checked, idx=i: self._on_func_btn(idx))
            self._func_btns.append(b)
            func_lay.addWidget(b, i // cols, i % cols)
        self._func_btns[0].setChecked(True)
        root.addWidget(func_box)

        # ── Range / Rate / AC-BW row ──────────────────────────────────────
        cfg_row = QHBoxLayout()

        rng_box = QGroupBox("Range")
        rng_lay = QHBoxLayout(rng_box)
        rng_lay.setContentsMargins(4, 4, 4, 4)
        self._range_cb = QComboBox()
        self._range_cb.setMinimumWidth(90)
        rng_lay.addWidget(self._range_cb)
        self._b_auto_rng = QPushButton("Auto")
        self._b_auto_rng.setCheckable(True)
        self._b_auto_rng.setChecked(True)
        self._b_auto_rng.clicked.connect(self._do_auto_range)
        rng_lay.addWidget(self._b_auto_rng)
        cfg_row.addWidget(rng_box)

        rate_box = QGroupBox("Rate")
        rate_lay = QHBoxLayout(rate_box)
        rate_lay.setContentsMargins(4, 4, 4, 4)
        self._rate_btns = {}
        for lbl, code in [("Slow", "S"), ("Mid", "M"), ("Fast", "F")]:
            rb = _func_btn(lbl)
            rb.setProperty("rate_code", code)
            rb.clicked.connect(lambda _, c=code: self._do_rate(c))
            rate_lay.addWidget(rb)
            self._rate_btns[code] = rb
        self._rate_btns["M"].setChecked(True)
        cfg_row.addWidget(rate_box)

        bw_box = QGroupBox("AC BW")
        bw_lay = QHBoxLayout(bw_box)
        bw_lay.setContentsMargins(4, 4, 4, 4)
        self._bw_btns = {}
        for hz in (3, 20, 200):
            rb = _func_btn(f"{hz}Hz")
            rb.clicked.connect(lambda _, h=hz: self._do_ac_bw(h))
            bw_lay.addWidget(rb)
            self._bw_btns[hz] = rb
        self._bw_btns[20].setChecked(True)
        cfg_row.addWidget(bw_box)

        cfg_row.addStretch()
        root.addLayout(cfg_row)

        # ── Math row ──────────────────────────────────────────────────────
        math_box = QGroupBox("Math / Measurement Mode")
        math_lay = QHBoxLayout(math_box)
        math_lay.setContentsMargins(6, 4, 6, 4)
        self._b_rel   = _func_btn("REL/NULL")
        self._b_stats = _func_btn("STATS")
        self._b_dbm   = _func_btn("dBm")
        self._b_db    = _func_btn("dB")
        self._b_math_off = _btn("Math OFF")
        self._b_rel.clicked.connect(self._do_rel)
        self._b_stats.clicked.connect(self._do_stats)
        self._b_dbm.clicked.connect(self._do_dbm)
        self._b_db.clicked.connect(self._do_db)
        self._b_math_off.clicked.connect(self._do_math_off)
        for b in (self._b_rel, self._b_stats, self._b_dbm,
                  self._b_db, self._b_math_off):
            math_lay.addWidget(b)
        math_lay.addStretch()
        root.addWidget(math_box)

        # ── Stats display ─────────────────────────────────────────────────
        stats_box = QGroupBox("Statistics")
        stats_grid = QGridLayout(stats_box)
        stats_grid.setContentsMargins(6, 4, 6, 4)
        stats_grid.setSpacing(6)
        self._stat_labels = {}
        for col, key in enumerate(("Min", "Max", "Avg", "StdDev", "Count")):
            stats_grid.addWidget(QLabel(key + ":"), 0, col)
            lbl = QLabel("——")
            lbl.setStyleSheet("font-family:'Courier New'; font-size:10pt;"
                              "color:#cba6f7; font-weight:bold;")
            stats_grid.addWidget(lbl, 1, col)
            self._stat_labels[key] = lbl
        b_get_stats = _btn("Refresh Stats")
        b_get_stats.clicked.connect(self._do_get_stats)
        stats_grid.addWidget(b_get_stats, 0, 5, 2, 1)
        root.addWidget(stats_box)

        # ── Secondary display ─────────────────────────────────────────────
        sec_box = QGroupBox("Secondary Display (Dual Measurement)")
        sec_lay = QHBoxLayout(sec_box)
        sec_lay.setContentsMargins(6, 4, 6, 4)
        sec_lay.addWidget(QLabel("Function 2:"))
        self._func2_cb = QComboBox()
        for f in _GDM_FUNC2:
            self._func2_cb.addItem(f)
        self._func2_cb.setMinimumWidth(90)
        sec_lay.addWidget(self._func2_cb)
        sec_lay.addWidget(QLabel("Range 2:"))
        self._range2_cb = QComboBox()
        for r in ["AUTO","0.1","1","10","100","1000"]:
            self._range2_cb.addItem(r)
        sec_lay.addWidget(self._range2_cb)
        b_apply2 = _btn("Apply", "blue")
        b_apply2.clicked.connect(self._do_display2)
        sec_lay.addWidget(b_apply2)
        sec_lay.addStretch()
        root.addWidget(sec_box)

        # ── Measure controls ──────────────────────────────────────────────
        meas_row = QHBoxLayout()
        self._b_single  = _btn("⚡ Single", "blue")
        self._b_live    = _btn("▶ Live")
        self._b_stop    = _btn("⏸ Stop")
        self._b_stop.setEnabled(False)
        self._live_interval = QSpinBox()
        self._live_interval.setRange(200, 10000)
        self._live_interval.setValue(500)
        self._live_interval.setSuffix(" ms")
        self._live_interval.setMinimumWidth(80)
        self._b_single.clicked.connect(self._do_single)
        self._b_live.clicked.connect(self._do_live_start)
        self._b_stop.clicked.connect(self._do_live_stop)
        meas_row.addWidget(self._b_single)
        meas_row.addWidget(self._b_live)
        meas_row.addWidget(self._b_stop)
        meas_row.addWidget(QLabel("Interval:"))
        meas_row.addWidget(self._live_interval)
        meas_row.addStretch()
        root.addLayout(meas_row)

        self._status = StatusLabel("")
        root.addWidget(self._status)

        # ── Raw debug log ─────────────────────────────────────────────────
        dbg_box = QGroupBox("Raw Response Log")
        dbg_lay = QVBoxLayout(dbg_box)
        dbg_lay.setContentsMargins(4, 4, 4, 4)
        self._debug_log = QTextEdit()
        self._debug_log.setReadOnly(True)
        self._debug_log.setMinimumHeight(80)
        self._debug_log.setStyleSheet(
            "font-family:'Courier New'; font-size:8pt; background:#0d0d14;"
            "color:#a6adc8; border:1px solid #313244;")
        dbg_lay.addWidget(self._debug_log)
        dbg_btn_row = QHBoxLayout()
        b_clr_log = _btn("Clear Log")
        b_clr_log.clicked.connect(self._debug_log.clear)
        dbg_btn_row.addWidget(b_clr_log)
        dbg_btn_row.addStretch()
        dbg_lay.addLayout(dbg_btn_row)
        root.addWidget(dbg_box)

        # initialise range combo
        self._refresh_range_combo()
        return w

    # ─── Advanced tab (filters, trigger, TC/RTD) ─────────────────────────

    def _build_advanced(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        # Digital filter
        df = QGroupBox("Digital Filter")
        dfl = QGridLayout(df)
        dfl.addWidget(QLabel("State:"), 0, 0)
        self._df_on  = _func_btn("ON");  self._df_off = _func_btn("OFF")
        self._df_off.setChecked(True)
        dfl.addWidget(self._df_on, 0, 1); dfl.addWidget(self._df_off, 0, 2)
        dfl.addWidget(QLabel("Type:"), 1, 0)
        self._df_type = QComboBox()
        self._df_type.addItems(["MOV", "REP"])
        dfl.addWidget(self._df_type, 1, 1, 1, 2)
        dfl.addWidget(QLabel("Count:"), 2, 0)
        self._df_count = QSpinBox()
        self._df_count.setRange(2, 100); self._df_count.setValue(10)
        dfl.addWidget(self._df_count, 2, 1, 1, 2)
        b_df = _btn("Apply Digital Filter", "blue")
        b_df.clicked.connect(self._do_dig_filter)
        dfl.addWidget(b_df, 3, 0, 1, 3)
        lay.addWidget(df)

        # Analog filter
        af = QGroupBox("Analog Filter")
        afl = QHBoxLayout(af)
        self._af_on = _func_btn("ON"); self._af_off = _func_btn("OFF")
        self._af_off.setChecked(True)
        b_af = _btn("Apply", "blue")
        b_af.clicked.connect(self._do_ana_filter)
        afl.addWidget(self._af_on); afl.addWidget(self._af_off)
        afl.addWidget(b_af); afl.addStretch()
        lay.addWidget(af)

        # Temperature
        tmp = QGroupBox("Temperature Sensor")
        tmpl = QHBoxLayout(tmp)
        tmpl.addWidget(QLabel("TC Type:"))
        self._tc_type = QComboBox()
        self._tc_type.addItems(["B","E","J","K","N","R","S","T"])
        self._tc_type.setCurrentText("K")
        tmpl.addWidget(self._tc_type)
        b_tc = _btn("Set TC", "blue"); b_tc.clicked.connect(self._do_set_tc)
        tmpl.addWidget(b_tc)
        tmpl.addWidget(QLabel("Unit:"))
        self._temp_unit = QComboBox(); self._temp_unit.addItems(["C","F"])
        tmpl.addWidget(self._temp_unit)
        b_tu = _btn("Set Unit", "blue"); b_tu.clicked.connect(self._do_set_unit)
        tmpl.addWidget(b_tu)
        tmpl.addWidget(QLabel("RTD:"))
        self._rtd_type = QComboBox()
        self._rtd_type.addItems(["PT100","D100","F100","PT385","PT3916","USER"])
        tmpl.addWidget(self._rtd_type)
        self._rtd_4w = QCheckBox("4W")
        tmpl.addWidget(self._rtd_4w)
        b_rtd = _btn("Set RTD", "blue"); b_rtd.clicked.connect(self._do_set_rtd)
        tmpl.addWidget(b_rtd)
        tmpl.addStretch()
        lay.addWidget(tmp)

        # Trigger / Trace
        trig = QGroupBox("Trigger / Trace")
        trigl = QGridLayout(trig)
        trigl.addWidget(QLabel("Source:"), 0, 0)
        self._trig_src = QComboBox(); self._trig_src.addItems(["IMM","EXT","BUS"])
        trigl.addWidget(self._trig_src, 0, 1)
        trigl.addWidget(QLabel("Delay (s):"), 0, 2)
        self._trig_del = QDoubleSpinBox()
        self._trig_del.setRange(0, 3600); self._trig_del.setDecimals(3)
        trigl.addWidget(self._trig_del, 0, 3)
        trigl.addWidget(QLabel("Sample Count:"), 1, 0)
        self._samp_cnt = QSpinBox()
        self._samp_cnt.setRange(1, 9999); self._samp_cnt.setValue(1)
        trigl.addWidget(self._samp_cnt, 1, 1)
        b_trig = _btn("Apply Trigger", "blue")
        b_trig.clicked.connect(self._do_trig)
        b_init = _btn("INIT")
        b_init.clicked.connect(self._do_init)
        b_fetch = _btn("FETCH?")
        b_fetch.clicked.connect(self._do_fetch)
        trigl.addWidget(b_trig, 2, 0, 1, 2)
        trigl.addWidget(b_init, 2, 2)
        trigl.addWidget(b_fetch, 2, 3)
        lay.addWidget(trig)

        # MX+B
        mxb = QGroupBox("MX+B Math")
        mxbl = QHBoxLayout(mxb)
        mxbl.addWidget(QLabel("M:"))
        self._mxb_m = QDoubleSpinBox()
        self._mxb_m.setRange(-1e9, 1e9); self._mxb_m.setValue(1.0)
        self._mxb_m.setDecimals(6)
        mxbl.addWidget(self._mxb_m)
        mxbl.addWidget(QLabel("B:"))
        self._mxb_b = QDoubleSpinBox()
        self._mxb_b.setRange(-1e9, 1e9); self._mxb_b.setValue(0.0)
        self._mxb_b.setDecimals(6)
        mxbl.addWidget(self._mxb_b)
        b_mxb = _btn("Apply MX+B", "blue")
        b_mxb.clicked.connect(self._do_mxb)
        mxbl.addWidget(b_mxb); mxbl.addStretch()
        lay.addWidget(mxb)

        self._adv_status = StatusLabel("")
        lay.addWidget(self._adv_status)
        lay.addStretch()
        return w

    # ─── Plot tab ────────────────────────────────────────────────────────

    def _build_plot_tab(self):
        self._plot = PlotPanel(["Primary"])
        self._plot.set_measure_fn(self._plot_meas)
        return self._plot

    def _plot_meas(self):
        func = self._current_func()
        def fn(res):
            drv = GWInstekGDM8261A(res)
            if func != self._last_func:
                drv.configure(func)
                import time as _t; _t.sleep(0.08)
                self._last_func = func
            return drv.read()
        val = self._run(fn)
        return {"Primary": val}

    # ─── Range combo refresh ─────────────────────────────────────────────

    def _refresh_range_combo(self):
        self._range_cb.blockSignals(True)
        self._range_cb.clear()
        for r in self._current_ranges():
            self._range_cb.addItem(r)
        self._range_cb.blockSignals(False)

    # ─── Function button handler ─────────────────────────────────────────

    def _on_func_btn(self, idx: int):
        for i, b in enumerate(self._func_btns):
            b.setChecked(i == idx)
        _, code, _ = _GDM_FUNCS[idx]
        self._disp_unit.setText(code.replace("VOLT:DC","V DC").replace("VOLT:AC","V AC")
            .replace("CURR:DC","A DC").replace("CURR:AC","A AC")
            .replace("RES","Ω 2W").replace("FRES","Ω 4W")
            .replace("FREQ","Hz").replace("PER","s")
            .replace("DIOD","diode").replace("CONT","cont")
            .replace("TEMP:TCO","°C TC").replace("TEMP:RTD","°C RTD")
            .replace("TEMP:FRTD","°C FRTD"))
        self._last_func = None
        self._refresh_range_combo()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).configure(code)
                return f"Function → {code}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], f"Setting {code}…")

    # ─── Action handlers ─────────────────────────────────────────────────

    def _do_idn(self):
        def job():
            return self._run(lambda res: res.query("*IDN?").strip())
        self._w = _run_panel_cmd(job, self._status, [], "Reading IDN…")

    def _do_remote(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_remote(); return "Remote mode"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Setting remote…")

    def _do_local(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_local(); return "Local mode"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Setting local…")

    def _do_reset(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).reset()
                return "Reset OK"
            self._last_func = None
            self._gdm_conn.invalidate()
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Resetting…")

    def _do_beeper(self, on: bool):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_beeper(on)
                return f"Beeper {'ON' if on else 'OFF'}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Beeper…")

    def _do_auto_range(self):
        on = self._b_auto_rng.isChecked()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_auto_range(on)
                return f"AutoRange {'ON' if on else 'OFF'}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "AutoRange…")

    def _do_rate(self, code: str):
        for k, b in self._rate_btns.items():
            b.setChecked(k == code)
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_rate(code)
                return f"Rate → {code}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], f"Rate {code}…")

    def _do_ac_bw(self, hz: int):
        for k, b in self._bw_btns.items():
            b.setChecked(k == hz)
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_ac_bandwidth(hz)
                return f"AC BW → {hz} Hz"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], f"AC BW {hz}Hz…")

    def _do_rel(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).enable_rel()
                return "REL/NULL ON"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "REL ON…")

    def _do_stats(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).enable_stats()
                return "Stats ON"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Stats ON…")

    def _do_dbm(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).enable_dbm()
                return "dBm ON"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "dBm…")

    def _do_db(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).enable_db()
                return "dB ON"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "dB…")

    def _do_math_off(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).disable_math()
                return "Math OFF"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._status, [], "Math OFF…")

    def _do_get_stats(self):
        def job():
            def fn(res):
                return GWInstekGDM8261A(res).get_stats()
            return self._run(fn)
        def done(val, err):
            if err:
                self._status.err(str(err))
                return
            if not val:
                self._status.warn("No stats data")
                return
            self._stat_labels["Min"].setText(f"{val.get('min', float('nan')):.6g}")
            self._stat_labels["Max"].setText(f"{val.get('max', float('nan')):.6g}")
            self._stat_labels["Avg"].setText(f"{val.get('avg', float('nan')):.6g}")
            self._stat_labels["StdDev"].setText(f"{val.get('stdev', float('nan')):.4g}")
            self._stat_labels["Count"].setText(str(val.get("count", "—")))
            self._status.ok("Stats updated")
            self._status.clear_after(4000)
        wk = InstrumentWorker(job)
        wk.result.connect(done)
        wk.start(); self._w = wk

    def _do_display2(self):
        func2 = self._func2_cb.currentText()
        rng2  = self._range2_cb.currentText()
        def job():
            def fn(res):
                return GWInstekGDM8261A(res).configure_display2(func2, rng2)
            return self._run(fn)
        def done(val, err):
            if err:
                self._status.err(str(err))
                return
            self._disp_unit2.setText(func2 if func2 != "OFF" else "(2nd OFF)")
            self._status.ok(str(val)); self._status.clear_after(3000)
        wk = InstrumentWorker(job)
        wk.result.connect(done); wk.start(); self._w = wk

    def _do_single(self):
        if self._measuring:
            return   # previous tick still in progress — skip
        self._measuring = True
        func = self._current_func()
        func2 = self._func2_cb.currentText()
        dual  = (func2 != "OFF")
        need_conf = (func != self._last_func)
        def job():
            def fn(res):
                import time as _t
                if need_conf:
                    res.write("*CLS")
                    res.write(f"CONF:{func}")
                    _t.sleep(0.15)   # let serial port settle after CONF
                    res.write("SENS:DET:RATE M")
                    _t.sleep(0.05)
                    self._last_func = func
                # Single READ? returns comma-separated dual values when dual
                # display is active — avoids a second VAL2? query which left
                # stale bytes in the serial buffer and caused VI_ERROR_INV_OBJECT.
                raw = res.query("READ?").strip()
                parts = [v.strip() for v in raw.split(",") if v.strip()]
                try:
                    p = float(parts[0])
                except (ValueError, IndexError):
                    p = float("nan")
                if dual and len(parts) >= 2:
                    try:
                        s = float(parts[1])
                    except ValueError:
                        s = float("nan")
                else:
                    s = float("nan")
                return p, s, raw
            return self._run(fn)
        def done(val, err):
            self._measuring = False
            ts = __import__('time').strftime("%H:%M:%S")
            if err:
                self._disp_primary.setText("ERR")
                self._disp_primary.setStyleSheet(
                    _GDM_LCD_STYLE.replace("#a6e3a1", "#f38ba8"))
                self._status.err(str(err))
                self._debug_log.append(f"[{ts}] ERROR: {err}")
                return
            p, s, raw = val
            self._disp_primary.setText(self._fmt(p))
            self._disp_primary.setStyleSheet(_GDM_LCD_STYLE)
            if dual and s == s:
                self._disp_secondary.setText(self._fmt(s))
            self._status.ok(self._fmt(p)); self._status.clear_after(3000)
            log = f"[{ts}] raw='{raw}'  p={p:.7g}"
            if dual:
                log += f"  s={s:.7g}"
            self._debug_log.append(log)
            sb = self._debug_log.verticalScrollBar()
            sb.setValue(sb.maximum())
        wk = InstrumentWorker(job)
        wk.result.connect(done); wk.start(); self._w = wk

    def _do_live_start(self):
        if self._live_timer.isActive():
            return
        self._b_live.setEnabled(False)
        self._b_stop.setEnabled(True)
        self._live_timer.start(self._live_interval.value())

    def _do_live_stop(self):
        self._live_timer.stop()
        self._b_live.setEnabled(True)
        self._b_stop.setEnabled(False)

    def _live_tick(self):
        self._do_single()

    def _do_dig_filter(self):
        on   = self._df_on.isChecked()
        ftyp = self._df_type.currentText()
        cnt  = self._df_count.value()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_digital_filter(on, cnt, ftyp)
                return f"DigFilt {'ON' if on else 'OFF'}, {ftyp}, {cnt}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "Setting dig filter…")

    def _do_ana_filter(self):
        on = self._af_on.isChecked()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_analog_filter(on)
                return f"AnaFilt {'ON' if on else 'OFF'}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "Setting analog filter…")

    def _do_set_tc(self):
        tc = self._tc_type.currentText()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_tc_type(tc); return f"TC → {tc}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], f"TC {tc}…")

    def _do_set_unit(self):
        unit = self._temp_unit.currentText()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_temp_unit(unit); return f"Unit → °{unit}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "Setting temp unit…")

    def _do_set_rtd(self):
        rtd = self._rtd_type.currentText()
        fw  = self._rtd_4w.isChecked()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).set_rtd_type(rtd, fw)
                return f"RTD → {rtd} ({'4W' if fw else '2W'})"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], f"RTD {rtd}…")

    def _do_trig(self):
        src = self._trig_src.currentText()
        dly = self._trig_del.value()
        cnt = self._samp_cnt.value()
        def job():
            def fn(res):
                drv = GWInstekGDM8261A(res)
                drv.set_trigger(src, dly)
                drv.set_sample_count(cnt)
                return f"Trig {src} dly={dly} samp={cnt}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "Trigger…")

    def _do_init(self):
        def job():
            def fn(res):
                GWInstekGDM8261A(res).initiate(); return "INIT sent"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "INIT…")

    def _do_fetch(self):
        def job():
            def fn(res):
                return GWInstekGDM8261A(res).fetch()
            return self._run(fn)
        def done(val, err):
            if err:
                self._adv_status.err(str(err)); return
            self._disp_primary.setText(self._fmt(val))
            self._adv_status.ok(f"FETCH: {val:.7g}")
            self._adv_status.clear_after(4000)
        wk = InstrumentWorker(job)
        wk.result.connect(done); wk.start(); self._w = wk

    def _do_mxb(self):
        m = self._mxb_m.value(); b = self._mxb_b.value()
        def job():
            def fn(res):
                GWInstekGDM8261A(res).enable_mxb(m, b)
                return f"MX+B: M={m}, B={b}"
            return self._run(fn)
        self._w = _run_panel_cmd(job, self._adv_status, [], "MX+B…")


# ─────────────────────────────────────────────────────────────────────────────
# Keysight 34461A  (6.5-Digit DMM via LAN)
# ─────────────────────────────────────────────────────────────────────────────

class DmmPanel(QWidget):
    """
    Full Keysight 34461A control:
    • All functions: VOLT:DC/AC, CURR:DC/AC, RES, FRES, FREQ, DIOD, CONT, CAP
    • Range, NPLC, auto-zero
    • Statistics (min/max/avg/std from captured data in Python)
    • Real-time plot
    """
    FUNCTIONS = [
        ("DC Voltage",   "VOLT:DC",  ["AUTO","100mV","1V","10V","100V","1000V"]),
        ("AC Voltage",   "VOLT:AC",  ["AUTO","100mV","1V","10V","100V","750V"]),
        ("DC Current",   "CURR:DC",  ["AUTO","1mA","10mA","100mA","1A","3A","10A"]),
        ("AC Current",   "CURR:AC",  ["AUTO","1mA","10mA","100mA","1A","3A","10A"]),
        ("2W Resistance","RES",      ["AUTO","100","1k","10k","100k","1M","10M","100M","1G"]),
        ("4W Resistance","FRES",     ["AUTO","100","1k","10k","100k","1M","10M","100M"]),
        ("Frequency",    "FREQ",     ["AUTO"]),
        ("Period",       "PER",      ["AUTO"]),
        ("Diode",        "DIOD",     ["AUTO"]),
        ("Continuity",   "CONT",     ["AUTO"]),
        ("Capacitance",  "CAP",      ["AUTO","1nF","10nF","100nF","1uF","10uF"]),
        ("Temperature",  "TEMP",     ["AUTO"]),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = deque(maxlen=2000)
        self._conn = shared_driver('dmm', lambda: Agilent34401A(ADDR['dmm']))
        lay  = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_control(), "Measure & Config")
        tabs.addTab(self._build_plot(),    "Real-Time Plot")
        tabs.addTab(self._build_stats(),   "Statistics")
        lay.addWidget(tabs)

    def _build_control(self):
        w = QWidget(); lay = QVBoxLayout(w)

        cfg = QGroupBox("Measurement Configuration")
        g   = QGridLayout(cfg)
        g.addWidget(QLabel("Function:"), 0, 0)
        self.func = QComboBox()
        for lbl, _, _ in self.FUNCTIONS:
            self.func.addItem(lbl)
        self.func.currentIndexChanged.connect(self._on_func_changed)
        g.addWidget(self.func, 0, 1)

        g.addWidget(QLabel("Range:"), 1, 0)
        self.range_sel = QComboBox()
        g.addWidget(self.range_sel, 1, 1)

        g.addWidget(QLabel("NPLC:"), 2, 0)
        self.nplc = QComboBox()
        for v in ["0.001","0.006","0.02","0.06","0.2","1","2","10","100"]:
            self.nplc.addItem(v)
        self.nplc.setCurrentText("1")
        g.addWidget(self.nplc, 2, 1)

        g.addWidget(QLabel("Auto-Zero:"), 3, 0)
        self.autozero = QComboBox(); self.autozero.addItems(["ON","OFF","ONCE"])
        g.addWidget(self.autozero, 3, 1)

        b_conf = _btn("Configure", "blue")
        b_idn  = _btn("Read IDN")
        b_rst  = _btn("Reset")
        b_conf.clicked.connect(self._configure)
        b_idn.clicked.connect(self._read_idn)
        b_rst.clicked.connect(self._reset)
        g.addWidget(b_conf, 4, 0, 1, 2)
        g.addWidget(b_idn, 5, 0); g.addWidget(b_rst, 5, 1)
        lay.addWidget(cfg)

        meas = QGroupBox("Measurement")
        ml   = QVBoxLayout(meas)
        self.val_lbl = QLabel("---")
        self.val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.val_lbl.setMinimumHeight(70)
        self.val_lbl.setStyleSheet(
            "font-family:'Courier New'; font-size:32pt; color:#a6e3a1;"
            "font-weight:bold; background:#181825; border:1px solid #45475a; border-radius:6px;")
        ml.addWidget(self.val_lbl)
        b_single = _btn("Single Measure", "blue")
        b_single.clicked.connect(self._measure_once)
        ml.addWidget(b_single)
        lay.addWidget(meas)

        self.status = StatusLabel(""); lay.addWidget(self.status)
        self._dmm_btns = [b_conf, b_idn, b_rst, b_single]
        lay.addStretch()
        self._on_func_changed(0)
        return w

    def _build_plot(self):
        self._plot = PlotPanel(["Measurement"])
        def mfn():
            func = self.FUNCTIONS[self.func.currentIndex()][1]
            def fn(drv):
                v = _dmm_measure(drv, func)
                self._history.append(v)
                return {"Measurement": v}
            return self._conn.run(fn, skip_if_busy=True)
        self._plot.set_measure_fn(mfn)
        return self._plot

    def _build_stats(self):
        w   = QWidget(); lay = QVBoxLayout(w)
        top = QHBoxLayout()
        b_calc = _btn("Calculate from Plot Data", "blue")
        b_calc.clicked.connect(self._calc_stats)
        top.addWidget(b_calc); top.addStretch()
        lay.addLayout(top)
        self.stats_view = QTextEdit(); self.stats_view.setReadOnly(True)
        lay.addWidget(self.stats_view)
        return w

    def _calc_stats(self):
        data = list(self._history)
        if not data:
            self.stats_view.setPlainText("No data yet — run continuous measurement first.")
            return
        import statistics
        n   = len(data)
        mn  = min(data); mx = max(data); avg = sum(data)/n
        try:
            std = statistics.stdev(data)
        except Exception:
            std = float('nan')
        self.stats_view.setPlainText(
            f"Count:   {n}\n"
            f"Min:     {mn:.7g}\n"
            f"Max:     {mx:.7g}\n"
            f"Mean:    {avg:.7g}\n"
            f"Std Dev: {std:.7g}\n"
            f"Range:   {mx-mn:.7g}"
        )

    def _on_func_changed(self, idx):
        self.range_sel.clear()
        _, _, ranges = self.FUNCTIONS[idx]
        for r in ranges:
            self.range_sel.addItem(r)

    def _configure(self):
        func = self.FUNCTIONS[self.func.currentIndex()][1]
        rng  = self.range_sel.currentText()
        nplc = self.nplc.currentText()
        az   = self.autozero.currentText()
        def job():
            return self._conn.run(lambda drv: _dmm_configure(drv, func, nplc, rng, az))
        self._w = _run_panel_cmd(job, self.status, self._dmm_btns,
                                 f"Configuring DMM: {func}…")

    def _reset(self):
        def job():
            def fn(drv):
                drv.reset(); return "DMM reset OK"
            return self._conn.run(fn)
        self._w = _run_panel_cmd(job, self.status, self._dmm_btns, "Resetting DMM…")

    def _read_idn(self):
        def job():
            return self._conn.run(lambda drv: drv.id)
        self._w = _run_panel_cmd(job, self.status, self._dmm_btns, "Reading IDN…")

    def _measure_once(self):
        func = self.FUNCTIONS[self.func.currentIndex()][1]
        for b in self._dmm_btns: b.setEnabled(False)
        self.status.info("⏳  Measuring…")
        def job():
            return self._conn.run(lambda drv: _dmm_measure(drv, func))
        def done(val, err):
            for b in self._dmm_btns: b.setEnabled(True)
            if err:
                self.val_lbl.setText("ERR")
                self.val_lbl.setStyleSheet(
                    "font-family:'Courier New'; font-size:32pt; color:#f38ba8;"
                    "font-weight:bold; background:#181825; border:1px solid #45475a; border-radius:6px;")
                self.status.err(err)
            else:
                self.val_lbl.setText(f"{val:.7g}")
                self.val_lbl.setStyleSheet(
                    "font-family:'Courier New'; font-size:32pt; color:#a6e3a1;"
                    "font-weight:bold; background:#181825; border:1px solid #45475a; border-radius:6px;")
                self.status.ok(f"{val:.7g}")
                self.status.clear_after(4000)
                self._history.append(val)
        w = InstrumentWorker(job)
        w.result.connect(done)
        w.start(); self._w = w


# ─────────────────────────────────────────────────────────────────────────────
# Tektronix MDO34  (Mixed Domain Oscilloscope)
# ─────────────────────────────────────────────────────────────────────────────

class MdoPanel(QWidget):
    """
    Full MDO34 control panel backed by MDO34Driver (SCPI, 3 Series MDO manual):
    • Channel setup: V/div, coupling, termination, probe, BW limit, invert, label
    • Timebase: s/div, position, record length
    • Acquisition mode: Sample / PeakDetect / HiRes / Average (N) / Envelope
    • Trigger: edge — source, slope, level, coupling
    • Acquisition: RUN / STOP / SINGLE / Autoset
    • Measurements: 22 immediate types per channel, plottable over time
    • Measurement slots MEAS1–MEAS4: persistent values + statistics
    • DVM: ACRMS / ACDCRMS / DC / Frequency with real-time readout
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn = shared_driver('mdo', lambda: MDO34Driver(ADDR['mdo']))
        lay  = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_channel_tab(),    "Channel Setup")
        tabs.addTab(self._build_timebase_tab(),   "Timebase & Trigger")
        tabs.addTab(self._build_measure_tab(),    "Measurements & Plot")
        tabs.addTab(self._build_meas_slots_tab(), "Meas Slots")
        tabs.addTab(self._build_dvm_tab(),        "DVM")
        tabs.addTab(self._build_waveform_tab(),   "Waveform")
        lay.addWidget(tabs)

    # ── Channel setup ────────────────────────────────────────────────────────

    def _build_channel_tab(self):
        """All 4 channels in a single 2×2 scrollable grid — no sub-tabs."""
        from PyQt6.QtWidgets import QScrollArea
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        container = QWidget(); outer = QVBoxLayout(container)
        outer.setSpacing(8)

        grid = QGridLayout(); grid.setSpacing(10)
        self._ch_widgets: dict = {}
        self._ch_groups:  dict = {}
        _ch_colors = ['#89b4fa', '#a6e3a1', '#f38ba8', '#fab387']
        _positions  = [(0, 0), (0, 1), (1, 0), (1, 1)]

        for i, ch in enumerate(MDO34Driver.CHANNELS):
            cfg = QGroupBox(f"  {ch}")
            cfg.setStyleSheet(
                "QGroupBox{color:#585b70;font-weight:bold;"
                "border:1px solid #45475a;border-radius:6px;padding-top:10px;}"
                "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}")
            g = QGridLayout(cfg); row = 0; wd = {}

            g.addWidget(QLabel("V/div:"), row, 0)
            scale = QDoubleSpinBox(); scale.setRange(0.001, 10); scale.setValue(1)
            scale.setDecimals(3); scale.setSingleStep(0.1)
            g.addWidget(scale, row, 1, 1, 2); row += 1; wd['scale'] = scale

            g.addWidget(QLabel("Position (div):"), row, 0)
            pos = QDoubleSpinBox(); pos.setRange(-8, 8); pos.setValue(0)
            pos.setSingleStep(0.5)
            g.addWidget(pos, row, 1, 1, 2); row += 1; wd['pos'] = pos

            g.addWidget(QLabel("Coupling:"), row, 0)
            coup = QComboBox(); coup.addItems(MDO34Driver.COUPLINGS)
            g.addWidget(coup, row, 1, 1, 2); row += 1; wd['coup'] = coup

            g.addWidget(QLabel("Termination:"), row, 0)
            term = QComboBox()
            term.addItem("1 MΩ", "1e6"); term.addItem("50 Ω", "50")
            g.addWidget(term, row, 1, 1, 2); row += 1; wd['term'] = term

            g.addWidget(QLabel("Probe factor:"), row, 0)
            probe = QComboBox()
            for p in MDO34Driver.PROBE_GAINS: probe.addItem(f"{p}x", p)
            g.addWidget(probe, row, 1, 1, 2); row += 1; wd['probe'] = probe

            g.addWidget(QLabel("BW Limit:"), row, 0)
            bw = QComboBox()
            bw.addItem("Full", "FULL"); bw.addItem("250 MHz", "250"); bw.addItem("20 MHz", "20")
            g.addWidget(bw, row, 1, 1, 2); row += 1; wd['bw'] = bw

            g.addWidget(QLabel("Label:"), row, 0)
            lbl_edit = QLineEdit(); lbl_edit.setPlaceholderText("optional label")
            g.addWidget(lbl_edit, row, 1, 1, 2); row += 1; wd['label'] = lbl_edit

            invert = QCheckBox("Invert")
            g.addWidget(invert, row, 0, 1, 3); row += 1; wd['invert'] = invert

            b_apply = _btn(f"Apply {ch}", "blue")
            b_on    = _btn("CH ON",  "green")
            b_off   = _btn("CH OFF", "red")
            b_apply.clicked.connect(lambda _chk, _c=ch: self._apply_channel(_c))
            b_on .clicked.connect(lambda _chk, _c=ch: self._ch_on(_c))
            b_off.clicked.connect(lambda _chk, _c=ch: self._ch_off(_c))
            g.addWidget(b_apply, row, 0, 1, 3); row += 1
            btn_hl = QHBoxLayout()
            btn_hl.addWidget(b_on); btn_hl.addWidget(b_off)
            g.addLayout(btn_hl, row, 0, 1, 3)

            self._ch_widgets[ch] = wd
            self._ch_groups[ch]  = cfg
            grid.addWidget(cfg, *_positions[i])

        outer.addLayout(grid)
        rfbar = QHBoxLayout()
        b_refresh  = _btn("🔄  Refresh from Scope", "blue")
        b_applyall = _btn("📤  Push All to Scope",  "green")
        b_refresh .clicked.connect(self._query_all_state)
        b_applyall.clicked.connect(self._apply_all_state)
        rfbar.addWidget(b_refresh); rfbar.addWidget(b_applyall); rfbar.addStretch()
        outer.addLayout(rfbar)
        self.ch_status = StatusLabel(""); outer.addWidget(self.ch_status)
        outer.addStretch()
        scroll.setWidget(container)
        return scroll

    def _ch_on(self, ch: str):
        self._mdo_write(f"SELect:{ch} ON")
        self._set_ch_active(ch, True)

    def _ch_off(self, ch: str):
        self._mdo_write(f"SELect:{ch} OFF")
        self._set_ch_active(ch, False)

    def _set_ch_active(self, ch: str, active: bool):
        """Highlight the channel group box when ON, grey it out when OFF."""
        grp = self._ch_groups.get(ch)
        if grp is None:
            return
        i = MDO34Driver.CHANNELS.index(ch)
        _colors = ['#89b4fa', '#a6e3a1', '#f38ba8', '#fab387']
        c = _colors[i]
        if active:
            grp.setStyleSheet(
                f"QGroupBox{{color:{c};font-weight:bold;"
                f"border:2px solid {c};border-radius:6px;padding-top:10px;}}"
                f"QGroupBox::title{{subcontrol-origin:margin;left:8px;padding:0 4px;}}")
        else:
            grp.setStyleSheet(
                "QGroupBox{color:#585b70;font-weight:bold;"
                "border:1px solid #45475a;border-radius:6px;padding-top:10px;}"
                "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}")

    def _query_all_state(self):
        """Query the scope and populate every UI field, then update channel highlights."""
        self.ch_status.ok("Querying scope…")

        def job():
            def fn(drv):
                s = {}
                for ch in MDO34Driver.CHANNELS:
                    n = ch[2]
                    s[f'{ch}.scale']  = drv.ask(f'CH{n}:SCAle?').strip()
                    s[f'{ch}.pos']    = drv.ask(f'CH{n}:POSition?').strip()
                    s[f'{ch}.coup']   = drv.ask(f'CH{n}:COUPling?').strip()
                    s[f'{ch}.term']   = drv.ask(f'CH{n}:TERmination?').strip()
                    s[f'{ch}.probe']  = drv.ask(f'CH{n}:PRObe:GAIn?').strip()
                    s[f'{ch}.bw']     = drv.ask(f'CH{n}:BANdwidth?').strip()
                    s[f'{ch}.label']  = drv.ask(f'CH{n}:LABel:NAME?').strip().strip('"')
                    s[f'{ch}.invert'] = drv.ask(f'CH{n}:INVert?').strip()
                    s[f'{ch}.select'] = drv.ask(f'SELect:CH{n}?').strip()
                s['tb.scale']   = drv.ask('HORizontal:SCAle?').strip()
                s['tb.pos']     = drv.ask('HORizontal:POSition?').strip()
                s['tb.reclen']  = drv.ask('HORizontal:RECOrdlength?').strip()
                s['acq.mode']   = drv.ask('ACQuire:MODe?').strip()
                s['acq.navg']   = drv.ask('ACQuire:NUMAVg?').strip()
                s['trig.src']   = drv.ask('TRIGger:A:EDGE:SOUrce?').strip()
                s['trig.slope'] = drv.ask('TRIGger:A:EDGE:SLOpe?').strip()
                s['trig.level'] = drv.ask('TRIGger:A:LEVel?').strip()
                s['trig.coup']  = drv.ask('TRIGger:A:EDGE:COUPling?').strip()
                return s
            return self._conn.run(fn)

        def _on_done(result, err):
            if err:
                self.ch_status.err(f"Refresh failed: {err}"); return
            for ch in MDO34Driver.CHANNELS:
                wd = self._ch_widgets[ch]
                try: wd['scale'].setValue(float(result[f'{ch}.scale']))
                except: pass
                try: wd['pos'].setValue(float(result[f'{ch}.pos']))
                except: pass
                coup = result.get(f'{ch}.coup', '').upper()
                for j in range(wd['coup'].count()):
                    if wd['coup'].itemText(j).upper() == coup:
                        wd['coup'].setCurrentIndex(j); break
                tdata = '50' if '50' in result.get(f'{ch}.term', '') else '1e6'
                for j in range(wd['term'].count()):
                    if wd['term'].itemData(j) == tdata:
                        wd['term'].setCurrentIndex(j); break
                try:
                    pval = float(result[f'{ch}.probe'])
                    for j in range(wd['probe'].count()):
                        if abs(float(wd['probe'].itemData(j)) - pval) < 0.01:
                            wd['probe'].setCurrentIndex(j); break
                except: pass
                bw = result.get(f'{ch}.bw', 'FULL').upper()
                for j in range(wd['bw'].count()):
                    if wd['bw'].itemData(j).upper() in bw or bw in wd['bw'].itemData(j).upper():
                        wd['bw'].setCurrentIndex(j); break
                wd['label'].setText(result.get(f'{ch}.label', ''))
                wd['invert'].setChecked(result.get(f'{ch}.invert', 'OFF').upper() == 'ON')
                self._set_ch_active(ch, result.get(f'{ch}.select', '0') in ('1', 'ON'))
            _units = {'ns': 1e-9, 'us': 1e-6, 'ms': 1e-3, 's': 1.0}
            try:
                tb = float(result['tb.scale'])
                best_j, best_d = 0, float('inf')
                for j in range(self.tb_scale.count()):
                    txt = self.tb_scale.itemText(j)
                    for sfx, mult in _units.items():
                        if txt.endswith(sfx):
                            try:
                                v = float(txt[:-len(sfx)]) * mult
                                if abs(v - tb) < best_d:
                                    best_d = abs(v - tb); best_j = j
                            except: pass
                            break
                self.tb_scale.setCurrentIndex(best_j)
            except: pass
            try: self.tb_pos.setValue(float(result['tb.pos']))
            except: pass
            rl_idx = self.tb_reclen.findText(result.get('tb.reclen', ''))
            if rl_idx >= 0: self.tb_reclen.setCurrentIndex(rl_idx)
            am_idx = self.acq_mode.findText(result.get('acq.mode', '').upper())
            if am_idx >= 0: self.acq_mode.setCurrentIndex(am_idx)
            try: self.acq_navg.setValue(int(result['acq.navg']))
            except: pass
            for val, cb in [
                (result.get('trig.src',   ''), self.trig_src),
                (result.get('trig.slope', ''), self.trig_slope),
                (result.get('trig.coup',  ''), self.trig_coup),
            ]:
                idx = cb.findText(val, Qt.MatchFlag.MatchContains)
                if idx >= 0: cb.setCurrentIndex(idx)
            try: self.trig_level.setValue(float(result['trig.level']))
            except: pass
            self.ch_status.ok("🔄 Refreshed from scope")

        wk = InstrumentWorker(job)
        wk.result.connect(_on_done)
        wk.start(); self._w = wk

    def _apply_all_state(self):
        """Collect all UI field values and push them to the scope in one worker."""
        ch_states = {}
        for ch in MDO34Driver.CHANNELS:
            wd = self._ch_widgets[ch]
            ch_states[ch] = dict(
                scale=wd['scale'].value(),  pos=wd['pos'].value(),
                coup=wd['coup'].currentText(), term=wd['term'].currentData(),
                probe=wd['probe'].currentData(), bw=wd['bw'].currentData(),
                inv="ON" if wd['invert'].isChecked() else "OFF",
                lbl=wd['label'].text().strip(),
            )
        _units = {'ns': 1e-9, 'us': 1e-6, 'ms': 1e-3, 's': 1.0}
        tb_s = 0.001
        for sfx, mult in _units.items():
            if self.tb_scale.currentText().endswith(sfx):
                try: tb_s = float(self.tb_scale.currentText()[:-len(sfx)]) * mult
                except: pass
                break
        tb_pos   = self.tb_pos.value();          tb_rl    = self.tb_reclen.currentText()
        acq_mode = self.acq_mode.currentText();  acq_avg  = self.acq_navg.value()
        trig_src = self.trig_src.currentText();  trig_slp = self.trig_slope.currentText()
        trig_lvl = self.trig_level.value();      trig_cp  = self.trig_coup.currentText()

        def job():
            def fn(drv):
                for ch, cs in ch_states.items():
                    drv.configure_channel(
                        ch, cs['scale'], cs['pos'], cs['coup'],
                        cs['term'], cs['probe'], cs['bw'], cs['inv'], cs['lbl'])
                drv.configure_timebase(tb_s, tb_pos, tb_rl)
                drv.configure_acquisition(acq_mode, acq_avg)
                drv.configure_trigger(trig_src, trig_slp, trig_lvl, trig_cp)
                return "✓ All settings pushed to scope"
            return self._conn.run(fn)

        def _on_done(v, e):
            if e: self.ch_status.err(e)
            else: self.ch_status.ok(v)

        wk = InstrumentWorker(job)
        wk.result.connect(_on_done)
        wk.start(); self._w = wk

    # ── Timebase & Trigger ───────────────────────────────────────────────────

    def _build_timebase_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        tb = QGroupBox("Timebase")
        g  = QGridLayout(tb)
        g.addWidget(QLabel("Scale (s/div):"), 0, 0)
        self.tb_scale = QComboBox(); self.tb_scale.addItems(MDO34Driver.TB_SCALES)
        self.tb_scale.setCurrentText("1ms")
        g.addWidget(self.tb_scale, 0, 1)
        g.addWidget(QLabel("Position (%):"), 1, 0)
        self.tb_pos = QDoubleSpinBox(); self.tb_pos.setRange(-50, 50); self.tb_pos.setValue(0)
        g.addWidget(self.tb_pos, 1, 1)
        g.addWidget(QLabel("Record Length:"), 2, 0)
        self.tb_reclen = QComboBox(); self.tb_reclen.addItems(MDO34Driver.RECORD_LENGTHS)
        self.tb_reclen.setCurrentText("10000")
        g.addWidget(self.tb_reclen, 2, 1)
        b_tb = _btn("Apply Timebase", "blue")
        b_tb.clicked.connect(self._apply_timebase)
        g.addWidget(b_tb, 3, 0, 1, 2)
        lay.addWidget(tb)

        acqm = QGroupBox("Acquisition Mode")
        gm   = QGridLayout(acqm)
        gm.addWidget(QLabel("Mode:"), 0, 0)
        self.acq_mode = QComboBox(); self.acq_mode.addItems(MDO34Driver.ACQ_MODES)
        gm.addWidget(self.acq_mode, 0, 1)
        gm.addWidget(QLabel("# Averages:"), 1, 0)
        self.acq_navg = QSpinBox(); self.acq_navg.setRange(2, 512); self.acq_navg.setValue(8)
        gm.addWidget(self.acq_navg, 1, 1)
        b_acqm = _btn("Apply Acq Mode", "blue")
        b_acqm.clicked.connect(self._apply_acquisition)
        gm.addWidget(b_acqm, 2, 0, 1, 2)
        lay.addWidget(acqm)

        trig = QGroupBox("Trigger (Edge)")
        g2   = QGridLayout(trig)
        g2.addWidget(QLabel("Source:"), 0, 0)
        self.trig_src = QComboBox()
        self.trig_src.addItems(MDO34Driver.CHANNELS + ["EXT","LINE"])
        g2.addWidget(self.trig_src, 0, 1)
        g2.addWidget(QLabel("Slope:"), 1, 0)
        self.trig_slope = QComboBox(); self.trig_slope.addItems(["RISE","FALL","EITHER"])
        g2.addWidget(self.trig_slope, 1, 1)
        g2.addWidget(QLabel("Level (V):"), 2, 0)
        self.trig_level = QDoubleSpinBox(); self.trig_level.setRange(-40, 40); self.trig_level.setValue(0)
        g2.addWidget(self.trig_level, 2, 1)
        g2.addWidget(QLabel("Coupling:"), 3, 0)
        self.trig_coup = QComboBox(); self.trig_coup.addItems(MDO34Driver.TRIG_COUPLINGS)
        g2.addWidget(self.trig_coup, 3, 1)
        b_trig = _btn("Apply Trigger", "blue")
        b_trig.clicked.connect(self._apply_trigger)
        g2.addWidget(b_trig, 4, 0, 1, 2)
        lay.addWidget(trig)

        acq = QGroupBox("Acquisition")
        al  = QHBoxLayout(acq)
        b_run    = _btn("RUN",    "green")
        b_stop   = _btn("STOP",   "red")
        b_single = _btn("SINGLE", "orange")
        b_auto   = _btn("Autoset","blue")
        b_idn    = _btn("IDN")
        b_run.clicked.connect(lambda: self._mdo_write("ACQ:STATE RUN"))
        b_stop.clicked.connect(lambda: self._mdo_write("ACQ:STATE STOP"))
        b_single.clicked.connect(lambda: self._mdo_write("ACQ:STOPAFTER SEQ"))
        b_auto.clicked.connect(self._autoset)
        b_idn.clicked.connect(lambda: self._mdo_query("*IDN?"))
        for b in [b_run, b_stop, b_single, b_auto, b_idn]: al.addWidget(b)
        lay.addWidget(acq)

        self.acq_status = StatusLabel(""); lay.addWidget(self.acq_status)
        lay.addStretch()
        return w

    # ── Measurements ─────────────────────────────────────────────────────────

    def _build_measure_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        sel = QGroupBox("Measurement Selection")
        g   = QGridLayout(sel)
        g.addWidget(QLabel("Channel:"), 0, 0)
        self.meas_ch = QComboBox(); self.meas_ch.addItems(MDO34Driver.CHANNELS)
        g.addWidget(self.meas_ch, 0, 1)
        g.addWidget(QLabel("Type:"), 1, 0)
        self.meas_type = QComboBox()
        for lbl, val in MDO34Driver.MEAS_TYPES: self.meas_type.addItem(lbl, val)
        g.addWidget(self.meas_type, 1, 1)
        lay.addWidget(sel)

        self.meas_val = QLabel("---")
        self.meas_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meas_val.setMinimumHeight(70)
        self.meas_val.setStyleSheet(
            "font-family:'Courier New'; font-size:32pt; color:#a6e3a1;"
            "font-weight:bold; background:#181825; border:1px solid #45475a; border-radius:6px;")
        lay.addWidget(self.meas_val)

        self._mdo_plot = PlotPanel(["Measurement"], y_label="Value")
        def meas_fn():
            ch   = self.meas_ch.currentText()
            mtyp = self.meas_type.currentData()
            def fn(drv):
                v = drv.measure_immediate(ch, mtyp)
                self.meas_val.setText(f"{v:.6g}")
                return {"Measurement": v}
            return self._conn.run(fn, skip_if_busy=True)
        self._mdo_plot.set_measure_fn(meas_fn)
        lay.addWidget(self._mdo_plot)

        self.meas_status = StatusLabel(""); lay.addWidget(self.meas_status)
        return w

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _apply_channel(self, ch: str = None):
        wd    = self._ch_widgets[ch]
        scale = wd['scale'].value()
        pos   = wd['pos'].value()
        coup  = wd['coup'].currentText()
        term  = wd['term'].currentData()
        probe = wd['probe'].currentData()
        bw    = wd['bw'].currentData()
        inv   = "ON" if wd['invert'].isChecked() else "OFF"
        lbl   = wd['label'].text().strip()
        def job():
            return self._conn.run(
                lambda drv: drv.configure_channel(ch, scale, pos, coup, term, probe, bw, inv, lbl))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.ch_status.ok(v) if not e else self.ch_status.err(e)))
        w.start(); self._w = w

    def _apply_timebase(self):
        scale_str = self.tb_scale.currentText()
        units = {'ns':1e-9,'us':1e-6,'ms':1e-3,'s':1.0}
        for sfx, mult in units.items():
            if scale_str.endswith(sfx):
                scale = float(scale_str[:-len(sfx)]) * mult; break
        else:
            scale = 0.001
        pos = self.tb_pos.value()
        rl  = self.tb_reclen.currentText()
        def job():
            return self._conn.run(lambda drv: drv.configure_timebase(scale, pos, rl))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    def _apply_acquisition(self):
        mode = self.acq_mode.currentText()
        navg = self.acq_navg.value()
        def job():
            return self._conn.run(lambda drv: drv.configure_acquisition(mode, navg))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    def _apply_trigger(self):
        src   = self.trig_src.currentText()
        slope = self.trig_slope.currentText()
        level = self.trig_level.value()
        coup  = self.trig_coup.currentText()
        def job():
            return self._conn.run(lambda drv: drv.configure_trigger(src, slope, level, coup))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    def _autoset(self):
        def job():
            return self._conn.run(lambda drv: drv.autoset())
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    def _mdo_write(self, cmd):
        def job():
            return self._conn.run(lambda drv: (drv.write(cmd) or cmd))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    def _mdo_query(self, cmd):
        def job():
            return self._conn.run(lambda drv: drv.ask(cmd).strip())
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.acq_status.ok(v) if not e else self.acq_status.err(e)))
        w.start(); self._w = w

    # ── Waveform capture tab ─────────────────────────────────────────────────

    def _build_waveform_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        self._wf_data: dict = {}
        self._wf_home_range = None          # (xr, yr) stored after first capture
        _ch_colors = ['#89b4fa', '#a6e3a1', '#f38ba8', '#fab387']

        # ── Channel selection + editable names (all 4 in one row) ────────────
        sel_grp = QGroupBox("Channels  (☑ = capture & show  |  name = custom label)")
        sel_h   = QHBoxLayout(sel_grp)
        sel_h.setSpacing(10)
        self._wf_ch_chks:  dict = {}
        self._wf_ch_names: dict = {}

        for i, ch in enumerate(MDO34Driver.CHANNELS):
            cell = QHBoxLayout(); cell.setSpacing(4)

            chk = QCheckBox(); chk.setChecked(i == 0)
            cell.addWidget(chk)
            self._wf_ch_chks[ch] = chk

            lbl = QLabel(ch)
            lbl.setStyleSheet(f"color:{_ch_colors[i]};font-weight:bold;min-width:32px;")
            cell.addWidget(lbl)

            name_edit = QLineEdit(ch)
            name_edit.setPlaceholderText("label…")
            name_edit.setFixedWidth(80)
            cell.addWidget(name_edit)
            self._wf_ch_names[ch] = name_edit

            b_scope = QPushButton("⬆ Scope")
            b_scope.setFixedHeight(22)
            b_scope.setStyleSheet(
                "QPushButton{background:#313244;color:#cdd6f4;border:1px solid #45475a;"
                "border-radius:3px;font-size:8pt;padding:0 4px;}"
                "QPushButton:hover{background:#45475a;}")
            b_scope.clicked.connect(lambda _c=None, _ch=ch: self._set_ch_label_on_scope(_ch))
            cell.addWidget(b_scope)

            sel_h.addLayout(cell)
            if i < 3:
                frm = QFrame(); frm.setFrameShape(QFrame.Shape.VLine)
                frm.setStyleSheet("color:#45475a;")
                sel_h.addWidget(frm)

        sel_h.addStretch()
        b_cap = _btn("▶  Capture", "blue")
        b_cap.clicked.connect(self._capture_waveform)
        sel_h.addWidget(b_cap)
        lay.addWidget(sel_grp)

        # ── Plot options + zoom ───────────────────────────────────────────────
        cust_grp = QGroupBox("Plot Options  &  Zoom")
        cust_v   = QVBoxLayout(cust_grp); cust_v.setSpacing(4)

        # row 1 — display options + Max pts + export
        row1 = QHBoxLayout()
        self.wf_grid_chk = QCheckBox("Grid"); self.wf_grid_chk.setChecked(True)
        row1.addWidget(self.wf_grid_chk)
        self.wf_legend_chk = QCheckBox("Legend"); self.wf_legend_chk.setChecked(True)
        row1.addWidget(self.wf_legend_chk)
        self.wf_autoscale_chk = QCheckBox("Autoscale"); self.wf_autoscale_chk.setChecked(True)
        row1.addWidget(self.wf_autoscale_chk)
        self.wf_split_chk = QCheckBox("Split Channels")
        row1.addWidget(self.wf_split_chk)
        row1.addSpacing(12)
        row1.addWidget(QLabel("Max pts:"))
        self.wf_pts = QSpinBox()
        self.wf_pts.setRange(100, 1_000_000); self.wf_pts.setValue(10_000)
        self.wf_pts.setSingleStep(1000); self.wf_pts.setFixedWidth(160)
        row1.addWidget(self.wf_pts)
        row1.addStretch()
        b_save_xl  = _btn("💾  Save Excel", "green")
        b_copy_png = _btn("📋  Copy PNG",   "orange")
        b_save_xl .clicked.connect(self._save_waveform_excel)
        b_copy_png.clicked.connect(self._copy_waveform_clipboard)
        row1.addWidget(b_save_xl); row1.addWidget(b_copy_png)
        cust_v.addLayout(row1)

        # row 2 — separate X and Y zoom controls
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("X  t₀:"))
        self.wf_zoom_t0 = QDoubleSpinBox()
        self.wf_zoom_t0.setRange(-1e6, 1e6); self.wf_zoom_t0.setDecimals(6)
        self.wf_zoom_t0.setSingleStep(1e-4); self.wf_zoom_t0.setFixedWidth(150)
        row2.addWidget(self.wf_zoom_t0)
        row2.addWidget(QLabel("t₁:"))
        self.wf_zoom_t1 = QDoubleSpinBox()
        self.wf_zoom_t1.setRange(-1e6, 1e6); self.wf_zoom_t1.setDecimals(6)
        self.wf_zoom_t1.setValue(1e-3); self.wf_zoom_t1.setSingleStep(1e-4)
        self.wf_zoom_t1.setFixedWidth(150)
        row2.addWidget(self.wf_zoom_t1)
        b_xzoom = _btn("Apply X", "orange")
        b_xzoom.clicked.connect(self._apply_wf_xrange)
        row2.addWidget(b_xzoom)
        row2.addSpacing(20)
        row2.addWidget(QLabel("Y  v₀:"))
        self.wf_zoom_v0 = QDoubleSpinBox()
        self.wf_zoom_v0.setRange(-1e6, 1e6); self.wf_zoom_v0.setDecimals(4)
        self.wf_zoom_v0.setSingleStep(0.1); self.wf_zoom_v0.setFixedWidth(150)
        row2.addWidget(self.wf_zoom_v0)
        row2.addWidget(QLabel("v₁:"))
        self.wf_zoom_v1 = QDoubleSpinBox()
        self.wf_zoom_v1.setRange(-1e6, 1e6); self.wf_zoom_v1.setDecimals(4)
        self.wf_zoom_v1.setValue(5.0); self.wf_zoom_v1.setSingleStep(0.1)
        self.wf_zoom_v1.setFixedWidth(150)
        row2.addWidget(self.wf_zoom_v1)
        b_yzoom = _btn("Apply Y", "orange")
        b_yzoom.clicked.connect(self._apply_wf_yrange)
        row2.addWidget(b_yzoom)
        row2.addStretch()
        b_rzoom = _btn("Reset Zoom")
        b_rzoom.clicked.connect(self._reset_wf_zoom)
        row2.addWidget(b_rzoom)
        cust_v.addLayout(row2)

        # row 3 — cursor controls
        row3 = QHBoxLayout()
        self.wf_cur_x_chk = QCheckBox("Cursors X  (Δt / freq)")
        self.wf_cur_y_chk = QCheckBox("Cursors Y  (ΔV)")
        self.wf_cur_lbl   = QLabel("—")
        self.wf_cur_lbl.setStyleSheet(
            "font-family:'Courier New';font-size:9pt;color:#a6e3a1;")
        row3.addWidget(self.wf_cur_x_chk)
        row3.addWidget(self.wf_cur_y_chk)
        row3.addSpacing(12)
        row3.addWidget(self.wf_cur_lbl)
        row3.addStretch()
        cust_v.addLayout(row3)

        lay.addWidget(cust_grp)

        # ── Plot widget ───────────────────────────────────────────────────────
        self._wf_cx: list = []          # active X InfiniteLines (0 or 2)
        self._wf_cy: list = []          # active Y InfiniteLines (0 or 2)
        self._wf_split_plots: list = [] # PlotItems in split view

        # page 0 — combined
        self.wf_plot = pg.PlotWidget(title="MDO34 Waveform — no data yet")
        self.wf_plot.setLabel('left',   'Voltage', units='V')
        self.wf_plot.setLabel('bottom', 'Time',    units='s')
        self.wf_plot.showGrid(x=True, y=True, alpha=0.3)
        self.wf_plot.setMinimumHeight(80)
        self._wf_legend = self.wf_plot.addLegend(offset=(10, 10))
        self._wf_legend.anchor(itemPos=(0, 0), parentPos=(0, 0), offset=(5, 5))
        self._wf_curves: dict = {}
        for i, ch in enumerate(MDO34Driver.CHANNELS):
            c = self.wf_plot.plot([], [], pen=pg.mkPen(_ch_colors[i], width=2), name=ch)
            c.setVisible(i == 0)
            self._wf_curves[ch] = c

        # page 1 — split (one sub-plot per channel)
        self._wf_split_w = pg.GraphicsLayoutWidget()

        # stacked container — export always grabs this widget
        self._wf_plot_container = QStackedWidget()
        self._wf_plot_container.addWidget(self.wf_plot)   # index 0
        self._wf_plot_container.addWidget(self._wf_split_w)  # index 1

        self.wf_grid_chk.stateChanged.connect(
            lambda s: self.wf_plot.showGrid(x=bool(s), y=bool(s), alpha=0.3))
        self.wf_legend_chk.stateChanged.connect(
            lambda s: self._wf_legend.setVisible(bool(s)))
        for ch, chk in self._wf_ch_chks.items():
            chk.stateChanged.connect(
                lambda s, _ch=ch: self._wf_curves[_ch].setVisible(bool(s)))
        for ch, ne in self._wf_ch_names.items():
            ne.textChanged.connect(
                lambda txt, _ch=ch: self._update_wf_legend_name(_ch, txt))
        self.wf_split_chk.stateChanged.connect(self._toggle_split_mode)
        self.wf_cur_x_chk.stateChanged.connect(self._toggle_cur_x)
        self.wf_cur_y_chk.stateChanged.connect(self._toggle_cur_y)

        lay.addWidget(self._wf_plot_container, stretch=1)
        self.wf_status = StatusLabel(""); lay.addWidget(self.wf_status)
        return w

    def _update_wf_legend_name(self, ch: str, name: str):
        """Live-rename a channel's legend entry when the name field changes."""
        curve = self._wf_curves.get(ch)
        if curve is None:
            return
        for sample, label in self._wf_legend.items:
            if getattr(sample, 'item', None) is curve:
                label.setText(name or ch)
                break

    def _toggle_split_mode(self, state: int):
        """Switch between combined (index 0) and split (index 1) plot views."""
        is_split = bool(state)
        self._wf_plot_container.setCurrentIndex(1 if is_split else 0)
        self.wf_cur_x_chk.setEnabled(not is_split)
        self.wf_cur_y_chk.setEnabled(not is_split)
        if is_split and self._wf_data:
            self._rebuild_split_plots()

    def _rebuild_split_plots(self):
        """Clear and repopulate the GraphicsLayoutWidget with one plot per channel."""
        _ch_colors = ['#89b4fa', '#a6e3a1', '#f38ba8', '#fab387']
        self._wf_split_w.clear()
        self._wf_split_plots = []
        chs = [ch for ch in MDO34Driver.CHANNELS if ch in self._wf_data]
        link_plot = None
        for row_idx, ch in enumerate(chs):
            t, v = self._wf_data[ch]
            color = _ch_colors[MDO34Driver.CHANNELS.index(ch)]
            name  = self._wf_ch_names[ch].text().strip() or ch
            p = self._wf_split_w.addPlot(row=row_idx, col=0, title=name)
            p.setLabel('left', 'V', units='V')
            if row_idx == len(chs) - 1:
                p.setLabel('bottom', 'Time', units='s')
            p.showGrid(x=True, y=True, alpha=0.3)
            p.plot(t, v, pen=pg.mkPen(color, width=2))
            if link_plot is None:
                link_plot = p
            else:
                p.setXLink(link_plot)
            self._wf_split_plots.append(p)

    def _toggle_cur_x(self, state: int):
        """Add or remove the two vertical (time) cursor lines."""
        for line in self._wf_cx:
            self.wf_plot.removeItem(line)
        self._wf_cx = []
        if state:
            vb = self.wf_plot.getPlotItem().getViewBox()
            xr = vb.viewRange()[0]
            span = xr[1] - xr[0]
            for frac, col, lbl in [(0.33, '#f38ba8', 'X1'), (0.67, '#fab387', 'X2')]:
                line = pg.InfiniteLine(
                    pos=xr[0] + span * frac, angle=90, movable=True,
                    pen=pg.mkPen(col, width=1, style=Qt.PenStyle.DashLine),
                    label=f'{lbl}={{value:.4g}}',
                    labelOpts={'color': col, 'position': 0.93, 'fill': (0, 0, 0, 100)})
                line.sigPositionChanged.connect(self._update_cursor_readout)
                self.wf_plot.addItem(line)
                self._wf_cx.append(line)
        self._update_cursor_readout()

    def _toggle_cur_y(self, state: int):
        """Add or remove the two horizontal (voltage) cursor lines."""
        for line in self._wf_cy:
            self.wf_plot.removeItem(line)
        self._wf_cy = []
        if state:
            vb = self.wf_plot.getPlotItem().getViewBox()
            yr = vb.viewRange()[1]
            span = yr[1] - yr[0]
            for frac, col, lbl in [(0.33, '#89b4fa', 'Y1'), (0.67, '#a6e3a1', 'Y2')]:
                line = pg.InfiniteLine(
                    pos=yr[0] + span * frac, angle=0, movable=True,
                    pen=pg.mkPen(col, width=1, style=Qt.PenStyle.DashLine),
                    label=f'{lbl}={{value:.4g}}',
                    labelOpts={'color': col, 'position': 0.05, 'fill': (0, 0, 0, 100)})
                line.sigPositionChanged.connect(self._update_cursor_readout)
                self.wf_plot.addItem(line)
                self._wf_cy.append(line)
        self._update_cursor_readout()

    def _update_cursor_readout(self, _=None):
        """Recompute Δt/freq and ΔV from current cursor positions."""
        parts = []
        if len(self._wf_cx) == 2:
            x1, x2 = self._wf_cx[0].value(), self._wf_cx[1].value()
            dt = x2 - x1
            freq_str = f"   f:{1/abs(dt):.4g} Hz" if abs(dt) > 1e-15 else ""
            parts.append(f"X1:{x1:.4g} s   X2:{x2:.4g} s   Δt:{dt:.4g} s{freq_str}")
        if len(self._wf_cy) == 2:
            y1, y2 = self._wf_cy[0].value(), self._wf_cy[1].value()
            parts.append(f"Y1:{y1:.4g} V   Y2:{y2:.4g} V   ΔV:{y2-y1:.4g} V")
        self.wf_cur_lbl.setText("     ".join(parts) if parts else "—")

    def _apply_wf_xrange(self):
        """Apply X zoom from spinboxes to whichever plot view is active."""
        x0, x1 = self.wf_zoom_t0.value(), self.wf_zoom_t1.value()
        self.wf_plot.setXRange(x0, x1, padding=0)
        for p in self._wf_split_plots:
            p.setXRange(x0, x1, padding=0)

    def _apply_wf_yrange(self):
        """Apply Y zoom from spinboxes — combined only (split tracks per-channel)."""
        y0, y1 = self.wf_zoom_v0.value(), self.wf_zoom_v1.value()
        if not self.wf_split_chk.isChecked():
            self.wf_plot.setYRange(y0, y1, padding=0)
        else:
            for p in self._wf_split_plots:
                p.setYRange(y0, y1, padding=0)

    def _set_ch_label_on_scope(self, ch: str):
        """Push the custom app label to the scope via :CH{n}:LABel:NAME."""
        name = self._wf_ch_names[ch].text().strip() or ch
        n    = ch[2]                            # '1' from 'CH1'
        self._mdo_write(f'CH{n}:LABel:NAME "{name}"')
        self.wf_status.ok(f"Scope label set: CH{n} → {name!r}")

    def _capture_waveform(self):
        chs = [ch for ch, chk in self._wf_ch_chks.items() if chk.isChecked()]
        if not chs:
            self.wf_status.err("Select at least one channel."); return
        pts = self.wf_pts.value()
        _ch_colors = ['#89b4fa', '#a6e3a1', '#f38ba8', '#fab387']
        self.wf_status.ok(f"Capturing {', '.join(chs)}…")

        def job():
            results = {}
            for ch in chs:
                t, v = self._conn.run(lambda drv, _ch=ch: drv.capture_waveform(_ch, pts))
                results[ch] = (t, v)
            return results

        def _on_done(result, err):
            if err:
                self.wf_status.err(err); return
            self._wf_data = {}
            for ch_c in self._wf_curves.values():
                ch_c.setData([], [])
            info = []
            t_first = None
            for ch, (t, v) in result.items():
                if t is None or v is None:
                    continue
                self._wf_data[ch] = (t, v)
                i = MDO34Driver.CHANNELS.index(ch)
                self._wf_curves[ch].setData(t, v)
                self._wf_curves[ch].setVisible(True)
                if t_first is None and len(t) > 1:
                    t_first = t
                name = self._wf_ch_names[ch].text() or ch
                span = f"{t[-1]-t[0]:.3g}s" if len(t) > 1 else "—"
                info.append(f"{name}: {len(t)}pts {v.min():.3g}…{v.max():.3g}V ({span})")
            if self.wf_autoscale_chk.isChecked():
                self.wf_plot.autoRange()
            vb = self.wf_plot.getPlotItem().getViewBox()
            xr, yr = vb.viewRange()
            self._wf_home_range = (xr, yr)
            self.wf_zoom_t0.setValue(xr[0]); self.wf_zoom_t1.setValue(xr[1])
            self.wf_zoom_v0.setValue(yr[0]); self.wf_zoom_v1.setValue(yr[1])
            self.wf_plot.setTitle("  |  ".join(info) if info else "No data")
            if self.wf_split_chk.isChecked():
                self._rebuild_split_plots()
            self.wf_status.ok(f"OK — {len(self._wf_data)} channel(s) captured")

        wk = InstrumentWorker(job)
        wk.result.connect(_on_done)
        wk.start(); self._w = wk

    def _reset_wf_zoom(self):
        """Restore X and Y ranges to those recorded immediately after the last capture."""
        if self.wf_split_chk.isChecked():
            for p in self._wf_split_plots:
                p.autoRange()
            return
        if self._wf_home_range is None:
            self.wf_plot.autoRange(); return
        xr, yr = self._wf_home_range
        self.wf_plot.setXRange(*xr, padding=0)
        self.wf_plot.setYRange(*yr, padding=0)

    def _save_waveform_excel(self):
        if not self._wf_data:
            self.wf_status.err("No data — capture waveform first."); return
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Waveform", "waveform.xlsx", "Excel files (*.xlsx)")
        if not path: return
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active; ws.title = "Waveform"
            chs = list(self._wf_data.keys())
            ws.cell(1, 1, "Time (s)")
            for c_idx, ch in enumerate(chs, 2):
                col_name = self._wf_ch_names[ch].text().strip() or ch
                ws.cell(1, c_idx, f"{col_name} Voltage (V)")
            ref_t   = self._wf_data[chs[0]][0]
            max_len = max(len(self._wf_data[ch][0]) for ch in chs)
            for r in range(max_len):
                ws.cell(r + 2, 1, float(ref_t[r]) if r < len(ref_t) else "")
                for c_idx, ch in enumerate(chs, 2):
                    t_c, v_c = self._wf_data[ch]
                    ws.cell(r + 2, c_idx, float(v_c[r]) if r < len(v_c) else "")
            try:
                import io
                from openpyxl.drawing.image import Image as XlImage
                px = self._wf_plot_container.grab()
                buf = io.BytesIO(); px.save(buf, 'PNG'); buf.seek(0)
                ws_plot = wb.create_sheet("Plot")
                ws_plot.add_image(XlImage(buf), 'A1')
            except Exception:
                pass
            wb.save(path)
            self.wf_status.ok(f"Saved → {Path(path).name}")
        except ImportError:
            self.wf_status.err("openpyxl not installed — pip install openpyxl")
        except Exception as exc:
            self.wf_status.err(str(exc))

    def _copy_waveform_clipboard(self):
        from PyQt6.QtWidgets import QApplication
        px = self._wf_plot_container.grab()
        QApplication.clipboard().setPixmap(px)
        self.wf_status.ok("Plot copied to clipboard as PNG")

    # ── DVM tab ──────────────────────────────────────────────────────────────

    def _build_dvm_tab(self):
        w   = QWidget(); lay = QVBoxLayout(w)

        cfg = QGroupBox("Digital Voltmeter (DVM)")
        g   = QGridLayout(cfg)
        g.addWidget(QLabel("Source:"), 0, 0)
        self.dvm_src = QComboBox(); self.dvm_src.addItems(MDO34Driver.CHANNELS)
        g.addWidget(self.dvm_src, 0, 1)
        g.addWidget(QLabel("Mode:"), 1, 0)
        self.dvm_mode = QComboBox(); self.dvm_mode.addItems(MDO34Driver.DVM_MODES)
        g.addWidget(self.dvm_mode, 1, 1)
        b_dvm_cfg = _btn("Apply DVM Config", "blue")
        b_dvm_cfg.clicked.connect(self._apply_dvm)
        g.addWidget(b_dvm_cfg, 2, 0, 1, 2)
        lay.addWidget(cfg)

        rdout = QGroupBox("DVM Readout")
        rl    = QGridLayout(rdout)
        self._dvm_labels = {}
        for r, (key, lbl) in enumerate([
            ("value", "Value:"), ("freq",  "Frequency:"),
            ("avg",   "Avg:"),   ("min",   "Min:"), ("max",   "Max:"),
        ]):
            rl.addWidget(QLabel(lbl), r, 0)
            lv = QLabel("---")
            lv.setStyleSheet("font-family:'Courier New'; color:#a6e3a1;")
            rl.addWidget(lv, r, 1)
            self._dvm_labels[key] = lv
        b_dvm_read = _btn("Read DVM", "blue")
        b_dvm_read.clicked.connect(self._read_dvm)
        rl.addWidget(b_dvm_read, len(self._dvm_labels), 0, 1, 2)
        lay.addWidget(rdout)

        self.dvm_status = StatusLabel("")
        lay.addWidget(self.dvm_status)
        lay.addStretch()
        return w

    def _apply_dvm(self):
        mode = self.dvm_mode.currentText()
        src  = self.dvm_src.currentText()
        def job():
            available = self._conn.run(lambda drv: drv.is_dvm_available())
            if not available:
                return "DVM option not installed on this unit"
            return self._conn.run(lambda drv: drv.dvm_configure(mode, src))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.dvm_status.ok(v) if not e else self.dvm_status.err(e)))
        w.start(); self._w = w

    def _read_dvm(self):
        def job():
            available = self._conn.run(lambda drv: drv.is_dvm_available())
            if not available:
                return {"_unavailable": True}
            return self._conn.run(lambda drv: drv.dvm_read_all())
        def _on_done(result, err):
            if err:
                self.dvm_status.err(err); return
            if isinstance(result, dict):
                if result.get("_unavailable"):
                    self.dvm_status.err("DVM option not installed on this unit")
                    return
                for key, lv in self._dvm_labels.items():
                    v = result.get(key, float('nan'))
                    lv.setText(f"{v:.6g}" if v == v else "---")
                self.dvm_status.ok("OK")
        w = InstrumentWorker(job)
        w.result.connect(_on_done)
        w.start(); self._w = w

    # ── Measurement Slots tab (dynamic, 1–8 tiles) ───────────────────────────

    def _build_meas_slots_tab(self):
        from PyQt6.QtWidgets import QScrollArea
        w = QWidget(); outer_lay = QVBoxLayout(w)

        bar = QHBoxLayout()
        b_add   = _btn("＋  Add Slot", "green")
        b_stats = _btn("Enable Statistics", "blue")
        b_clr   = _btn("Reset Statistics")
        b_rdall = _btn("Read All", "blue")
        bar.addWidget(b_add); bar.addWidget(b_stats)
        bar.addWidget(b_clr); bar.addStretch(); bar.addWidget(b_rdall)
        outer_lay.addLayout(bar)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        self._slots_container = QWidget()
        self._slots_grid = QGridLayout(self._slots_container)
        self._slots_grid.setSpacing(6)
        scroll.setWidget(self._slots_container)
        outer_lay.addWidget(scroll, stretch=1)

        self._meas_tiles: list = []
        self._slot_ch   = {}
        self._slot_type = {}
        self._slot_val  = {}
        self._slot_mean = {}
        self._slot_min  = {}
        self._slot_max  = {}
        self._slot_std  = {}

        self.slots_status = StatusLabel(""); outer_lay.addWidget(self.slots_status)

        for s in range(1, 5):
            self._add_meas_tile(s)

        b_add  .clicked.connect(self._add_meas_tile_auto)
        b_stats.clicked.connect(lambda: self._mdo_write("MEASUrement:STATIstics:MODe ON"))
        b_clr  .clicked.connect(lambda: self._mdo_write("MEASUrement:STATIstics CLEar"))
        b_rdall.clicked.connect(self._read_all_slots)
        return w

    def _add_meas_tile_auto(self):
        if len(self._meas_tiles) >= 8:
            self.slots_status.err("Maximum 8 slots."); return
        used = {t['slot'] for t in self._meas_tiles}
        for s in range(1, 9):
            if s not in used:
                self._add_meas_tile(s); return

    def _add_meas_tile(self, slot: int):
        if len(self._meas_tiles) >= 8:
            return
        _def_chs = ["CH1", "CH2", "CH3", "CH4", "CH1", "CH2", "CH3", "CH4"]
        idx = len(self._meas_tiles)
        r, c = idx // 2, idx % 2

        grp = QGroupBox(f"MEAS{slot}  —  {_def_chs[idx]}")
        g = QGridLayout(grp); g.setVerticalSpacing(3)

        src_h = QHBoxLayout()
        ch_cb = QComboBox(); ch_cb.addItems(MDO34Driver.CHANNELS)
        ch_cb.setCurrentText(_def_chs[idx])
        b_rm = QPushButton("✕"); b_rm.setFixedSize(22, 22)
        b_rm.setStyleSheet(
            "QPushButton{background:#f38ba8;color:#1e1e2e;border:none;"
            "border-radius:3px;font-weight:bold;}")
        src_h.addWidget(QLabel("Src:")); src_h.addWidget(ch_cb, 1); src_h.addWidget(b_rm)
        g.addLayout(src_h, 0, 0, 1, 2)

        t_cb = QComboBox()
        for lbl_t, val_t in MDO34Driver.MEAS_TYPES: t_cb.addItem(lbl_t, val_t)
        g.addWidget(QLabel("Type:"), 1, 0); g.addWidget(t_cb, 1, 1)

        btn_h = QHBoxLayout()
        b_ap = _btn("Apply", "blue"); b_ap.setFixedHeight(24)
        b_rd = _btn("Read");          b_rd.setFixedHeight(24)
        btn_h.addWidget(b_ap); btn_h.addWidget(b_rd)
        g.addLayout(btn_h, 2, 0, 1, 2)

        val_lbs = {}
        for r_i, (key, lbl_t) in enumerate([
            ('val',  "Value:"), ('mean', "Mean:"),
            ('min',  "Min:"),   ('max',  "Max:"), ('std', "StdDev:"),
        ], start=3):
            g.addWidget(QLabel(lbl_t), r_i, 0)
            lv = QLabel("---")
            lv.setStyleSheet("font-family:'Courier New'; font-size:9pt; color:#cba6f7;")
            g.addWidget(lv, r_i, 1)
            val_lbs[key] = lv

        tile = {'slot': slot, 'grp': grp, 'ch_cb': ch_cb,
                'type_cb': t_cb, 'labels': val_lbs}
        self._meas_tiles.append(tile)

        self._slot_ch[slot]   = ch_cb
        self._slot_type[slot] = t_cb
        self._slot_val[slot]  = val_lbs['val']
        self._slot_mean[slot] = val_lbs['mean']
        self._slot_min[slot]  = val_lbs['min']
        self._slot_max[slot]  = val_lbs['max']
        self._slot_std[slot]  = val_lbs['std']

        ch_cb.currentTextChanged.connect(
            lambda txt, _g=grp, _s=slot: _g.setTitle(f"MEAS{_s}  —  {txt}"))
        b_ap.clicked.connect(partial(self._apply_meas_slot, slot))
        b_rd.clicked.connect(partial(self._read_meas_slot, slot))
        b_rm.clicked.connect(lambda _chk=None, _t=tile: self._remove_meas_tile(_t))

        self._slots_grid.addWidget(grp, r, c)

    def _remove_meas_tile(self, tile: dict):
        if len(self._meas_tiles) <= 1:
            self.slots_status.err("Must keep at least one slot."); return
        slot = tile['slot']
        self._slots_grid.removeWidget(tile['grp'])
        tile['grp'].setParent(None); tile['grp'].deleteLater()
        self._meas_tiles.remove(tile)
        for d in (self._slot_ch, self._slot_type, self._slot_val,
                  self._slot_mean, self._slot_min, self._slot_max, self._slot_std):
            d.pop(slot, None)
        for t in self._meas_tiles:
            self._slots_grid.removeWidget(t['grp'])
        for n, t in enumerate(self._meas_tiles):
            self._slots_grid.addWidget(t['grp'], n // 2, n % 2)

    def _apply_meas_slot(self, slot: int):
        ch   = self._slot_ch[slot].currentText()
        mtyp = self._slot_type[slot].currentData()
        def job():
            return self._conn.run(lambda drv: (
                drv.setup_meas_slot(slot, ch, mtyp) or f"MEAS{slot}: {ch} {mtyp}"))
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: (
            self.slots_status.ok(v) if not e else self.slots_status.err(e)))
        w.start(); self._w = w

    def _read_meas_slot(self, slot: int):
        def job():
            def fn(drv):
                val   = drv.read_meas_slot(slot)
                stats = drv.read_meas_stats(slot)
                return (val, stats)
            return self._conn.run(fn)
        def _on_done(result, err):
            if err:
                self.slots_status.err(err); return
            if result:
                val, stats = result
                fmt = lambda v: f"{v:.6g}" if v == v else "---"
                self._slot_val[slot].setText(fmt(val))
                self._slot_mean[slot].setText(fmt(stats.get('mean', float('nan'))))
                self._slot_min[slot].setText(fmt(stats.get('min',  float('nan'))))
                self._slot_max[slot].setText(fmt(stats.get('max',  float('nan'))))
                self._slot_std[slot].setText(fmt(stats.get('stddev', float('nan'))))
                self.slots_status.ok(f"MEAS{slot} read")
        w = InstrumentWorker(job)
        w.result.connect(_on_done)
        w.start(); self._w = w

    def _read_all_slots(self):
        self._all_slot_workers = []          # keep refs — prevents GC while workers run
        for tile in self._meas_tiles:
            self._read_meas_slot(tile['slot'])
            self._all_slot_workers.append(self._w)


# ─────────────────────────────────────────────────────────────────────────────
# Yokogawa WT5000  (Precision Power Analyzer)
# ─────────────────────────────────────────────────────────────────────────────

class WT5000Panel(QWidget):
    """
    Full Yokogawa WT5000 control:
    • Input Setup:  per-element V/I range, sync source, line & BW filters,
                    system wiring (1P2W … 3P4W)
    • Averaging:    Linear / Exponential, count 1–256
    • Integration:  Normal / Continuous / Manual, H:M:S timer,
                    Start / Stop / Reset with live-state polling
    • Measurements: any subset of 14 element + 4 sigma quantities via
                    NUMeric list, timestamped log output
    • Real-Time Plot: V, A, W, PF for a selected element
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wt_conn = shared_driver(
            'wt5000', lambda: WT5000Driver(ADDR['wt5000']))
        lay  = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_setup_tab(),   "Input Setup")
        tabs.addTab(self._build_avg_int_tab(), "Averaging & Integration")
        tabs.addTab(self._build_meas_tab(),    "Measurements")
        tabs.addTab(self._build_plot_tab(),    "Real-Time Plot")
        lay.addWidget(tabs)

    # ── shared helper ────────────────────────────────────────────────────────

    def _wt_run(self, fn, msg: str, status, buttons: list):
        def job():
            return self._wt_conn.run(fn)
        self._w = _run_panel_cmd(job, status, buttons, msg)

    # ── Tab 1: Input Setup ───────────────────────────────────────────────────

    def _build_setup_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)

        wg = QGroupBox("Element Wiring")
        wr = QHBoxLayout(wg)
        self.wiring_elem = QSpinBox()
        self.wiring_elem.setRange(1, 7); self.wiring_elem.setValue(1)
        self.wiring_sel = QComboBox()
        self.wiring_sel.addItems(WT5000Driver.WIRINGS)
        b_wiring = _btn("Apply Wiring", "blue")
        b_wiring.clicked.connect(self._apply_wiring)
        wr.addWidget(QLabel("Element:")); wr.addWidget(self.wiring_elem)
        wr.addWidget(QLabel("Wiring:")); wr.addWidget(self.wiring_sel)
        wr.addWidget(b_wiring); wr.addStretch()
        lay.addWidget(wg)

        eg = QGroupBox("Element Configuration")
        g  = QGridLayout(eg); row = 0

        g.addWidget(QLabel("Element (1–7):"), row, 0)
        self.elem_sel = QSpinBox()
        self.elem_sel.setRange(1, 7); self.elem_sel.setValue(1)
        g.addWidget(self.elem_sel, row, 1); row += 1

        g.addWidget(QLabel("Voltage Range:"), row, 0)
        self.v_range = QComboBox()
        self.v_range.addItems(WT5000Driver.V_RANGES)
        g.addWidget(self.v_range, row, 1); row += 1

        g.addWidget(QLabel("Current Range:"), row, 0)
        self.i_range = QComboBox()
        self.i_range.addItems(WT5000Driver.I_RANGES)
        g.addWidget(self.i_range, row, 1); row += 1

        g.addWidget(QLabel("Sync Source:"), row, 0)
        self.sync_sel = QComboBox()
        self.sync_sel.addItems(WT5000Driver.SYNC_SRC)
        g.addWidget(self.sync_sel, row, 1); row += 1

        g.addWidget(QLabel("Freq Bandwidth:"), row, 0)
        self.freq_filt = QComboBox()
        self.freq_filt.addItems(WT5000Driver.FREQ_FILT)
        g.addWidget(self.freq_filt, row, 1); row += 1

        self.line_filt = QCheckBox("Line Filter  (50/60 Hz notch)")
        g.addWidget(self.line_filt, row, 0, 1, 2); row += 1

        b_elem = _btn("Apply Element Config", "blue")
        b_idn  = _btn("Read IDN")
        b_rst  = _btn("Reset (*RST)")
        b_elem.clicked.connect(self._apply_element)
        b_idn.clicked.connect(self._read_idn)
        b_rst.clicked.connect(self._reset)
        g.addWidget(b_elem, row, 0, 1, 2); row += 1
        g.addWidget(b_idn,  row, 0); g.addWidget(b_rst, row, 1); row += 1

        self.setup_status = StatusLabel("")
        g.addWidget(self.setup_status, row, 0, 1, 2)
        self._setup_btns = [b_wiring, b_elem, b_idn, b_rst]
        lay.addWidget(eg)
        lay.addStretch()
        return w

    # ── Tab 2: Averaging & Integration ───────────────────────────────────────

    def _build_avg_int_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)

        ag = QGroupBox("Averaging")
        gg = QGridLayout(ag); r = 0
        self.avg_en = QCheckBox("Enable Averaging")
        gg.addWidget(self.avg_en, r, 0, 1, 2); r += 1

        gg.addWidget(QLabel("Type:"), r, 0)
        self.avg_type = QComboBox()
        self.avg_type.addItems(WT5000Driver.AVG_TYPES)
        gg.addWidget(self.avg_type, r, 1); r += 1

        gg.addWidget(QLabel("Count (1–256):"), r, 0)
        self.avg_count = QSpinBox()
        self.avg_count.setRange(1, 256); self.avg_count.setValue(8)
        gg.addWidget(self.avg_count, r, 1); r += 1

        b_avg = _btn("Apply Averaging", "blue")
        b_avg.clicked.connect(self._apply_averaging)
        gg.addWidget(b_avg, r, 0, 1, 2)
        lay.addWidget(ag)

        ig = QGroupBox("Integration")
        gi = QGridLayout(ig); r = 0

        gi.addWidget(QLabel("Mode:"), r, 0)
        self.int_mode = QComboBox()
        self.int_mode.addItems(WT5000Driver.INT_MODES)
        gi.addWidget(self.int_mode, r, 1); r += 1

        gi.addWidget(QLabel("Timer  (H : M : S):"), r, 0)
        tw = QWidget(); tl = QHBoxLayout(tw); tl.setContentsMargins(0, 0, 0, 0)
        self.int_h = QSpinBox(); self.int_h.setRange(0, 9999); self.int_h.setSuffix(" h")
        self.int_m = QSpinBox(); self.int_m.setRange(0, 59);   self.int_m.setSuffix(" m")
        self.int_s = QSpinBox(); self.int_s.setRange(0, 59);   self.int_s.setSuffix(" s")
        tl.addWidget(self.int_h); tl.addWidget(self.int_m); tl.addWidget(self.int_s)
        gi.addWidget(tw, r, 1); r += 1

        b_int_cfg = _btn("Apply Mode & Timer", "blue")
        b_int_cfg.clicked.connect(self._apply_integration_cfg)
        gi.addWidget(b_int_cfg, r, 0, 1, 2); r += 1

        btn_row = QHBoxLayout()
        b_start = _btn("▶  Start", "green")
        b_stop  = _btn("■  Stop",  "red")
        b_irst  = _btn("↺  Reset")
        b_start.clicked.connect(self._int_start)
        b_stop.clicked.connect(self._int_stop)
        b_irst.clicked.connect(self._int_reset)
        btn_row.addWidget(b_start); btn_row.addWidget(b_stop)
        btn_row.addWidget(b_irst);  btn_row.addStretch()
        gi.addLayout(btn_row, r, 0, 1, 2); r += 1

        self.int_state_lbl = QLabel("State: —")
        self.int_state_lbl.setStyleSheet("color:#f9e2af; font-size:9pt;")
        gi.addWidget(self.int_state_lbl, r, 0, 1, 2); r += 1

        self.avg_int_status = StatusLabel("")
        gi.addWidget(self.avg_int_status, r, 0, 1, 2)
        self._avg_int_btns = [b_avg, b_int_cfg, b_start, b_stop, b_irst]
        lay.addWidget(ig)

        self._int_poll = QTimer(self)
        self._int_poll.setInterval(2000)
        self._int_poll.timeout.connect(self._poll_int_state)
        self._int_poll.start()

        lay.addStretch()
        return w

    # ── Tab 3: Measurements ──────────────────────────────────────────────────

    def _build_meas_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("Element (1–7):"))
        self.meas_elem = QSpinBox()
        self.meas_elem.setRange(1, 7); self.meas_elem.setValue(1)
        top.addWidget(self.meas_elem); top.addStretch()
        lay.addLayout(top)

        items_grp = QGroupBox("Element Quantities to Read")
        ig = QGridLayout(items_grp)
        self._meas_checks = {}
        for i, (func, label) in enumerate(WT5000Driver.ELEM_ITEMS):
            cb = QCheckBox(label)
            cb.setChecked(func in ("URMS", "IRMS", "P", "LAMBda", "FU"))
            self._meas_checks[func] = cb
            ig.addWidget(cb, i // 2, i % 2)
        lay.addWidget(items_grp)

        sigma_grp = QGroupBox("Sigma (Total) Quantities")
        sg = QGridLayout(sigma_grp)
        self._sigma_checks = {}
        for i, (func, label) in enumerate(WT5000Driver.SIGMA_ITEMS):
            cb = QCheckBox(label); cb.setChecked(True)
            self._sigma_checks[func] = cb
            sg.addWidget(cb, 0, i)
        lay.addWidget(sigma_grp)

        b_read = _btn("Read All Checked", "blue")
        b_read.clicked.connect(self._read_measurements)
        b_clr  = _btn("Clear Log")
        b_clr.clicked.connect(lambda: self.meas_out.clear())
        btn_row = QHBoxLayout()
        btn_row.addWidget(b_read); btn_row.addWidget(b_clr); btn_row.addStretch()
        lay.addLayout(btn_row)

        self.meas_out = QTextEdit(); self.meas_out.setReadOnly(True)
        self.meas_out.setMinimumHeight(160)
        lay.addWidget(self.meas_out)

        self.meas_status = StatusLabel("")
        lay.addWidget(self.meas_status)
        self._meas_btns = [b_read, b_clr]
        return w

    # ── Tab 4: Real-Time Plot ────────────────────────────────────────────────

    def _build_plot_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Element (1–7):"))
        self.plot_elem = QSpinBox()
        self.plot_elem.setRange(1, 7); self.plot_elem.setValue(1)
        self.plot_elem.valueChanged.connect(lambda: self._plot.clear())
        ctrl.addWidget(self.plot_elem); ctrl.addStretch()
        lay.addLayout(ctrl)

        self._plot = PlotPanel(
            channels=["V (V)", "A (A)", "W (W)", "PF"],
            y_label="Power Quantities"
        )
        self._plot.set_measure_fn(self._plot_meas_fn)
        lay.addWidget(self._plot)
        return w

    # ── Actions: Setup ───────────────────────────────────────────────────────

    def _apply_wiring(self):
        elem   = self.wiring_elem.value()
        wiring = self.wiring_sel.currentText()
        self._wt_run(lambda d: d.set_wiring(elem, wiring),
                     f"Setting E{elem} wiring {wiring}…",
                     self.setup_status, self._setup_btns)

    def _apply_element(self):
        e   = self.elem_sel.value()
        v_r = self.v_range.currentText()
        i_r = self.i_range.currentText()
        syn = self.sync_sel.currentText()
        lf  = self.line_filt.isChecked()
        ff  = self.freq_filt.currentText()
        self._wt_run(lambda d: d.configure_element(e, v_r, i_r, syn, lf, ff),
                     f"Configuring element {e}…",
                     self.setup_status, self._setup_btns)

    def _read_idn(self):
        self._wt_run(lambda d: d.identify(),
                     "Reading IDN…", self.setup_status, self._setup_btns)

    def _reset(self):
        self._wt_run(lambda d: (d.reset(), "Reset OK")[1],
                     "Resetting WT5000…", self.setup_status, self._setup_btns)

    # ── Actions: Averaging & Integration ────────────────────────────────────

    def _apply_averaging(self):
        en = self.avg_en.isChecked()
        t  = self.avg_type.currentText()
        c  = self.avg_count.value()
        self._wt_run(lambda d: d.configure_averaging(en, t, c),
                     "Configuring averaging…",
                     self.avg_int_status, self._avg_int_btns)

    def _apply_integration_cfg(self):
        mode = self.int_mode.currentText()
        h, m, s = self.int_h.value(), self.int_m.value(), self.int_s.value()
        self._wt_run(lambda d: d.configure_integration(mode, h, m, s),
                     "Configuring integration…",
                     self.avg_int_status, self._avg_int_btns)

    def _int_start(self):
        self._wt_run(lambda d: (d.start_integration(), "Integration started")[1],
                     "Starting…", self.avg_int_status, self._avg_int_btns)

    def _int_stop(self):
        self._wt_run(lambda d: (d.stop_integration(), "Integration stopped")[1],
                     "Stopping…", self.avg_int_status, self._avg_int_btns)

    def _int_reset(self):
        self._wt_run(lambda d: (d.reset_integration(), "Integration reset")[1],
                     "Resetting integration…", self.avg_int_status, self._avg_int_btns)

    def _poll_int_state(self):
        addr = ADDR.get('wt5000', '')
        if not addr or 'XXX' in addr:
            return
        if getattr(self, '_w_state', None) and self._w_state.isRunning():
            return
        def job():
            return self._wt_conn.run(lambda drv: drv.integration_state())
        w = InstrumentWorker(job)
        w.result.connect(lambda v, e: self.int_state_lbl.setText(
            f"State: {v}" if not e else "State: —"))
        w.start(); self._w_state = w

    # ── Actions: Measurements ────────────────────────────────────────────────

    def _read_measurements(self):
        elem        = self.meas_elem.value()
        elem_items  = [f for f, cb in self._meas_checks.items()  if cb.isChecked()]
        sigma_items = [f for f, cb in self._sigma_checks.items() if cb.isChecked()]

        for b in self._meas_btns: b.setEnabled(False)
        self.meas_status.info("⏳  Reading…")

        def job():
            def _do(drv):
                lines = []
                if elem_items:
                    result = drv.measure_element(elem, elem_items)
                    lines.append(f"── Element {elem} ──────────────────────")
                    for k, v in result.items():
                        lines.append(f"  {k:<24} {v:.7g}")
                if sigma_items:
                    drv.setup_numeric([(f,) for f in sigma_items])
                    vals    = drv.read_numeric()
                    lbl_map = dict(WT5000Driver.SIGMA_ITEMS)
                    lines.append("── Sigma (Total) ───────────────────────")
                    for f, v in zip(sigma_items, vals):
                        lines.append(f"  {lbl_map.get(f, f):<24} {v:.7g}")
                return "\n".join(lines)
            return self._wt_conn.run(_do)

        def done(val, err):
            for b in self._meas_btns: b.setEnabled(True)
            if err:
                self.meas_status.err(err)
            else:
                ts = time.strftime("%H:%M:%S")
                self.meas_out.append(f"\n[{ts}]\n{val}")
                self.meas_out.moveCursor(QTextCursor.MoveOperation.End)
                self.meas_status.ok("Read complete")
                self.meas_status.clear_after(3000)

        w = InstrumentWorker(job)
        w.result.connect(done)
        w.start(); self._w = w

    # ── Real-time plot measure fn ────────────────────────────────────────────

    def _plot_meas_fn(self):
        elem = self.plot_elem.value()
        def _do(drv):
            drv.setup_numeric([("URMS", elem), ("IRMS", elem),
                               ("P",    elem), ("LAMBda", elem)])
            vals = drv.read_numeric()
            return {"V (V)": vals[0], "A (A)": vals[1],
                    "W (W)": vals[2], "PF":    vals[3]}
        return self._wt_conn.run(_do)


# ═══════════════════════════════════════════════════════════════════════════════
#  Instrument Library Panel  —  Enphase testlib instrument selector
# ═══════════════════════════════════════════════════════════════════════════════

class InstrumentLibraryPanel(QWidget):
    """
    Displays every instrument available in enphase_equipment.
    Users enable instruments, enter VISA addresses, connect, and test them.
    Configuration is persisted to instrument_config.json.
    """

    instrument_connected    = pyqtSignal(str, object)  # key, driver
    instrument_disconnected = pyqtSignal(str)           # key

    CONFIG_FILE = APP_DIR / "instrument_config.json"

    COL_EN     = 0
    COL_CAT    = 1
    COL_MODEL  = 2
    COL_ADDR   = 3
    COL_STATUS = 4
    COL_CTRL   = 5

    _STATUS_COLORS = {
        "disconnected": "#6c7086",
        "connected":    "#a6e3a1",
        "error":        "#f38ba8",
        "busy":         "#fab387",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        from instrument_registry import CATEGORIES, flat_list
        self._all_entries   = flat_list()           # [{category, name, cls, module, key, tab_label}]
        self._config:  dict = {}                    # key → {addr, enabled}
        self._drivers: dict = {}                    # key → driver instance (connected)
        self._workers: list = []
        self._row_map: dict = {}                    # key → row index
        self._load_config()
        self._build_ui()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_config(self):
        try:
            if self.CONFIG_FILE.exists():
                self._config = json.loads(self.CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._config = {}

    def _save_config(self):
        try:
            self.CONFIG_FILE.write_text(
                json.dumps(self._config, indent=2), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        # ── toolbar ──────────────────────────────────────────────────────────
        tb = QHBoxLayout()
        tb.addWidget(QLabel("Search:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("filter by name…")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(200)
        self._search.textChanged.connect(self._apply_filter)
        tb.addWidget(self._search)

        tb.addWidget(QLabel("Category:"))
        self._cat_filter = QComboBox()
        self._cat_filter.setMinimumWidth(160)
        tb.addWidget(self._cat_filter)
        self._cat_filter.currentTextChanged.connect(self._apply_filter)

        tb.addStretch()
        btn_save = _btn("💾  Save Config", "blue")
        btn_save.setFixedWidth(130)
        btn_save.clicked.connect(self._save_config)
        tb.addWidget(btn_save)

        btn_dis_all = _btn("⏏  Disconnect All", "red")
        btn_dis_all.setFixedWidth(140)
        btn_dis_all.clicked.connect(self._disconnect_all)
        tb.addWidget(btn_dis_all)
        lay.addLayout(tb)

        # ── table ─────────────────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 6)
        self._tbl.setHorizontalHeaderLabels(
            ["En", "Category", "Model", "VISA Address", "Status", ""])
        hdr = self._tbl.horizontalHeader()
        hdr.setSectionResizeMode(self.COL_EN,     QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(self.COL_CAT,    QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(self.COL_MODEL,  QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(self.COL_ADDR,   QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(self.COL_CTRL,   QHeaderView.ResizeMode.Fixed)
        hdr.setStretchLastSection(False)
        self._tbl.setColumnWidth(self.COL_EN,     36)
        self._tbl.setColumnWidth(self.COL_CAT,   160)
        self._tbl.setColumnWidth(self.COL_MODEL, 240)
        self._tbl.setColumnWidth(self.COL_ADDR,  220)
        self._tbl.setColumnWidth(self.COL_STATUS, 110)
        self._tbl.setColumnWidth(self.COL_CTRL,   96)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tbl.itemSelectionChanged.connect(
            lambda: self._on_row_changed(self._tbl.currentRow()))
        lay.addWidget(self._tbl, stretch=4)

        # ── details pane ──────────────────────────────────────────────────────
        det = QGroupBox("Instrument Details")
        det_lay = QVBoxLayout(det)

        info_row = QHBoxLayout()
        self._det_name   = QLabel("—")
        self._det_name.setStyleSheet("font-weight:bold; font-size:11pt;")
        info_row.addWidget(self._det_name)
        info_row.addStretch()
        self._det_cat = QLabel("")
        self._det_cat.setStyleSheet("color:#89b4fa; font-size:9pt;")
        info_row.addWidget(self._det_cat)
        det_lay.addLayout(info_row)

        addr_row = QHBoxLayout()
        addr_row.addWidget(QLabel("Address:"))
        self._det_addr = QLineEdit()
        self._det_addr.setPlaceholderText("e.g. TCPIP::192.168.1.10::INSTR")
        addr_row.addWidget(self._det_addr)
        self._btn_connect = _btn("⚡  Connect", "green")
        self._btn_connect.setFixedWidth(110)
        self._btn_connect.clicked.connect(self._connect_selected)
        addr_row.addWidget(self._btn_connect)
        self._btn_disconnect = _btn("✕  Disconnect", "red")
        self._btn_disconnect.setFixedWidth(110)
        self._btn_disconnect.clicked.connect(self._disconnect_selected)
        addr_row.addWidget(self._btn_disconnect)
        det_lay.addLayout(addr_row)

        self._det_idn = QLabel("IDN: —")
        self._det_idn.setStyleSheet("color:#a6adc8; font-family:'Courier New'; font-size:9pt;")
        self._det_idn.setWordWrap(True)
        det_lay.addWidget(self._det_idn)

        self._det_ctrl = QWidget()
        self._det_ctrl_lay = QHBoxLayout(self._det_ctrl)
        self._det_ctrl_lay.setContentsMargins(0, 0, 0, 0)
        det_lay.addWidget(self._det_ctrl)

        lay.addWidget(det, stretch=1)

        # ── debug log ─────────────────────────────────────────────────────────
        dbg_hdr = QHBoxLayout()
        self._dbg_toggle = QPushButton("▼  Debug Log")
        self._dbg_toggle.setCheckable(True)
        self._dbg_toggle.setFixedWidth(120)
        self._dbg_toggle.setStyleSheet(
            "background:#181825;color:#a6adc8;border:1px solid #45475a;"
            "border-radius:3px;font-size:8pt;")
        self._dbg_toggle.toggled.connect(self._toggle_dbg_log)
        btn_clr_dbg = QPushButton("Clear")
        btn_clr_dbg.setFixedWidth(60)
        btn_clr_dbg.setStyleSheet(
            "background:#181825;color:#a6adc8;border:1px solid #45475a;"
            "border-radius:3px;font-size:8pt;")
        btn_clr_dbg.clicked.connect(lambda: self._dbg_view.clear())
        dbg_hdr.addWidget(self._dbg_toggle)
        dbg_hdr.addWidget(btn_clr_dbg)
        dbg_hdr.addStretch()
        lay.addLayout(dbg_hdr)

        self._dbg_view = QTextEdit()
        self._dbg_view.setReadOnly(True)
        self._dbg_view.setVisible(False)
        self._dbg_view.setMaximumHeight(160)
        self._dbg_view.setStyleSheet(
            "background:#0d1117; color:#a6e3a1; font-family:'Courier New';"
            "font-size:8pt; border:1px solid #45475a; border-radius:4px;")
        lay.addWidget(self._dbg_view)

        # ── populate ──────────────────────────────────────────────────────────
        self._populate_table()

    # ── Table population ──────────────────────────────────────────────────────

    def _populate_table(self):
        from instrument_registry import CATEGORIES
        cats = ["All"] + list(CATEGORIES.keys())
        self._cat_filter.blockSignals(True)
        self._cat_filter.clear()
        self._cat_filter.addItems(cats)
        self._cat_filter.blockSignals(False)

        self._tbl.setRowCount(len(self._all_entries))
        for row, entry in enumerate(self._all_entries):
            key = entry["key"]
            self._row_map[key] = row
            cfg = self._config.get(key, {})

            # Enable checkbox
            chk = QCheckBox()
            chk.setChecked(cfg.get("enabled", False))
            chk.setStyleSheet("margin-left:8px;")
            chk.stateChanged.connect(lambda st, k=key: self._on_enable_changed(k, st))
            self._tbl.setCellWidget(row, self.COL_EN, chk)

            # Category
            cat_it = QTableWidgetItem(entry["category"])
            cat_it.setFlags(cat_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            cat_it.setForeground(QColor("#89b4fa"))
            self._tbl.setItem(row, self.COL_CAT, cat_it)

            # Model
            mod_it = QTableWidgetItem(entry["name"])
            mod_it.setFlags(mod_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(row, self.COL_MODEL, mod_it)

            # Address (editable inline)
            addr_edit = QLineEdit(cfg.get("addr", ""))
            addr_edit.setPlaceholderText("VISA address…")
            addr_edit.editingFinished.connect(
                lambda k=key, w=addr_edit: self._on_addr_inline_changed(k, w.text()))
            self._tbl.setCellWidget(row, self.COL_ADDR, addr_edit)

            # Status
            st_it = QTableWidgetItem("○  Off")
            st_it.setFlags(st_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            st_it.setForeground(QColor(self._STATUS_COLORS["disconnected"]))
            self._tbl.setItem(row, self.COL_STATUS, st_it)

            # Connect button
            btn = QPushButton("Connect")
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                "background:#313244;color:#a6e3a1;border:1px solid #a6e3a1;"
                "border-radius:3px;font-size:8pt;")
            btn.clicked.connect(lambda _, k=key: self._connect_key(k))
            self._tbl.setCellWidget(row, self.COL_CTRL, btn)

        self._tbl.resizeRowsToContents()

    # ── Filter ────────────────────────────────────────────────────────────────

    def _apply_filter(self):
        cat_sel  = self._cat_filter.currentText()
        txt      = self._search.text().lower()
        for row, entry in enumerate(self._all_entries):
            hide = False
            if cat_sel != "All" and entry["category"] != cat_sel:
                hide = True
            if txt and txt not in entry["name"].lower() and txt not in entry["category"].lower():
                hide = True
            self._tbl.setRowHidden(row, hide)

    # ── Config callbacks ──────────────────────────────────────────────────────

    def _on_enable_changed(self, key: str, state: int):
        self._config.setdefault(key, {})["enabled"] = (state == Qt.CheckState.Checked.value)

    def _on_addr_inline_changed(self, key: str, addr: str):
        self._config.setdefault(key, {})["addr"] = addr.strip()
        row = self._row_map.get(key)
        if row is not None and self._tbl.currentRow() == row:
            self._det_addr.setText(addr.strip())

    # ── Details pane ──────────────────────────────────────────────────────────

    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self._all_entries):
            return
        entry = self._all_entries[row]
        key   = entry["key"]
        cfg   = self._config.get(key, {})

        self._det_name.setText(entry["name"])
        self._det_cat.setText(entry["category"])
        self._det_addr.blockSignals(True)
        self._det_addr.setText(cfg.get("addr", ""))
        self._det_addr.blockSignals(False)

        drv = self._drivers.get(key)
        if drv is not None:
            self._det_idn.setText(f"IDN: {getattr(drv, '_last_idn', 'connected')}")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
        else:
            self._det_idn.setText("IDN: —")
            self._btn_connect.setEnabled(True)
            self._btn_disconnect.setEnabled(False)

        self._det_addr.editingFinished.disconnect() if self._det_addr.receivers(
            self._det_addr.editingFinished) else None
        self._det_addr.editingFinished.connect(
            lambda k=key: self._on_addr_detail_changed(k))

        self._rebuild_ctrl_pane(entry, drv)

    def _on_addr_detail_changed(self, key: str):
        addr = self._det_addr.text().strip()
        self._config.setdefault(key, {})["addr"] = addr
        row = self._row_map.get(key)
        if row is not None:
            w = self._tbl.cellWidget(row, self.COL_ADDR)
            if isinstance(w, QLineEdit):
                w.setText(addr)

    # ── Connect / Disconnect ──────────────────────────────────────────────────

    def _connect_selected(self):
        row = self._tbl.currentRow()
        if row < 0: return
        self._connect_key(self._all_entries[row]["key"])

    def _dbg(self, msg: str):
        """Append a timestamped debug line to the log view."""
        from datetime import datetime
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}"
        self._dbg_view.append(line)
        cur = self._dbg_view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self._dbg_view.setTextCursor(cur)

    def _toggle_dbg_log(self, checked: bool):
        self._dbg_view.setVisible(checked)
        self._dbg_toggle.setText("▲  Debug Log" if checked else "▼  Debug Log")

    def _connect_key(self, key: str):
        from instrument_registry import find_by_key, load_driver
        entry = find_by_key(key)
        if entry is None:
            return
        addr = self._config.get(key, {}).get("addr", "").strip()
        if not addr:
            addr_widget = self._tbl.cellWidget(self._row_map.get(key, -1), self.COL_ADDR)
            if isinstance(addr_widget, QLineEdit):
                addr = addr_widget.text().strip()
        if not addr:
            QMessageBox.warning(self, "No Address",
                                f"Enter a VISA address for {entry['name']} first.")
            return

        self._config.setdefault(key, {})["addr"] = addr
        self._set_row_status(key, "busy", "⟳  Connecting…")
        self._dbg(f"CONNECT  [{key}]  {entry['name']}")
        self._dbg(f"  addr : {addr}")
        self._dbg(f"  cls  : {entry['module']}.{entry['cls']}")

        def job():
            drv = load_driver(entry, addr)
            idn = drv.ask("*IDN?").strip()
            return drv, idn

        _log.info("Library: connecting %s (%s) addr=%s", key, entry['name'], addr)
        w = InstrumentWorker(job)
        def done(val, err):
            if err:
                _log.error("Library: connect FAILED %s: %s", key, err)
                self._set_row_status(key, "error", f"\u2715  {err[:40]}")
                self._det_idn.setText(f"Error: {err[:80]}")
                self._dbg(f"  ERROR: {err}")
            else:
                drv, idn = val
                drv._last_idn = idn
                self._drivers[key] = drv
                _log.info("Library: connected %s IDN=%s", key, idn)
                self._set_row_status(key, "connected", "\u25cf  Connected")
                self._det_idn.setText(f"IDN: {idn}")
                self._btn_connect.setEnabled(False)
                self._btn_disconnect.setEnabled(True)
                self._dbg(f"  OK  IDN: {idn}")
                self.instrument_connected.emit(key, drv)
                cur_row = self._tbl.currentRow()
                if cur_row >= 0 and self._all_entries[cur_row]["key"] == key:
                    self._rebuild_ctrl_pane(entry, drv)
            self._workers.remove(w) if w in self._workers else None
        w.result.connect(done)
        w.start()
        self._workers.append(w)

    def _disconnect_selected(self):
        row = self._tbl.currentRow()
        if row < 0: return
        self._disconnect_key(self._all_entries[row]["key"])

    def _disconnect_key(self, key: str):
        _log.info("Library: disconnecting %s", key)
        drv = self._drivers.pop(key, None)
        if drv:
            try:
                drv.close()
                _log.info("Library: disconnected %s OK", key)
                self._dbg(f"DISCONNECT [{key}]  closed OK")
            except Exception as e:
                _log.warning("Library: disconnect %s close error: %s", key, e)
                self._dbg(f"DISCONNECT [{key}]  close error: {e}")
        else:
            self._dbg(f"DISCONNECT [{key}]  (was not connected)")
        self._set_row_status(key, "disconnected", "○  Off")
        self._det_idn.setText("IDN: —")
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._clear_ctrl_pane()
        self.instrument_disconnected.emit(key)

    def _disconnect_all(self):
        for key in list(self._drivers.keys()):
            self._disconnect_key(key)

    def connected_keys(self) -> set:
        """Return the set of instrument keys that are currently connected."""
        return set(self._drivers.keys())

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_row_status(self, key: str, state: str, text: str):
        row = self._row_map.get(key)
        if row is None:
            return
        it = self._tbl.item(row, self.COL_STATUS)
        if it:
            it.setText(text)
            it.setForeground(QColor(self._STATUS_COLORS.get(state, "#cdd6f4")))

    # ── Quick-control pane ────────────────────────────────────────────────────

    def _clear_ctrl_pane(self):
        while self._det_ctrl_lay.count():
            item = self._det_ctrl_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _rebuild_ctrl_pane(self, entry: dict, drv):
        self._clear_ctrl_pane()
        if drv is None:
            lbl = QLabel("Connect to enable controls")
            lbl.setStyleSheet("color:#6c7086; font-style:italic;")
            self._det_ctrl_lay.addWidget(lbl)
            return

        cat = entry["category"]

        if cat == "DC Source":
            self._det_ctrl_lay.addWidget(QLabel("V (V):"))
            v_spin = QDoubleSpinBox(); v_spin.setRange(0, 1000); v_spin.setDecimals(3)
            v_spin.setFixedWidth(90); self._det_ctrl_lay.addWidget(v_spin)
            self._det_ctrl_lay.addWidget(QLabel("I (A):"))
            i_spin = QDoubleSpinBox(); i_spin.setRange(0, 1000); i_spin.setDecimals(3)
            i_spin.setFixedWidth(90); self._det_ctrl_lay.addWidget(i_spin)
            b_on  = _btn("Output ON",  "green"); b_on.setFixedWidth(100)
            b_off = _btn("Output OFF", "red");   b_off.setFixedWidth(100)
            def _dc_on():
                try: drv.set_voltage(v_spin.value()); drv.set_current(i_spin.value()); drv.output_on()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            def _dc_off():
                try: drv.output_off()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            b_on.clicked.connect(_dc_on); b_off.clicked.connect(_dc_off)
            self._det_ctrl_lay.addWidget(b_on); self._det_ctrl_lay.addWidget(b_off)

        elif cat == "AC Source":
            self._det_ctrl_lay.addWidget(QLabel("V (Vrms):"))
            v_spin = QDoubleSpinBox(); v_spin.setRange(0, 500); v_spin.setValue(230)
            v_spin.setFixedWidth(90); self._det_ctrl_lay.addWidget(v_spin)
            self._det_ctrl_lay.addWidget(QLabel("Hz:"))
            f_spin = QDoubleSpinBox(); f_spin.setRange(40, 70); f_spin.setValue(50)
            f_spin.setDecimals(2); f_spin.setFixedWidth(80)
            self._det_ctrl_lay.addWidget(f_spin)
            b_on  = _btn("Output ON",  "green"); b_on.setFixedWidth(100)
            b_off = _btn("Output OFF", "red");   b_off.setFixedWidth(100)
            def _ac_on():
                try:
                    drv.set_voltage(v_spin.value())
                    drv.set_frequency(f_spin.value())
                    drv.output_on()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            def _ac_off():
                try: drv.output_off()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            b_on.clicked.connect(_ac_on); b_off.clicked.connect(_ac_off)
            self._det_ctrl_lay.addWidget(b_on); self._det_ctrl_lay.addWidget(b_off)

        elif cat in ("DC Load", "AC Load"):
            b_on  = _btn("Load ON",  "green"); b_on.setFixedWidth(100)
            b_off = _btn("Load OFF", "red");   b_off.setFixedWidth(100)
            def _load_on():
                try: drv.on()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            def _load_off():
                try: drv.off()
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            b_on.clicked.connect(_load_on); b_off.clicked.connect(_load_off)
            self._det_ctrl_lay.addWidget(b_on); self._det_ctrl_lay.addWidget(b_off)

        elif cat == "Thermal Chamber":
            self._det_ctrl_lay.addWidget(QLabel("Setpoint (°C):"))
            t_spin = QDoubleSpinBox(); t_spin.setRange(-70, 180); t_spin.setValue(25)
            t_spin.setFixedWidth(80); self._det_ctrl_lay.addWidget(t_spin)
            b_set = _btn("Set Temp", "blue"); b_set.setFixedWidth(90)
            def _set_temp():
                try: drv.set_temperature(t_spin.value())
                except Exception as e: QMessageBox.warning(self, "Error", str(e))
            b_set.clicked.connect(_set_temp)
            self._det_ctrl_lay.addWidget(b_set)
            b_read = _btn("Read Temp", "blue"); b_read.setFixedWidth(90)
            result_lbl = QLabel("—"); result_lbl.setStyleSheet("font-family:'Courier New';")
            def _read_temp():
                try:
                    st = drv.get_state()
                    result_lbl.setText(f"{st.get('IndicatedTemperature', '?')} °C")
                except Exception as e: result_lbl.setText(f"Err: {e}")
            b_read.clicked.connect(_read_temp)
            self._det_ctrl_lay.addWidget(b_read)
            self._det_ctrl_lay.addWidget(result_lbl)

        elif cat in ("DMM / Multimeter",):
            b_idn = _btn("Query IDN", "blue"); b_idn.setFixedWidth(100)
            result_lbl = QLabel("—"); result_lbl.setStyleSheet("font-family:'Courier New';")
            def _query():
                try: result_lbl.setText(drv.ask("*IDN?").strip())
                except Exception as e: result_lbl.setText(f"Err: {e}")
            b_idn.clicked.connect(_query)
            self._det_ctrl_lay.addWidget(b_idn)
            self._det_ctrl_lay.addWidget(result_lbl)

        else:
            b_idn = _btn("Query IDN", "blue"); b_idn.setFixedWidth(100)
            result_lbl = QLabel("—"); result_lbl.setStyleSheet("font-family:'Courier New';")
            def _query():
                try: result_lbl.setText(drv.ask("*IDN?").strip())
                except Exception as e: result_lbl.setText(f"Err: {e}")
            b_idn.clicked.connect(_query)
            self._det_ctrl_lay.addWidget(b_idn)
            self._det_ctrl_lay.addWidget(result_lbl)

        self._det_ctrl_lay.addStretch()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_driver(self, key: str):
        """Return the live driver for *key*, or None if not connected."""
        return self._drivers.get(key)

    def enabled_keys(self) -> list:
        """Return list of keys the user has enabled (checkbox ticked)."""
        return [k for k, v in self._config.items() if v.get("enabled")]


# ─────────────────────────────────────────────────────────────────────────────
# NGI N83624 Cell Simulator Panel — 17 active channels (ch9 = busbar)
# ─────────────────────────────────────────────────────────────────────────────

# Channel layout:
#   CH1–CH8  = cells 1–8
#   CH9      = busbar (middle channel)
#   CH10–CH17 = cells 9–16
# Total 17 channels out of 24 physical channels on the N83624.
# SCPI via TCP port 7000 (command board) controls all channels.

_NGI_CELL_CHANNELS = list(range(1, 18))   # 1-17
_NGI_BUSBAR_CH = 9
_NGI_MODE_MAP = {0: "Source", 1: "Charge", 3: "SOC", 128: "SEQ"}
_NGI_MODE_INV = {v: k for k, v in _NGI_MODE_MAP.items()}
_NGI_RANGE_MAP = {0: "High", 2: "Low", 3: "Auto"}


class NgiCellSimPanel(QWidget):
    """
    NGI N83624 24-Channel Cell Simulator control panel.

    Uses 17 of 24 physical channels to simulate a 16-cell battery pack
    with a busbar on channel 9.

    Tabs
    ----
    Overview   : compact grid — voltage / current setpoints, ON/OFF and
                 live readback for all 17 channels at a glance
    CH <n>     : per-channel detailed control (Source / Charge / SOC modes)
    Batch      : apply same voltage/current to all cell channels or all-on/off
    Protection : OVP / OCP / OPP for each channel
    System     : IP, baud, beeper, language, power-down save
    Plot       : real-time V / I trends for all 17 channels
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn = shared_resource('ngi')
        self._w = None  # keeps last worker alive

        lay = QVBoxLayout(self)
        tabs = QTabWidget()

        tabs.addTab(self._build_overview(),    "Overview")
        tabs.addTab(self._build_batch(),       "Batch")
        tabs.addTab(self._build_protection(),  "Protection")
        tabs.addTab(self._build_system(),      "System")
        tabs.addTab(self._build_plot(),        "Plot  V / I")

        # Per-channel detailed tabs (inside a nested QTabWidget)
        ch_tabs = QTabWidget()
        self._ch_widgets = {}
        for ch in _NGI_CELL_CHANNELS:
            label = f"CH{ch} — Busbar" if ch == _NGI_BUSBAR_CH else f"CH{ch}"
            w = self._make_ch_detail(ch)
            self._ch_widgets[ch] = w
            ch_tabs.addTab(w, label)
        tabs.addTab(ch_tabs, "Channels")

        lay.addWidget(tabs)

    # ── SCPI runner ───────────────────────────────────────────────────────────

    def _ngi_run(self, fn, msg, status, buttons):
        conn = self._conn
        def job():
            r = conn.run(fn)
            if isinstance(r, tuple):
                return r[1]
            return str(r) if r is not None else "Done"
        self._w = _run_panel_cmd(job, status, buttons, msg)

    # ══════════════════════════════════════════════════════════════════════════
    # Overview tab — compact grid of all 17 channels
    # ══════════════════════════════════════════════════════════════════════════

    def _build_overview(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        # ── Refresh + All ON/OFF ──────────────────────────────────────────
        ctrl = QHBoxLayout()
        b_read  = _btn("Read All", "blue")
        b_on    = _btn("All ON",   "green")
        b_off   = _btn("All OFF",  "red")
        self._ov_status = StatusLabel("")
        self._ov_btns   = [b_read, b_on, b_off]
        ctrl.addWidget(b_read); ctrl.addWidget(b_on); ctrl.addWidget(b_off)
        ctrl.addWidget(self._ov_status); ctrl.addStretch()
        lay.addLayout(ctrl)

        # ── Channel table ─────────────────────────────────────────────────
        tbl = QTableWidget(len(_NGI_CELL_CHANNELS), 8)
        tbl.setHorizontalHeaderLabels([
            "CH", "Role", "Set V", "Set I (mA)",
            "Meas V", "Meas I (mA)", "Meas P (W)", "ON/OFF"
        ])
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)

        self._ov_items = {}
        for row, ch in enumerate(_NGI_CELL_CHANNELS):
            role = "Busbar" if ch == _NGI_BUSBAR_CH else f"Cell {ch if ch < _NGI_BUSBAR_CH else ch - 1}"
            it_ch   = QTableWidgetItem(str(ch))
            it_role = QTableWidgetItem(role)
            it_sv   = QTableWidgetItem("---")
            it_si   = QTableWidgetItem("---")
            it_mv   = QTableWidgetItem("---")
            it_mi   = QTableWidgetItem("---")
            it_mp   = QTableWidgetItem("---")
            it_on   = QTableWidgetItem("---")

            if ch == _NGI_BUSBAR_CH:
                for it in (it_ch, it_role, it_sv, it_si, it_mv, it_mi, it_mp, it_on):
                    it.setBackground(QColor("#45475a"))
                    it.setForeground(QColor("#fab387"))

            tbl.setItem(row, 0, it_ch)
            tbl.setItem(row, 1, it_role)
            tbl.setItem(row, 2, it_sv)
            tbl.setItem(row, 3, it_si)
            tbl.setItem(row, 4, it_mv)
            tbl.setItem(row, 5, it_mi)
            tbl.setItem(row, 6, it_mp)
            tbl.setItem(row, 7, it_on)
            self._ov_items[ch] = (it_sv, it_si, it_mv, it_mi, it_mp, it_on)

        self._ov_table = tbl
        lay.addWidget(tbl)

        # ── Connections ───────────────────────────────────────────────────
        b_read.clicked.connect(self._ov_read_all)
        b_on.clicked.connect(lambda: self._ov_all_onoff(1))
        b_off.clicked.connect(lambda: self._ov_all_onoff(0))
        return w

    def _ov_read_all(self):
        def fn(res):
            results = {}
            for ch in _NGI_CELL_CHANNELS:
                v = res.query(f"MEAS{ch}:VOLT?").strip()
                i = res.query(f"MEAS{ch}:CURR?").strip()
                p = res.query(f"MEAS{ch}:POW?").strip()
                sv = res.query(f"SOUR{ch}:VOLT?").strip()
                si = res.query(f"SOUR{ch}:OUTCURR?").strip()
                on = res.query(f"OUTP{ch}:ONOFF?").strip()
                results[ch] = (sv, si, v, i, p, on)
            return results
        def _update_table(val, err):
            if err:
                self._ov_status.err(err)
                self._ov_status.clear_after()
                return
            for ch, (sv, si, v, i, p, on) in val.items():
                items = self._ov_items[ch]
                items[0].setText(f"{float(sv):.4f}")
                items[1].setText(f"{float(si):.1f}")
                items[2].setText(f"{float(v):.4f}")
                items[3].setText(f"{float(i):.1f}")
                items[4].setText(f"{float(p):.3f}")
                items[5].setText("ON" if on == "1" else "OFF")
            self._ov_status.ok("All channels read")
            self._ov_status.clear_after()
        conn = self._conn
        def job():
            return conn.run(fn)
        for b in self._ov_btns:
            b.setEnabled(False)
        self._ov_status.info("Reading all channels...")
        w = InstrumentWorker(job)
        w.result.connect(_update_table)
        w.result.connect(lambda *_: [b.setEnabled(True) for b in self._ov_btns])
        w.start()
        self._w = w

    def _ov_all_onoff(self, state):
        def fn(res):
            for ch in _NGI_CELL_CHANNELS:
                res.write(f"OUTP{ch}:ONOFF {state}")
            return f"All {'ON' if state else 'OFF'}"
        self._ngi_run(fn, f"Turning all {'ON' if state else 'OFF'}...",
                      self._ov_status, self._ov_btns)

    # ══════════════════════════════════════════════════════════════════════════
    # Per-channel detail tab
    # ══════════════════════════════════════════════════════════════════════════

    def _make_ch_detail(self, ch):
        w = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)

        role_text = "BUSBAR" if ch == _NGI_BUSBAR_CH else f"Cell {ch if ch < _NGI_BUSBAR_CH else ch - 1}"
        header = QLabel(f"Channel {ch} — {role_text}")
        header.setStyleSheet("font-size:12pt; font-weight:bold; color:#89b4fa;")
        if ch == _NGI_BUSBAR_CH:
            header.setStyleSheet("font-size:12pt; font-weight:bold; color:#fab387;")
        lay.addWidget(header)

        # ── Mode selector ─────────────────────────────────────────────────
        mode_grp = QGroupBox("Operation Mode")
        mg = QGridLayout(mode_grp)
        mg.addWidget(QLabel("Mode:"), 0, 0)
        mode_cb = QComboBox()
        mode_cb.addItems(["Source", "Charge", "SOC", "SEQ"])
        mg.addWidget(mode_cb, 0, 1)
        b_set_mode = _btn("Set Mode", "blue")
        b_qry_mode = _btn("Query Mode")
        mg.addWidget(b_set_mode, 0, 2)
        mg.addWidget(b_qry_mode, 0, 3)
        lay.addWidget(mode_grp)

        # ── Source mode controls ──────────────────────────────────────────
        src_grp = QGroupBox("Source Mode — CV / Current Limit")
        sg = QGridLayout(src_grp)
        sg.addWidget(QLabel("Voltage (V):"), 0, 0)
        v_sp = QDoubleSpinBox(); v_sp.setRange(0, 6.0); v_sp.setValue(3.3)
        v_sp.setSingleStep(0.01); v_sp.setDecimals(4)
        sg.addWidget(v_sp, 0, 1)

        sg.addWidget(QLabel("Current Limit (mA):"), 1, 0)
        i_sp = QDoubleSpinBox(); i_sp.setRange(0, 5000.0); i_sp.setValue(1000.0)
        i_sp.setSingleStep(10); i_sp.setDecimals(1)
        sg.addWidget(i_sp, 1, 1)

        sg.addWidget(QLabel("Current Range:"), 2, 0)
        range_cb = QComboBox()
        range_cb.addItems(["High", "Low", "Auto"])
        range_cb.setCurrentIndex(2)  # default Auto
        sg.addWidget(range_cb, 2, 1)

        b_apply_src = _btn("Apply Source", "green")
        sg.addWidget(b_apply_src, 3, 0, 1, 2)
        lay.addWidget(src_grp)

        # ── Charge mode controls ──────────────────────────────────────────
        chg_grp = QGroupBox("Charge Mode — CV / Current Limit / Resistance")
        cg = QGridLayout(chg_grp)
        cg.addWidget(QLabel("Voltage (V):"), 0, 0)
        chg_v = QDoubleSpinBox(); chg_v.setRange(0, 6.0); chg_v.setValue(3.3)
        chg_v.setSingleStep(0.01); chg_v.setDecimals(4)
        cg.addWidget(chg_v, 0, 1)

        cg.addWidget(QLabel("Current Limit (mA):"), 1, 0)
        chg_i = QDoubleSpinBox(); chg_i.setRange(0, 5000.0); chg_i.setValue(1000.0)
        chg_i.setSingleStep(10); chg_i.setDecimals(1)
        cg.addWidget(chg_i, 1, 1)

        cg.addWidget(QLabel("Resistance (mOhm):"), 2, 0)
        chg_r = QDoubleSpinBox(); chg_r.setRange(0, 1000.0); chg_r.setValue(10.0)
        chg_r.setSingleStep(0.1); chg_r.setDecimals(2)
        cg.addWidget(chg_r, 2, 1)

        b_apply_chg = _btn("Apply Charge", "orange")
        cg.addWidget(b_apply_chg, 3, 0, 1, 2)
        lay.addWidget(chg_grp)

        # ── Output ON/OFF ─────────────────────────────────────────────────
        out_grp = QGroupBox("Output")
        og = QHBoxLayout(out_grp)
        b_on  = _btn(f"CH{ch} ON",  "green")
        b_off = _btn(f"CH{ch} OFF", "red")
        og.addWidget(b_on); og.addWidget(b_off)
        lay.addWidget(out_grp)

        # ── Measurement readback ──────────────────────────────────────────
        meas_grp = QGroupBox("Measurement Readback")
        meas_g = QGridLayout(meas_grp)
        _LS = ("font-family:'Courier New'; font-size:13pt; font-weight:bold;"
               "background:#181825; border:1px solid #45475a;"
               "border-radius:4px; padding:4px 8px;")
        mv_lbl = QLabel("---"); mv_lbl.setStyleSheet(_LS + "color:#a6e3a1;")
        mi_lbl = QLabel("---"); mi_lbl.setStyleSheet(_LS + "color:#89b4fa;")
        mp_lbl = QLabel("---"); mp_lbl.setStyleSheet(_LS + "color:#fab387;")
        meas_g.addWidget(QLabel("V:"), 0, 0); meas_g.addWidget(mv_lbl, 0, 1)
        meas_g.addWidget(QLabel("mA:"), 0, 2); meas_g.addWidget(mi_lbl, 0, 3)
        meas_g.addWidget(QLabel("W:"), 0, 4); meas_g.addWidget(mp_lbl, 0, 5)
        b_meas = _btn("Measure", "blue")
        meas_g.addWidget(b_meas, 1, 0, 1, 6)
        lay.addWidget(meas_grp)

        # ── Status ────────────────────────────────────────────────────────
        status = StatusLabel("")
        lay.addWidget(status)
        lay.addStretch()

        all_btns = [b_set_mode, b_qry_mode, b_apply_src, b_apply_chg,
                    b_on, b_off, b_meas]

        # ── Connections ───────────────────────────────────────────────────
        def _set_mode(_chk=None, _ch=ch, _cb=mode_cb, _st=status, _bl=all_btns):
            mode_name = _cb.currentText()
            code = _NGI_MODE_INV[mode_name]
            def fn(res):
                res.write(f"OUTP{_ch}:MODE {code}")
                return f"CH{_ch} mode = {mode_name}"
            self._ngi_run(fn, f"Setting CH{_ch} mode...", _st, _bl)

        def _qry_mode(_chk=None, _ch=ch, _cb=mode_cb, _st=status, _bl=all_btns):
            def fn(res):
                r = int(res.query(f"OUTP{_ch}:MODE?").strip())
                return _NGI_MODE_MAP.get(r, f"Unknown({r})")
            self._ngi_run(fn, f"Querying CH{_ch} mode...", _st, _bl)

        def _apply_source(_chk=None, _ch=ch, _v=v_sp, _i=i_sp, _r=range_cb,
                          _st=status, _bl=all_btns):
            v_val = _v.value(); i_val = _i.value()
            range_code = [0, 2, 3][_r.currentIndex()]
            def fn(res):
                res.write(f"SOUR{_ch}:VOLT {v_val:.4f}")
                res.write(f"SOUR{_ch}:OUTCURR {i_val:.1f}")
                res.write(f"SOUR{_ch}:RANG {range_code}")
                return f"CH{_ch}: V={v_val:.4f}V  I={i_val:.1f}mA"
            self._ngi_run(fn, f"Applying source CH{_ch}...", _st, _bl)

        def _apply_charge(_chk=None, _ch=ch, _cv=chg_v, _ci=chg_i, _cr=chg_r,
                          _st=status, _bl=all_btns):
            cv_val = _cv.value(); ci_val = _ci.value(); cr_val = _cr.value()
            def fn(res):
                res.write(f"CHAR{_ch}:VOLT {cv_val:.4f}")
                res.write(f"CHAR{_ch}:OUTCURR {ci_val:.1f}")
                res.write(f"CHAR{_ch}:R {cr_val:.2f}")
                return f"CH{_ch}: V={cv_val:.4f}V  I={ci_val:.1f}mA  R={cr_val:.2f}mOhm"
            self._ngi_run(fn, f"Applying charge CH{_ch}...", _st, _bl)

        def _ch_on(_chk=None, _ch=ch, _st=status, _bl=all_btns):
            def fn(res):
                res.write(f"OUTP{_ch}:ONOFF 1")
                return f"CH{_ch} ON"
            self._ngi_run(fn, f"CH{_ch} ON...", _st, _bl)

        def _ch_off(_chk=None, _ch=ch, _st=status, _bl=all_btns):
            def fn(res):
                res.write(f"OUTP{_ch}:ONOFF 0")
                return f"CH{_ch} OFF"
            self._ngi_run(fn, f"CH{_ch} OFF...", _st, _bl)

        def _measure(_chk=None, _ch=ch, _vl=mv_lbl, _il=mi_lbl, _pl=mp_lbl,
                     _st=status, _bl=all_btns):
            def fn(res):
                v = float(res.query(f"MEAS{_ch}:VOLT?").strip())
                i = float(res.query(f"MEAS{_ch}:CURR?").strip())
                p = float(res.query(f"MEAS{_ch}:POW?").strip())
                return (v, i, p)
            def _upd(val, err):
                if err:
                    _st.err(err); _st.clear_after()
                    for b in _bl: b.setEnabled(True)
                    return
                _vl.setText(f"{val[0]:.4f} V")
                _il.setText(f"{val[1]:.1f} mA")
                _pl.setText(f"{val[2]:.3f} W")
                _st.ok(f"CH{_ch} measured"); _st.clear_after()
                for b in _bl: b.setEnabled(True)
            conn = self._conn
            for b in _bl: b.setEnabled(False)
            _st.info(f"Measuring CH{_ch}...")
            wk = InstrumentWorker(lambda: conn.run(fn))
            wk.result.connect(_upd)
            wk.start()
            self._w = wk

        b_set_mode.clicked.connect(_set_mode)
        b_qry_mode.clicked.connect(_qry_mode)
        b_apply_src.clicked.connect(_apply_source)
        b_apply_chg.clicked.connect(_apply_charge)
        b_on.clicked.connect(_ch_on)
        b_off.clicked.connect(_ch_off)
        b_meas.clicked.connect(_measure)

        scroll.setWidget(inner)
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        return w

    # ══════════════════════════════════════════════════════════════════════════
    # Batch tab — apply same voltage to all cell channels at once
    # ══════════════════════════════════════════════════════════════════════════

    def _build_batch(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        # ── Uniform cell voltage ──────────────────────────────────────────
        cell_grp = QGroupBox("Set All Cell Channels (excl. Busbar)")
        cg = QGridLayout(cell_grp)
        cg.addWidget(QLabel("Cell Voltage (V):"), 0, 0)
        self._bat_v = QDoubleSpinBox()
        self._bat_v.setRange(0, 6.0); self._bat_v.setValue(3.3)
        self._bat_v.setSingleStep(0.01); self._bat_v.setDecimals(4)
        cg.addWidget(self._bat_v, 0, 1)

        cg.addWidget(QLabel("Current Limit (mA):"), 1, 0)
        self._bat_i = QDoubleSpinBox()
        self._bat_i.setRange(0, 5000.0); self._bat_i.setValue(1000.0)
        self._bat_i.setSingleStep(10); self._bat_i.setDecimals(1)
        cg.addWidget(self._bat_i, 1, 1)

        b_apply_cells = _btn("Apply to All Cells", "green")
        cg.addWidget(b_apply_cells, 2, 0, 1, 2)
        lay.addWidget(cell_grp)

        # ── Busbar voltage ────────────────────────────────────────────────
        bus_grp = QGroupBox(f"Busbar Channel (CH{_NGI_BUSBAR_CH})")
        bg = QGridLayout(bus_grp)
        bg.addWidget(QLabel("Busbar Voltage (V):"), 0, 0)
        self._bus_v = QDoubleSpinBox()
        self._bus_v.setRange(0, 6.0); self._bus_v.setValue(0.0)
        self._bus_v.setSingleStep(0.01); self._bus_v.setDecimals(4)
        bg.addWidget(self._bus_v, 0, 1)

        bg.addWidget(QLabel("Current Limit (mA):"), 1, 0)
        self._bus_i = QDoubleSpinBox()
        self._bus_i.setRange(0, 5000.0); self._bus_i.setValue(5000.0)
        self._bus_i.setSingleStep(10); self._bus_i.setDecimals(1)
        bg.addWidget(self._bus_i, 1, 1)

        b_apply_bus = _btn("Apply Busbar", "orange")
        bg.addWidget(b_apply_bus, 2, 0, 1, 2)
        lay.addWidget(bus_grp)

        # ── Mode batch ────────────────────────────────────────────────────
        mode_grp = QGroupBox("Set All Channels Mode")
        mg = QGridLayout(mode_grp)
        mg.addWidget(QLabel("Mode:"), 0, 0)
        self._bat_mode = QComboBox()
        self._bat_mode.addItems(["Source", "Charge", "SOC", "SEQ"])
        mg.addWidget(self._bat_mode, 0, 1)
        b_set_all_mode = _btn("Set All Mode", "blue")
        mg.addWidget(b_set_all_mode, 1, 0, 1, 2)
        lay.addWidget(mode_grp)

        # ── ON / OFF ──────────────────────────────────────────────────────
        onoff_grp = QGroupBox("Output Control")
        og = QHBoxLayout(onoff_grp)
        b_all_on  = _btn("All 17 CH ON",  "green")
        b_all_off = _btn("All 17 CH OFF", "red")
        b_cells_on  = _btn("Cells ON",  "green")
        b_cells_off = _btn("Cells OFF", "red")
        og.addWidget(b_all_on); og.addWidget(b_all_off)
        og.addWidget(b_cells_on); og.addWidget(b_cells_off)
        lay.addWidget(onoff_grp)

        self._bat_status = StatusLabel("")
        lay.addWidget(self._bat_status)
        lay.addStretch()

        _all_btns = [b_apply_cells, b_apply_bus, b_set_all_mode,
                     b_all_on, b_all_off, b_cells_on, b_cells_off]
        _cell_chs = [c for c in _NGI_CELL_CHANNELS if c != _NGI_BUSBAR_CH]

        def _apply_cells_fn():
            v = self._bat_v.value(); i = self._bat_i.value()
            def fn(res):
                for ch in _cell_chs:
                    res.write(f"SOUR{ch}:VOLT {v:.4f}")
                    res.write(f"SOUR{ch}:OUTCURR {i:.1f}")
                return f"16 cells: V={v:.4f}V  I={i:.1f}mA"
            self._ngi_run(fn, "Applying to all cells...",
                          self._bat_status, _all_btns)

        def _apply_bus_fn():
            v = self._bus_v.value(); i = self._bus_i.value()
            def fn(res):
                res.write(f"SOUR{_NGI_BUSBAR_CH}:VOLT {v:.4f}")
                res.write(f"SOUR{_NGI_BUSBAR_CH}:OUTCURR {i:.1f}")
                return f"Busbar CH{_NGI_BUSBAR_CH}: V={v:.4f}V  I={i:.1f}mA"
            self._ngi_run(fn, "Applying busbar...",
                          self._bat_status, _all_btns)

        def _set_all_mode_fn():
            mode_name = self._bat_mode.currentText()
            code = _NGI_MODE_INV[mode_name]
            def fn(res):
                for ch in _NGI_CELL_CHANNELS:
                    res.write(f"OUTP{ch}:MODE {code}")
                return f"All 17 CH mode = {mode_name}"
            self._ngi_run(fn, "Setting all modes...",
                          self._bat_status, _all_btns)

        def _onoff(channels, state):
            tag = "ON" if state else "OFF"
            def fn(res):
                for ch in channels:
                    res.write(f"OUTP{ch}:ONOFF {state}")
                return f"{len(channels)} ch {tag}"
            self._ngi_run(fn, f"Turning {tag}...",
                          self._bat_status, _all_btns)

        b_apply_cells.clicked.connect(_apply_cells_fn)
        b_apply_bus.clicked.connect(_apply_bus_fn)
        b_set_all_mode.clicked.connect(_set_all_mode_fn)
        b_all_on.clicked.connect(lambda: _onoff(_NGI_CELL_CHANNELS, 1))
        b_all_off.clicked.connect(lambda: _onoff(_NGI_CELL_CHANNELS, 0))
        b_cells_on.clicked.connect(lambda: _onoff(_cell_chs, 1))
        b_cells_off.clicked.connect(lambda: _onoff(_cell_chs, 0))
        return w

    # ══════════════════════════════════════════════════════════════════════════
    # Protection tab — OVP / OCP / OPP for all 17 channels
    # ══════════════════════════════════════════════════════════════════════════

    def _build_protection(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        tbl = QTableWidget(len(_NGI_CELL_CHANNELS), 7)
        tbl.setHorizontalHeaderLabels([
            "CH", "OVP (V)", "OCP (mA)", "OPP (mW)",
            "", "", ""
        ])
        tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

        self._prot_spins = {}
        for row, ch in enumerate(_NGI_CELL_CHANNELS):
            tbl.setItem(row, 0, QTableWidgetItem(str(ch)))
            ovp = QDoubleSpinBox(); ovp.setRange(0, 7.0); ovp.setValue(6.5)
            ovp.setDecimals(3); ovp.setSingleStep(0.1)
            ocp = QDoubleSpinBox(); ocp.setRange(0, 6000.0); ocp.setValue(5500.0)
            ocp.setDecimals(1); ocp.setSingleStep(100)
            opp = QDoubleSpinBox(); opp.setRange(0, 40000.0); opp.setValue(35000.0)
            opp.setDecimals(1); opp.setSingleStep(1000)
            tbl.setCellWidget(row, 1, ovp)
            tbl.setCellWidget(row, 2, ocp)
            tbl.setCellWidget(row, 3, opp)
            self._prot_spins[ch] = (ovp, ocp, opp)

        lay.addWidget(tbl)

        ctrl = QHBoxLayout()
        b_apply = _btn("Apply All Protection", "orange")
        b_read  = _btn("Read All Protection", "blue")
        self._prot_status = StatusLabel("")
        self._prot_btns = [b_apply, b_read]
        ctrl.addWidget(b_apply); ctrl.addWidget(b_read)
        ctrl.addWidget(self._prot_status); ctrl.addStretch()
        lay.addLayout(ctrl)

        def _apply_prot():
            vals = {ch: (s[0].value(), s[1].value(), s[2].value())
                    for ch, s in self._prot_spins.items()}
            def fn(res):
                for ch, (ov, oc, op) in vals.items():
                    res.write(f"PRO{ch}:VOLT {ov:.3f}")
                    res.write(f"PRO{ch}:CURR {oc:.1f}")
                    res.write(f"PRO{ch}:POWE {op:.1f}")
                return "Protection applied to all 17 channels"
            self._ngi_run(fn, "Applying protection...",
                          self._prot_status, self._prot_btns)

        def _read_prot():
            def fn(res):
                result = {}
                for ch in _NGI_CELL_CHANNELS:
                    ov = float(res.query(f"PRO{ch}:VOLT?").strip())
                    oc = float(res.query(f"PRO{ch}:CURR?").strip())
                    op = float(res.query(f"PRO{ch}:POWE?").strip())
                    result[ch] = (ov, oc, op)
                return result
            def _upd(val, err):
                if err:
                    self._prot_status.err(err)
                    self._prot_status.clear_after()
                    for b in self._prot_btns: b.setEnabled(True)
                    return
                for ch, (ov, oc, op) in val.items():
                    s = self._prot_spins[ch]
                    s[0].setValue(ov); s[1].setValue(oc); s[2].setValue(op)
                self._prot_status.ok("Protection values read")
                self._prot_status.clear_after()
                for b in self._prot_btns: b.setEnabled(True)
            conn = self._conn
            for b in self._prot_btns: b.setEnabled(False)
            self._prot_status.info("Reading protection...")
            wk = InstrumentWorker(lambda: conn.run(fn))
            wk.result.connect(_upd)
            wk.start()
            self._w = wk

        b_apply.clicked.connect(_apply_prot)
        b_read.clicked.connect(_read_prot)
        return w

    # ══════════════════════════════════════════════════════════════════════════
    # System tab — IP, baud, beeper, language
    # ══════════════════════════════════════════════════════════════════════════

    def _build_system(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        note = QLabel(
            "Note: System commands (IP, baud, beeper, etc.) may not respond "
            "over TCP/LAN on some firmware versions. Use the front panel or "
            "RS232 to change system settings if Apply/Read times out.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#fab387; font-size:9pt; padding:4px;")
        lay.addWidget(note)

        sys_grp = QGroupBox("System Configuration")
        sg = QGridLayout(sys_grp)

        sg.addWidget(QLabel("IP Address:"), 0, 0)
        self._sys_ip = QLineEdit("10.73.77.200")
        sg.addWidget(self._sys_ip, 0, 1)

        sg.addWidget(QLabel("Baud Rate:"), 1, 0)
        self._sys_baud = QComboBox()
        self._sys_baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self._sys_baud.setCurrentText("115200")
        sg.addWidget(self._sys_baud, 1, 1)

        sg.addWidget(QLabel("Beeper:"), 2, 0)
        self._sys_beep = QComboBox()
        self._sys_beep.addItems(["OFF", "ON"])
        self._sys_beep.setCurrentIndex(1)
        sg.addWidget(self._sys_beep, 2, 1)

        sg.addWidget(QLabel("Language:"), 3, 0)
        self._sys_lang = QComboBox()
        self._sys_lang.addItems(["Chinese", "English"])
        self._sys_lang.setCurrentIndex(1)
        sg.addWidget(self._sys_lang, 3, 1)

        sg.addWidget(QLabel("Connection Type:"), 4, 0)
        self._sys_conn_type = QComboBox()
        self._sys_conn_type.addItems(["UDP", "TCP"])
        self._sys_conn_type.setCurrentIndex(1)
        sg.addWidget(self._sys_conn_type, 4, 1)

        sg.addWidget(QLabel("Power-down Save:"), 5, 0)
        self._sys_powsave = QComboBox()
        self._sys_powsave.addItems(["OFF", "ON"])
        sg.addWidget(self._sys_powsave, 5, 1)

        lay.addWidget(sys_grp)

        ctrl = QHBoxLayout()
        b_apply = _btn("Apply System", "green")
        b_read  = _btn("Read System",  "blue")
        b_idn   = _btn("*IDN?")
        b_rst   = _btn("*RST  Factory Reset", "red")
        self._sys_status = StatusLabel("")
        self._sys_btns = [b_apply, b_read, b_idn, b_rst]
        ctrl.addWidget(b_apply); ctrl.addWidget(b_read)
        ctrl.addWidget(b_idn); ctrl.addWidget(b_rst)
        ctrl.addWidget(self._sys_status); ctrl.addStretch()
        lay.addLayout(ctrl)

        # ── IDN display ───────────────────────────────────────────────────
        self._sys_idn_lbl = QLabel("---")
        self._sys_idn_lbl.setStyleSheet(
            "font-family:'Courier New'; font-size:10pt; color:#a6adc8;"
            "background:#181825; border:1px solid #45475a;"
            "border-radius:4px; padding:6px;")
        lay.addWidget(self._sys_idn_lbl)
        lay.addStretch()

        def _apply_sys():
            ip = self._sys_ip.text().strip()
            baud = self._sys_baud.currentText()
            beep = self._sys_beep.currentIndex()
            lang = self._sys_lang.currentIndex()
            conn_type = self._sys_conn_type.currentIndex()
            powsave = self._sys_powsave.currentIndex()
            def fn(res):
                res.write(f'SYSTem1:COMMand:LAN:IPADdr "{ip}"')
                res.write(f"SYSTem1:COMMand:SERial:BAUDrate {baud}")
                res.write(f"SYSTem1:SOUNd {beep}")
                res.write(f"SYSTem1:LANGuage {lang}")
                res.write(f"SYSTem1:COMMand:LAN:TYPe {conn_type}")
                res.write(f"SYSTem1:POWDown:SAVe {powsave}")
                return "System settings applied"
            self._ngi_run(fn, "Applying system settings...",
                          self._sys_status, self._sys_btns)

        def _read_sys():
            def fn(res):
                ip   = res.query("SYSTem1:COMMand:LAN:IPADdr?").strip()
                baud = res.query("SYSTem1:COMMand:SERial:BAUDrate?").strip()
                beep = res.query("SYSTem1:SOUNd?").strip()
                lang = res.query("SYSTem1:LANGuage?").strip()
                ct   = res.query("SYSTem1:COMMand:LAN:TYPe?").strip()
                ps   = res.query("SYSTem1:POWDown:SAVe?").strip()
                return {"ip": ip, "baud": baud, "beep": beep,
                        "lang": lang, "ct": ct, "ps": ps}
            def _upd(val, err):
                if err:
                    self._sys_status.err(err)
                    self._sys_status.clear_after()
                    for b in self._sys_btns: b.setEnabled(True)
                    return
                self._sys_ip.setText(val["ip"])
                self._sys_baud.setCurrentText(val["baud"])
                self._sys_beep.setCurrentIndex(int(val["beep"]))
                self._sys_lang.setCurrentIndex(int(val["lang"]))
                self._sys_conn_type.setCurrentIndex(int(val["ct"]))
                self._sys_powsave.setCurrentIndex(int(val["ps"]))
                self._sys_status.ok("System settings read")
                self._sys_status.clear_after()
                for b in self._sys_btns: b.setEnabled(True)
            conn = self._conn
            for b in self._sys_btns: b.setEnabled(False)
            self._sys_status.info("Reading system...")
            wk = InstrumentWorker(lambda: conn.run(fn))
            wk.result.connect(_upd)
            wk.start()
            self._w = wk

        def _idn():
            def fn(res):
                return res.query("*IDN?").strip()
            def _upd(val, err):
                if err:
                    self._sys_idn_lbl.setText(f"Err: {err}")
                else:
                    self._sys_idn_lbl.setText(str(val))
                for b in self._sys_btns: b.setEnabled(True)
            conn = self._conn
            for b in self._sys_btns: b.setEnabled(False)
            wk = InstrumentWorker(lambda: conn.run(fn))
            wk.result.connect(_upd)
            wk.start()
            self._w = wk

        def _rst():
            def fn(res):
                res.write("*RST")
                return "Factory reset sent (takes ~10s)"
            self._ngi_run(fn, "Factory reset...",
                          self._sys_status, self._sys_btns)

        b_apply.clicked.connect(_apply_sys)
        b_read.clicked.connect(_read_sys)
        b_idn.clicked.connect(_idn)
        b_rst.clicked.connect(_rst)
        return w

    # ══════════════════════════════════════════════════════════════════════════
    # Plot tab — real-time V/I trends
    # ══════════════════════════════════════════════════════════════════════════

    def _build_plot(self):
        channels = []
        for ch in _NGI_CELL_CHANNELS:
            lbl = f"CH{ch}_busbar" if ch == _NGI_BUSBAR_CH else f"CH{ch}"
            channels.append(f"{lbl}_V")
            channels.append(f"{lbl}_I")

        def _measure(res):
            data = {}
            for ch in _NGI_CELL_CHANNELS:
                lbl = f"CH{ch}_busbar" if ch == _NGI_BUSBAR_CH else f"CH{ch}"
                try:
                    v = float(res.query(f"MEAS{ch}:VOLT?").strip())
                except Exception:
                    v = 0.0
                try:
                    i = float(res.query(f"MEAS{ch}:CURR?").strip())
                except Exception:
                    i = 0.0
                data[f"{lbl}_V"] = v
                data[f"{lbl}_I"] = i
            return data

        pp = PlotPanel(channels=channels, y_label="V / mA")
        pp.set_measure_fn(lambda: self._conn.run(_measure, skip_if_busy=True))
        return pp
