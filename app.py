# -*- coding: utf-8 -*-
"""
Automated Test Suite — Desktop Application
==========================================
Native PyQt6 desktop app for instrument control and automated sweeps.
Direct VISA communication — no HTTP overhead.

Usage:
    python app.py

Dependencies:
    pip install PyQt6 pyqtgraph pyvisa pyvisa-py
"""

import sys
import json
import socket
import re
import time
from pathlib import Path
from datetime import datetime

import pyvisa
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QCheckBox, QGroupBox,
    QProgressBar, QTextEdit, QLineEdit, QGridLayout, QMessageBox, QDoubleSpinBox, QSpinBox,
    QScrollArea, QComboBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QSplitter, QTreeWidget,
    QTreeWidgetItem, QStackedWidget, QFrame, QSizePolicy,
    QToolButton, QDockWidget,
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QTextCursor, QColor, QShortcut, QKeySequence, QIcon, QFont

from test_automation import ADDR, ITechFixed, GWGPP4323
from workers import (
    open_resource, use_instrument, use_driver,
    shared_resource, invalidate_shared,
    SweepWorker, InstrumentWorker, _instr_lock,
)
from panels import (
    ItechPanel, GppPanel, GdmPanel, DmmPanel, MdoPanel, ScpiTerminal,
    WT5000Panel, InstrumentLibraryPanel, NgiCellSimPanel
)
from sweep_panel import SweepPanel
from sequence_panel import SequencePanel
from eddie_panel import EddiePanel
from daq_espec_panel import KeysightDAQ973APanel, EspecSH662Panel
from ui_helpers import TH
from app_logger import get_logger, APP_DIR
import persist
import instrument_registry

_log = get_logger("app")

# ─────────────────────────────────────────────────────────────────────────────
ADDRESSES_FILE = APP_DIR / "instrument_addresses.json"
SETTINGS_FILE  = APP_DIR / "app_settings.json"

pg.setConfigOption('background', TH.BASE)
pg.setConfigOption('foreground', TH.TEXT)
pg.setConfigOption('antialias', True)

STYLE = f"""
QMainWindow, QWidget {{
    background-color: {TH.BASE}; color: {TH.TEXT};
    font-family: {TH.SANS}; font-size: {TH.FONT_MD};
}}
QTabWidget::pane {{ border: 1px solid {TH.SURFACE0}; border-radius: 4px; }}
QTabBar::tab {{
    background: {TH.MANTLE}; color: {TH.SUBTEXT0};
    padding: 8px 22px; border-radius: 4px 4px 0 0; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {TH.SURFACE0}; color: {TH.BLUE}; font-weight: bold; }}
QTabBar::tab:hover {{ background: {TH.SURFACE0}; color: {TH.TEXT}; }}
QGroupBox {{
    border: 1px solid {TH.SURFACE1}; border-radius: 6px;
    margin-top: 12px; padding: 8px 6px 6px 6px;
}}
QGroupBox::title {{
    color: {TH.BLUE}; subcontrol-origin: margin; left: 10px; padding: 0 6px;
    font-weight: bold;
}}
QPushButton {{
    background: {TH.SURFACE0}; color: {TH.TEXT}; border: 1px solid {TH.SURFACE1};
    border-radius: 5px; padding: 6px 16px; min-width: 70px;
}}
QPushButton:hover {{ background: {TH.SURFACE1}; border-color: {TH.BLUE}; }}
QPushButton:pressed {{ background: {TH.SURFACE2}; }}
QPushButton:disabled {{ color: {TH.OVERLAY0}; border-color: {TH.SURFACE0}; }}
QPushButton[role="green"]  {{ background: {TH.GREEN}; color: {TH.BASE}; border-color: {TH.GREEN}; font-weight: bold; }}
QPushButton[role="red"]    {{ background: {TH.RED}; color: {TH.BASE}; border-color: {TH.RED}; font-weight: bold; }}
QPushButton[role="blue"]   {{ background: {TH.BLUE}; color: {TH.BASE}; border-color: {TH.BLUE}; font-weight: bold; }}
QPushButton[role="orange"] {{ background: {TH.ORANGE}; color: {TH.BASE}; border-color: {TH.ORANGE}; font-weight: bold; }}
QPushButton[role="green"]:hover  {{ background: #94d3a2; }}
QPushButton[role="red"]:hover    {{ background: #e57896; }}
QPushButton[role="estop"]  {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ff4444, stop:1 #cc0000);
    color: white; border: 2px solid #ff6666; border-radius: 6px;
    font-weight: bold; font-size: 11pt; min-width: 0; padding: 8px 12px;
}}
QPushButton[role="estop"]:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ff6666, stop:1 #ee2222); }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {TH.SURFACE0}; color: {TH.TEXT}; border: 1px solid {TH.SURFACE1};
    border-radius: 4px; padding: 5px 8px;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {TH.BLUE};
}}
QProgressBar {{
    border: 1px solid {TH.SURFACE1}; border-radius: 4px;
    background: {TH.MANTLE}; text-align: center; color: {TH.TEXT}; min-height: 20px;
}}
QProgressBar::chunk {{ background: {TH.BLUE}; border-radius: 3px; }}
QTextEdit {{
    background: {TH.MANTLE}; color: {TH.TEXT}; border: 1px solid {TH.SURFACE1};
    border-radius: 4px; font-family: {TH.MONO}; font-size: {TH.FONT_SM};
}}
QTableWidget {{
    background: {TH.MANTLE}; color: {TH.TEXT}; border: 1px solid {TH.SURFACE1};
    border-radius: 4px; gridline-color: {TH.SURFACE0};
}}
QHeaderView::section {{
    background: {TH.SURFACE0}; color: {TH.BLUE}; padding: 5px;
    border: none; font-weight: bold;
}}
QTableWidget::item:selected {{ background: {TH.SURFACE1}; }}
QScrollBar:vertical {{ background: {TH.BASE}; width: 10px; border-radius: 5px; }}
QScrollBar::handle:vertical {{ background: {TH.SURFACE1}; border-radius: 5px; min-height: 20px; }}
QScrollBar:horizontal {{ background: {TH.BASE}; height: 10px; border-radius: 5px; }}
QScrollBar::handle:horizontal {{ background: {TH.SURFACE1}; border-radius: 5px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {TH.SURFACE1}; border-radius: 3px; background: {TH.SURFACE0}; }}
QCheckBox::indicator:checked {{ background: {TH.BLUE}; border-color: {TH.BLUE}; }}
QSplitter::handle {{ background: {TH.SURFACE1}; }}
QTreeWidget {{
    background: {TH.MANTLE}; color: {TH.TEXT}; border: none;
    font-size: {TH.FONT_MD};
    outline: 0;
}}
QTreeWidget::item {{
    padding: 6px 8px; border-radius: 4px; margin: 1px 4px;
}}
QTreeWidget::item:selected {{
    background: {TH.SURFACE0}; color: {TH.BLUE}; font-weight: bold;
}}
QTreeWidget::item:hover:!selected {{
    background: {TH.SURFACE0};
}}
QTreeWidget::branch {{ background: {TH.MANTLE}; }}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Address persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_addresses():
    if ADDRESSES_FILE.exists():
        try:
            saved = json.loads(ADDRESSES_FILE.read_text(encoding='utf-8'))
            for k, v in saved.items():
                if k in ADDR:
                    ADDR[k] = v
        except Exception as e:
            print(f"Address load error: {e}")

def save_addresses():
    try:
        ADDRESSES_FILE.write_text(json.dumps(ADDR, indent=4), encoding='utf-8')
        return True
    except Exception as e:
        print(f"Address save error: {e}")
        return False

def _check_ports(address: str) -> dict:
    """Returns port-open dict for the address.  Always includes the port
    that is explicitly named in a SOCKET address (e.g. 1026 for GPP)."""
    ip_match = re.search(r'TCPIP\d*::([^:]+)', address)
    if not ip_match:
        return {}
    ip = ip_match.group(1)

    # Ports to probe — start with the common ones
    ports = [5025, 1234, 111, 3490, 8080]
    # If the address names a specific SOCKET port, probe that first
    m = re.search(r'::(\d+)::SOCKET', address, re.IGNORECASE)
    if m:
        p = int(m.group(1))
        if p not in ports:
            ports.insert(0, p)

    results = {}
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            results[f'tcp_{port}'] = s.connect_ex((ip, port)) == 0
            s.close()
        except Exception:
            results[f'tcp_{port}'] = False

    # VXI-11 portmapper lives on UDP 111
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(b'\x00' * 4, (ip, 111))
        s.recvfrom(64)
        results['udp_111'] = True
        s.close()
    except Exception:
        results['udp_111'] = False
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Dashboard tab — live front-panel readings
# ─────────────────────────────────────────────────────────────────────────────

class DashboardTab(QWidget):
    """Live front-panel readings for all instruments, refreshed every second."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # ── Toggle + timestamp row ─────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        self.btn_live = QPushButton("▶  Start Live Readings")
        self.btn_live.setProperty("role", "green")
        self.btn_live.setMinimumHeight(36)
        self.btn_live.setFixedWidth(210)
        self.btn_live.clicked.connect(self._toggle)
        self._ts_lbl = QLabel("—")
        self._ts_lbl.setStyleSheet(f"color:{TH.SUBTEXT0}; font-style:italic;")
        ctrl.addWidget(self.btn_live)
        ctrl.addSpacing(16)
        ctrl.addWidget(QLabel("Last updated:"))
        ctrl.addWidget(self._ts_lbl)
        ctrl.addStretch()
        lay.addLayout(ctrl)

        # ── Instrument panels ───────────────────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(10)
        grid.addWidget(self._build_itech_grp(), 0, 0)
        grid.addWidget(self._build_gpp_grp(),   0, 1)
        grid.addWidget(self._build_gdm_grp(),   1, 0)
        grid.addWidget(self._build_dmm_grp(),   1, 1)
        lay.addLayout(grid)
        lay.addWidget(self._build_eddie_grp())
        lay.addWidget(self._build_keepalive_grp())
        lay.addStretch()

        self._test_running = False  # set True while sweep/sequence is active

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._ka_timer = QTimer(self)
        self._ka_timer.setInterval(10_000)
        self._ka_timer.timeout.connect(self._tick_keepalive)
        self._ka_chk.toggled.connect(self._toggle_keepalive)
        self._ka_timer.start()
        QTimer.singleShot(2000, self._tick_keepalive)  # first ping after 2 s

    # ── UI builders ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _val_lbl(color: str = None) -> QLabel:
        if color is None:
            color = TH.GREEN
        lbl = QLabel("—")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"font-family:{TH.MONO}; font-size:{TH.FONT_LG}; font-weight:bold;"
            f" color:{color}; background:{TH.MANTLE}; border:1px solid {TH.SURFACE1};"
            f" border-radius:4px; padding:4px 10px; min-width:115px;")
        return lbl

    def _build_itech_grp(self):
        grp = QGroupBox("ITECH IT6005C")
        g = QGridLayout(grp)
        self._it_v = self._val_lbl()
        self._it_i = self._val_lbl()
        self._it_p = self._val_lbl()
        for col, (hdr, lbl) in enumerate([
                ("Voltage (V)", self._it_v),
                ("Current (A)", self._it_i),
                ("Power (W)",   self._it_p)]):
            g.addWidget(QLabel(hdr), 0, col, Qt.AlignmentFlag.AlignHCenter)
            g.addWidget(lbl,         1, col)

        # Quick-set controls
        g.addWidget(QLabel("Set V:"), 2, 0)
        self._it_set_v = QDoubleSpinBox()
        self._it_set_v.setRange(0, 80); self._it_set_v.setValue(12.0)
        self._it_set_v.setSuffix(" V"); self._it_set_v.setDecimals(3)
        self._it_set_v.setFixedWidth(110)
        g.addWidget(self._it_set_v, 2, 1)

        g.addWidget(QLabel("Set I:"), 3, 0)
        self._it_set_i = QDoubleSpinBox()
        self._it_set_i.setRange(-150, 150); self._it_set_i.setValue(5.0)
        self._it_set_i.setSuffix(" A"); self._it_set_i.setDecimals(3)
        self._it_set_i.setFixedWidth(110)
        g.addWidget(self._it_set_i, 3, 1)

        btn_row = QHBoxLayout()
        self._it_apply = QPushButton("Apply")
        self._it_apply.setProperty("role", "blue")
        self._it_apply.setFixedHeight(24); self._it_apply.setFixedWidth(60)
        self._it_apply.clicked.connect(self._apply_itech)
        self._it_on = QPushButton("ON")
        self._it_on.setProperty("role", "green")
        self._it_on.setFixedHeight(24); self._it_on.setFixedWidth(40)
        self._it_on.clicked.connect(lambda: self._itech_output(True))
        self._it_off = QPushButton("OFF")
        self._it_off.setProperty("role", "red")
        self._it_off.setFixedHeight(24); self._it_off.setFixedWidth(40)
        self._it_off.clicked.connect(lambda: self._itech_output(False))
        btn_row.addWidget(self._it_apply)
        btn_row.addWidget(self._it_on)
        btn_row.addWidget(self._it_off)
        btn_row.addStretch()
        g.addLayout(btn_row, 2, 2, 2, 1)
        return grp

    def _apply_itech(self):
        v = self._it_set_v.value()
        i = self._it_set_i.value()
        _log.info("Dashboard: ITECH apply V=%.3f I=%.3f", v, i)
        def fn():
            with use_driver('itech', lambda: ITechFixed(ADDR['itech'])) as drv:
                drv.set_volts(v)
                signed = i if i >= 0 else -abs(i)
                drv.set_amps(signed)
                return f"V={v:.3f}, I={i:.3f}"
        w = InstrumentWorker(fn)
        w.result.connect(lambda val, err:
            _log.error("Dashboard: ITECH apply failed: %s", err) if err else None)
        w.start(); self._w_it_set = w

    def _itech_output(self, on: bool):
        _log.info("Dashboard: ITECH output %s", "ON" if on else "OFF")
        def fn():
            with use_driver('itech', lambda: ITechFixed(ADDR['itech'])) as drv:
                return drv.on() if on else drv.off()
        w = InstrumentWorker(fn)
        w.result.connect(lambda val, err:
            _log.error("Dashboard: ITECH output toggle failed: %s", err) if err else None)
        w.start(); self._w_it_sw = w

    def _build_gpp_grp(self):
        grp = QGroupBox("GPP-4323")
        g = QGridLayout(grp)
        # Headers: row 0
        for col, hdr in enumerate(["", "Mode", "V (V)", "I (A)", "Set V", "Set I", ""]):
            lbl = QLabel(hdr); lbl.setStyleSheet("font-weight:bold;")
            g.addWidget(lbl, 0, col)
        self._gv = {}; self._gi = {}
        self._gpp_dash_mode = {}
        self._gpp_set_v = {}; self._gpp_set_i = {}
        for ch in range(1, 5):
            row = ch
            g.addWidget(QLabel(f"CH{ch}"), row, 0)

            # Mode combo for CH1/CH2; static label for CH3/CH4
            if ch in (1, 2):
                mode_cb = QComboBox()
                mode_cb.addItems(["Source", "Load CV", "Load CC", "Load CR"])
                mode_cb.setFixedWidth(80)
                mode_cb.setStyleSheet("font-size:8pt;")
                mode_cb.currentIndexChanged.connect(
                    lambda idx, _ch=ch: self._on_dash_gpp_mode(_ch, idx))
                self._gpp_dash_mode[ch] = mode_cb
                g.addWidget(mode_cb, row, 1)
            else:
                src_lbl = QLabel("Source")
                src_lbl.setStyleSheet("color:#a6adc8; font-style:italic; font-size:8pt;")
                g.addWidget(src_lbl, row, 1)

            v = self._val_lbl(TH.BLUE)
            i = self._val_lbl(TH.BLUE)
            g.addWidget(v, row, 2); g.addWidget(i, row, 3)
            self._gv[ch] = v; self._gi[ch] = i

            sv = QDoubleSpinBox()
            sv.setRange(0, 32); sv.setValue({1:5.0, 2:12.0, 3:3.3, 4:15.0}[ch])
            sv.setSuffix("V"); sv.setDecimals(3); sv.setFixedWidth(80)
            self._gpp_set_v[ch] = sv
            g.addWidget(sv, row, 4)

            si = QDoubleSpinBox()
            si.setRange(0, 3); si.setValue(1.0)
            si.setSuffix("A"); si.setDecimals(3); si.setFixedWidth(80)
            self._gpp_set_i[ch] = si
            g.addWidget(si, row, 5)

            btn_h = QHBoxLayout()
            b_set = QPushButton("Set")
            b_set.setProperty("role", "blue")
            b_set.setFixedHeight(22); b_set.setFixedWidth(36)
            b_set.setStyleSheet("min-width:0; padding:2px 4px; font-size:8pt;")
            b_set.clicked.connect(lambda _, c=ch: self._apply_gpp_ch(c))
            b_on = QPushButton("On")
            b_on.setProperty("role", "green")
            b_on.setFixedHeight(22); b_on.setFixedWidth(28)
            b_on.setStyleSheet("min-width:0; padding:2px 4px; font-size:8pt;")
            b_on.clicked.connect(lambda _, c=ch: self._gpp_output(c, True))
            b_off = QPushButton("Off")
            b_off.setProperty("role", "red")
            b_off.setFixedHeight(22); b_off.setFixedWidth(28)
            b_off.setStyleSheet("min-width:0; padding:2px 4px; font-size:8pt;")
            b_off.clicked.connect(lambda _, c=ch: self._gpp_output(c, False))
            btn_h.addWidget(b_set); btn_h.addWidget(b_on); btn_h.addWidget(b_off)
            btn_h.setSpacing(2)
            g.addLayout(btn_h, row, 6)
        return grp

    def _on_dash_gpp_mode(self, ch, idx):
        """Update V spinbox label when dashboard mode changes."""
        sv = self._gpp_set_v[ch]
        if idx == 3:  # CR
            sv.setSuffix("\u03a9"); sv.setRange(0.1, 10000); sv.setDecimals(2)
        else:
            sv.setSuffix("V"); sv.setRange(0, 32); sv.setDecimals(3)

    def _apply_gpp_ch(self, ch):
        v = self._gpp_set_v[ch].value()
        i = self._gpp_set_i[ch].value()
        _log.info("Dashboard: GPP CH%d apply V=%.3f I=%.3f", ch, v, i)
        mode_str = 'source'
        if ch in (1, 2):
            mode_str = ['source', 'cv', 'cc', 'cr'][
                self._gpp_dash_mode[ch].currentIndex()]
        _mode_map = {'cv': 'CV', 'cc': 'CC', 'cr': 'CR'}
        scpi_mode = _mode_map.get(mode_str)
        def fn():
            def _set(res):
                drv = GWGPP4323(res)
                if scpi_mode:
                    drv.set_load_mode(ch, scpi_mode)
                    if scpi_mode == 'CR':
                        drv.set_load_resistance(ch, v)
                    else:
                        drv.set_voltage(ch, v)
                    drv.set_current(ch, i)
                else:
                    if ch in (1, 2):
                        drv.set_load_mode(ch, 'OFF')
                    drv.set_voltage(ch, v)
                    drv.set_current(ch, i)
            return shared_resource('gpp').run(_set)
        w = InstrumentWorker(fn)
        w.result.connect(lambda val, err: None)
        w.start(); setattr(self, f'_w_gpp_set{ch}', w)

    def _gpp_output(self, ch, on: bool):
        def fn():
            def _sw(res):
                drv = GWGPP4323(res)
                drv.enable_channel(ch, on)
            return shared_resource('gpp').run(_sw)
        w = InstrumentWorker(fn)
        w.result.connect(lambda val, err: None)
        w.start(); setattr(self, f'_w_gpp_sw{ch}', w)

    def _build_gdm_grp(self):
        grp = QGroupBox("GDM-8261A")
        lay = QVBoxLayout(grp)
        top = QHBoxLayout()
        top.addWidget(QLabel("Function:"))
        self.gdm_func = QComboBox()
        self.gdm_func.addItems(["VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC", "RES", "FREQ"])
        top.addWidget(self.gdm_func); top.addStretch()
        lay.addLayout(top)
        self._gdm_val = self._val_lbl()
        self._gdm_val.setMinimumHeight(54)
        lay.addWidget(self._gdm_val)
        return grp

    def _build_dmm_grp(self):
        grp = QGroupBox("Keysight 34461A")
        lay = QVBoxLayout(grp)
        top = QHBoxLayout()
        top.addWidget(QLabel("Function:"))
        self.dmm_func = QComboBox()
        self.dmm_func.addItems(["VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC", "RES", "FREQ"])
        top.addWidget(self.dmm_func); top.addStretch()
        lay.addLayout(top)
        self._dmm_val = self._val_lbl()
        self._dmm_val.setMinimumHeight(54)
        lay.addWidget(self._dmm_val)
        return grp

    # ── Keepalive ────────────────────────────────────────────────────────────────────

    _KA_KEYS = instrument_registry.keepalive_keys()

    def _build_keepalive_grp(self):
        grp = QGroupBox("Instrument Status")
        row = QHBoxLayout(grp)
        row.setSpacing(10)

        self._ka_chk = QCheckBox("Auto-ping every 10 s")
        self._ka_chk.setChecked(True)
        self._ka_chk.setToolTip("Enable / disable automatic keepalive ping")
        row.addWidget(self._ka_chk)

        btn_now = QPushButton("Ping Now")
        btn_now.setFixedWidth(80)
        btn_now.setFixedHeight(22)
        btn_now.clicked.connect(self._tick_keepalive)
        row.addWidget(btn_now)
        row.addSpacing(12)

        self._ka_lbl  = {}
        self._ka_busy = {}
        self._ka_err_until = {}  # key -> monotonic time: skip pings until this time
        for key, name in self._KA_KEYS:
            lbl = QLabel(f"●  {name}")
            lbl.setStyleSheet(f"color:{TH.OVERLAY0}; font-size:{TH.FONT_SM};")
            lbl.setToolTip("Not yet pinged")
            row.addWidget(lbl)
            self._ka_lbl[key]  = lbl
            self._ka_busy[key] = False
        self._ka_ts = QLabel("—")
        self._ka_ts.setStyleSheet(f"color:{TH.SUBTEXT0}; font-size:{TH.FONT_SM}; font-style:italic;")
        row.addStretch()
        row.addWidget(QLabel("Last ping:"))
        row.addWidget(self._ka_ts)
        return grp

    def set_test_running(self, running: bool):
        """Called by sweep/sequence panels to pause keepalive during test runs."""
        self._test_running = running
        if running:
            _log.info("Dashboard: test running — keepalive paused")
        else:
            _log.info("Dashboard: test finished — keepalive resumed")

    def _toggle_keepalive(self, enabled: bool):
        if enabled:
            self._ka_timer.start()
            self._tick_keepalive()
        else:
            self._ka_timer.stop()

    def _tick_keepalive(self):
        if not self._ka_chk.isChecked():
            return
        # Skip keepalive pings while a sweep or sequence is actively running
        # to avoid VISA session conflicts (two threads hitting the same instrument)
        if self._test_running:
            self._ka_ts.setText(datetime.now().strftime("%H:%M:%S") + " (paused)")
            return
        self._ka_ts.setText(datetime.now().strftime("%H:%M:%S"))
        now_mono = time.monotonic()
        for key, _name in self._KA_KEYS:
            if self._ka_busy.get(key):
                continue  # previous ping still in flight
            # Skip instruments in error cooldown (30s after last failure)
            cooldown_until = self._ka_err_until.get(key, 0)
            if now_mono < cooldown_until:
                continue
            addr = ADDR.get(key, '')
            if not addr or 'XXX' in addr:
                self._ka_set(key, 'gray', 'address not configured')
                continue
            self._ka_busy[key] = True
            w = InstrumentWorker(lambda k=key: self._ka_ping(k))
            w.result.connect(lambda val, err, k=key: self._ka_done(k, val, err))
            w.start()
            setattr(self, f'_kaw_{key}', w)

    def _ka_ping(self, key):
        lock = _instr_lock(key)
        if not lock.acquire(blocking=False):
            return "ok (busy)"   # sequence/sweep runner holds the instrument
        rm = res = None
        try:
            rm, res = open_resource(key, timeout=2000)
            return res.query("*IDN?").strip()
        finally:
            for obj in (res, rm):
                try:
                    if obj is not None: obj.close()
                except Exception:
                    pass
            lock.release()

    def _ka_done(self, key, val, err):
        self._ka_busy[key] = False
        if err:
            _log.debug("Keepalive %s FAIL: %s", key, err[:120])
            self._ka_set(key, 'red', err[:80])
            # Back off: don't re-ping this instrument for 30 seconds after a failure
            self._ka_err_until[key] = time.monotonic() + 30
        else:
            tip = str(val)[:60] if val else 'ok'
            self._ka_set(key, 'green', tip)
            self._ka_err_until.pop(key, None)  # clear cooldown on success

    def _ka_set(self, key, color, tooltip=''):
        lbl = self._ka_lbl.get(key)
        if not lbl:
            return
        name = lbl.text().split('  ', 1)[-1]
        c = {'green': TH.GREEN, 'red': TH.RED, 'gray': TH.OVERLAY0}.get(color, TH.OVERLAY0)
        lbl.setText(f"●  {name}")
        lbl.setStyleSheet(f"color:{c}; font-size:{TH.FONT_SM}; font-weight:{'bold' if color=='red' else 'normal'};")
        lbl.setToolTip(tooltip)

    # ── Toggle ──────────────────────────────────────────────────────────────────────

    def _toggle(self):
        if self._timer.isActive():
            self._timer.stop()
            self.btn_live.setText("▶  Start Live Readings")
            self.btn_live.setProperty("role", "green")
        else:
            self.btn_live.setText("■  Stop Live Readings")
            self.btn_live.setProperty("role", "red")
            self._timer.start()
            self._tick()
        self.btn_live.style().unpolish(self.btn_live)
        self.btn_live.style().polish(self.btn_live)

    # ── Periodic readings ──────────────────────────────────────────────────────────

    def _tick(self):
        self._ts_lbl.setText(datetime.now().strftime("%H:%M:%S"))
        if self._test_running:
            return  # pause live readings while sweep/sequence is active
        self._read_itech()
        self._read_gpp()
        self._read_gdm()
        self._read_dmm()
        self._read_eddie()

    def _read_itech(self):
        if getattr(self, "_w_it", None) and self._w_it.isRunning():
            return
        def fn():
            with use_driver('itech', lambda: ITechFixed(ADDR['itech'])) as drv:
                data = drv.measure()
                return float(data['dc_volts']), float(data['dc_amps'])
        w = InstrumentWorker(fn)
        vl, il, pl = self._it_v, self._it_i, self._it_p
        def done(val, err):
            if err or val is None:
                vl.setText("—"); il.setText("—"); pl.setText("—")
            else:
                v, i = val
                vl.setText(f"{v:.4f} V"); il.setText(f"{i:.4f} A")
                pl.setText(f"{v*i:.4f} W")
        w.result.connect(done); w.start(); self._w_it = w

    def _read_gpp(self):
        if getattr(self, "_w_gpp", None) and self._w_gpp.isRunning():
            return
        def fn():
            def _meas(res):
                data = {}
                for ch in range(1, 5):
                    data[ch] = (
                        float(res.query(f"MEAS{ch}:VOLT?").strip()),
                        float(res.query(f"MEAS{ch}:CURR?").strip()),
                    )
                return data
            return shared_resource('gpp').run(_meas, skip_if_busy=True)
        w = InstrumentWorker(fn)
        gv, gi = self._gv, self._gi
        def done(val, err):
            for ch in range(1, 5):
                if err or val is None:
                    gv[ch].setText("—"); gi[ch].setText("—")
                else:
                    v, i = val[ch]
                    gv[ch].setText(f"{v:.4f} V"); gi[ch].setText(f"{i:.4f} A")
        w.result.connect(done); w.start(); self._w_gpp = w

    def _read_gdm(self):
        if getattr(self, "_w_gdm", None) and self._w_gdm.isRunning():
            return
        func = self.gdm_func.currentText()
        needs_conf = getattr(self, '_dash_gdm_last_func', None) != func
        self._dash_gdm_last_func = func
        def job(f=func, _conf=needs_conf):
            with use_instrument('gdm') as res:
                if _conf:
                    res.write(f"CONF:{f}")
                    time.sleep(0.1)
                return float(res.query("READ?").strip().split(",")[0])
        w = InstrumentWorker(job)
        lbl = self._gdm_val
        def done(val, err):
            lbl.setText("—" if err or val is None else f"{val:.6g}")
        w.result.connect(done); w.start(); self._w_gdm = w

    def _read_dmm(self):
        if getattr(self, "_w_dmm", None) and self._w_dmm.isRunning():
            return
        func = self.dmm_func.currentText()
        def fn(f=func):
            with use_instrument("dmm") as res:
                return float(res.query(f"MEAS:{f}? AUTO,DEF").strip())
        w = InstrumentWorker(fn)
        lbl = self._dmm_val
        def done(val, err):
            lbl.setText("—" if err or val is None else f"{val:.6g}")
        w.result.connect(done); w.start(); self._w_dmm = w

    def set_eddie_panel(self, panel):
        """Link the EddiePanel so its watchlist snapshot feeds the Dashboard."""
        self._eddie_panel = panel

    def _build_eddie_grp(self):
        grp = QGroupBox("EDDIE Device  —  Watchlist Live Values")
        lay = QVBoxLayout(grp)
        self._eddie_status = QLabel("EDDIE panel not linked")
        self._eddie_status.setStyleSheet(f"color:{TH.OVERLAY0}; font-style:italic; font-size:{TH.FONT_SM};")
        lay.addWidget(self._eddie_status)
        self._eddie_tbl = QTableWidget(0, 2)
        self._eddie_tbl.setHorizontalHeaderLabels(["Register", "Value"])
        hdr = self._eddie_tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._eddie_tbl.verticalHeader().setVisible(False)
        self._eddie_tbl.setMaximumHeight(120)
        self._eddie_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._eddie_tbl.setAlternatingRowColors(True)
        lay.addWidget(self._eddie_tbl)
        self._eddie_panel = None
        self._eddie_snap_keys: list = []
        return grp

    def _read_eddie(self):
        panel = getattr(self, '_eddie_panel', None)
        if panel is None:
            return
        snap      = panel.get_watchlist_snapshot()
        connected = getattr(panel, '_pcu', None) is not None
        if not snap:
            self._eddie_status.setText(
                "Connected  —  watchlist empty" if connected else "Not connected")
            self._eddie_status.setStyleSheet(
                f"color:{TH.GREEN}; font-style:italic; font-size:{TH.FONT_SM};" if connected
                else f"color:{TH.OVERLAY0}; font-style:italic; font-size:{TH.FONT_SM};")
            if self._eddie_tbl.rowCount():
                self._eddie_tbl.setRowCount(0)
                self._eddie_snap_keys = []
            return
        keys = list(snap.keys())
        if keys != self._eddie_snap_keys:
            self._eddie_snap_keys = keys
            self._eddie_tbl.setRowCount(len(keys))
            for r, k in enumerate(keys):
                name_it = QTableWidgetItem(k.split('.')[-1])
                name_it.setToolTip(k)
                name_it.setFlags(name_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._eddie_tbl.setItem(r, 0, name_it)
                val_it = QTableWidgetItem("—")
                val_it.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                val_it.setFlags(val_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._eddie_tbl.setItem(r, 1, val_it)
        for r, k in enumerate(keys):
            val  = snap.get(k, "—")
            item = self._eddie_tbl.item(r, 1)
            if item:
                s = str(val)
                item.setText(s)
                item.setForeground(
                    QColor(TH.RED) if s.startswith("Err") else QColor(TH.GREEN))
        n = len(keys)
        self._eddie_status.setText(
            f"{'Connected' if connected else 'Last snapshot'}  —  "
            f"{n} register{'s' if n != 1 else ''}")
        self._eddie_status.setStyleSheet(f"color:{TH.GREEN}; font-style:italic; font-size:{TH.FONT_SM};")


# ─────────────────────────────────────────────────────────────────────────────
# Addresses tab
# ─────────────────────────────────────────────────────────────────────────────

class AddressesTab(QWidget):
    _META = instrument_registry.address_meta()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)

        grp  = QGroupBox("Instrument VISA Addresses")
        grid = QGridLayout(grp)
        grid.setColumnStretch(1, 1)
        self.inputs = {}

        for row, (key, (label, placeholder)) in enumerate(self._META.items()):
            grid.addWidget(QLabel(label + ":"), row, 0)
            inp = QLineEdit()
            inp.setText(ADDR.get(key, ""))
            inp.setPlaceholderText(placeholder)
            self.inputs[key] = inp
            grid.addWidget(inp, row, 1)

            btn_test = QPushButton("Test"); btn_test.setFixedWidth(55)
            btn_test.clicked.connect(lambda _, k=key: self._test(k))
            grid.addWidget(btn_test, row, 2)

            btn_ports = QPushButton("Ports"); btn_ports.setFixedWidth(55)
            btn_ports.clicked.connect(lambda _, k=key: self._ports(k))
            grid.addWidget(btn_ports, row, 3)

        lay.addWidget(grp)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("Save Addresses")
        btn_save.setProperty("role", "blue")
        btn_save.clicked.connect(self._save)
        self.save_status = QLabel("")
        btn_row.addWidget(btn_save); btn_row.addWidget(self.save_status)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        scan_grp = QGroupBox("Scan for VISA Resources  (USB / GPIB)")
        sl  = QVBoxLayout(scan_grp)
        btn_scan = QPushButton("Scan")
        btn_scan.clicked.connect(self._scan)
        self.scan_out = QTextEdit(); self.scan_out.setReadOnly(True)
        self.scan_out.setMaximumHeight(130)
        sl.addWidget(btn_scan); sl.addWidget(self.scan_out)
        lay.addWidget(scan_grp)
        lay.addStretch()

    def _save(self):
        for key, inp in self.inputs.items():
            v = inp.text().strip()
            if v:
                ADDR[key] = v
        if save_addresses():
            self.save_status.setText("Saved")
            self.save_status.setStyleSheet(f"color:{TH.GREEN};")
        else:
            self.save_status.setText("Failed")
            self.save_status.setStyleSheet(f"color:{TH.RED};")
        QTimer.singleShot(3000, lambda: self.save_status.setText(""))

    def _test(self, key):
        addr = self.inputs[key].text().strip()
        if not addr or 'XXX' in addr:
            QMessageBox.warning(self, "Invalid", "Enter a valid address first.")
            return
        ADDR[key] = addr
        try:
            rm, res = open_resource(key, timeout=4000)
            idn = res.query("*IDN?").strip()
            res.close(); rm.close()
            QMessageBox.information(self, f"Connected — {key.upper()}", idn)
        except Exception as e:
            QMessageBox.critical(self, "Connection Failed", str(e))

    def _ports(self, key):
        addr = self.inputs[key].text().strip()
        r = _check_ports(addr)
        if not r:
            QMessageBox.warning(self, "N/A", "Port scan only works for TCPIP addresses.")
            return

        # Extract the instrument's configured port (if any) from the address
        m = re.search(r'::(\d+)::SOCKET', addr, re.IGNORECASE)
        cfg_port = int(m.group(1)) if m else None

        lines = [f"{k:12s}: {'OPEN' if o else 'closed'}" for k, o in r.items()]
        any_open   = any(r.values())
        vxi11      = r.get('udp_111', False)
        cfg_open   = r.get(f'tcp_{cfg_port}', False) if cfg_port else False
        scpi_open  = r.get('tcp_5025', False) or r.get('tcp_1234', False)

        lines.append("")
        if cfg_open:
            lines.append(f"Instrument port {cfg_port} is OPEN — connection should work")
        elif cfg_port:
            lines.append(
                f"Port {cfg_port} (configured in address) is CLOSED.\n"
                f"   GPP-4323: press SHIFT+LAN on front panel -> Socket -> Server: ON\n"
                f"   Note the port shown and update the address here if it changed.\n"
                f"   Also verify IP: 169.254.x.x auto-IP can change on reconnect.")
        if vxi11:
            lines.append("VXI-11 portmapper (UDP 111) reachable")
        if scpi_open:
            lines.append("SCPI socket port (5025 or 1234) open")
        if not any_open:
            lines.append(
                "No TCP/UDP ports open on this IP.\n"
                "   Verify the IP address (auto-IP 169.254.x.x changes on reconnect)\n"
                "   GPP-4323: SHIFT+LAN -> Socket -> Server: ON, note port\n"
                "   Use 'Test' button to try a live SCPI connection")
        QMessageBox.information(self, f"Port Scan — {key.upper()}", '\n'.join(lines))

    def _scan(self):
        self.scan_out.clear(); self.scan_out.append("Scanning...")
        try:
            rm = pyvisa.ResourceManager()
            found = rm.list_resources(); rm.close()
            self.scan_out.clear()
            if found:
                for r in found:
                    self.scan_out.append(r)
            else:
                self.scan_out.append(
                    "No USB/GPIB resources found.\n"
                    "LAN instruments must be entered manually.")
        except Exception as e:
            self.scan_out.append(f"Error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Detachable Tab Widget — tabs can be popped out into floating windows
# ─────────────────────────────────────────────────────────────────────────────

class DetachableTabWidget(QTabWidget):
    """QTabWidget where tabs are moveable and can be popped out to floating
    windows via double-click on the tab bar or right-click context menu."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(True)
        self.setTabsClosable(False)
        self._detached: dict = {}   # title -> QDockWidget-like floating window
        self.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._tab_ctx_menu)
        self.tabBar().tabBarDoubleClicked.connect(self._pop_out_tab)

    def _tab_ctx_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        idx = self.tabBar().tabAt(pos)
        if idx < 0:
            return
        menu = QMenu(self)
        act_pop = menu.addAction("Pop Out to Window")
        chosen = menu.exec(self.tabBar().mapToGlobal(pos))
        if chosen == act_pop:
            self._pop_out_tab(idx)

    def _pop_out_tab(self, idx):
        if idx < 0 or idx >= self.count():
            return
        title  = self.tabText(idx)
        widget = self.widget(idx)
        if widget is None:
            return

        # Create floating window
        win = QWidget()
        win.setWindowTitle(f"Automated Test Suite  —  {title}")
        win.resize(900, 650)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(0, 0, 0, 0)

        # Re-dock button
        dock_btn = QPushButton(f"Dock back: {title}")
        dock_btn.setProperty("role", "blue")
        dock_btn.setFixedHeight(28)
        lay.addWidget(dock_btn)
        lay.addWidget(widget)

        self.removeTab(idx)
        win.show()
        self._detached[title] = (win, widget, idx)

        def _re_dock():
            win.hide()
            lay.removeWidget(widget)
            # Re-insert at original position (or end if tabs shifted)
            insert_idx = min(idx, self.count())
            self.insertTab(insert_idx, widget, title)
            self.setCurrentIndex(insert_idx)
            del self._detached[title]
            win.deleteLater()

        dock_btn.clicked.connect(_re_dock)
        win.closeEvent = lambda ev: (_re_dock(), ev.accept())

    def re_dock_all(self):
        """Re-dock all floating tabs — call before app close."""
        for title in list(self._detached.keys()):
            win, widget, idx = self._detached[title]
            win.hide()
            insert_idx = min(idx, self.count())
            self.insertTab(insert_idx, widget, title)
            win.deleteLater()
        self._detached.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        _log.info("=" * 60)
        _log.info("Application starting")
        self.setWindowTitle("Automated Test Suite  —  Instrument Control")
        self.resize(1400, 900)
        self.setMinimumSize(1000, 650)

        # ── Create all panels ─────────────────────────────────────────────
        self._sweep_p = SweepPanel()
        self._seq_p   = SequencePanel()
        self._dash_p  = DashboardTab()
        self._eddie_p = EddiePanel()
        self._sweep_p.set_eddie_panel(self._eddie_p)
        self._seq_p.set_eddie_panel(self._eddie_p)
        self._sweep_p.steps_exported.connect(self._seq_p.load_steps)
        self._sweep_p.test_running.connect(self._dash_p.set_test_running)
        self._seq_p.test_running.connect(self._dash_p.set_test_running)
        self._dash_p.set_eddie_panel(self._eddie_p)

        # Instrument panels — keyed by registry key
        self._inst_panels: dict = {}
        _panel_classes = {
            'ItechPanel': ItechPanel,
            'GppPanel': GppPanel,
            'GdmPanel': GdmPanel,
            'DmmPanel': DmmPanel,
            'MdoPanel': MdoPanel,
            'WT5000Panel': WT5000Panel,
            'NgiCellSimPanel': NgiCellSimPanel,
        }
        for entry in instrument_registry.native_instruments():
            cls_name = entry['panel_cls']
            if cls_name in _panel_classes:
                widget = _panel_classes[cls_name]()
                self._inst_panels[entry['key']] = (widget, entry['name'])

        # Non-registry panels
        self._daq_p    = KeysightDAQ973APanel()
        self._espec_p  = EspecSH662Panel()
        self._inst_panels['daq']   = (self._daq_p,   'Keysight DAQ 973A')
        self._inst_panels['espec'] = (self._espec_p,  'ESPEC SH-662')

        self._scpi_p  = ScpiTerminal()
        self._lib_p   = InstrumentLibraryPanel()
        self._addr_p  = AddressesTab()

        # Convenience aliases
        self._itech_p  = self._inst_panels.get('itech',  (None,))[0]
        self._gpp_p    = self._inst_panels.get('gpp',    (None,))[0]
        self._gdm_p    = self._inst_panels.get('gdm',    (None,))[0]
        self._dmm_p    = self._inst_panels.get('dmm',    (None,))[0]
        self._mdo_p    = self._inst_panels.get('mdo',    (None,))[0]
        self._wt5000_p = self._inst_panels.get('wt5000', (None,))[0]
        self._ngi_p    = self._inst_panels.get('ngi',    (None,))[0]

        # Wire cross-panel dependencies
        self._sweep_p.set_daq_panel(self._daq_p)
        self._sweep_p.set_espec_panel(self._espec_p)
        self._sweep_p.set_gpp_panel(self._gpp_p)
        self._seq_p.set_daq_panel(self._daq_p)
        self._seq_p.set_espec_panel(self._espec_p)
        self._seq_p.set_gpp_panel(self._gpp_p)

        # ── Build the main layout: sidebar + stacked content ──────────────
        central = QWidget()
        main_h  = QHBoxLayout(central)
        main_h.setContentsMargins(0, 0, 0, 0)
        main_h.setSpacing(0)

        # Left sidebar
        sidebar_w = QWidget()
        sidebar_w.setFixedWidth(220)
        sidebar_w.setStyleSheet(f"background:{TH.MANTLE};")
        sb_lay = QVBoxLayout(sidebar_w)
        sb_lay.setContentsMargins(0, 6, 0, 6)
        sb_lay.setSpacing(4)

        # E-STOP button at top of sidebar
        self._estop_btn = QPushButton("E-STOP")
        self._estop_btn.setProperty("role", "estop")
        self._estop_btn.setFixedHeight(40)
        self._estop_btn.setToolTip("Emergency stop — turn off ALL instrument outputs\n(Ctrl+Shift+X)")
        self._estop_btn.clicked.connect(self._emergency_stop)
        sb_lay.addWidget(self._estop_btn)

        # Sidebar navigation tree
        self._nav_tree = QTreeWidget()
        self._nav_tree.setHeaderHidden(True)
        self._nav_tree.setIndentation(16)
        self._nav_tree.setAnimated(True)
        self._nav_tree.setRootIsDecorated(True)
        self._nav_tree.itemClicked.connect(self._on_nav_click)
        sb_lay.addWidget(self._nav_tree, stretch=1)

        # Collapse sidebar button
        self._collapse_btn = QPushButton("<<")
        self._collapse_btn.setFixedHeight(22)
        self._collapse_btn.setStyleSheet(
            f"background:{TH.SURFACE0}; color:{TH.SUBTEXT0}; border:none;"
            f"border-radius:3px; font-size:9pt; min-width:0; padding:2px;")
        self._collapse_btn.clicked.connect(self._toggle_sidebar)
        sb_lay.addWidget(self._collapse_btn)

        # Content area — QStackedWidget for main sections
        self._content_stack = QStackedWidget()

        # Right side: content
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)
        right_lay.addWidget(self._content_stack)

        main_h.addWidget(sidebar_w)
        self._sidebar_w = sidebar_w

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color:{TH.SURFACE1};")
        main_h.addWidget(sep)

        main_h.addWidget(right_w, stretch=1)
        self.setCentralWidget(central)

        # ── Register pages in the content stack ───────────────────────────
        self._page_map: dict = {}   # nav_key → stack index

        def _add_page(key, widget):
            idx = self._content_stack.addWidget(widget)
            self._page_map[key] = idx
            return idx

        _add_page('sweep',      self._sweep_p)
        _add_page('sequence',   self._seq_p)
        _add_page('dashboard',  self._dash_p)
        _add_page('eddie',      self._eddie_p)

        # Instruments: detachable tab widget
        self._inst_tabs = DetachableTabWidget()
        self._inst_tab_idx: dict = {}
        for key, (panel, label) in self._inst_panels.items():
            idx = self._inst_tabs.addTab(panel, label)
            self._inst_tabs.setTabVisible(idx, False)
            self._inst_tab_idx[key] = idx
        self._inst_tabs.addTab(self._lib_p, "Instrument Library")
        _add_page('instruments', self._inst_tabs)

        _add_page('addresses', self._addr_p)
        _add_page('scpi',     self._scpi_p)
        self._help_page = self._build_help_page()
        _add_page('help',     self._help_page)

        # ── Build navigation tree ─────────────────────────────────────────
        self._nav_items: dict = {}   # nav_key → QTreeWidgetItem

        # Test Automation section
        auto_root = QTreeWidgetItem(self._nav_tree, ["Test Automation"])
        auto_root.setFlags(auto_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        f = auto_root.font(0)
        f.setBold(True)
        auto_root.setFont(0, f)
        auto_root.setForeground(0, QColor(TH.MAUVE))

        for key, label in [('sweep', 'Sweep'), ('sequence', 'Sequence')]:
            item = QTreeWidgetItem(auto_root, [label])
            self._nav_items[key] = item

        # Monitoring section
        mon_root = QTreeWidgetItem(self._nav_tree, ["Monitoring"])
        mon_root.setFlags(mon_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        f2 = mon_root.font(0)
        f2.setBold(True)
        mon_root.setFont(0, f2)
        mon_root.setForeground(0, QColor(TH.MAUVE))

        for key, label in [('dashboard', 'Dashboard'), ('eddie', 'EDDIE Device')]:
            item = QTreeWidgetItem(mon_root, [label])
            self._nav_items[key] = item

        # Instruments section
        inst_root = QTreeWidgetItem(self._nav_tree, ["Instruments"])
        inst_root.setFlags(inst_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        f3 = inst_root.font(0)
        f3.setBold(True)
        inst_root.setFont(0, f3)
        inst_root.setForeground(0, QColor(TH.MAUVE))

        item = QTreeWidgetItem(inst_root, ["Instrument Library"])
        self._nav_items['instruments'] = item

        # Individual instrument items under Instruments (greyed out until connected)
        self._inst_nav_items: dict = {}
        for key, (panel, label) in self._inst_panels.items():
            inst_item = QTreeWidgetItem(inst_root, [label])
            inst_item.setForeground(0, QColor(TH.OVERLAY0))
            inst_item.setToolTip(0, "Not connected")
            self._inst_nav_items[key] = inst_item

        # Configuration section
        cfg_root = QTreeWidgetItem(self._nav_tree, ["Configuration"])
        cfg_root.setFlags(cfg_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        f4 = cfg_root.font(0)
        f4.setBold(True)
        cfg_root.setFont(0, f4)
        cfg_root.setForeground(0, QColor(TH.MAUVE))

        for key, label in [('addresses', 'Addresses'), ('scpi', 'SCPI Terminal')]:
            item = QTreeWidgetItem(cfg_root, [label])
            self._nav_items[key] = item

        # Help section (top-level leaf)
        help_item = QTreeWidgetItem(self._nav_tree, ["Help"])
        help_item.setForeground(0, QColor(TH.TEAL))
        f_help = help_item.font(0)
        f_help.setBold(True)
        help_item.setFont(0, f_help)
        self._nav_items['help'] = help_item

        self._nav_tree.expandAll()

        # ── Connect library signals ───────────────────────────────────────
        self._lib_p.instrument_connected.connect(self._on_lib_connected)
        self._lib_p.instrument_disconnected.connect(self._on_lib_disconnected)
        self._lib_p.instrument_connected.connect(
            lambda key, _drv: self._sweep_p.notify_instrument_connected(key))
        self._lib_p.instrument_connected.connect(
            lambda key, _drv: self._seq_p.notify_instrument_connected(key))
        self._lib_p.instrument_disconnected.connect(
            self._sweep_p.notify_instrument_disconnected)
        self._lib_p.instrument_disconnected.connect(
            self._seq_p.notify_instrument_disconnected)

        # ── Status bar with connection indicators ─────────────────────────
        self._build_status_bar()

        # ── Load persisted state ──────────────────────────────────────────
        persist.load_all(self._panels())
        self._seq_p.restore_steps_session()

        # ── Auto-save ─────────────────────────────────────────────────────
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(2000)
        self._autosave_timer.timeout.connect(self._autosave)
        persist.connect_autosave(self._panels(), self._autosave_timer.start)
        self._seq_p.connect_steps_autosave(self._autosave_timer.start)

        # ── Keyboard shortcuts ────────────────────────────────────────────
        self._setup_shortcuts()

        # ── Select Sweep by default ───────────────────────────────────────
        self._navigate_to('sweep')

        # ── Restore window geometry ───────────────────────────────────────
        self._load_settings()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_nav_click(self, item, column):
        # Check main pages
        for key, nav_item in self._nav_items.items():
            if item is nav_item:
                self._navigate_to(key)
                return
        # Check instrument items — navigate to instruments tab and select
        for key, inst_item in self._inst_nav_items.items():
            if item is inst_item:
                idx = self._inst_tab_idx.get(key)
                if idx is not None and self._inst_tabs.isTabVisible(idx):
                    self._navigate_to('instruments')
                    self._inst_tabs.setCurrentIndex(idx)
                else:
                    # Not connected — go to library
                    self._navigate_to('instruments')
                return

    def _navigate_to(self, key):
        idx = self._page_map.get(key)
        if idx is not None:
            self._content_stack.setCurrentIndex(idx)
            # Highlight in tree
            item = self._nav_items.get(key)
            if item:
                self._nav_tree.setCurrentItem(item)

    def _toggle_sidebar(self):
        if self._sidebar_w.isVisible():
            self._sidebar_w.hide()
            self._collapse_btn.setText(">>")
            # Move button outside sidebar — not possible cleanly, so just hide
        else:
            self._sidebar_w.show()
            self._collapse_btn.setText("<<")

    # ── Status bar with connection indicators ─────────────────────────────

    # ── Help page ─────────────────────────────────────────────────────────

    def _build_help_page(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)

        def _title(text):
            l = QLabel(text)
            l.setStyleSheet(f"font-size:18pt; font-weight:bold; color:{TH.MAUVE};"
                            f"border-bottom:2px solid {TH.SURFACE1}; padding-bottom:6px;")
            l.setWordWrap(True)
            return l

        def _section(heading):
            l = QLabel(heading)
            l.setStyleSheet(f"font-size:13pt; font-weight:bold; color:{TH.BLUE};"
                            f"margin-top:8px;")
            l.setWordWrap(True)
            return l

        def _body(text):
            l = QLabel(text)
            l.setStyleSheet(f"font-size:10pt; color:{TH.TEXT}; line-height:1.5;")
            l.setWordWrap(True)
            l.setTextFormat(Qt.TextFormat.RichText)
            l.setOpenExternalLinks(True)
            return l

        lay.addWidget(_title("Automated Test Suite — Help"))

        # ── Navigation
        lay.addWidget(_section("Navigation"))
        lay.addWidget(_body(
            "The app uses a <b>sidebar tree</b> on the left for navigation. "
            "Click any item to switch to that page. Sections are:<br>"
            "<b>Test Automation</b> — Sweep (parametric sweeps) and Sequence (step-based tests)<br>"
            "<b>Monitoring</b> — Dashboard (live readings, keepalive, quick-set controls) and EDDIE Device<br>"
            "<b>Instruments</b> — Instrument Library (connect/disconnect) plus individual instrument panels<br>"
            "<b>Configuration</b> — Addresses (IP/VISA addresses) and SCPI Terminal (raw commands)<br><br>"
            "Toggle the sidebar with the <code>&lt;&lt;</code> / <code>&gt;&gt;</code> button or <b>Ctrl+B</b>."
        ))

        # ── Keyboard Shortcuts
        lay.addWidget(_section("Keyboard Shortcuts"))
        lay.addWidget(_body(
            "<table cellpadding='4' style='border-collapse:collapse;'>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+Shift+X</b></td>"
            "<td>Emergency Stop — turns off all instrument outputs immediately</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+1</b></td><td>Go to Sweep</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+2</b></td><td>Go to Sequence</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+3</b></td><td>Go to Dashboard</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+4</b></td><td>Go to EDDIE</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+5</b></td><td>Go to Instruments</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+6</b></td><td>Go to Addresses</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+7</b></td><td>Go to SCPI Terminal</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+S</b></td><td>Save all panel state</td></tr>"
            "<tr><td style='color:#f9e2af;'><b>Ctrl+B</b></td><td>Toggle sidebar</td></tr>"
            "</table>"
        ))

        # ── E-STOP
        lay.addWidget(_section("Emergency Stop (E-STOP)"))
        lay.addWidget(_body(
            "The red <b>E-STOP</b> button is always visible at the top of the sidebar. "
            "Press it or use <b>Ctrl+Shift+X</b> to immediately:<br>"
            "- Abort any running sweep or sequence<br>"
            "- Turn off ITECH output<br>"
            "- Turn off all 4 GPP channels and reset load modes<br>"
            "- Turn off all 17 NGI channels<br><br>"
            "A summary dialog shows what was stopped."
        ))

        # ── Dashboard
        lay.addWidget(_section("Dashboard"))
        lay.addWidget(_body(
            "The Dashboard shows <b>live readings</b> from connected instruments. "
            "Press <b>Start Live</b> to begin polling (1-second interval).<br><br>"
            "<b>Quick-set controls:</b><br>"
            "- <b>ITECH</b>: Set voltage, current, and toggle output ON/OFF directly from the dashboard<br>"
            "- <b>GPP-4323</b>: Per-channel voltage/current set + ON/OFF buttons<br><br>"
            "<b>Keepalive</b>: When enabled, periodically pings all instruments and shows "
            "connection status in the bottom status bar (green = OK, red = error, grey = unknown)."
        ))

        # ── Sweep Panel
        lay.addWidget(_section("Sweep Panel"))
        lay.addWidget(_body(
            "Run <b>parametric sweeps</b> across one or more source instruments. "
            "The layout is split left (config) and right (run controls + log).<br><br>"
            "<b>Source instruments</b> (any combination):<br>"
            "- <b>ITECH IT6005C</b> — voltage/current sweep, source or sink mode<br>"
            "- <b>GPP-4323</b> — per-channel voltage/current sweep with load mode support "
            "(CV/CC/CR on CH1 and CH2)<br>"
            "- <b>NGI N83624</b> — cell voltage sweep across 16 cells + busbar control<br>"
            "- <b>ESPEC SH-662</b> — temperature sweep<br><br>"
            "<b>Measurements</b>: GDM (multimeter), DMM, ITECH V/I, GPP per-channel V/I, "
            "WT5000 power analyzer, DAQ relay scanner, ESPEC T/H, NGI cell/bus V/I, MDO waveform capture<br><br>"
            "<b>Spacing</b>: Linear or logarithmic sweep steps<br>"
            "<b>Output</b>: Excel file with all setpoints, measurements, and optional waveform images<br>"
            "<b>Send to Sequence</b>: Convert sweep points to editable sequence steps"
        ))

        # ── Sequence Panel
        lay.addWidget(_section("Sequence Panel"))
        lay.addWidget(_body(
            "Define <b>step-by-step test sequences</b> in a table. Each row specifies "
            "instrument setpoints (ITECH, GPP CH1-4, ESPEC).<br><br>"
            "<b>Table features</b>:<br>"
            "- Add, delete, duplicate, reorder rows<br>"
            "- Column group toggles (ITECH, GPP CH1-4, Tracking, Load Mode, ESPEC) to show/hide columns<br>"
            "- Load mode cells are highlighted in orange when not 'Source'<br>"
            "- GPP CH1/CH2 load mode support (CV/CC/CR) per step<br>"
            "- Right-click for context menu<br><br>"
            "<b>Timing</b>: Configure readings per step, interval, settling time, and averaging<br>"
            "<b>Measurements</b>: Same measurement inputs as the Sweep panel<br>"
            "<b>Post-test</b>: After the sequence completes, instruments are set to match the "
            "<b>first row</b> of the sequence table (voltage, current, enable state)<br>"
            "<b>Export/Import</b>: Save and load sequences as Excel files"
        ))

        # ── Instrument Library
        lay.addWidget(_section("Instrument Library"))
        lay.addWidget(_body(
            "The Instrument Library shows all registered instruments organized by category "
            "(Power Supplies, Multimeters, Power Analyzers, Environmental, Oscilloscopes, Custom).<br><br>"
            "Click <b>Connect</b> to establish a VISA connection. On success, the instrument's "
            "dedicated panel tab becomes visible in the Instruments section.<br><br>"
            "<b>Instrument tabs</b> are moveable (drag to reorder) and can be <b>popped out</b> to floating windows:<br>"
            "- <b>Double-click</b> a tab to pop it out<br>"
            "- <b>Right-click</b> a tab for 'Pop Out to Window' option<br>"
            "- Floating windows have a 'Dock back' button to return the tab<br>"
            "- All floating tabs auto-dock when the app closes"
        ))

        # ── Instrument Panels
        lay.addWidget(_section("Individual Instrument Panels"))
        lay.addWidget(_body(
            "<b>ITECH IT6005C</b> — Voltage/current set, source/sink mode toggle, output ON/OFF, "
            "live V/I/P plot, protection settings<br>"
            "<b>GPP-4323</b> — 4-channel control with per-channel voltage, current, enable. "
            "CH1/CH2 support electronic load modes (CV/CC/CR). Tracking modes (Independent/Series/Parallel)<br>"
            "<b>GDM-8261A</b> — Multimeter function selection, manual read, continuous monitoring<br>"
            "<b>DMM (MDO)</b> — Secondary multimeter<br>"
            "<b>WT5000</b> — Power analyzer with multi-element measurement, live plotting, "
            "integration state control<br>"
            "<b>NGI N83624</b> — 24-channel cell simulator. Overview (all channels), Batch control, "
            "per-Channel settings, Protection, System info, live Plot. "
            "16 cell channels + 1 busbar<br>"
            "<b>MDO 3034</b> — Oscilloscope waveform capture<br>"
            "<b>DAQ 973A</b> — Data acquisition relay scanner<br>"
            "<b>ESPEC SH-662</b> — Environmental chamber temperature and humidity control"
        ))

        # ── Addresses
        lay.addWidget(_section("Addresses Configuration"))
        lay.addWidget(_body(
            "Set VISA addresses for each instrument. Supports:<br>"
            "- <b>TCPIP</b>: e.g. <code>TCPIP0::10.73.77.200::7000::SOCKET</code><br>"
            "- <b>ASRL</b>: e.g. <code>ASRL3::INSTR</code> (USB/serial)<br><br>"
            "Changes take effect on the next connection attempt. "
            "The app caches successful addresses for faster reconnection."
        ))

        # ── SCPI Terminal
        lay.addWidget(_section("SCPI Terminal"))
        lay.addWidget(_body(
            "Send <b>raw SCPI commands</b> to any connected instrument. "
            "Select the instrument from the dropdown, type the command, and press Enter or 'Send'.<br>"
            "Query responses are displayed in the log area."
        ))

        # ── GPP Load Mode
        lay.addWidget(_section("GPP Load Mode (CH1 & CH2)"))
        lay.addWidget(_body(
            "The GPP-4323 CH1 and CH2 support electronic load mode. Available in:<br>"
            "- <b>GPP panel</b> (Instruments tab) — per-channel load controls<br>"
            "- <b>Sweep panel</b> — configure fixed load mode during sweeps<br>"
            "- <b>Sequence panel</b> — per-step load mode via G1 Load / G2 Load columns<br><br>"
            "<b>Modes</b>:<br>"
            "- <b>Source</b> (default) — normal voltage/current source<br>"
            "- <b>Load CV</b> — constant voltage load (setpoint in V)<br>"
            "- <b>Load CC</b> — constant current load (setpoint in A)<br>"
            "- <b>Load CR</b> — constant resistance load (setpoint in Ohms)"
        ))

        # ── Connection Tips
        lay.addWidget(_section("Connection Tips"))
        lay.addWidget(_body(
            "<b>GPP-4323 timeout after idle</b>: The app automatically detects stale TCP connections "
            "and reconnects. If the GPP becomes unresponsive after a long idle period, "
            "the next command will trigger an automatic reconnect (may take ~1 second).<br><br>"
            "<b>NGI System commands</b>: The SYSTem1:* commands do NOT respond over TCP. "
            "Use the front panel or RS232 for system configuration.<br><br>"
            "<b>GDM-8261A (USB-CDC)</b>: Uses serial protocol (115200 baud). "
            "The app handles reconnection automatically if the USB connection is interrupted."
        ))

        # ── File Output
        lay.addWidget(_section("Output Files"))
        lay.addWidget(_body(
            "Both Sweep and Sequence panels save results to <b>Excel (.xlsx)</b> files.<br>"
            "- Auto-generated filenames go to the <code>results/</code> folder<br>"
            "- Each file includes: measurement data sheet, configuration sheet, "
            "and optional waveform images<br>"
            "- Sequence files can be exported/imported to save and reload test configurations"
        ))

        lay.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _build_status_bar(self):
        sb = self.statusBar()
        sb.setStyleSheet(
            f"background:{TH.MANTLE}; color:{TH.SUBTEXT0}; font-size:{TH.FONT_SM};"
            f"border-top:1px solid {TH.SURFACE1};")

        self._sb_indicators: dict = {}
        _KA = instrument_registry.keepalive_keys()
        for key, name in _KA:
            lbl = QLabel(f" {name} ")
            lbl.setStyleSheet(
                f"color:{TH.OVERLAY0}; font-size:8pt; padding:1px 4px;"
                f"border:1px solid {TH.SURFACE1}; border-radius:3px;"
                f"background:{TH.SURFACE0};")
            lbl.setToolTip(f"{name}: unknown")
            sb.addPermanentWidget(lbl)
            self._sb_indicators[key] = lbl

        # Update indicators when keepalive results come in
        self._ka_update_timer = QTimer(self)
        self._ka_update_timer.setInterval(5000)
        self._ka_update_timer.timeout.connect(self._update_sb_indicators)
        self._ka_update_timer.start()

    def _update_sb_indicators(self):
        """Sync status bar indicators with dashboard keepalive state."""
        dash = self._dash_p
        for key, lbl in self._sb_indicators.items():
            ka_lbl = dash._ka_lbl.get(key)
            if ka_lbl is None:
                continue
            tip = ka_lbl.toolTip()
            style = ka_lbl.styleSheet()
            if TH.GREEN in style:
                lbl.setStyleSheet(
                    f"color:{TH.GREEN}; font-size:8pt; padding:1px 4px;"
                    f"border:1px solid {TH.GREEN}; border-radius:3px;"
                    f"background:{TH.SURFACE0}; font-weight:bold;")
                lbl.setToolTip(tip)
            elif TH.RED in style:
                lbl.setStyleSheet(
                    f"color:{TH.RED}; font-size:8pt; padding:1px 4px;"
                    f"border:1px solid {TH.RED}; border-radius:3px;"
                    f"background:{TH.SURFACE0}; font-weight:bold;")
                lbl.setToolTip(tip)
            else:
                lbl.setStyleSheet(
                    f"color:{TH.OVERLAY0}; font-size:8pt; padding:1px 4px;"
                    f"border:1px solid {TH.SURFACE1}; border-radius:3px;"
                    f"background:{TH.SURFACE0};")

    # ── E-STOP ────────────────────────────────────────────────────────────

    def _emergency_stop(self):
        """Turn off ALL instrument outputs immediately."""
        _log.warning("E-STOP activated by user")
        log_lines = []
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] E-STOP activated")

        # Stop running sweep/sequence
        if hasattr(self._sweep_p, '_runner') and self._sweep_p._runner is not None:
            try:
                self._sweep_p._stop_sweep()
                log_lines.append("  Sweep aborted")
            except Exception:
                pass
        if hasattr(self._seq_p, '_runner') and self._seq_p._runner is not None:
            try:
                self._seq_p._stop_run()
                log_lines.append("  Sequence aborted")
            except Exception:
                pass

        # ITECH OFF
        try:
            with use_driver('itech', lambda: ITechFixed(ADDR['itech'])) as drv:
                drv.off()
            log_lines.append("  ITECH output OFF")
        except Exception as e:
            log_lines.append(f"  ITECH: {e}")

        # GPP all channels OFF
        try:
            with use_instrument("gpp") as res:
                drv = GWGPP4323(res)
                for ch in range(1, 5):
                    drv.enable_channel(ch, False)
                # Reset load modes
                for ch in (1, 2):
                    drv.set_load_mode(ch, 'OFF')
            log_lines.append("  GPP all channels OFF")
        except Exception as e:
            log_lines.append(f"  GPP: {e}")

        # NGI all channels OFF
        try:
            with use_instrument('ngi') as res:
                for ch in range(1, 18):
                    res.write(f"OUTP{ch}:ONOFF 0")
            log_lines.append("  NGI all channels OFF")
        except Exception as e:
            log_lines.append(f"  NGI: {e}")

        msg = "\n".join(log_lines)
        QMessageBox.warning(self, "E-STOP", msg)

    # ── Keyboard shortcuts ────────────────────────────────────────────────

    def _setup_shortcuts(self):
        # E-STOP: Ctrl+Shift+X
        QShortcut(QKeySequence("Ctrl+Shift+X"), self, self._emergency_stop)

        # Navigation shortcuts
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._navigate_to('sweep'))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._navigate_to('sequence'))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self._navigate_to('dashboard'))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self._navigate_to('eddie'))
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self._navigate_to('instruments'))
        QShortcut(QKeySequence("Ctrl+6"), self, lambda: self._navigate_to('addresses'))
        QShortcut(QKeySequence("Ctrl+7"), self, lambda: self._navigate_to('scpi'))

        # Save: Ctrl+S
        QShortcut(QKeySequence("Ctrl+S"), self, self._autosave)

        # Toggle sidebar: Ctrl+B
        QShortcut(QKeySequence("Ctrl+B"), self, self._toggle_sidebar)

    # ── Panel accessors ───────────────────────────────────────────────────

    def _panels(self) -> dict:
        d = {
            'sweep':    self._sweep_p,
            'sequence': self._seq_p,
            'dashboard': self._dash_p,
            'eddie':     self._eddie_p,
            'scpi':      self._scpi_p,
        }
        for key, (panel, _label) in self._inst_panels.items():
            d[key] = panel
        return d

    # ── Library-driven tab visibility ─────────────────────────────────────

    def _on_lib_connected(self, key: str, driver):
        _log.info("Instrument connected: %s  (driver=%s)", key, type(driver).__name__)
        # Close any persistent SOCKET connection to avoid dual-session conflicts
        invalidate_shared(key)
        idx = self._inst_tab_idx.get(key)
        if idx is not None:
            self._inst_tabs.setTabVisible(idx, True)
            self._inst_tabs.setCurrentIndex(idx)
        # Update sidebar nav item
        inst_item = self._inst_nav_items.get(key)
        if inst_item:
            inst_item.setForeground(0, QColor(TH.GREEN))
            inst_item.setToolTip(0, "Connected")
        # Update status bar
        sb_lbl = self._sb_indicators.get(key)
        if sb_lbl:
            name = sb_lbl.text().strip()
            sb_lbl.setStyleSheet(
                f"color:{TH.GREEN}; font-size:8pt; padding:1px 4px;"
                f"border:1px solid {TH.GREEN}; border-radius:3px;"
                f"background:{TH.SURFACE0}; font-weight:bold;")
        self._handle_instrument_connect(key)

    def _on_lib_disconnected(self, key: str):
        _log.info("Instrument disconnected: %s", key)
        # Close persistent SOCKET connection so the next user gets a fresh one
        invalidate_shared(key)
        idx = self._inst_tab_idx.get(key)
        if idx is not None:
            self._inst_tabs.setTabVisible(idx, False)
        # Update sidebar nav item
        inst_item = self._inst_nav_items.get(key)
        if inst_item:
            inst_item.setForeground(0, QColor(TH.OVERLAY0))
            inst_item.setToolTip(0, "Not connected")
        # Update status bar
        sb_lbl = self._sb_indicators.get(key)
        if sb_lbl:
            sb_lbl.setStyleSheet(
                f"color:{TH.OVERLAY0}; font-size:8pt; padding:1px 4px;"
                f"border:1px solid {TH.SURFACE1}; border-radius:3px;"
                f"background:{TH.SURFACE0};")
        panel = self._panels().get(key)
        if panel is not None:
            persist.save_all({key: panel})

    def _handle_instrument_connect(self, key: str):
        panel = self._panels().get(key)
        if panel is None:
            return
        if persist.has_panel_state(key):
            reply = QMessageBox.question(
                self,
                f"Restore {key.upper()} Configuration",
                f"A saved configuration was found for <b>{key.upper()}</b>.<br><br>"
                f"<b>Yes</b> — restore saved UI values <i>and push them to the instrument</i><br>"
                f"<b>No</b> — read current settings <i>from the instrument</i> into the UI<br>"
                f"<b>Cancel</b> — do nothing",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No  |
                QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                persist.load_all({key: panel})
                if hasattr(panel, '_apply_all_state'):
                    panel._apply_all_state()
            elif reply == QMessageBox.StandardButton.No:
                if hasattr(panel, '_query_all_state'):
                    panel._query_all_state()
        else:
            if hasattr(panel, '_query_all_state'):
                panel._query_all_state()

    # ── Settings persistence ──────────────────────────────────────────────

    def _save_settings(self):
        try:
            data = {}
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            # Window geometry
            geo = self.geometry()
            data['window'] = {
                'x': geo.x(), 'y': geo.y(),
                'w': geo.width(), 'h': geo.height(),
                'maximized': self.isMaximized(),
            }
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception as e:
            print(f"[settings] save error: {e}")

    def _load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            w = data.get('window', {})
            if w:
                if w.get('maximized'):
                    self.showMaximized()
                else:
                    self.setGeometry(
                        w.get('x', 100), w.get('y', 100),
                        w.get('w', 1400), w.get('h', 900))
        except Exception as e:
            print(f"[settings] load error: {e}")

    def _autosave(self):
        persist.save_all(self._panels())
        self._seq_p.save_steps_session()

    def closeEvent(self, event):
        _log.info("Application closing")
        # Re-dock all floating tabs before saving
        self._inst_tabs.re_dock_all()
        persist.save_all(self._panels())
        self._seq_p.save_steps_session()
        self._save_settings()
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────

def _install_exception_hooks():
    """Install global exception hooks so unhandled errors are logged to the
    rotating log file before the process dies."""
    import traceback as _tb

    _orig_excepthook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        _orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    # Also catch unhandled exceptions from QThread subclasses
    def _qt_msg_handler(mode, context, message):
        if mode == 3:  # QtCriticalMsg / QtFatalMsg
            _log.critical("Qt fatal: %s  (file=%s line=%s)",
                          message, context.file, context.line)
        elif mode == 2:
            _log.error("Qt critical: %s", message)
        elif mode == 1:
            _log.warning("Qt warning: %s", message)

    from PyQt6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_msg_handler)


def main():
    _install_exception_hooks()
    _log.info("main() entry")
    load_addresses()
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
