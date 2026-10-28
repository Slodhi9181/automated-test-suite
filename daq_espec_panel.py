# -*- coding: utf-8 -*-
"""
daq_espec_panel.py — Instrument panels for:
    • Keysight DAQ 973A  Data Acquisition System  (multi-channel scanner)
    • ESPEC SH-662       Temperature & Humidity Chamber
"""

import math
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
    QLineEdit, QTextEdit, QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from workers import use_instrument
from ui_helpers import _btn


# ─────────────────────────────────────────────────────────────────────────────
# DAQ 973A  —  constants
# ─────────────────────────────────────────────────────────────────────────────

_DAQ_TYPES = [
    "VOLT:DC", "VOLT:AC",
    "CURR:DC", "CURR:AC",
    "RES",     "FRES",
    "TEMP TC,K",  "TEMP TC,J", "TEMP TC,T", "TEMP TC,E", "TEMP TC,N",
    "TEMP RTD,100", "TEMP THER,5000",
    "FREQ", "PERIOD", "DIOD",
]

_DAQ_RANGES = {
    "VOLT:DC":  ["AUTO", "0.1",   "1",    "10",   "100",   "300"],
    "VOLT:AC":  ["AUTO", "0.1",   "1",    "10",   "100",   "300"],
    "CURR:DC":  ["AUTO", "0.01",  "0.1",  "1"],
    "CURR:AC":  ["AUTO", "0.01",  "0.1",  "1"],
    "RES":      ["AUTO", "100",   "1E3",  "10E3", "100E3", "1E6",  "10E6", "100E6"],
    "FRES":     ["AUTO", "100",   "1E3",  "10E3", "100E3", "1E6",  "10E6", "100E6"],
    "FREQ":     ["AUTO", "100",   "1E3",  "1E4",  "3E5"],
    "PERIOD":   ["N/A"],
    "DIOD":     ["N/A"],
}  # types not present → ["N/A"]

_DAQ_NPLC    = ["0.02", "0.2", "1", "2", "10", "20", "100", "200"]
_N_CH        = 12   # fixed channel rows (enough for a full slot + extras)
_N_PROF      = 10   # ESPEC profile steps


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hdr(text):
    l = QLabel(text); l.setStyleSheet("font-weight:bold; color:#89b4fa;")
    return l

def _ts():
    return datetime.now().strftime("[%H:%M:%S]")


# ─────────────────────────────────────────────────────────────────────────────
# DAQ 973A  —  driver helpers  (standalone so runners can call without panel)
# ─────────────────────────────────────────────────────────────────────────────

def _daq_configure_channel(res, c: dict):
    """Send CONF + NPLC + ZERO:AUTO commands for one channel dict."""
    ch    = c["ch"]
    mtype = c["type"]
    rng   = c["range"]
    nplc  = c["nplc"]
    az    = c["autozero"]

    if mtype.startswith("TEMP"):
        res.write(f"CONF:{mtype},(@{ch})")
    elif rng in ("AUTO", "N/A"):
        res.write(f"CONF:{mtype} AUTO,DEF,(@{ch})")
    else:
        res.write(f"CONF:{mtype} {rng},DEF,(@{ch})")

    base = mtype.split(":")[0] if ":" in mtype else mtype.split(" ")[0]
    if base in ("VOLT", "CURR", "RES", "FRES"):
        subcmd = mtype.replace(":", ":DC:").replace("DC:DC:", "DC:")  # normalise
        res.write(f"SENS:{mtype}:NPLC {nplc},(@{ch})")
        res.write(f"SENS:{base}:ZERO:AUTO {'ON' if az else 'OFF'},(@{ch})")


def daq_scan(res, cfg_list: list) -> dict:
    """Configure scan list, trigger READ, return {label: float}."""
    if not cfg_list:
        return {}
    for c in cfg_list:
        _daq_configure_channel(res, c)
    ch_str = ",".join(str(c["ch"]) for c in cfg_list)
    res.write(f"ROUT:SCAN (@{ch_str})")
    raw  = res.query("READ?")
    vals = []
    for v in raw.split(","):
        try:    vals.append(float(v.strip()))
        except: vals.append(float("nan"))
    return {cfg_list[i]["label"]: vals[i] if i < len(vals) else float("nan")
            for i in range(len(cfg_list))}


# ─────────────────────────────────────────────────────────────────────────────
# Keysight DAQ 973A  —  panel
# ─────────────────────────────────────────────────────────────────────────────

class KeysightDAQ973APanel(QWidget):
    """
    Full control panel for the Keysight DAQ 973A Data Acquisition System.

    Features
    --------
    • Per-channel: slot, channel number, label, type, range, NPLC, auto-zero
    • Scan settings: trigger source, trigger timer, scan count, live poll rate
    • Manual Configure, Scan-Once, and continuous Live Scan modes
    • Exposes ``get_scan_config()`` / ``do_scan()`` for sweep & sequence runners
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # Per-channel widget dicts — auto-persisted by persist.py
        self._ch_en       = {}
        self._ch_slot     = {}
        self._ch_num      = {}
        self._ch_label_w  = {}
        self._ch_type     = {}
        self._ch_range    = {}
        self._ch_nplc     = {}
        self._ch_autozero = {}

        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._last_scan  = {}
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.addWidget(self._build_channel_group(), stretch=3)
        lay.addWidget(self._build_scan_settings_group())
        lay.addWidget(self._build_controls_group(), stretch=1)

    def _build_channel_group(self):
        grp = QGroupBox("Channel Configuration  (slot × 100 + channel = DAQ address, e.g. slot 1 ch 3 → 103)")
        vlay = QVBoxLayout(grp)

        # Column headers
        hrow = QHBoxLayout(); hrow.setSpacing(4)
        for txt, w in [("En", 28), ("Slot", 50), ("Ch #", 48), ("Label", 88),
                       ("Type", 132), ("Range", 78), ("NPLC", 58), ("Auto-Zero", 72)]:
            lbl = _hdr(txt); lbl.setFixedWidth(w); hrow.addWidget(lbl)
        hrow.addStretch(); vlay.addLayout(hrow)

        # Scrollable rows
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumHeight(260)
        inner  = QWidget()
        il     = QVBoxLayout(inner); il.setSpacing(2); il.setContentsMargins(0, 0, 0, 0)

        for i in range(_N_CH):
            rw  = QWidget()
            rl  = QHBoxLayout(rw); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)

            en = QCheckBox(); en.setFixedWidth(28)
            self._ch_en[i] = en; rl.addWidget(en)

            slot = QComboBox(); slot.addItems(["1", "2", "3"]); slot.setFixedWidth(50)
            self._ch_slot[i] = slot; rl.addWidget(slot)

            num = QSpinBox(); num.setRange(1, 20); num.setValue(i + 1); num.setFixedWidth(48)
            self._ch_num[i] = num; rl.addWidget(num)

            lbl_e = QLineEdit(); lbl_e.setPlaceholderText(f"CH{i+1}"); lbl_e.setFixedWidth(88)
            self._ch_label_w[i] = lbl_e; rl.addWidget(lbl_e)

            type_cb = QComboBox(); type_cb.addItems(_DAQ_TYPES); type_cb.setFixedWidth(132)
            type_cb.currentTextChanged.connect(lambda t, idx=i: self._on_type_changed(idx, t))
            self._ch_type[i] = type_cb; rl.addWidget(type_cb)

            range_cb = QComboBox()
            range_cb.addItems(_DAQ_RANGES.get("VOLT:DC", ["AUTO"])); range_cb.setFixedWidth(78)
            self._ch_range[i] = range_cb; rl.addWidget(range_cb)

            nplc_cb = QComboBox(); nplc_cb.addItems(_DAQ_NPLC); nplc_cb.setCurrentIndex(2)
            nplc_cb.setFixedWidth(58)
            self._ch_nplc[i] = nplc_cb; rl.addWidget(nplc_cb)

            az = QCheckBox(); az.setChecked(True); az.setToolTip("Auto-Zero ON")
            az.setFixedWidth(72)
            self._ch_autozero[i] = az; rl.addWidget(az)

            rl.addStretch()
            il.addWidget(rw)

        il.addStretch()
        scroll.setWidget(inner)
        vlay.addWidget(scroll)
        return grp

    def _build_scan_settings_group(self):
        grp = QGroupBox("Scan Settings")
        g   = QGridLayout(grp)

        g.addWidget(QLabel("Trigger source:"), 0, 0)
        self.trig_src = QComboBox()
        self.trig_src.addItems(["IMM", "EXT", "BUS", "TIM"])
        self.trig_src.setToolTip("IMM = immediate (most common)  TIM = timer-driven")
        g.addWidget(self.trig_src, 0, 1)

        g.addWidget(QLabel("Trigger timer:"), 0, 2)
        self.trig_timer_ms = QSpinBox()
        self.trig_timer_ms.setRange(100, 3_600_000); self.trig_timer_ms.setValue(1000)
        self.trig_timer_ms.setSuffix(" ms"); self.trig_timer_ms.setToolTip("Used when source = TIM")
        g.addWidget(self.trig_timer_ms, 0, 3)

        g.addWidget(QLabel("Scan count:"), 0, 4)
        self.scan_count = QSpinBox()
        self.scan_count.setRange(1, 50000); self.scan_count.setValue(1)
        self.scan_count.setToolTip("Complete scans per READ command (TRIG:COUN)")
        g.addWidget(self.scan_count, 0, 5)

        g.addWidget(QLabel("Sample rate:"), 1, 0)
        self.sample_rate = QDoubleSpinBox()
        self.sample_rate.setRange(0.001, 50000); self.sample_rate.setValue(0.0)
        self.sample_rate.setSuffix(" S/ch"); self.sample_rate.setDecimals(3)
        self.sample_rate.setToolTip("0 = use NPLC-derived rate.  Manual override for SAMP:RATE.")
        g.addWidget(self.sample_rate, 1, 1)

        g.addWidget(QLabel("Live poll interval:"), 1, 2)
        self.live_poll_ms = QSpinBox()
        self.live_poll_ms.setRange(500, 60000); self.live_poll_ms.setValue(2000)
        self.live_poll_ms.setSuffix(" ms")
        self.live_poll_ms.valueChanged.connect(lambda ms: self._live_timer.setInterval(ms))
        g.addWidget(self.live_poll_ms, 1, 3)

        g.addWidget(QLabel("Channel delay:"), 1, 4)
        self.ch_delay_ms = QDoubleSpinBox()
        self.ch_delay_ms.setRange(0, 60000); self.ch_delay_ms.setValue(0)
        self.ch_delay_ms.setSuffix(" ms")
        self.ch_delay_ms.setToolTip("ROUT:CHAN:DEL — per-channel settling delay before measurement")
        g.addWidget(self.ch_delay_ms, 1, 5)

        return grp

    def _build_controls_group(self):
        grp  = QGroupBox("Controls & Results")
        vlay = QVBoxLayout(grp)

        brow = QHBoxLayout()
        self.btn_configure = _btn("⚙  Configure", "blue", 120)
        self.btn_configure.setToolTip("Send CONF + NPLC + ZERO:AUTO for all enabled channels")
        self.btn_configure.clicked.connect(self._configure)

        self.btn_scan_once = _btn("▶  Scan Once", width=110)
        self.btn_scan_once.clicked.connect(self._scan_once)

        self.btn_live = _btn("⟳  Start Live", "green", 112)
        self.btn_live.clicked.connect(self._toggle_live)

        self.btn_idn = _btn("*IDN?", width=70)
        self.btn_idn.clicked.connect(self._query_idn)

        self.btn_clear = _btn("🗑  Clear", width=70)
        self.btn_clear.clicked.connect(lambda: self.log_view.clear())

        for b in [self.btn_configure, self.btn_scan_once, self.btn_live,
                  self.btn_idn, self.btn_clear]:
            brow.addWidget(b)
        brow.addStretch()
        vlay.addLayout(brow)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Courier New", 9))
        self.log_view.setMinimumHeight(110)
        vlay.addWidget(self.log_view)
        return grp

    # ── Slot handlers ─────────────────────────────────────────────────────────

    def _on_type_changed(self, idx: int, t: str):
        ranges  = _DAQ_RANGES.get(t, ["N/A"])
        cb      = self._ch_range[idx]
        cb.blockSignals(True); cb.clear(); cb.addItems(ranges); cb.blockSignals(False)
        no_nplc = any(t.startswith(p) for p in ("TEMP", "DIOD", "FREQ", "PERIOD"))
        self._ch_nplc[idx].setEnabled(not no_nplc)

    def _log(self, msg: str):
        self.log_view.append(f"{_ts()}  {msg}")
        self.log_view.moveCursor(self.log_view.textCursor().End)

    # ── Public API — for runners ──────────────────────────────────────────────

    def get_scan_config(self) -> list:
        """Return enabled-channel dicts: [{ch, label, type, range, nplc, autozero}]."""
        out = []
        for i in range(_N_CH):
            if not self._ch_en[i].isChecked():
                continue
            slot   = int(self._ch_slot[i].currentText())
            ch_num = (slot - 1) * 100 + 100 + self._ch_num[i].value()
            label  = self._ch_label_w[i].text().strip() or f"DAQ {ch_num}"
            out.append({
                "ch":       ch_num,
                "label":    label,
                "type":     self._ch_type[i].currentText(),
                "range":    self._ch_range[i].currentText(),
                "nplc":     float(self._ch_nplc[i].currentText()),
                "autozero": self._ch_autozero[i].isChecked(),
            })
        return out

    def do_scan(self) -> dict:
        """Perform one complete scan. Returns {label: float}. Safe to call from runner thread."""
        cfg = self.get_scan_config()
        if not cfg:
            return {}
        try:
            with use_instrument("daq") as res:
                if self.ch_delay_ms.value() > 0:
                    ch_str = ",".join(str(c["ch"]) for c in cfg)
                    res.write(f"ROUT:CHAN:DEL {self.ch_delay_ms.value()/1000:.4f},(@{ch_str})")
                return daq_scan(res, cfg)
        except Exception as e:
            self._log(f"⚠ Scan error: {e}")
            return {c["label"]: float("nan") for c in cfg}

    # ── Button actions ────────────────────────────────────────────────────────

    def _configure(self):
        cfg = self.get_scan_config()
        if not cfg:
            self._log("⚠ No channels enabled."); return
        try:
            with use_instrument("daq") as res:
                for c in cfg:
                    _daq_configure_channel(res, c)
                    self._log(f"  ✓ CH{c['ch']}  {c['type']}  range={c['range']}  NPLC={c['nplc']}")
                ch_str = ",".join(str(c["ch"]) for c in cfg)
                res.write(f"ROUT:SCAN (@{ch_str})")
                if self.ch_delay_ms.value() > 0:
                    res.write(f"ROUT:CHAN:DEL {self.ch_delay_ms.value()/1000:.4f},(@{ch_str})")
                if self.scan_count.value() > 1:
                    res.write(f"TRIG:COUN {self.scan_count.value()}")
                if self.trig_src.currentText() == "TIM":
                    res.write(f"TRIG:SOUR TIM")
                    res.write(f"TRIG:TIM {self.trig_timer_ms.value()/1000:.3f}")
                self._log(f"✓ Configured {len(cfg)} channels  scan→@{ch_str}")
        except Exception as e:
            self._log(f"⚠ Configure error: {e}")

    def _scan_once(self):
        cfg = self.get_scan_config()
        if not cfg:
            self._log("⚠ No channels enabled."); return
        try:
            with use_instrument("daq") as res:
                vals = daq_scan(res, cfg)
            self._last_scan = vals
            self._log("Scan result:")
            for lbl, v in vals.items():
                self._log(f"  {lbl:20s}  {v:.6g}")
        except Exception as e:
            self._log(f"⚠ Scan error: {e}")

    def _query_idn(self):
        try:
            with use_instrument("daq") as res:
                idn = res.query("*IDN?").strip()
            self._log(f"IDN: {idn}")
        except Exception as e:
            self._log(f"⚠ {e}")

    def _toggle_live(self):
        if self._live_timer.isActive():
            self._live_timer.stop()
            self.btn_live.setText("⟳  Start Live"); self.btn_live.setProperty("role", "green")
        else:
            self._live_timer.setInterval(self.live_poll_ms.value())
            self._live_timer.start()
            self.btn_live.setText("■  Stop Live"); self.btn_live.setProperty("role", "red")
            self._live_tick()
        self.btn_live.style().unpolish(self.btn_live); self.btn_live.style().polish(self.btn_live)

    def _live_tick(self):
        cfg = self.get_scan_config()
        if not cfg: return
        try:
            with use_instrument("daq") as res:
                vals = daq_scan(res, cfg)
            self._last_scan = vals
            line = "  ".join(f"{l}={v:.5g}" for l, v in vals.items())
            self._log(f"live: {line}")
        except Exception as e:
            self._log(f"⚠ {e}")


# ─────────────────────────────────────────────────────────────────────────────
# ESPEC SH-662  —  driver helpers
# ─────────────────────────────────────────────────────────────────────────────

def _espec_setup(res):
    """Configure RS-232 / GPIB termination for ESPEC SH-662."""
    try:
        import pyvisa.constants as vc
        res.baud_rate         = 9600
        res.data_bits         = 8
        res.stop_bits         = vc.StopBits.one
        res.parity            = vc.Parity.none
        res.write_termination = "\r\n"
        res.read_termination  = "\r\n"
        res.timeout           = 5000
    except Exception:
        pass   # GPIB or LAN — termination already set by pyvisa


def espec_read_temp(res) -> float:
    return float(res.query("TEMP").strip().lstrip("+"))


def espec_read_humi(res) -> float:
    return float(res.query("HUMI").strip().lstrip("+"))


def espec_read_mode(res) -> str:
    return res.query("MODE").strip()


def espec_set_temp(res, t: float, ramp: float = 0.0):
    sign = "+" if t >= 0 else ""
    cmd  = f"TEMP,S,{sign}{t:.1f}"
    if ramp > 0:
        cmd += f",C,{ramp:.1f}"
    res.query(cmd)   # expects "OK"


def espec_set_humi(res, h: float, ramp: float = 0.0):
    cmd = f"HUMI,S,{h:.1f}"
    if ramp > 0:
        cmd += f",C,{ramp:.1f}"
    res.query(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# ESPEC SH-662  —  panel
# ─────────────────────────────────────────────────────────────────────────────

class EspecSH662Panel(QWidget):
    """
    Full control panel for the ESPEC SH-662 Temperature & Humidity Chamber.

    Features
    --------
    • Live T / H / Mode readback with configurable poll interval
    • Setpoints for T (°C) and H (%), with ramp rate controls
    • Stabilization tolerance + timeout (used by sweep & sequence runners)
    • Chamber control: Power ON/OFF, Run, Standby
    • 10-step programmable profile (T, H, ramp, soak)
    • Exposes ``apply_setpoints_for_runner()`` / ``read_actual_*_for_runner()``
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Profile row widgets — auto-persisted
        self._prof_en     = {}
        self._prof_t      = {}
        self._prof_h      = {}
        self._prof_ramp_t = {}
        self._prof_ramp_h = {}
        self._prof_soak   = {}

        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(self._build_status_group(),        stretch=1)
        top.addWidget(self._build_setpoint_group(),      stretch=1)
        top.addWidget(self._build_stabilization_group(), stretch=1)
        lay.addLayout(top)

        lay.addWidget(self._build_control_group())
        lay.addWidget(self._build_profile_group(), stretch=1)

    def _build_status_group(self):
        grp = QGroupBox("Live Status")
        g   = QGridLayout(grp)

        def _val_lbl():
            l = QLabel("—")
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setStyleSheet(
                "font-family:'Courier New'; font-size:13pt; font-weight:bold;"
                " color:#a6e3a1; background:#181825; border:1px solid #45475a;"
                " border-radius:4px; padding:4px 10px; min-width:90px;")
            return l

        g.addWidget(QLabel("T actual:"),  0, 0)
        self._lbl_t = _val_lbl(); g.addWidget(self._lbl_t, 0, 1)

        g.addWidget(QLabel("H actual:"),  1, 0)
        self._lbl_h = _val_lbl(); g.addWidget(self._lbl_h, 1, 1)

        g.addWidget(QLabel("Mode:"), 2, 0)
        self._lbl_mode = QLabel("—")
        self._lbl_mode.setStyleSheet("color:#fab387; font-weight:bold;")
        g.addWidget(self._lbl_mode, 2, 1)

        poll_row = QHBoxLayout()
        self._live_btn = _btn("▶  Live", "green", 80)
        self._live_btn.clicked.connect(self._toggle_live)
        self.live_poll_ms = QSpinBox()
        self.live_poll_ms.setRange(500, 30000); self.live_poll_ms.setValue(2000)
        self.live_poll_ms.setSuffix(" ms")
        self.live_poll_ms.valueChanged.connect(lambda ms: self._live_timer.setInterval(ms))
        poll_row.addWidget(self._live_btn)
        poll_row.addWidget(QLabel("Poll:")); poll_row.addWidget(self.live_poll_ms)
        poll_row.addStretch()
        g.addLayout(poll_row, 3, 0, 1, 2)
        return grp

    def _build_setpoint_group(self):
        grp = QGroupBox("Setpoints")
        g   = QGridLayout(grp)

        g.addWidget(QLabel("T set (°C):"), 0, 0)
        self.t_set = QDoubleSpinBox()
        self.t_set.setRange(-70, 180); self.t_set.setValue(25.0); self.t_set.setDecimals(1)
        g.addWidget(self.t_set, 0, 1)

        g.addWidget(QLabel("H set (%):"), 1, 0)
        self.h_set = QDoubleSpinBox()
        self.h_set.setRange(10, 98); self.h_set.setValue(50.0); self.h_set.setDecimals(1)
        g.addWidget(self.h_set, 1, 1)

        g.addWidget(QLabel("T ramp (°C/min):"), 2, 0)
        self.ramp_t = QDoubleSpinBox()
        self.ramp_t.setRange(0, 10); self.ramp_t.setValue(0); self.ramp_t.setDecimals(1)
        self.ramp_t.setToolTip("0 = maximum ramp rate (as fast as possible)")
        g.addWidget(self.ramp_t, 2, 1)

        g.addWidget(QLabel("H ramp (%/min):"), 3, 0)
        self.ramp_h = QDoubleSpinBox()
        self.ramp_h.setRange(0, 10); self.ramp_h.setValue(0); self.ramp_h.setDecimals(1)
        self.ramp_h.setToolTip("0 = maximum ramp rate")
        g.addWidget(self.ramp_h, 3, 1)

        return grp

    def _build_stabilization_group(self):
        grp = QGroupBox("Stabilization  (Sweep / Sequence)")
        g   = QGridLayout(grp)

        self.wait_stable = QCheckBox("Wait for stabilization at each step")
        self.wait_stable.setChecked(True)
        self.wait_stable.setToolTip(
            "After sending a setpoint, poll actual T/H until within\n"
            "tolerance or until timeout expires.")
        g.addWidget(self.wait_stable, 0, 0, 1, 2)

        g.addWidget(QLabel("T tolerance (°C):"), 1, 0)
        self.tol_t = QDoubleSpinBox()
        self.tol_t.setRange(0.1, 10); self.tol_t.setValue(0.5); self.tol_t.setDecimals(1)
        g.addWidget(self.tol_t, 1, 1)

        g.addWidget(QLabel("H tolerance (%):"), 2, 0)
        self.tol_h = QDoubleSpinBox()
        self.tol_h.setRange(0.5, 20); self.tol_h.setValue(2.0); self.tol_h.setDecimals(1)
        g.addWidget(self.tol_h, 2, 1)

        g.addWidget(QLabel("Timeout (s):"), 3, 0)
        self.timeout_s = QSpinBox()
        self.timeout_s.setRange(30, 7200); self.timeout_s.setValue(600); self.timeout_s.setSuffix(" s")
        g.addWidget(self.timeout_s, 3, 1)

        g.addWidget(QLabel("Poll interval (s):"), 4, 0)
        self.stable_poll_s = QSpinBox()
        self.stable_poll_s.setRange(1, 120); self.stable_poll_s.setValue(5); self.stable_poll_s.setSuffix(" s")
        self.stable_poll_s.setToolTip("How often to check T/H while waiting for stabilization")
        g.addWidget(self.stable_poll_s, 4, 1)

        return grp

    def _build_control_group(self):
        grp  = QGroupBox("Chamber Control")
        hlay = QHBoxLayout(grp)

        btns = [
            ("⚡ Power ON",      "green",  110, self._power_on),
            ("⏹ Power OFF",     "red",    110, self._power_off),
            ("▶ Run",           "blue",    80, self._set_run),
            ("⏸ Standby",      None,       90, self._set_standby),
            ("✓ Apply Setpoints","orange", 145, self._apply_setpoints_ui),
            ("↺ Read Status",   None,      110, self._read_status),
            ("*IDN?",           None,       70, self._query_idn),
        ]
        for txt, role, w, fn in btns:
            b = _btn(txt, role, w); b.clicked.connect(fn); hlay.addWidget(b)
        hlay.addStretch()

        self.espec_log = QTextEdit()
        self.espec_log.setReadOnly(True)
        self.espec_log.setFont(QFont("Courier New", 9))
        self.espec_log.setMaximumHeight(100)

        wrap  = QWidget()
        wlay  = QVBoxLayout(wrap); wlay.setContentsMargins(0, 0, 0, 0)
        wlay.addWidget(grp); wlay.addWidget(self.espec_log)
        return wrap

    def _build_profile_group(self):
        grp  = QGroupBox("Profile  (sequential T / H program)")
        vlay = QVBoxLayout(grp)

        hrow = QHBoxLayout(); hrow.setSpacing(4)
        for txt, w in [("En", 28), ("T_set °C", 80), ("H_set %", 80),
                       ("Ramp T °C/min", 102), ("Ramp H %/min", 102), ("Soak (min)", 80)]:
            l = _hdr(txt); l.setFixedWidth(w); hrow.addWidget(l)
        hrow.addStretch(); vlay.addLayout(hrow)

        for i in range(_N_PROF):
            rw  = QWidget()
            rl  = QHBoxLayout(rw); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)

            en = QCheckBox(); en.setFixedWidth(28)
            self._prof_en[i] = en; rl.addWidget(en)

            t_sb = QDoubleSpinBox(); t_sb.setRange(-70, 180); t_sb.setValue(25.0)
            t_sb.setDecimals(1); t_sb.setFixedWidth(80)
            self._prof_t[i] = t_sb; rl.addWidget(t_sb)

            h_sb = QDoubleSpinBox(); h_sb.setRange(10, 98); h_sb.setValue(50.0)
            h_sb.setDecimals(1); h_sb.setFixedWidth(80)
            self._prof_h[i] = h_sb; rl.addWidget(h_sb)

            rt = QDoubleSpinBox(); rt.setRange(0, 10); rt.setValue(0)
            rt.setDecimals(1); rt.setFixedWidth(102); rt.setToolTip("0 = max rate")
            self._prof_ramp_t[i] = rt; rl.addWidget(rt)

            rh = QDoubleSpinBox(); rh.setRange(0, 10); rh.setValue(0)
            rh.setDecimals(1); rh.setFixedWidth(102); rh.setToolTip("0 = max rate")
            self._prof_ramp_h[i] = rh; rl.addWidget(rh)

            sk = QDoubleSpinBox(); sk.setRange(0, 9999); sk.setValue(10)
            sk.setDecimals(1); sk.setSuffix(" min"); sk.setFixedWidth(80)
            self._prof_soak[i] = sk; rl.addWidget(sk)

            rl.addStretch()
            vlay.addWidget(rw)

        # Profile execution buttons
        pbrow = QHBoxLayout()
        btn_run_p  = _btn("▶ Run Profile", "blue", 130)
        btn_run_p.setToolTip("Send setpoints step by step and soak")
        btn_run_p.clicked.connect(self._run_profile)
        btn_stop_p = _btn("■ Abort", "red", 80)
        btn_stop_p.clicked.connect(self._abort_profile)
        pbrow.addWidget(btn_run_p); pbrow.addWidget(btn_stop_p); pbrow.addStretch()
        vlay.addLayout(pbrow)
        return grp

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self.espec_log.append(f"{_ts()}  {msg}")
        self.espec_log.moveCursor(self.espec_log.textCursor().End)

    # ── Live status ───────────────────────────────────────────────────────────

    def _toggle_live(self):
        if self._live_timer.isActive():
            self._live_timer.stop()
            self._live_btn.setText("▶  Live"); self._live_btn.setProperty("role", "green")
        else:
            self._live_timer.setInterval(self.live_poll_ms.value())
            self._live_timer.start()
            self._live_btn.setText("■  Stop"); self._live_btn.setProperty("role", "red")
            self._live_tick()
        self._live_btn.style().unpolish(self._live_btn); self._live_btn.style().polish(self._live_btn)

    def _live_tick(self):
        try:
            with use_instrument("espec") as res:
                _espec_setup(res)
                t    = espec_read_temp(res)
                h    = espec_read_humi(res)
                mode = espec_read_mode(res)
            self._lbl_t.setText(f"{t:.1f} °C")
            self._lbl_h.setText(f"{h:.1f} %")
            self._lbl_mode.setText(mode)
        except Exception:
            self._lbl_t.setText("—"); self._lbl_h.setText("—"); self._lbl_mode.setText("—")

    # ── Control button handlers ───────────────────────────────────────────────

    def _read_status(self):
        try:
            with use_instrument("espec") as res:
                _espec_setup(res)
                t    = espec_read_temp(res)
                h    = espec_read_humi(res)
                mode = espec_read_mode(res)
            self._lbl_t.setText(f"{t:.1f} °C")
            self._lbl_h.setText(f"{h:.1f} %")
            self._lbl_mode.setText(mode)
            self._log(f"T={t:.1f}°C  H={h:.1f}%  Mode={mode}")
        except Exception as e:
            self._log(f"⚠ Read error: {e}")

    def _query_idn(self):
        try:
            with use_instrument("espec") as res:
                _espec_setup(res)
                idn = res.query("*IDN?").strip()
            self._log(f"IDN: {idn}")
        except Exception as e:
            self._log(f"⚠ {e}")

    def _power_on(self):
        self._send_cmd("POWER,ON",    "POWER ON")

    def _power_off(self):
        self._send_cmd("POWER,OFF",   "POWER OFF")

    def _set_run(self):
        self._send_cmd("MODE,RUN",    "MODE RUN")

    def _set_standby(self):
        self._send_cmd("MODE,STANDBY","MODE STANDBY")

    def _send_cmd(self, cmd: str, label: str):
        try:
            with use_instrument("espec") as res:
                _espec_setup(res)
                r = res.query(cmd).strip()
            self._log(f"{label} → {r}")
        except Exception as e:
            self._log(f"⚠ {e}")

    def _apply_setpoints_ui(self):
        t = self.t_set.value(); h = self.h_set.value()
        rt = self.ramp_t.value(); rh = self.ramp_h.value()
        try:
            with use_instrument("espec") as res:
                _espec_setup(res)
                espec_set_temp(res, t, rt)
                espec_set_humi(res, h, rh)
            self._log(f"✓ T={t:.1f}°C  H={h:.1f}%  ramp_T={rt}  ramp_H={rh}")
        except Exception as e:
            self._log(f"⚠ Apply error: {e}")

    def _run_profile(self):
        steps = [
            {"t": self._prof_t[i].value(), "h": self._prof_h[i].value(),
             "ramp_t": self._prof_ramp_t[i].value(), "ramp_h": self._prof_ramp_h[i].value(),
             "soak_min": self._prof_soak[i].value()}
            for i in range(_N_PROF) if self._prof_en[i].isChecked()
        ]
        if not steps:
            self._log("⚠ No profile steps enabled."); return
        self._log(f"Profile queued: {len(steps)} steps. Use Sweep or Sequence tab for automated run.")
        for i, s in enumerate(steps):
            self._log(f"  [{i+1}] T={s['t']:.1f}°C  H={s['h']:.1f}%  "
                      f"ramp_T={s['ramp_t']}  ramp_H={s['ramp_h']}  soak={s['soak_min']:.1f}min")

    def _abort_profile(self):
        self._log("Profile abort requested (set Mode → Standby to stop chamber).")

    # ── Public API for runners ────────────────────────────────────────────────

    def get_stabilization_config(self) -> dict:
        return {
            "wait_stable":  self.wait_stable.isChecked(),
            "tol_t":        self.tol_t.value(),
            "tol_h":        self.tol_h.value(),
            "timeout_s":    self.timeout_s.value(),
            "stable_poll_s": self.stable_poll_s.value(),
        }

    def apply_setpoints_for_runner(self, t: float, h: float,
                                   ramp_t: float = 0.0, ramp_h: float = 0.0):
        """Apply T/H setpoints. Call from runner thread."""
        with use_instrument("espec") as res:
            _espec_setup(res)
            espec_set_temp(res, t, ramp_t)
            espec_set_humi(res, h, ramp_h)

    def read_actual_temp_for_runner(self) -> float:
        with use_instrument("espec") as res:
            _espec_setup(res); return espec_read_temp(res)

    def read_actual_humi_for_runner(self) -> float:
        with use_instrument("espec") as res:
            _espec_setup(res); return espec_read_humi(res)

    def get_meas_callables(self) -> list:
        """Return [(label, fn)] for ESPEC actual T and H logging."""
        return [
            ("ESPEC T_actual (°C)", self.read_actual_temp_for_runner),
            ("ESPEC H_actual (%)",  self.read_actual_humi_for_runner),
        ]

    def get_profile_steps(self) -> list:
        """Return enabled profile steps as list of dicts."""
        return [
            {"t": self._prof_t[i].value(), "h": self._prof_h[i].value(),
             "ramp_t": self._prof_ramp_t[i].value(), "ramp_h": self._prof_ramp_h[i].value(),
             "soak_min": self._prof_soak[i].value()}
            for i in range(_N_PROF) if self._prof_en[i].isChecked()
        ]
