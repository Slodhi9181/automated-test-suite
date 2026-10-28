# -*- coding: utf-8 -*-
"""
sweep_panel.py — Automated sweep UI for ITECH IT6005C + GPP-4323.

Features:
  • Linear and decade step generation per source channel
  • Per-channel voltage start/stop + current limit for GPP (all 4 ch)
  • ITECH voltage sweep + current limit + mode
  • Measurement input selector: GDM, DMM, ITECH readback, GPP readback
  • N readings per step + step interval → dwell time calculated live
  • Excel (.xlsx) output with real datetime timestamps
  • Interruptible background sweep worker
"""

import math
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QDoubleSpinBox,
    QSpinBox, QCheckBox, QGroupBox, QTabWidget,
    QTextEdit, QLineEdit, QFileDialog, QProgressBar,
    QScrollArea, QFrame, QSizePolicy, QMessageBox,
    QSplitter,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor

from workers import (open_resource, use_instrument, shared_resource, shared_driver,
                     check_instruments, _INSTR_NAMES)
from test_automation import ADDR, GWGPP4323, WT5000Driver, ITechFixed, MDO34Driver
from daq_espec_panel import daq_scan
from ui_helpers import _btn, unique_path
from app_logger import get_logger, APP_DIR

_log = get_logger("sweep")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mdo_waveform_png(ch: str, pts: int, label: str) -> str:
    """Capture one waveform from MDO34 and save as a PNG to a temp file.

    Returns the absolute path to the saved PNG, or '' on failure.
    The caller is responsible for deleting the file after workbook save.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import tempfile, os
        conn = shared_driver('mdo', lambda: MDO34Driver(ADDR['mdo']))
        result = conn.run(lambda drv: drv.capture_waveform(ch, pts))
        if result is None or result[0] is None:
            return ''
        t, v = result
        fig, ax = plt.subplots(figsize=(5, 2.5), dpi=100)
        t_us = t * 1e6
        ax.plot(t_us, v, linewidth=0.8, color='#1f77b4')
        ax.set_xlabel('Time (\u03bcs)', fontsize=8)
        ax.set_ylabel('V', fontsize=8)
        ax.set_title(label, fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=7)
        fig.tight_layout(pad=0.5)
        fd, path = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        fig.savefig(path, dpi=100)
        plt.close(fig)
        return path
    except Exception:
        return ''


def _make_steps(start: float, stop: float, n: int, spacing: str,
                linthresh: float = None) -> list:
    """Generate n evenly spaced (linear) or decade-spaced (log) points.

    Decade spacing works for:
      - All-positive or all-negative ranges: standard log space on magnitudes.
      - Mixed-sign ranges (e.g. -5 A → +5 A): symmetric-log — splits the n
        steps between the negative and positive halves, skipping zero, using a
        small threshold equal to 1 % of the smaller magnitude.
    """
    if n <= 1:
        return [float(start)]
    if spacing == "decade":
        if start == 0 or stop == 0:
            raise ValueError(
                "Decade sweep requires non-zero start and stop "
                "(zero has no log representation).\n"
                "Use a small non-zero boundary, e.g. ±0.01."
            )
        if (start > 0) == (stop > 0):
            # Same sign — standard log spacing on absolute values
            sign = -1.0 if start < 0 else 1.0
            a, b = abs(start), abs(stop)
            ls, le = math.log10(a), math.log10(b)
            return [sign * 10 ** (ls + (le - ls) * i / (n - 1)) for i in range(n)]
        else:
            # Mixed sign — normalise so start < 0 < stop, mirror back if needed
            if start > 0:
                return [-x for x in _make_steps(-start, -stop, n, 'decade', linthresh)]
            lt = linthresh if linthresh is not None \
                 else min(abs(start), abs(stop)) * 0.01
            lt = max(1e-9, lt)
            n_neg = n // 2
            n_pos = n - n_neg
            neg_mags = (_make_steps(abs(start), lt, n_neg, 'decade')
                        if n_neg > 1 else [abs(start)])
            pos_mags = (_make_steps(lt, abs(stop),  n_pos, 'decade')
                        if n_pos > 1 else [abs(stop)])
            return [-m for m in neg_mags] + list(pos_mags)
    else:
        return [start + (stop - start) * i / (n - 1) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# Background sweep worker
# ─────────────────────────────────────────────────────────────────────────────

class SweepRunner(QThread):
    """
    Executes the sweep in a background thread.

    Each supply (ITECH, GPP CH1-4) has its own step count and spacing.
    Total steps = max across all enabled supplies.
    Supplies with fewer steps hold their last value for remaining steps.

    Config keys (new format)
    ────────────────────────
    n_readings    int
    interval_ms   int
    itech_enable  bool
    itech_v_start/v_stop  float  — voltage sweep
    itech_i_start/i_stop  float  — current limit sweep
    itech_steps   int
    itech_spacing "linear"|"decade"
    itech_mode    "SOURCE"|"SINK"
    gpp_tracking  "independent"|"series"|"parallel"
    gpp_channels  {1..4: {enable,v_start,v_stop,i_start,i_stop,steps,spacing}}
    meas_inputs   [(label, fn→float), ...]
    file_path     str
    """

    log_msg   = pyqtSignal(str)
    progress  = pyqtSignal(int, int)
    finished  = pyqtSignal(str)
    error_sig = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self._cfg       = config
        self._stop      = threading.Event()
        self._eddie     = config.get('eddie_panel')
        self._daq       = config.get('daq_panel')
        self._espec     = config.get('espec_panel')
        self._gpp_panel = config.get('gpp_panel')

    def stop(self):
        self._stop.set()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_msg.emit(f"[{ts}]  {msg}")
        # Also write to the rotating file log — warnings/errors get extra attention
        if '\u26a0' in msg or '\u274c' in msg or 'error' in msg.lower() or 'fail' in msg.lower():
            _log.warning("SweepRunner: %s", msg)
        else:
            _log.debug("SweepRunner: %s", msg)

    def run(self):
        _log.info("SweepRunner started")
        try:
            self._execute()
            _log.info("SweepRunner completed normally")
        except Exception as e:
            _log.error("SweepRunner crashed: %s", e, exc_info=True)
            self.error_sig.emit(str(e))

    @staticmethod
    def _at(arr: list, step: int, supply_total: int):
        """Equidistant index into arr within supply_total steps.

        The lower-count axis switches at evenly-spaced intervals:
          index = step * len(arr) // supply_total
        Steps beyond supply_total (when a different supply has more steps)
        are clamped to keep the last value.
        """
        n   = len(arr)
        eff = min(step, supply_total - 1)   # clamp once this supply is exhausted
        return arr[eff * n // supply_total]

    def _execute(self):
        try:
            import openpyxl
            from openpyxl.styles import Font as XFont
        except ImportError:
            raise RuntimeError("openpyxl not installed.\nRun:  pip install openpyxl")

        cfg          = self._cfg
        n_readings   = cfg["n_readings"]
        interval_s   = cfg["interval_ms"] / 1000.0
        avg_readings = bool(cfg.get("avg_readings", True))
        meas_inputs  = cfg["meas_inputs"]

        # ── Per-supply step arrays ─────────────────────────────────────────
        itech_en    = cfg["itech_enable"]
        itech_v_arr = itech_i_arr = []
        if itech_en:
            itech_v_arr = _make_steps(cfg["itech_v_start"], cfg["itech_v_stop"],
                                      cfg["itech_v_steps"], cfg["itech_v_spacing"])
            itech_i_arr = _make_steps(cfg["itech_i_start"], cfg["itech_i_stop"],
                                      cfg["itech_i_steps"], cfg["itech_i_spacing"],
                                      cfg.get("itech_i_linthresh"))

        gpp_v_arr = {}
        gpp_i_arr = {}
        tracking  = cfg.get("gpp_tracking", "independent")
        for ch, cd in cfg["gpp_channels"].items():
            if cd["enable"]:
                gpp_v_arr[ch] = _make_steps(cd["v_start"], cd["v_stop"], cd["v_steps"], cd["v_spacing"])
                gpp_i_arr[ch] = _make_steps(cd["i_start"], cd["i_stop"], cd["i_steps"], cd["i_spacing"])

        itech_supply_total = max(len(itech_v_arr), len(itech_i_arr)) if itech_en else 1
        gpp_supply_total   = {ch: max(len(gpp_v_arr[ch]), len(gpp_i_arr[ch]))
                               for ch in gpp_v_arr}
        all_lens = ([itech_supply_total] if itech_en else []) + list(gpp_supply_total.values())
        total_steps = max(all_lens) if all_lens else 1

        # ── ESPEC step arrays ──────────────────────────────────────────────
        espec_en    = cfg.get("espec_enable", False) and self._espec is not None
        espec_t_arr = espec_h_arr = []
        espec_total = 1
        if espec_en:
            espec_t_arr = _make_steps(cfg.get("espec_t_start", 25), cfg.get("espec_t_stop", 85),
                                      cfg.get("espec_t_steps", 5),   "linear")
            espec_h_arr = _make_steps(cfg.get("espec_h_start", 50), cfg.get("espec_h_stop", 90),
                                      cfg.get("espec_h_steps", 3),   "linear")
            espec_total = max(len(espec_t_arr), len(espec_h_arr), 1)
            all_lens.append(espec_total)
            total_steps = max(all_lens)

        # ── NGI step arrays ─────────────────────────────────────────────────
        ngi_en    = cfg.get("ngi_enable", False)
        ngi_v_arr = []
        ngi_total = 1
        ngi_cell_chs = [c for c in range(1, 18) if c != 9]  # 16 cells, skip busbar
        if ngi_en:
            ngi_v_arr = _make_steps(cfg.get("ngi_v_start", 2.5), cfg.get("ngi_v_stop", 4.2),
                                    cfg.get("ngi_v_steps", 10),
                                    cfg.get("ngi_v_spacing", "linear"))
            ngi_total = len(ngi_v_arr) or 1
            all_lens.append(ngi_total)
            total_steps = max(all_lens)

        # ── File path (resolved once, before loop) ─────────────────────────
        fp = cfg.get("file_path", "").strip()
        if not fp or fp == ".xlsx":
            fp = str(APP_DIR / "results" /
                     f"sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        if not fp.endswith(".xlsx"):
            fp += ".xlsx"
        Path(fp).parent.mkdir(parents=True, exist_ok=True)
        fp = unique_path(fp)

        def _flush():
            try:
                wb.save(fp)
            except Exception as _e:
                self._log(f"  ⚠ Mid-run save error: {_e}")

        # ── MDO waveform config ────────────────────────────────────────────
        mdo_wf      = cfg.get('mdo_waveform', {})
        mdo_wf_en   = mdo_wf.get('enabled', False)
        mdo_wf_ch   = mdo_wf.get('ch', 'CH1')
        mdo_wf_pts  = mdo_wf.get('pts', 10000)
        _wf_tmpfiles: list = []   # track temp PNGs to delete after save

        # ── Excel workbook ─────────────────────────────────────────────────
        wb = openpyxl.Workbook()
        ws = wb.active; ws.title = "Sweep Data"
        ws_wf = None   # created lazily when first waveform is captured

        eddie_keys = list(self._eddie.get_watchlist_snapshot().keys()) if self._eddie else []

        _rd_hdr = [] if avg_readings else ["Reading"]
        headers = ["Timestamp", "Step"] + _rd_hdr
        if itech_en:
            headers += ["ITECH V_set (V)", "ITECH I_lim (A)", "Mode"]
        for ch in sorted(gpp_v_arr):
            headers += [f"GPP CH{ch} V_set (V)", f"GPP CH{ch} I_lim (A)"]
        if ngi_en:
            headers.append("NGI Cell V_set (V)")
        for lbl, _ in meas_inputs:
            headers.append(lbl)
        for k in eddie_keys:
            headers.append(f"EDDIE {k}")
        daq_cfg_run = self._daq.get_scan_config() if (cfg.get("daq_en") and self._daq) else []
        for c in daq_cfg_run:
            headers.append(c["label"])
        if cfg.get("espec_meas_t") and self._espec:
            headers.append("ESPEC T_actual (\u00b0C)")
        if cfg.get("espec_meas_h") and self._espec:
            headers.append("ESPEC H_actual (%)")
        ws.append(headers)
        for cell in ws[1]:
            cell.font = XFont(bold=True)
        _flush()
        self._log(f"✏️  Logging to: {fp}")

        # ── Log summary ────────────────────────────────────────────────────
        self._log(
            f"Sweep: {total_steps} total steps, "
            f"{n_readings} readings/step, {interval_s*1000:.0f} ms interval, "
            f"dwell={n_readings*interval_s:.2f} s/step"
        )
        if itech_en:
            self._log(
                f"ITECH (auto SOURCE/SINK): "
                f"V {cfg['itech_v_start']:.3f}→{cfg['itech_v_stop']:.3f} V  "
                f"I {cfg['itech_i_start']:+.3f}→{cfg['itech_i_stop']:+.3f} A  "
                f"(V: {cfg['itech_v_steps']} steps, {cfg['itech_v_spacing']}  "
                f"I: {cfg['itech_i_steps']} steps, {cfg['itech_i_spacing']})"
            )
        if tracking != "independent":
            self._log(f"GPP tracking: {tracking.upper()} (CH1+CH2)")
        for ch in sorted(gpp_v_arr):
            cd = cfg["gpp_channels"][ch]
            self._log(
                f"GPP CH{ch}: "
                f"V {cd['v_start']:.3f}→{cd['v_stop']:.3f} V  "
                f"I {cd['i_start']:.3f}→{cd['i_stop']:.3f} A  "
                f"(V: {cd['v_steps']} steps, {cd['v_spacing']}  "
                f"I: {cd['i_steps']} steps, {cd['i_spacing']})"
            )

        if ngi_en:
            self._log(
                f"NGI Cell Sim: V {cfg['ngi_v_start']:.4f}→{cfg['ngi_v_stop']:.4f} V  "
                f"({cfg['ngi_v_steps']} steps, {cfg['ngi_v_spacing']})  "
                f"I_lim={cfg['ngi_i_limit']:.0f} mA  "
                f"Busbar={cfg['ngi_bus_v']:.4f} V"
            )
            # Set current limits and busbar once before sweep
            try:
                conn = shared_resource('ngi')
                def _ngi_init(res):
                    i_lim = cfg['ngi_i_limit']
                    for ch in ngi_cell_chs:
                        res.write(f"SOUR{ch}:OUTCURR {i_lim:.1f}")
                    # Set busbar voltage and turn it on
                    bus_v = cfg['ngi_bus_v']
                    res.write(f"SOUR9:VOLT {bus_v:.4f}")
                    res.write(f"OUTP9:ONOFF 1")
                conn.run(_ngi_init)
                self._log("  NGI: current limits & busbar set")
            except Exception as e:
                self._log(f"  ⚠ NGI init: {e}")

        # ── Apply GPP tracking mode once ───────────────────────────────────
        if tracking != "independent" and gpp_v_arr:
            try:
                shared_resource('gpp').run(
                    lambda res, _t=tracking: GWGPP4323(res).set_tracking_mode(_t))
                self._log(f"GPP tracking set → {tracking.upper()}")
            except Exception as e:
                self._log(f"⚠  GPP tracking error: {e}")

        # ── Apply GPP OVP/OCP once at sweep start (from Instruments tab) ───────
        _prot_cfg = (self._gpp_panel.get_protection_config()
                     if self._gpp_panel is not None else {})
        if gpp_v_arr and _prot_cfg:
            try:
                def _apply_prot(res, _cfg=_prot_cfg):
                    d = GWGPP4323(res)
                    for ch in range(1, 5):
                        cd = _cfg.get(ch, {})
                        if cd.get('ovp_en', False):
                            d.set_ovp(ch, cd['ovp']); d.enable_ovp(ch, True)
                        if cd.get('ocp_en', False):
                            d.set_ocp(ch, cd['ocp']); d.enable_ocp(ch, True)
                shared_resource('gpp').run(_apply_prot)
                self._log("GPP OVP/OCP applied (from Instruments tab)")
            except Exception as e:
                self._log(f"⚠  GPP protection setup error: {e}")

        # ── Build GPP mode map from gpp_channels config ─────────────────────
        _gpp_modes = {}  # ch → 'source'/'cv'/'cc'/'cr'
        for ch in range(1, 5):
            _gpp_modes[ch] = gpp_ch.get(ch, {}).get("mode", "source")

        # Set load mode for CH1/CH2 at sweep start
        try:
            def _mode_setup(res, _modes=_gpp_modes):
                drv = GWGPP4323(res)
                msgs = []
                for ch in (1, 2):
                    m = _modes[ch]
                    if m != "source":
                        drv.set_load_mode(ch, m.upper())
                        msgs.append(f"GPP CH{ch} \u2192 Load {m.upper()}")
                    else:
                        drv.set_load_mode(ch, 'OFF')
                return msgs
            for m in (shared_resource('gpp').run(_mode_setup) or []):
                self._log(m)
        except Exception as e:
            self._log(f"\u26a0  GPP mode setup: {e}")

        # ── Main loop ──────────────────────────────────────────────────────
        for step in range(total_steps):
            if self._stop.is_set():
                self._log("\u23f9  Stopped by user."); break

            self.progress.emit(step, total_steps)
            sd = f"Step {step+1}/{total_steps}"

            itech_v = self._at(itech_v_arr, step, itech_supply_total) if itech_en else None
            itech_i = self._at(itech_i_arr, step, itech_supply_total) if itech_en else None

            # Set ITECH
            if itech_en:
                try:
                    mode = "SOURCE" if itech_i >= 0 else "SINK"
                    signed_i = itech_i if mode == "SOURCE" else -abs(itech_i)
                    _conn = shared_driver('itech', lambda: ITechFixed(ADDR['itech']))
                    def _apply(drv, _v=itech_v, _i=signed_i):
                        drv.set_volts(_v); drv.set_amps(_i); drv.on()
                    _conn.run(_apply)
                    self._log(f"{sd}  ITECH \u2192 V={itech_v:.4f} V  I={itech_i:+.4f} A  ({mode})")
                except Exception as e:
                    self._log(f"\u26a0  {sd} ITECH: {e}")

            # Set GPP channels — mode-aware (batched via shared_resource)
            if gpp_v_arr:
                try:
                    def _gpp_step(res, _step=step, _varr=gpp_v_arr,
                                  _iarr=gpp_i_arr, _st=gpp_supply_total,
                                  _modes=_gpp_modes):
                        drv = GWGPP4323(res)
                        msgs = []
                        for ch in sorted(_varr):
                            v = self._at(_varr[ch], _step, _st[ch])
                            i = self._at(_iarr[ch], _step, _st[ch])
                            m = _modes.get(ch, "source")
                            if m == "cr":
                                drv.set_load_resistance(ch, v)
                            else:
                                drv.set_voltage(ch, v)
                            drv.set_current(ch, i)
                            drv.enable_channel(ch, True)
                            if m != "source":
                                msgs.append(f"      GPP CH{ch} {m.upper()} \u2192 V/R={v:.4f}  I={i:.4f}")
                            else:
                                msgs.append(f"      GPP CH{ch} Source \u2192 V={v:.4f} V  I_lim={i:.4f} A")
                        return msgs
                    for m in (shared_resource('gpp').run(_gpp_step) or []):
                        self._log(m)
                except Exception as e:
                    self._log(f"\u26a0  {sd} GPP: {e}")

            # ── NGI cell voltage at this step ──────────────────────────────
            if ngi_en:
                ngi_v = self._at(ngi_v_arr, step, ngi_total)
                try:
                    conn = shared_resource('ngi')
                    def _ngi_set(res, _v=ngi_v):
                        for ch in ngi_cell_chs:
                            res.write(f"SOUR{ch}:VOLT {_v:.4f}")
                            res.write(f"OUTP{ch}:ONOFF 1")
                    conn.run(_ngi_set)
                    self._log(f"      NGI cells → V={ngi_v:.4f} V  (16 CH)")
                except Exception as e:
                    self._log(f"⚠  {sd} NGI: {e}")

            # ── ESPEC setpoint at this step ────────────────────────────────
            if espec_en:
                et = self._at(espec_t_arr, step, espec_total)
                eh = self._at(espec_h_arr, step, espec_total)
                try:
                    self._espec.apply_setpoints_for_runner(
                        et, eh,
                        ramp_t=cfg.get("espec_ramp_t", 0),
                        ramp_h=cfg.get("espec_ramp_h", 0))
                    self._log(f"      ESPEC \u2192 T={et:.1f}\u00b0C  H={eh:.1f}%")
                except Exception as e:
                    self._log(f"\u26a0  ESPEC setpoint: {e}")
                if cfg.get("espec_wait_stable", False):
                    tol_t = cfg.get("espec_tol_t",  0.5)
                    tol_h = cfg.get("espec_tol_h",  2.0)
                    tout  = cfg.get("espec_timeout_s", 600)
                    poll  = cfg.get("espec_stable_poll_s", 5)
                    t0_s  = time.perf_counter()
                    self._log(f"  \u23f3 Waiting for stabilization "
                              f"(\u00b1{tol_t}\u00b0C / \u00b1{tol_h}%  timeout {tout}s)\u2026")
                    while not self._stop.is_set() and (time.perf_counter() - t0_s < tout):
                        try:
                            at = self._espec.read_actual_temp_for_runner()
                            ah = self._espec.read_actual_humi_for_runner()
                            if abs(at - et) <= tol_t and abs(ah - eh) <= tol_h:
                                self._log(f"  \u2713 Stable  T={at:.1f}\u00b0C  H={ah:.1f}%"); break
                            self._log(f"    T={at:.1f}\u00b0C (\u0394{at-et:+.1f})  "
                                      f"H={ah:.1f}% (\u0394{ah-eh:+.1f})  "
                                      f"[{time.perf_counter()-t0_s:.0f}s]")
                        except Exception as e:
                            self._log(f"  \u26a0 Stab poll: {e}")
                        time.sleep(poll)

            # Settling time
            settling_s = cfg.get("settling_ms", 0) / 1000.0
            if settling_s > 0 and not self._stop.is_set():
                time.sleep(settling_s)

            # ── DAQ scan (once per step) + ESPEC actual ────────────────────
            daq_vals = {}
            if daq_cfg_run:
                try:
                    with use_instrument("daq") as res:
                        daq_vals = daq_scan(res, daq_cfg_run)
                    self._log("  DAQ: " +
                              "  ".join(f"{c['label']}={daq_vals.get(c['label'], float('nan')):.5g}"
                                        for c in daq_cfg_run))
                except Exception as e:
                    self._log(f"\u26a0  DAQ scan: {e}")
                    daq_vals = {c["label"]: float("nan") for c in daq_cfg_run}
            espec_t_act = espec_h_act = float("nan")
            if cfg.get("espec_meas_t") and self._espec:
                try:    espec_t_act = self._espec.read_actual_temp_for_runner()
                except Exception as e: self._log(f"\u26a0  ESPEC T_act: {e}")
            if cfg.get("espec_meas_h") and self._espec:
                try:    espec_h_act = self._espec.read_actual_humi_for_runner()
                except Exception as e: self._log(f"\u26a0  ESPEC H_act: {e}")

            # Collect N readings (+ Eddie snapshots at every sample)
            acc       = {lbl: [] for lbl, _ in meas_inputs}
            eddie_acc = {k: [] for k in eddie_keys}

            def _row_base(ts_val, rd_num=None):
                r = [ts_val, step + 1]
                if not avg_readings: r.append(rd_num)
                if itech_en:
                    mode = "SOURCE" if itech_i >= 0 else "SINK"
                    r += [itech_v, itech_i, mode]
                for ch in sorted(gpp_v_arr):
                    st = gpp_supply_total[ch]
                    r += [self._at(gpp_v_arr[ch], step, st),
                          self._at(gpp_i_arr[ch], step, st)]
                if ngi_en:
                    r.append(self._at(ngi_v_arr, step, ngi_total))
                return r

            for _rd in range(n_readings):
                if self._stop.is_set(): break
                t0 = time.perf_counter()
                meas_now = {}; eddie_now = {}
                for lbl, fn in meas_inputs:
                    try:
                        v = fn(); acc[lbl].append(v); meas_now[lbl] = v
                    except Exception as e:
                        self._log(f"⚠  [{lbl}]: {e}")
                        acc[lbl].append(float("nan")); meas_now[lbl] = float("nan")
                if self._eddie:
                    snap = self._eddie.get_watchlist_snapshot()
                    for k in eddie_keys:
                        try:
                            val = float(snap.get(k, float('nan')))
                            eddie_acc[k].append(val); eddie_now[k] = val
                        except (ValueError, TypeError):
                            eddie_acc[k].append(float('nan')); eddie_now[k] = float('nan')
                if not avg_readings:
                    ts_rd = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    row_data = _row_base(ts_rd, _rd + 1)
                    for lbl, _ in meas_inputs:
                        row_data.append(meas_now.get(lbl, float('nan')))
                    for k in eddie_keys:
                        row_data.append(eddie_now.get(k, float('nan')))
                    for c in daq_cfg_run:
                        row_data.append(daq_vals.get(c["label"], float("nan")))
                    if cfg.get("espec_meas_t") and self._espec:
                        row_data.append(espec_t_act)
                    if cfg.get("espec_meas_h") and self._espec:
                        row_data.append(espec_h_act)
                    ws.append(row_data)
                    if mdo_wf_en:
                        _png = _mdo_waveform_png(
                            mdo_wf_ch, mdo_wf_pts, f"Step {step+1} Rd {_rd+1}")
                        if _png:
                            _wf_tmpfiles.append(_png)
                            try:
                                from openpyxl.drawing.image import Image as OpxImage
                                if ws_wf is None:
                                    ws_wf = wb.create_sheet("Waveforms")
                                    ws_wf.append(["Step", "Reading", "Timestamp", "Ch"])
                                ws_wf.append([step + 1, _rd + 1,
                                              datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                              mdo_wf_ch])
                                _ir = ws_wf.max_row + 1
                                ws_wf.row_dimensions[_ir].height = 150
                                _oi = OpxImage(_png)
                                _oi.width = 500; _oi.height = 200
                                ws_wf.add_image(_oi, f"A{_ir}")
                                ws_wf.row_dimensions[_ir + 1].height = 10
                            except Exception as _ie:
                                self._log(f"  ⚠ Waveform embed: {_ie}")
                    _flush()
                rem = interval_s - (time.perf_counter() - t0)
                if rem > 0:
                    time.sleep(rem)

            if avg_readings:
                avg = {}
                for lbl, vals in acc.items():
                    good = [x for x in vals if not math.isnan(x)]
                    avg[lbl] = sum(good) / len(good) if good else float("nan")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                row_data = _row_base(ts)
                for lbl, _ in meas_inputs:
                    row_data.append(avg[lbl])
                for k in eddie_keys:
                    vals = [v for v in eddie_acc.get(k, []) if not math.isnan(v)]
                    row_data.append(sum(vals) / len(vals) if vals else float('nan'))
                for c in daq_cfg_run:
                    row_data.append(daq_vals.get(c["label"], float("nan")))
                if cfg.get("espec_meas_t") and self._espec:
                    row_data.append(espec_t_act)
                if cfg.get("espec_meas_h") and self._espec:
                    row_data.append(espec_h_act)
                ws.append(row_data)

                if mdo_wf_en:
                    png = _mdo_waveform_png(mdo_wf_ch, mdo_wf_pts, f"Step {step+1} avg")
                    if png:
                        _wf_tmpfiles.append(png)
                        try:
                            from openpyxl.drawing.image import Image as OpxImage
                            if ws_wf is None:
                                ws_wf = wb.create_sheet("Waveforms")
                                ws_wf.append(["Step", "Timestamp", "Channel"])
                            ws_wf.append([step + 1,
                                          datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                          mdo_wf_ch])
                            img_row = ws_wf.max_row + 1
                            ws_wf.row_dimensions[img_row].height = 150
                            opx_img = OpxImage(png)
                            opx_img.width = 500; opx_img.height = 200
                            ws_wf.add_image(opx_img, f"A{img_row}")
                            ws_wf.row_dimensions[img_row + 1].height = 10
                        except Exception as _ie:
                            self._log(f"  ⚠ Waveform embed: {_ie}")

                _flush()
                self._log("  ✓ " + sd + "  " +
                          ",  ".join(f"{k}={v:.5g}" for k, v in avg.items()) +
                          "  (averaged)")

        # ── Final save ─────────────────────────────────────────────────────
        self.progress.emit(total_steps, total_steps)
        try:
            wb.save(fp)
            self._log(f"✓  File closed: {fp}")
        except Exception as e:
            self._log(f"⚠  Final save error: {e}")
        finally:
            import os
            for _tmp in _wf_tmpfiles:
                try: os.unlink(_tmp)
                except Exception: pass

        # ── Turn off all enabled supplies ────────────────────────────────────────────────────
        self._log("Powering off all enabled supplies…")
        if itech_en:
            try:
                _conn = shared_driver('itech', lambda: ITechFixed(ADDR['itech']))
                _conn.run(lambda drv: drv.off())
                self._log("  ✓ ITECH output OFF")
            except Exception as e:
                self._log(f"  ⚠ ITECH shutdown: {e}")
        if gpp_v_arr:
            try:
                def _gpp_shutdown(res, _varr=gpp_v_arr, _modes=_gpp_modes,
                                  _track=tracking):
                    drv = GWGPP4323(res)
                    for ch in sorted(_varr):
                        drv.enable_channel(ch, False)
                    for ch in (1, 2):
                        if _modes.get(ch, 'source') != 'source':
                            drv.set_load_mode(ch, "OFF")
                    if _track != "independent":
                        drv.set_tracking_mode("independent")
                shared_resource('gpp').run(_gpp_shutdown)
                _load_reset = [ch for ch in (1, 2)
                               if _gpp_modes.get(ch, 'source') != 'source']
                self._log(f"  \u2713 GPP CH{sorted(list(gpp_v_arr.keys()))} OFF"
                          + (f"  (load mode reset: CH{_load_reset})"
                             if _load_reset else ""))
            except Exception as e:
                self._log(f"  \u26a0 GPP shutdown: {e}")
        if ngi_en:
            try:
                conn = shared_resource('ngi')
                def _ngi_off(res):
                    for ch in range(1, 18):
                        res.write(f"OUTP{ch}:ONOFF 0")
                conn.run(_ngi_off)
                self._log("  ✓ NGI all channels OFF")
            except Exception as e:
                self._log(f"  ⚠ NGI shutdown: {e}")

        self.finished.emit(fp)


# ─────────────────────────────────────────────────────────────────────────────
# Sweep panel UI
# ─────────────────────────────────────────────────────────────────────────────

class SweepPanel(QWidget):
    steps_exported = pyqtSignal(list)   # emitted by → Sequence button
    test_running   = pyqtSignal(bool)   # True when sweep starts, False when it ends
    """
    Full sweep configuration and run panel.
    Hosted as a top-level tab in the main window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runner      = None
        self._last_file   = ""
        self._eddie_panel = None
        self._daq_panel   = None
        self._espec_panel = None
        self._gpp_panel   = None
        self._lib_panel   = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        # ── Left-right splitter: config | run/log ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left side: scrollable source configuration
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        cfg_w = QWidget()
        cfg_lay = QVBoxLayout(cfg_w)
        cfg_lay.setSpacing(8)
        cfg_lay.setContentsMargins(2, 2, 2, 2)

        cfg_lay.addWidget(self._build_global_group())
        cfg_lay.addWidget(self._build_itech_group())
        cfg_lay.addWidget(self._build_gpp_group())
        self._ngi_container = self._build_ngi_group()
        self._ngi_container.setVisible(False)  # shown when NGI connects
        cfg_lay.addWidget(self._ngi_container)
        cfg_lay.addWidget(self._build_espec_group())
        cfg_lay.addWidget(self._build_meas_group())
        cfg_lay.addWidget(self._build_file_group())
        cfg_lay.addStretch()
        scroll.setWidget(cfg_w)
        splitter.addWidget(scroll)

        # Right side: run controls + progress + log (always visible)
        splitter.addWidget(self._build_run_group())

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([600, 400])

        outer.addWidget(splitter)
        self._update_totals()

    def set_eddie_panel(self, panel):
        """Called by MainWindow to pass the EddiePanel reference for watchlist logging."""
        self._eddie_panel = panel

    def set_daq_panel(self, panel):
        """Called by MainWindow to pass the DAQ 973A panel reference."""
        self._daq_panel = panel

    def set_espec_panel(self, panel):
        """Called by MainWindow to pass the ESPEC SH-662 panel reference."""
        self._espec_panel = panel

    def set_gpp_panel(self, panel):
        """Called by MainWindow to pass the GppPanel reference for OVP/OCP config."""
        self._gpp_panel = panel

    def set_library_panel(self, panel):
        """Called by MainWindow to pass the InstrumentLibraryPanel reference."""
        self._lib_panel = panel

    # ── Global (timing only — steps/spacing are per-supply) ───────────────────

    def _build_global_group(self):
        grp = QGroupBox("Sweep Timing")
        g   = QGridLayout(grp)

        g.addWidget(QLabel("Readings / step:"), 0, 0)
        self.n_readings = QSpinBox()
        self.n_readings.setRange(1, 1000); self.n_readings.setValue(5)
        g.addWidget(self.n_readings, 0, 1)

        g.addWidget(QLabel("Reading interval:"), 0, 2)
        self.interval_ms = QSpinBox()
        self.interval_ms.setRange(100, 60000); self.interval_ms.setValue(1000)
        self.interval_ms.setSuffix(" ms")
        g.addWidget(self.interval_ms, 0, 3)

        g.addWidget(QLabel("Settling time:"), 0, 4)
        self.settling_ms = QSpinBox()
        self.settling_ms.setRange(0, 60000); self.settling_ms.setValue(500)
        self.settling_ms.setSuffix(" ms")
        self.settling_ms.setToolTip(
            "Time to wait after switching to a new step before taking readings.")
        g.addWidget(self.settling_ms, 0, 5)

        g.addWidget(QLabel("Dwell / step:"), 1, 0)
        self.dwell_lbl = QLabel("—")
        self.dwell_lbl.setStyleSheet("color:#fab387; font-weight:bold;")
        g.addWidget(self.dwell_lbl, 1, 1)

        g.addWidget(QLabel("Total steps:"), 1, 2)
        self.total_steps_lbl = QLabel("—")
        self.total_steps_lbl.setStyleSheet("color:#89b4fa; font-weight:bold;")
        g.addWidget(self.total_steps_lbl, 1, 3)

        g.addWidget(QLabel("Total time:"), 2, 0)
        self.total_time_lbl = QLabel("—")
        self.total_time_lbl.setStyleSheet("color:#fab387; font-weight:bold;")
        g.addWidget(self.total_time_lbl, 2, 1, 1, 3)

        self.avg_readings = QCheckBox("Average readings  (one Excel row per step)")
        self.avg_readings.setChecked(True)
        self.avg_readings.setToolTip(
            "If unchecked, every individual reading is written as its own row in Excel")
        g.addWidget(self.avg_readings, 3, 0, 1, 6)

        self.n_readings.valueChanged.connect(self._update_totals)
        self.interval_ms.valueChanged.connect(self._update_totals)
        self.settling_ms.valueChanged.connect(self._update_totals)
        return grp

    def _update_totals(self):
        counts = []
        if hasattr(self, 'itech_en') and self.itech_en.isChecked():
            if hasattr(self, 'itech_v_steps'):
                counts.append(max(self.itech_v_steps.value(), self.itech_i_steps.value()))
        if hasattr(self, '_gpp_en'):
            tr_idx = self.gpp_tracking.currentIndex() if hasattr(self, 'gpp_tracking') else 0
            for ch in range(1, 5):
                if ch == 2 and tr_idx > 0:
                    continue
                if self._gpp_en[ch].isChecked():
                    if hasattr(self, '_gpp_vsteps'):
                        counts.append(max(self._gpp_vsteps[ch].value(), self._gpp_isteps[ch].value()))
        if hasattr(self, 'espec_src_en') and self.espec_src_en.isChecked():
            counts.append(max(self.espec_t_steps.value(), self.espec_h_steps.value()))
        if hasattr(self, 'ngi_src_en') and self.ngi_src_en.isChecked():
            counts.append(self.ngi_v_steps.value())
        total    = max(counts) if counts else 0
        dwell    = self.n_readings.value() * self.interval_ms.value() / 1000.0
        settling = self.settling_ms.value() / 1000.0
        total_s  = (dwell + settling) * total
        self.dwell_lbl.setText(f"{dwell:.1f} s")
        self.total_steps_lbl.setText(str(total) if total else "—")
        self.total_time_lbl.setText(
            f"{total_s:.1f} s  ({total_s/60:.1f} min)" if total else "—")

    # ── ITECH config ──────────────────────────────────────────────────────────

    def _build_itech_group(self):
        grp = QGroupBox("ITECH IT6005C")
        g   = QGridLayout(grp)

        # Row 0: enable
        self.itech_en = QCheckBox("Include in sweep")
        self.itech_en.setChecked(True)
        self.itech_en.toggled.connect(self._update_totals)
        g.addWidget(self.itech_en, 0, 0, 1, 6)

        # Column headers
        for col, hdr in enumerate(["", "Start", "", "Stop", "Steps", "Spacing"]):
            lbl = QLabel(hdr); lbl.setStyleSheet("font-weight:bold; color:#89b4fa;")
            g.addWidget(lbl, 1, col)

        # Row 2: voltage sweep + V steps / spacing
        g.addWidget(QLabel("Voltage:"), 2, 0)
        self.itech_v_start = QDoubleSpinBox()
        self.itech_v_start.setRange(0.001, 80); self.itech_v_start.setValue(1.0)
        self.itech_v_start.setSuffix(" V"); self.itech_v_start.setDecimals(3)
        g.addWidget(self.itech_v_start, 2, 1)
        g.addWidget(QLabel("→"), 2, 2)
        self.itech_v_stop = QDoubleSpinBox()
        self.itech_v_stop.setRange(0.001, 80); self.itech_v_stop.setValue(12.0)
        self.itech_v_stop.setSuffix(" V"); self.itech_v_stop.setDecimals(3)
        g.addWidget(self.itech_v_stop, 2, 3)
        self.itech_v_steps = QSpinBox()
        self.itech_v_steps.setRange(1, 10000); self.itech_v_steps.setValue(10)
        self.itech_v_steps.valueChanged.connect(self._update_totals)
        g.addWidget(self.itech_v_steps, 2, 4)
        self.itech_v_spacing = QComboBox()
        self.itech_v_spacing.addItems(["Linear", "Decade"])
        self.itech_v_spacing.setToolTip("Linear: equal steps\nDecade: log-spaced (start > 0)")
        g.addWidget(self.itech_v_spacing, 2, 5)

        # Row 3: current sweep + I steps / spacing
        g.addWidget(QLabel("Current:"), 3, 0)
        self.itech_i_start = QDoubleSpinBox()
        self.itech_i_start.setRange(-150, 150); self.itech_i_start.setValue(5.0)
        self.itech_i_start.setSuffix(" A"); self.itech_i_start.setDecimals(3)
        self.itech_i_start.setToolTip(
            "Positive → SOURCE mode.  Negative → SINK mode.  Auto per step.")
        g.addWidget(self.itech_i_start, 3, 1)
        g.addWidget(QLabel("→"), 3, 2)
        self.itech_i_stop = QDoubleSpinBox()
        self.itech_i_stop.setRange(-150, 150); self.itech_i_stop.setValue(5.0)
        self.itech_i_stop.setSuffix(" A"); self.itech_i_stop.setDecimals(3)
        self.itech_i_stop.setToolTip(
            "Positive → SOURCE mode.  Negative → SINK mode.  Auto per step.")
        g.addWidget(self.itech_i_stop, 3, 3)
        self.itech_i_steps = QSpinBox()
        self.itech_i_steps.setRange(1, 10000); self.itech_i_steps.setValue(10)
        self.itech_i_steps.valueChanged.connect(self._update_totals)
        g.addWidget(self.itech_i_steps, 3, 4)
        self.itech_i_spacing = QComboBox()
        self.itech_i_spacing.addItems(["Linear", "Decade"])
        g.addWidget(self.itech_i_spacing, 3, 5)

        g.addWidget(QLabel("Decade min:"), 3, 6)
        self.itech_i_linthresh = QDoubleSpinBox()
        self.itech_i_linthresh.setRange(1e-6, 100)
        self.itech_i_linthresh.setValue(0.001)
        self.itech_i_linthresh.setDecimals(6)
        self.itech_i_linthresh.setSuffix(" A")
        self.itech_i_linthresh.setToolTip(
            "Mixed-sign decade sweeps only: minimum |current| near zero.\n"
            "e.g. 0.001 A \u2192 sweep never goes closer than 1 mA to zero.\n"
            "Has no effect on linear or single-sign decade sweeps.")
        g.addWidget(self.itech_i_linthresh, 3, 7)

        mode_note = QLabel("\u2139  Mode auto-selected per step: positive current = SOURCE, negative = SINK")
        mode_note.setStyleSheet("color:#a6adc8; font-style:italic; font-size:9pt;")
        g.addWidget(mode_note, 4, 0, 1, 8)

        return grp

    # ── GPP channel config ────────────────────────────────────────────────────

    def _build_gpp_group(self):
        grp  = QGroupBox("GPP-4323 Channels")
        vlay = QVBoxLayout(grp)

        # Tracking selector
        tr_row = QHBoxLayout()
        tr_row.addWidget(QLabel("CH1/CH2 Tracking:"))
        self.gpp_tracking = QComboBox()
        self.gpp_tracking.addItems([
            "Independent",
            "Series (CH1+CH2)  \u2014  2\u00d7 voltage",
            "Parallel (CH1+CH2)  \u2014  2\u00d7 current",
        ])
        self.gpp_tracking.setToolTip(
            "Independent: all 4 channels controlled separately.\n"
            "Series: CH2 slaves to CH1; effective output = 2\u00d7 CH1 voltage.\n"
            "Parallel: CH2 slaves to CH1; effective output = 2\u00d7 CH1 current."
        )
        self.gpp_tracking.currentIndexChanged.connect(self._on_tracking_changed)
        tr_row.addWidget(self.gpp_tracking)
        self.track_info = QLabel("")
        self.track_info.setStyleSheet("color:#f9e2af; font-style:italic;")
        tr_row.addWidget(self.track_info)
        tr_row.addStretch()
        vlay.addLayout(tr_row)

        # Channel grid — Ch | Mode | En | V Start | V Stop | V Steps | V Spc | I Start | I Stop | I Steps | I Spc
        g = QGridLayout()
        for col, hdr in enumerate(
                ["Ch", "Mode", "En", "V Start", "V Stop", "V Steps", "V Spc",
                 "I Start", "I Stop", "I Steps", "I Spc"]):
            lbl = QLabel(hdr); lbl.setStyleSheet("font-weight:bold;")
            g.addWidget(lbl, 0, col)

        self._gpp_mode     = {}
        self._gpp_en       = {}
        self._gpp_vstart   = {}
        self._gpp_vstop    = {}
        self._gpp_vsteps   = {}
        self._gpp_vspacing = {}
        self._gpp_istart   = {}
        self._gpp_istop    = {}
        self._gpp_isteps   = {}
        self._gpp_ispacing = {}
        self._gpp_rows     = {}

        # defaults: (v_start, v_stop, i_start, i_stop, v_steps, i_steps)
        defaults = {
            1: (1.0,  5.0, 0.1, 1.0, 10, 10),
            2: (1.0, 12.0, 0.1, 0.5, 10, 10),
            3: (1.0,  3.3, 0.1, 0.5, 10, 10),
            4: (1.0, 15.0, 0.1, 0.5, 10, 10),
        }

        for row, ch in enumerate(range(1, 5), start=1):
            vs, ve, ist, isp, vnst, inst = defaults[ch]
            rw = []

            lbl = QLabel(f"CH{ch}"); g.addWidget(lbl, row, 0); rw.append(lbl)

            # Mode combo for CH1/CH2 (Source/Load CV/CC/CR); fixed "Source" label for CH3/CH4
            if ch in (1, 2):
                mode_cb = QComboBox()
                mode_cb.addItems(["Source", "Load CV", "Load CC", "Load CR"])
                mode_cb.setToolTip(
                    "Source: normal power supply output.\n"
                    "Load CV: constant-voltage electronic load.\n"
                    "Load CC: constant-current electronic load.\n"
                    "Load CR: constant-resistance electronic load.")
                self._gpp_mode[ch] = mode_cb
                g.addWidget(mode_cb, row, 1); rw.append(mode_cb)
                mode_cb.currentIndexChanged.connect(
                    lambda idx, _ch=ch: self._on_gpp_mode_changed(_ch, idx))
            else:
                src_lbl = QLabel("Source")
                src_lbl.setStyleSheet("color:#a6adc8; font-style:italic;")
                g.addWidget(src_lbl, row, 1); rw.append(src_lbl)

            en = QCheckBox(); en.setChecked(ch == 1)
            en.toggled.connect(self._update_totals)
            self._gpp_en[ch] = en
            g.addWidget(en, row, 2); rw.append(en)

            vst_w = QDoubleSpinBox()
            vst_w.setRange(0.001, 32); vst_w.setValue(vs)
            vst_w.setSuffix(" V"); vst_w.setDecimals(3)
            self._gpp_vstart[ch] = vst_w
            g.addWidget(vst_w, row, 3); rw.append(vst_w)

            vsp_w = QDoubleSpinBox()
            vsp_w.setRange(0.001, 32); vsp_w.setValue(ve)
            vsp_w.setSuffix(" V"); vsp_w.setDecimals(3)
            self._gpp_vstop[ch] = vsp_w
            g.addWidget(vsp_w, row, 4); rw.append(vsp_w)

            vstep_w = QSpinBox()
            vstep_w.setRange(1, 10000); vstep_w.setValue(vnst)
            vstep_w.valueChanged.connect(self._update_totals)
            self._gpp_vsteps[ch] = vstep_w
            g.addWidget(vstep_w, row, 5); rw.append(vstep_w)

            vspc_w = QComboBox()
            vspc_w.addItems(["Linear", "Decade"])
            self._gpp_vspacing[ch] = vspc_w
            g.addWidget(vspc_w, row, 6); rw.append(vspc_w)

            ist_w = QDoubleSpinBox()
            ist_w.setRange(0.001, 3); ist_w.setValue(ist)
            ist_w.setSuffix(" A"); ist_w.setDecimals(3)
            self._gpp_istart[ch] = ist_w
            g.addWidget(ist_w, row, 7); rw.append(ist_w)

            isp_w = QDoubleSpinBox()
            isp_w.setRange(0.001, 3); isp_w.setValue(isp)
            isp_w.setSuffix(" A"); isp_w.setDecimals(3)
            self._gpp_istop[ch] = isp_w
            g.addWidget(isp_w, row, 8); rw.append(isp_w)

            istep_w = QSpinBox()
            istep_w.setRange(1, 10000); istep_w.setValue(inst)
            istep_w.valueChanged.connect(self._update_totals)
            self._gpp_isteps[ch] = istep_w
            g.addWidget(istep_w, row, 9); rw.append(istep_w)

            ispc_w = QComboBox()
            ispc_w.addItems(["Linear", "Decade"])
            self._gpp_ispacing[ch] = ispc_w
            g.addWidget(ispc_w, row, 10); rw.append(ispc_w)

            self._gpp_rows[ch] = rw

        vlay.addLayout(g)

        return grp

    def _on_gpp_mode_changed(self, ch, idx):
        """Update V/I field labels and ranges when GPP channel mode changes."""
        is_load = idx > 0  # 0=Source, 1=CV, 2=CC, 3=CR
        vstart = self._gpp_vstart[ch]
        vstop  = self._gpp_vstop[ch]
        vsteps = self._gpp_vsteps[ch]
        vspc   = self._gpp_vspacing[ch]
        istart = self._gpp_istart[ch]
        istop  = self._gpp_istop[ch]
        isteps = self._gpp_isteps[ch]
        ispc   = self._gpp_ispacing[ch]

        if idx == 3:  # Load CR — V field becomes R
            vstart.setSuffix(" \u03a9"); vstart.setRange(0.1, 10000); vstart.setDecimals(2)
            vstop.setSuffix(" \u03a9");  vstop.setRange(0.1, 10000);  vstop.setDecimals(2)
        else:
            vstart.setSuffix(" V"); vstart.setRange(0.001, 32); vstart.setDecimals(3)
            vstop.setSuffix(" V");  vstop.setRange(0.001, 32);  vstop.setDecimals(3)

        # In load mode, V/I are still sweepable (user may sweep load setpoint)
        # so we keep start/stop/steps — no need to hide them

    def _on_tracking_changed(self):
        idx       = self.gpp_tracking.currentIndex()
        ch2_slave = idx > 0
        for w in self._gpp_rows[2]:
            w.setEnabled(not ch2_slave)
        if idx == 1:
            self.track_info.setText("CH2 slaved \u2014 effective output: 2\u00d7 CH1 voltage")
        elif idx == 2:
            self.track_info.setText("CH2 slaved \u2014 effective output: 2\u00d7 CH1 current")
        else:
            self.track_info.setText("")
        self._update_totals()

    # ── ESPEC SH-662 source ───────────────────────────────────────────────

    def _build_espec_group(self):
        grp = QGroupBox("ESPEC SH-662  (Temperature & Humidity Source)")
        g   = QGridLayout(grp)

        self.espec_src_en = QCheckBox("Include ESPEC in sweep")
        self.espec_src_en.setChecked(False)
        self.espec_src_en.toggled.connect(self._update_totals)
        g.addWidget(self.espec_src_en, 0, 0, 1, 8)

        for col, hdr in enumerate(["", "Start", "", "Stop", "Steps", "Ramp rate"]):
            lbl = QLabel(hdr); lbl.setStyleSheet("font-weight:bold; color:#89b4fa;")
            g.addWidget(lbl, 1, col)

        g.addWidget(QLabel("Temperature:"), 2, 0)
        self.espec_t_start = QDoubleSpinBox()
        self.espec_t_start.setRange(-70, 180); self.espec_t_start.setValue(25.0)
        self.espec_t_start.setSuffix(" \u00b0C"); self.espec_t_start.setDecimals(1)
        g.addWidget(self.espec_t_start, 2, 1)
        g.addWidget(QLabel("\u2192"), 2, 2)
        self.espec_t_stop = QDoubleSpinBox()
        self.espec_t_stop.setRange(-70, 180); self.espec_t_stop.setValue(85.0)
        self.espec_t_stop.setSuffix(" \u00b0C"); self.espec_t_stop.setDecimals(1)
        g.addWidget(self.espec_t_stop, 2, 3)
        self.espec_t_steps = QSpinBox()
        self.espec_t_steps.setRange(1, 500); self.espec_t_steps.setValue(5)
        self.espec_t_steps.valueChanged.connect(self._update_totals)
        g.addWidget(self.espec_t_steps, 2, 4)
        self.espec_ramp_t = QDoubleSpinBox()
        self.espec_ramp_t.setRange(0, 10); self.espec_ramp_t.setValue(0)
        self.espec_ramp_t.setDecimals(1); self.espec_ramp_t.setSuffix(" \u00b0C/min")
        self.espec_ramp_t.setToolTip("0 = maximum ramp rate")
        g.addWidget(self.espec_ramp_t, 2, 5)

        g.addWidget(QLabel("Humidity:"), 3, 0)
        self.espec_h_start = QDoubleSpinBox()
        self.espec_h_start.setRange(10, 98); self.espec_h_start.setValue(50.0)
        self.espec_h_start.setSuffix(" %"); self.espec_h_start.setDecimals(1)
        g.addWidget(self.espec_h_start, 3, 1)
        g.addWidget(QLabel("\u2192"), 3, 2)
        self.espec_h_stop = QDoubleSpinBox()
        self.espec_h_stop.setRange(10, 98); self.espec_h_stop.setValue(90.0)
        self.espec_h_stop.setSuffix(" %"); self.espec_h_stop.setDecimals(1)
        g.addWidget(self.espec_h_stop, 3, 3)
        self.espec_h_steps = QSpinBox()
        self.espec_h_steps.setRange(1, 500); self.espec_h_steps.setValue(3)
        self.espec_h_steps.valueChanged.connect(self._update_totals)
        g.addWidget(self.espec_h_steps, 3, 4)
        self.espec_ramp_h = QDoubleSpinBox()
        self.espec_ramp_h.setRange(0, 10); self.espec_ramp_h.setValue(0)
        self.espec_ramp_h.setDecimals(1); self.espec_ramp_h.setSuffix(" %/min")
        self.espec_ramp_h.setToolTip("0 = maximum ramp rate")
        g.addWidget(self.espec_ramp_h, 3, 5)

        self.espec_wait_stable = QCheckBox("Wait for stabilization at each step")
        self.espec_wait_stable.setChecked(True)
        g.addWidget(self.espec_wait_stable, 4, 0, 1, 2)

        g.addWidget(QLabel("Tol T:"), 4, 2)
        self.espec_tol_t = QDoubleSpinBox()
        self.espec_tol_t.setRange(0.1, 10); self.espec_tol_t.setValue(0.5)
        self.espec_tol_t.setDecimals(1); self.espec_tol_t.setSuffix(" \u00b0C")
        g.addWidget(self.espec_tol_t, 4, 3)

        g.addWidget(QLabel("Tol H:"), 4, 4)
        self.espec_tol_h = QDoubleSpinBox()
        self.espec_tol_h.setRange(0.5, 20); self.espec_tol_h.setValue(2.0)
        self.espec_tol_h.setDecimals(1); self.espec_tol_h.setSuffix(" %")
        g.addWidget(self.espec_tol_h, 4, 5)

        g.addWidget(QLabel("Timeout:"), 4, 6)
        self.espec_timeout_s = QSpinBox()
        self.espec_timeout_s.setRange(30, 7200); self.espec_timeout_s.setValue(600)
        self.espec_timeout_s.setSuffix(" s")
        g.addWidget(self.espec_timeout_s, 4, 7)

        return grp

    # ── NGI Cell Simulator source ────────────────────────────────────────

    def _build_ngi_group(self):
        grp = QGroupBox("NGI N83624  (Cell Voltage Sweep Source)")
        g   = QGridLayout(grp)

        self.ngi_src_en = QCheckBox("Include NGI in sweep")
        self.ngi_src_en.setChecked(False)
        self.ngi_src_en.toggled.connect(self._update_totals)
        g.addWidget(self.ngi_src_en, 0, 0, 1, 6)

        for col, hdr in enumerate(["", "Start", "", "Stop", "Steps", "Spacing"]):
            lbl = QLabel(hdr); lbl.setStyleSheet("font-weight:bold; color:#89b4fa;")
            g.addWidget(lbl, 1, col)

        # Cell voltage sweep (applied to all 16 cell channels)
        g.addWidget(QLabel("Cell V:"), 2, 0)
        self.ngi_v_start = QDoubleSpinBox()
        self.ngi_v_start.setRange(0, 6.0); self.ngi_v_start.setValue(2.5)
        self.ngi_v_start.setSuffix(" V"); self.ngi_v_start.setDecimals(4)
        g.addWidget(self.ngi_v_start, 2, 1)
        g.addWidget(QLabel("\u2192"), 2, 2)
        self.ngi_v_stop = QDoubleSpinBox()
        self.ngi_v_stop.setRange(0, 6.0); self.ngi_v_stop.setValue(4.2)
        self.ngi_v_stop.setSuffix(" V"); self.ngi_v_stop.setDecimals(4)
        g.addWidget(self.ngi_v_stop, 2, 3)
        self.ngi_v_steps = QSpinBox()
        self.ngi_v_steps.setRange(1, 10000); self.ngi_v_steps.setValue(10)
        self.ngi_v_steps.valueChanged.connect(self._update_totals)
        g.addWidget(self.ngi_v_steps, 2, 4)
        self.ngi_v_spacing = QComboBox()
        self.ngi_v_spacing.addItems(["Linear", "Decade"])
        g.addWidget(self.ngi_v_spacing, 2, 5)

        # Current limit
        g.addWidget(QLabel("I limit:"), 3, 0)
        self.ngi_i_limit = QDoubleSpinBox()
        self.ngi_i_limit.setRange(0, 5000.0); self.ngi_i_limit.setValue(3000.0)
        self.ngi_i_limit.setSuffix(" mA"); self.ngi_i_limit.setDecimals(1)
        g.addWidget(self.ngi_i_limit, 3, 1, 1, 2)

        # Busbar voltage (held constant during sweep)
        g.addWidget(QLabel("Busbar V:"), 3, 3)
        self.ngi_bus_v = QDoubleSpinBox()
        self.ngi_bus_v.setRange(0, 6.0); self.ngi_bus_v.setValue(0.0)
        self.ngi_bus_v.setSuffix(" V"); self.ngi_bus_v.setDecimals(4)
        self.ngi_bus_v.setToolTip("Fixed busbar voltage during sweep (CH9)")
        g.addWidget(self.ngi_bus_v, 3, 4, 1, 2)

        # Measurement checkboxes
        self.ngi_meas_v = QCheckBox("Measure cell V (avg)")
        self.ngi_meas_v.setChecked(True)
        self.ngi_meas_i = QCheckBox("Measure cell I")
        self.ngi_meas_i.setChecked(True)
        self.ngi_meas_bus = QCheckBox("Measure busbar V/I")
        self.ngi_meas_bus.setChecked(True)
        g.addWidget(self.ngi_meas_v, 4, 0, 1, 2)
        g.addWidget(self.ngi_meas_i, 4, 2, 1, 2)
        g.addWidget(self.ngi_meas_bus, 4, 4, 1, 2)

        return grp

    # ── Measurement inputs ───────────────────────────────────────────────

    def _build_meas_group(self):
        grp = QGroupBox("Measurement Inputs  (averaged over N readings, saved to Excel)")
        g   = QGridLayout(grp)
        row = 0

        # GDM-8261A
        self.gdm_en   = QCheckBox("GDM-8261A")
        self.gdm_en.setChecked(True)
        self.gdm_func = QComboBox()
        self.gdm_func.addItems(["VOLT:DC","VOLT:AC","CURR:DC","CURR:AC","RES","FREQ"])
        g.addWidget(self.gdm_en,   row, 0)
        g.addWidget(self.gdm_func, row, 1)
        row += 1

        # Keysight 34461A
        self.dmm_en   = QCheckBox("Keysight 34461A")
        self.dmm_en.setChecked(True)
        self.dmm_func = QComboBox()
        self.dmm_func.addItems(["VOLT:DC","VOLT:AC","CURR:DC","CURR:AC","RES","FREQ"])
        g.addWidget(self.dmm_en,   row, 0)
        g.addWidget(self.dmm_func, row, 1)
        row += 1

        # ITECH readback
        g.addWidget(QLabel("ITECH readback:"), row, 0)
        self.itech_meas_v = QCheckBox("V_meas"); self.itech_meas_v.setChecked(True)
        self.itech_meas_i = QCheckBox("I_meas"); self.itech_meas_i.setChecked(True)
        g.addWidget(self.itech_meas_v, row, 1)
        g.addWidget(self.itech_meas_i, row, 2)
        row += 1

        # GPP readback — V
        g.addWidget(QLabel("GPP meas V:"), row, 0)
        self._gpp_meas_v = {}
        for ch in range(1, 5):
            cb = QCheckBox(f"CH{ch}"); cb.setChecked(ch == 1)
            self._gpp_meas_v[ch] = cb
            g.addWidget(cb, row, ch)
        row += 1

        # GPP readback — I
        g.addWidget(QLabel("GPP meas I:"), row, 0)
        self._gpp_meas_i = {}
        for ch in range(1, 5):
            cb = QCheckBox(f"CH{ch}"); cb.setChecked(False)
            self._gpp_meas_i[ch] = cb
            g.addWidget(cb, row, ch)
        row += 1

        # DAQ 973A
        self.daq_en = QCheckBox("DAQ 973A  (all enabled channels)")
        self.daq_en.setToolTip(
            "Scan all channels configured in the DAQ 973A instrument panel.\n"
            "One Excel column per enabled channel.")
        g.addWidget(self.daq_en, row, 0, 1, 4)
        row += 1

        # ESPEC actual T/H
        g.addWidget(QLabel("ESPEC actual:"), row, 0)
        self.espec_meas_t = QCheckBox("T_actual (\u00b0C)"); self.espec_meas_t.setChecked(False)
        self.espec_meas_h = QCheckBox("H_actual (%)");  self.espec_meas_h.setChecked(False)
        g.addWidget(self.espec_meas_t, row, 1)
        g.addWidget(self.espec_meas_h, row, 2)
        row += 1

        # Yokogawa WT5000
        self._wt_container = QFrame()
        wt_lay = QHBoxLayout(self._wt_container)
        wt_lay.setContentsMargins(0, 0, 0, 0); wt_lay.setSpacing(4)
        self.wt_en = QCheckBox("WT5000  E:")
        wt_lay.addWidget(self.wt_en)
        self.wt_elem = QSpinBox(); self.wt_elem.setRange(1, 7); self.wt_elem.setValue(1)
        self.wt_elem.setFixedWidth(132)
        wt_lay.addWidget(self.wt_elem)
        self._wt_checks = {}
        for _func, _lbl in [("URMS","Vrms"),("IRMS","Arms"),("UDC","Udc"),("IDC","Idc"),
                             ("P","W"),("LAMBda","PF"),("S","VA"),("Q","VAR")]:
            _cb = QCheckBox(_lbl)
            _cb.setChecked(_func in ("URMS", "IRMS", "P", "LAMBda"))
            self._wt_checks[_func] = _cb
            wt_lay.addWidget(_cb)
        wt_lay.addStretch()
        g.addWidget(self._wt_container, row, 0, 1, 5)
        row += 1

        # Tektronix MDO34
        self._mdo_container = QFrame()
        mdo_lay = QHBoxLayout(self._mdo_container)
        mdo_lay.setContentsMargins(0, 0, 0, 0); mdo_lay.setSpacing(4)
        self.mdo_en = QCheckBox("MDO34")
        mdo_lay.addWidget(self.mdo_en)
        mdo_lay.addWidget(QLabel("Ch:"))
        self.mdo_ch = QComboBox()
        self.mdo_ch.addItems(MDO34Driver.CHANNELS)
        self.mdo_ch.setFixedWidth(64)
        mdo_lay.addWidget(self.mdo_ch)
        mdo_lay.addWidget(QLabel("Meas:"))
        self.mdo_meas = QComboBox()
        for lbl, val in MDO34Driver.MEAS_TYPES:
            self.mdo_meas.addItem(lbl, val)
        mdo_lay.addWidget(self.mdo_meas)
        mdo_lay.addStretch()
        g.addWidget(self._mdo_container, row, 0, 1, 5)

        return grp

    # ── File output ───────────────────────────────────────────────────────────

    def _build_file_group(self):
        grp = QGroupBox("Output File  (.xlsx)")
        lay = QHBoxLayout(grp)

        lay.addWidget(QLabel("Save as:"))
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText(
            "Leave blank → auto-generate in results/ folder")
        lay.addWidget(self.file_edit, stretch=1)

        btn = _btn("Browse…")
        btn.clicked.connect(self._browse_file)
        lay.addWidget(btn)
        return grp

    def _browse_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save sweep results", "",
            "Excel Workbook (*.xlsx)")
        if path:
            if not path.endswith(".xlsx"):
                path += ".xlsx"
            self.file_edit.setText(path)

    # ── Run controls + log ────────────────────────────────────────────────────

    def _build_run_group(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # Title
        title = QLabel("Run Controls")
        title.setStyleSheet("font-weight:bold; font-size:11pt; color:#89b4fa;")
        lay.addWidget(title)

        # Buttons — 2x2 grid for compact layout
        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)
        self.btn_start = _btn("Start Sweep", "green")
        self.btn_stop  = _btn("Stop",        "red")
        self.btn_open  = _btn("Open Results")
        self.btn_to_seq = _btn("Export to Seq", "orange")
        self.btn_to_seq.setToolTip(
            "Generate steps from current sweep config and send them\n"
            "to the Sequence tab for manual editing / run.")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_open.clicked.connect(self._open_results)
        self.btn_to_seq.clicked.connect(self._export_to_sequence)
        btn_grid.addWidget(self.btn_start,  0, 0)
        btn_grid.addWidget(self.btn_stop,   0, 1)
        btn_grid.addWidget(self.btn_open,   1, 0)
        btn_grid.addWidget(self.btn_to_seq, 1, 1)
        lay.addLayout(btn_grid)

        self.progress   = QProgressBar()
        self.progress.setRange(0, 100); self.progress.setValue(0)
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setStyleSheet("color:#a6adc8; font-style:italic;")

        lay.addWidget(self.progress)
        lay.addWidget(self.status_lbl)

        log_title = QLabel("Sweep Log")
        log_title.setStyleSheet("font-weight:bold; color:#89b4fa; margin-top:4px;")
        lay.addWidget(log_title)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Courier New", 9))

        lay.addWidget(self.log_view, stretch=1)
        return w

    # ── Config builder ────────────────────────────────────────────────────────

    def _build_config(self) -> dict:
        meas_inputs = []
        if self.gdm_en.isChecked():
            f = self.gdm_func.currentText()
            meas_inputs.append((f"GDM {f}", lambda ff=f: self._meas_gdm(ff)))
        if self.dmm_en.isChecked():
            f = self.dmm_func.currentText()
            meas_inputs.append((f"DMM {f}", lambda ff=f: self._meas_dmm(ff)))
        if self.itech_meas_v.isChecked():
            meas_inputs.append(("ITECH V_meas (V)", self._meas_itech_v))
        if self.itech_meas_i.isChecked():
            meas_inputs.append(("ITECH I_meas (A)", self._meas_itech_i))
        for ch in range(1, 5):
            if self._gpp_meas_v[ch].isChecked():
                meas_inputs.append((f"GPP CH{ch} V_meas (V)",
                                    lambda c=ch: self._meas_gpp_v(c)))
            if self._gpp_meas_i[ch].isChecked():
                meas_inputs.append((f"GPP CH{ch} I_meas (A)",
                                    lambda c=ch: self._meas_gpp_i(c)))
        if self.wt_en.isChecked():
            _elem  = self.wt_elem.value()
            _funcs = [f for f, cb in self._wt_checks.items() if cb.isChecked()]
            if _funcs:
                meas_inputs.extend(self._build_wt5000_meas(_elem, _funcs))
        if self.mdo_en.isChecked():
            _ch   = self.mdo_ch.currentText()
            _mtyp = self.mdo_meas.currentData()
            _lbl  = self.mdo_meas.currentText()
            meas_inputs.append(
                (f"MDO34 {_ch} {_lbl}", lambda c=_ch, m=_mtyp: self._meas_mdo(c, m)))
            cfg['mdo_waveform'] = {'enabled': True, 'ch': _ch, 'pts': 10000}

        # NGI measurement inputs
        if self.ngi_meas_v.isChecked() and self.ngi_src_en.isChecked():
            meas_inputs.append(("NGI Cell V_avg (V)", self._meas_ngi_cell_v_avg))
        if self.ngi_meas_i.isChecked() and self.ngi_src_en.isChecked():
            meas_inputs.append(("NGI Cell I_avg (mA)", self._meas_ngi_cell_i_avg))
        if self.ngi_meas_bus.isChecked() and self.ngi_src_en.isChecked():
            meas_inputs.append(("NGI Bus V (V)", self._meas_ngi_bus_v))
            meas_inputs.append(("NGI Bus I (mA)", self._meas_ngi_bus_i))

        tracking = {0: "independent", 1: "series", 2: "parallel"}[
            self.gpp_tracking.currentIndex()]

        gpp_channels = {}
        for ch in range(1, 5):
            en = self._gpp_en[ch].isChecked()
            if ch == 2 and tracking != "independent":
                en = False
            mode_str = "source"
            if ch in (1, 2):
                mode_str = ["source", "cv", "cc", "cr"][self._gpp_mode[ch].currentIndex()]
            gpp_channels[ch] = {
                "enable":  en,
                "mode":    mode_str,
                "v_start": self._gpp_vstart[ch].value(),
                "v_stop":  self._gpp_vstop[ch].value(),
                "i_start": self._gpp_istart[ch].value(),
                "i_stop":  self._gpp_istop[ch].value(),
                "v_steps":   self._gpp_vsteps[ch].value(),
                "v_spacing": self._gpp_vspacing[ch].currentText().lower(),
                "i_steps":   self._gpp_isteps[ch].value(),
                "i_spacing": self._gpp_ispacing[ch].currentText().lower(),
            }

        return {
            "n_readings":    self.n_readings.value(),
            "interval_ms":   self.interval_ms.value(),
            "settling_ms":   self.settling_ms.value(),
            "avg_readings":  self.avg_readings.isChecked(),
            "itech_enable":  self.itech_en.isChecked(),
            "itech_v_start": self.itech_v_start.value(),
            "itech_v_stop":  self.itech_v_stop.value(),
            "itech_i_start": self.itech_i_start.value(),
            "itech_i_stop":  self.itech_i_stop.value(),
            "itech_v_steps":   self.itech_v_steps.value(),
            "itech_v_spacing": self.itech_v_spacing.currentText().lower(),
            "itech_i_steps":    self.itech_i_steps.value(),
            "itech_i_spacing":  self.itech_i_spacing.currentText().lower(),
            "itech_i_linthresh": self.itech_i_linthresh.value(),
            "gpp_tracking":  tracking,
            "gpp_channels":  gpp_channels,
            "meas_inputs":   meas_inputs,
            "file_path":     self.file_edit.text().strip(),
            "daq_en":         self.daq_en.isChecked(),
            "espec_enable":   self.espec_src_en.isChecked(),
            "espec_t_start":  self.espec_t_start.value(),
            "espec_t_stop":   self.espec_t_stop.value(),
            "espec_t_steps":  self.espec_t_steps.value(),
            "espec_h_start":  self.espec_h_start.value(),
            "espec_h_stop":   self.espec_h_stop.value(),
            "espec_h_steps":  self.espec_h_steps.value(),
            "espec_ramp_t":   self.espec_ramp_t.value(),
            "espec_ramp_h":   self.espec_ramp_h.value(),
            "espec_wait_stable":  self.espec_wait_stable.isChecked(),
            "espec_tol_t":    self.espec_tol_t.value(),
            "espec_tol_h":    self.espec_tol_h.value(),
            "espec_timeout_s":self.espec_timeout_s.value(),
            "espec_stable_poll_s": 5,
            "espec_meas_t":   self.espec_meas_t.isChecked(),
            "espec_meas_h":   self.espec_meas_h.isChecked(),
            "ngi_enable":     self.ngi_src_en.isChecked(),
            "ngi_v_start":    self.ngi_v_start.value(),
            "ngi_v_stop":     self.ngi_v_stop.value(),
            "ngi_v_steps":    self.ngi_v_steps.value(),
            "ngi_v_spacing":  self.ngi_v_spacing.currentText().lower(),
            "ngi_i_limit":    self.ngi_i_limit.value(),
            "ngi_bus_v":      self.ngi_bus_v.value(),
        }

    # ── Measurement callables (run inside SweepRunner thread) ────────────────

    def _meas_gdm(self, func: str) -> float:
        needs_conf = getattr(self, '_gdm_last_func', None) != func
        self._gdm_last_func = func
        def fn(res, _f=func, _conf=needs_conf):
            if _conf:
                res.write(f"CONF:{_f}")
                time.sleep(0.1)
            return float(res.query("READ?").strip().split(",")[0])
        return shared_resource('gdm').run(fn)

    def _meas_dmm(self, func: str) -> float:
        with use_instrument("dmm") as res:
            return float(res.query(f"MEAS:{func}? AUTO,DEF").strip())

    def _meas_itech_v(self) -> float:
        conn = shared_driver('itech', lambda: ITechFixed(ADDR['itech']))
        result = conn.run(lambda drv: float(drv.ask('MEAS:VOLT?').strip()))
        return result if result is not None else float('nan')

    def _meas_itech_i(self) -> float:
        conn = shared_driver('itech', lambda: ITechFixed(ADDR['itech']))
        result = conn.run(lambda drv: float(drv.ask('MEAS:CURR?').strip()))
        return result if result is not None else float('nan')

    def _meas_gpp_v(self, ch: int) -> float:
        conn = shared_resource('gpp')
        result = conn.run(lambda res, c=ch: float(res.query(f"MEAS{c}:VOLT?").strip()))
        return result if result is not None else float('nan')

    def _meas_gpp_i(self, ch: int) -> float:
        conn = shared_resource('gpp')
        result = conn.run(lambda res, c=ch: float(res.query(f"MEAS{c}:CURR?").strip()))
        return result if result is not None else float('nan')

    def _meas_mdo(self, ch: str, meas_type: str) -> float:
        conn = shared_driver('mdo', lambda: MDO34Driver(ADDR['mdo']))
        result = conn.run(lambda drv: drv.measure_immediate(ch, meas_type))
        return result if result is not None else float('nan')

    # ── NGI measurements ───────────────────────────────────────────────────

    _NGI_CELL_CHS = [c for c in range(1, 18) if c != 9]  # 16 cell channels

    def _meas_ngi_cell_v_avg(self) -> float:
        """Average measured voltage across all 16 cell channels."""
        conn = shared_resource('ngi')
        def fn(res):
            vals = []
            for ch in self._NGI_CELL_CHS:
                v = float(res.query(f"MEAS{ch}:VOLT?").strip())
                vals.append(v)
            return sum(vals) / len(vals) if vals else float('nan')
        return conn.run(fn)

    def _meas_ngi_cell_i_avg(self) -> float:
        """Average measured current across all 16 cell channels (mA)."""
        conn = shared_resource('ngi')
        def fn(res):
            vals = []
            for ch in self._NGI_CELL_CHS:
                i = float(res.query(f"MEAS{ch}:CURR?").strip())
                vals.append(i)
            return sum(vals) / len(vals) if vals else float('nan')
        return conn.run(fn)

    def _meas_ngi_bus_v(self) -> float:
        conn = shared_resource('ngi')
        return conn.run(lambda res: float(res.query("MEAS9:VOLT?").strip()))

    def _meas_ngi_bus_i(self) -> float:
        conn = shared_resource('ngi')
        return conn.run(lambda res: float(res.query("MEAS9:CURR?").strip()))

    def _build_wt5000_meas(self, elem: int, funcs: list) -> list:
        """Returns (label, fn) pairs for selected WT5000 functions.
        All share one connection per reading cycle via a shared state dict."""
        label_map = dict(WT5000Driver.ELEM_ITEMS)
        _state = {'data': {}}

        def _fetch():
            conn = shared_driver('wt5000', lambda: WT5000Driver(ADDR['wt5000']))
            _state['data'] = conn.run(lambda drv: drv.measure_element(elem, funcs))

        result = []
        for i, func in enumerate(funcs):
            full_lbl  = f"WT5000 E{elem} {label_map.get(func, func)}"
            cache_lbl = label_map.get(func, func)
            if i == 0:
                def _make_first(cl):
                    def fn():
                        _fetch()
                        return _state['data'].get(cl, float('nan'))
                    return fn
                result.append((full_lbl, _make_first(cache_lbl)))
            else:
                def _make_rest(cl):
                    return lambda: _state['data'].get(cl, float('nan'))
                result.append((full_lbl, _make_rest(cache_lbl)))
        return result

    def notify_instrument_connected(self, key: str):
        if key == 'ngi':
            self._ngi_container.setVisible(True)

    def notify_instrument_disconnected(self, key: str):
        if key == 'wt5000':
            self.wt_en.setChecked(False)
        elif key == 'mdo':
            self.mdo_en.setChecked(False)
        elif key == 'ngi':
            self._ngi_container.setVisible(False)
            self.ngi_src_en.setChecked(False)

    # ── Sweep control ─────────────────────────────────────────────────────────

    def _start(self):
        try:
            cfg = self._build_config()
        except Exception as e:
            QMessageBox.critical(self, "Config Error", str(e)); return

        if not cfg["itech_enable"] and not any(
                v["enable"] for v in cfg["gpp_channels"].values()):
            QMessageBox.warning(self, "No Sources",
                                "Enable at least one source."); return

        # ── Pre-flight: check required instruments are connected ──────────
        required = []
        if cfg["itech_enable"]:
            required.append('itech')
        if any(v["enable"] for v in cfg["gpp_channels"].values()):
            required.append('gpp')
        if cfg.get("espec_enable"):
            required.append('espec')
        if cfg.get("ngi_enable"):
            required.append('ngi')
        for lbl, _ in cfg.get("meas_inputs", []):
            ll = lbl.lower()
            if 'gdm' in ll and 'gdm' not in required:     required.append('gdm')
            if 'dmm' in ll and 'dmm' not in required:     required.append('dmm')
            if 'mdo' in ll and 'mdo' not in required:     required.append('mdo')
            if 'wt5000' in ll and 'wt5000' not in required: required.append('wt5000')
        if cfg.get("daq_en"):
            required.append('daq')

        # Check address configuration first
        no_addr = check_instruments(required)
        if no_addr:
            names = "\n".join(f"  \u2022 {n}  ({k}) \u2014 no address" for k, n in no_addr)
            mb = QMessageBox(self)
            mb.setWindowTitle("Instruments Not Configured")
            mb.setIcon(QMessageBox.Icon.Critical)
            mb.setText(f"The following instruments have no valid address:\n\n{names}\n\n"
                       "Configure them in the Addresses tab first.")
            mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            mb.exec()
            return

        # Check instruments are connected via Instruments tab
        connected = (self._lib_panel.connected_keys()
                     if self._lib_panel is not None else set())
        not_connected = [(k, _INSTR_NAMES.get(k, k))
                         for k in required if k not in connected]
        if not_connected:
            names = "\n".join(f"  \u2022 {n}  ({k})" for k, n in not_connected)
            mb = QMessageBox(self)
            mb.setWindowTitle("Instruments Not Connected")
            mb.setIcon(QMessageBox.Icon.Warning)
            mb.setText(f"The following instruments are not connected via the Instruments tab:\n\n"
                       f"{names}\n\n"
                       "Connect them first for reliable operation, or proceed anyway\n"
                       "(the test will attempt to open connections on the fly).")
            btn_proceed = mb.addButton("Proceed Anyway", QMessageBox.ButtonRole.AcceptRole)
            mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            mb.exec()
            if mb.clickedButton() != btn_proceed:
                return

        if self._eddie_panel is not None and getattr(self._eddie_panel, '_pcu', None) is None:
            mb = QMessageBox(self)
            mb.setWindowTitle("EDDIE Not Connected")
            mb.setText("The EDDIE device is not connected.\n"
                       "EDDIE data will not be logged in this sweep.")
            mb.setIcon(QMessageBox.Icon.Warning)
            mb.addButton("Proceed Anyway", QMessageBox.ButtonRole.AcceptRole)
            mb.addButton("Cancel",         QMessageBox.ButtonRole.RejectRole)
            mb.exec()
            if mb.clickedButton().text() == "Cancel":
                return

        # Validate: lower step count must be a factor of the higher for each supply
        if cfg["itech_enable"]:
            hi = max(cfg["itech_v_steps"], cfg["itech_i_steps"])
            lo = min(cfg["itech_v_steps"], cfg["itech_i_steps"])
            if hi % lo != 0:
                QMessageBox.critical(self, "Config Error",
                    f"ITECH: the lower step count ({lo}) must be a factor of "
                    f"the higher ({hi}).\nPick a count so that {hi} ÷ {lo} is a whole number.")
                return
        for ch, cd in cfg["gpp_channels"].items():
            if cd["enable"]:
                hi = max(cd["v_steps"], cd["i_steps"])
                lo = min(cd["v_steps"], cd["i_steps"])
                if hi % lo != 0:
                    QMessageBox.critical(self, "Config Error",
                        f"GPP CH{ch}: the lower step count ({lo}) must be a factor of "
                        f"the higher ({hi}).\nPick a count so that {hi} ÷ {lo} is a whole number.")
                    return

        sc = ([max(cfg["itech_v_steps"], cfg["itech_i_steps"])] if cfg["itech_enable"] else []) + \
             [max(cd["v_steps"], cd["i_steps"]) for cd in cfg["gpp_channels"].values() if cd["enable"]]
        total = max(sc) if sc else 1

        self.log_view.clear()
        self.progress.setRange(0, total); self.progress.setValue(0)
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("Sweep running…")
        self.status_lbl.setStyleSheet("color:#fab387;")

        _log.info("Sweep starting: total=%d steps, itech=%s, gpp_ch=%s",
                  total, cfg["itech_enable"],
                  [ch for ch, cd in cfg["gpp_channels"].items() if cd["enable"]])
        cfg['eddie_panel'] = self._eddie_panel
        cfg['daq_panel']   = self._daq_panel
        cfg['espec_panel'] = self._espec_panel
        cfg['gpp_panel']   = self._gpp_panel
        self._runner = SweepRunner(cfg)
        self._runner.log_msg.connect(self._append_log)
        self._runner.progress.connect(self._on_progress)
        self._runner.finished.connect(self._on_finished)
        self._runner.error_sig.connect(self._on_error)
        self._runner.start()
        self.test_running.emit(True)

    def _stop(self):
        if self._runner:
            self._runner.stop()
        self.btn_stop.setEnabled(False)
        self.status_lbl.setText("Stopping after current reading…")
        self.status_lbl.setStyleSheet("color:#f9e2af;")

    def _on_progress(self, current, total):
        self.progress.setValue(current)
        pct = int(100 * current / total) if total else 0
        self.status_lbl.setText(f"Step {current}/{total}  ({pct}%)")

    def _on_finished(self, path):
        _log.info("Sweep finished, output: %s", path)
        self.test_running.emit(False)
        self._last_file = path
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(self.progress.maximum())
        self.status_lbl.setText(f"\u2713  Complete  \u2192  {path}")
        self.status_lbl.setStyleSheet("color:#a6e3a1;")

    def _on_error(self, msg):
        _log.error("Sweep error: %s", msg)
        self.test_running.emit(False)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_lbl.setText(f"\u274c  {msg}")
        self.status_lbl.setStyleSheet("color:#f38ba8;")
        QMessageBox.critical(self, "Sweep Error", msg)

    def _append_log(self, msg):
        self.log_view.append(msg)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _export_to_sequence(self):
        """Build the cross-product step list from current sweep config
        and emit it via steps_exported for the Sequence tab to consume."""
        try:
            cfg = self._build_config()
        except Exception as e:
            QMessageBox.critical(self, "Config Error", str(e))
            return

        itech_en  = cfg["itech_enable"]
        itech_v_arr = itech_i_arr = []
        if itech_en:
            itech_v_arr = _make_steps(
                cfg["itech_v_start"], cfg["itech_v_stop"],
                cfg["itech_v_steps"], cfg["itech_v_spacing"])
            itech_i_arr = _make_steps(
                cfg["itech_i_start"], cfg["itech_i_stop"],
                cfg["itech_i_steps"], cfg["itech_i_spacing"],
                cfg.get("itech_i_linthresh"))

        gpp_v_arr = {}; gpp_i_arr = {}
        for ch, cd in cfg["gpp_channels"].items():
            if cd["enable"]:
                gpp_v_arr[ch] = _make_steps(
                    cd["v_start"], cd["v_stop"], cd["v_steps"], cd["v_spacing"])
                gpp_i_arr[ch] = _make_steps(
                    cd["i_start"], cd["i_stop"], cd["i_steps"], cd["i_spacing"])

        itech_total = max(len(itech_v_arr), len(itech_i_arr)) if itech_en else 1
        gpp_total   = {ch: max(len(gpp_v_arr[ch]), len(gpp_i_arr[ch]))
                       for ch in gpp_v_arr}
        all_lens    = ([itech_total] if itech_en else []) + list(gpp_total.values())
        total_steps = max(all_lens) if all_lens else 1

        dwell_ms    = cfg["n_readings"] * cfg["interval_ms"]
        interval_ms = cfg["interval_ms"]
        settling_ms = cfg["settling_ms"]
        tracking_str = cfg.get("gpp_tracking", "independent").capitalize()
        steps    = []
        for step in range(total_steps):
            row = {
                'itech_en':    itech_en,
                'itech_v':     round(SweepRunner._at(itech_v_arr, step, itech_total), 3)
                               if itech_en else 0.0,
                'itech_i':     round(SweepRunner._at(itech_i_arr, step, itech_total), 3)
                               if itech_en else 0.0,
                'gpp_tracking': tracking_str,
                'dwell_ms':    dwell_ms,
                'interval_ms': interval_ms,
                'settling_ms': settling_ms,
                'notes':       f"Step {step + 1}",
            }
            for ch in range(1, 5):
                g_en = ch in gpp_v_arr
                row[f'g{ch}_en'] = g_en
                if ch in (1, 2):
                    mode_str = cfg["gpp_channels"].get(ch, {}).get("mode", "source")
                    mode_label = {"source": "Source", "cv": "Load CV",
                                  "cc": "Load CC", "cr": "Load CR"}.get(mode_str, "Source")
                    row[f'g{ch}_mode'] = mode_label
                row[f'g{ch}_v']  = round(SweepRunner._at(
                    gpp_v_arr[ch], step, gpp_total[ch]), 3) if g_en else 0.0
                row[f'g{ch}_i']  = round(SweepRunner._at(
                    gpp_i_arr[ch], step, gpp_total[ch]), 3) if g_en else 0.0
            steps.append(row)

        self.steps_exported.emit(steps)

    def _open_results(self):
        path = getattr(self, "_last_file", "")
        if path and Path(path).is_file():
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            results = APP_DIR / "results"
            results.mkdir(exist_ok=True)
            subprocess.Popen(f'explorer "{results}"')
