# -*- coding: utf-8 -*-
"""
EDDIE / ECDC live register panel — reads from a serial-connected Enphase device
via the enphase_ecdc Python library (bundled with ecdc-gui).

Usage inside the test app: EDDIE must NOT be running (serial port is exclusive).
"""

import json
import time
import threading
from collections import deque
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import NamedTuple, Dict, List

from app_logger import APP_DIR

try:
    import matplotlib
    matplotlib.use('QtAgg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    import matplotlib.dates as mdates
    _MPL = True
except ImportError:
    _MPL = False

_PLOT_COLORS = ['#89b4fa', '#a6e3a1', '#fab387', '#f38ba8',
                '#cba6f7', '#89dceb', '#f9e2af', '#94e2d5']

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QLineEdit, QHeaderView, QAbstractItemView,
    QFileDialog, QTabWidget, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QColor


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

class RegEntry(NamedTuple):
    """One browser row — scalar (index=-1) or a specific array element (index≥0)."""
    path:      str
    array_len: int
    index:     int   # -1 = scalar, ≥0 = array element


class WatchEntry(NamedTuple):
    display: str   # shown in table and used as Excel column header
    path:    str
    index:   int   # -1 = scalar, ≥0 = array element


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve(live_regs, path: str):
    """Walk a dot-separated path through the LiveRegEditor tree."""
    return reduce(getattr, path.split('.'), live_regs)


def _read_entry(live_regs, path: str, index: int):
    """Read a register value; handle array indexing."""
    val = _resolve(live_regs, path).get_value()
    if index >= 0 and hasattr(val, '__getitem__'):
        return val[index]
    return val


def _entry_label(path: str, index: int) -> str:
    """Display label: last path component, with [N] suffix for arrays."""
    short = path.split('.')[-1]
    return f"{short}[{index}]" if index >= 0 else short


def _full_label(path: str, index: int) -> str:
    """Full path label used in watchlist and Excel headers."""
    return f"{path}[{index}]" if index >= 0 else path


# ─────────────────────────────────────────────────────────────────────────────
# Background workers
# ─────────────────────────────────────────────────────────────────────────────

class EddieConnectWorker(QThread):
    """Opens the serial port and loads datamodels (from local path or device)."""
    done = pyqtSignal(object, str)   # (pcu_object | None, error_string)

    def __init__(self, com_port: str, dbx_path: str = ""):
        super().__init__()
        self._com      = com_port
        self._dbx_path = dbx_path.strip()

    def run(self):
        pcu = None
        try:
            from enphase_ecdc import PCU
            import os
            search_paths = [self._dbx_path] if self._dbx_path and os.path.isdir(self._dbx_path) else None
            pcu = PCU(self._com, dbx_search_paths=search_paths)
            _ = pcu.live_regs   # serial port opened in ECDCSerial.__init__; don't call pcu.open()
            self.done.emit(pcu, "")
        except Exception as e:
            try:
                if pcu is not None:
                    pcu.ecdc.close()
            except Exception:
                pass
            self.done.emit(None, str(e))


class EddieEnumWorker(QThread):
    """Walks the DataModelItem tree and returns all leaf registers grouped by namespace."""
    done = pyqtSignal(dict, str)   # {ns_name: [RegEntry]}, error

    def __init__(self, pcu, lock):
        super().__init__()
        self._pcu  = pcu
        self._lock = lock

    @staticmethod
    def _walk(node, path: str, entries: list, depth: int = 0):
        if depth > 8:
            return
        try:
            children = list(node.get_all_children(recursive=False))
        except Exception:
            return
        if not children:
            try:
                al = max(1, node._dm.get_array_length() or 1)
            except Exception:
                al = 1
            entries.append(RegEntry(path, al, -1))
            return
        for child in children:
            try:
                child_name = child._dm.get_location().split('.')[-1]
                EddieEnumWorker._walk(child, f"{path}.{child_name}", entries, depth + 1)
            except Exception:
                pass

    def run(self):
        try:
            result: Dict[str, List[RegEntry]] = {}
            with self._lock:
                ns_nodes = list(self._pcu.live_regs.get_all_children(recursive=False))
            for ns_node in ns_nodes:
                try:
                    ns_name = ns_node._dm.get_location().split('.')[-1]
                    raw: List[RegEntry] = []
                    self._walk(ns_node, ns_name, raw)
                    expanded: List[RegEntry] = []
                    for e in raw:
                        if e.array_len <= 1:
                            expanded.append(e)
                        else:
                            for i in range(min(e.array_len, 64)):
                                expanded.append(RegEntry(e.path, e.array_len, i))
                    result[ns_name] = expanded
                except Exception:
                    pass
            self.done.emit(result, "")
        except Exception as ex:
            self.done.emit({}, str(ex))


class EddieBrowserUpdateWorker(QThread):
    """Reads values for a list of RegEntry objects (visible browser rows)."""
    result = pyqtSignal(list, str)   # [str_value, ...], error

    def __init__(self, pcu, entries: List[RegEntry], lock):
        super().__init__()
        self._pcu     = pcu
        self._entries = list(entries)
        self._lock    = lock

    def run(self):
        values = []
        try:
            with self._lock:
                for e in self._entries:
                    try:
                        val = _read_entry(self._pcu.live_regs, e.path, e.index)
                        values.append(str(val))
                    except Exception as ex:
                        values.append(f"Err: {ex}")
            self.result.emit(values, "")
        except Exception as ex:
            self.result.emit([], str(ex))


class EddieWatchlistPollWorker(QThread):
    """Reads all watchlist entries once and returns {display: value}."""
    result = pyqtSignal(dict, str)

    def __init__(self, pcu, entries: List[WatchEntry], lock):
        super().__init__()
        self._pcu     = pcu
        self._entries = list(entries)
        self._lock    = lock

    def run(self):
        out: Dict[str, object] = {}
        try:
            with self._lock:
                for e in self._entries:
                    try:
                        val = _read_entry(self._pcu.live_regs, e.path, e.index)
                        out[e.display] = val
                    except Exception as ex:
                        out[e.display] = f"Err: {ex}"
            self.result.emit(out, "")
        except Exception as ex:
            self.result.emit({}, str(ex))


class EddieWriteWorker(QThread):
    """Writes a new value to a single register (scalar or array element)."""
    done = pyqtSignal(str, str)   # full_label, error_string

    def __init__(self, pcu, path: str, index: int, raw: str, lock):
        super().__init__()
        self._pcu   = pcu
        self._path  = path
        self._index = index
        self._raw   = raw
        self._lock  = lock

    @staticmethod
    def _parse(s: str):
        """Try int → float → str."""
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s

    def run(self):
        label = _full_label(self._path, self._index)
        try:
            parsed = self._parse(self._raw)
            with self._lock:
                node = _resolve(self._pcu.live_regs, self._path)
                if self._index >= 0:
                    arr = list(node.get_value())
                    arr[self._index] = parsed
                    node.write_value(arr)
                else:
                    node.write_value(parsed)
            self.done.emit(label, "")
        except Exception as ex:
            self.done.emit(label, str(ex))


# ─────────────────────────────────────────────────────────────────────────────
# Panel widget
# ─────────────────────────────────────────────────────────────────────────────

class EddiePanel(QWidget):
    """
    EDDIE device panel — register browser + live watchlist.

    Public API for sweep integration:
        get_watchlist_snapshot() → dict[str, any]
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pcu              = None
        self._pcu_lock         = threading.Lock()
        self._conn_worker      = None
        self._enum_worker      = None
        self._browser_worker   = None
        self._watch_worker     = None
        self._browser_entries: Dict[str, List[RegEntry]] = {}
        self._browser_tables:  Dict[str, QTableWidget]   = {}
        self._browser_rows:    Dict[str, List[int]]       = {}   # visible row indices cache
        self._watchlist:       List[WatchEntry]           = []
        self._last_snapshot:   dict                       = {}
        self._snap_lock        = threading.Lock()
        self._bulk_update_active = False
        self._write_workers:   list                       = []
        self._plot_enabled:    Dict[str, bool]            = {}
        self._plot_data:       Dict[str, deque]           = {}
        self._plot_paused:     bool                       = False
        self._watch_col_widths:   dict                    = {}   # col_int → width
        self._browser_col_widths: dict                    = {}   # ns_name → {col_int → width}

        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.addWidget(self._build_conn_grp())

        self._main_tabs = QTabWidget()
        self._main_tabs.setEnabled(False)
        self._main_tabs.addTab(self._build_browser_tab(),   "📋  Register Browser")
        self._main_tabs.addTab(self._build_watchlist_tab(), "⭐  Watchlist")
        self._main_tabs.addTab(self._build_plot_tab(),      "📈  Plot")
        lay.addWidget(self._main_tabs)

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._poll_watchlist)

        self._watchlist_path = APP_DIR / "eddie_watchlist.json"
        self._load_watchlist()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_watchlist_snapshot(self) -> dict:
        """Return the latest polled watchlist values (thread-safe). Used by sweep runner."""
        with self._snap_lock:
            return dict(self._last_snapshot)

    # ── Connection group ──────────────────────────────────────────────────────

    def _build_conn_grp(self):
        grp  = QGroupBox("EDDIE Device — Serial Connection")
        outer = QVBoxLayout(grp)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("COM Port:"))
        self.com_combo = QComboBox()
        self.com_combo.setMinimumWidth(120)
        row1.addWidget(self.com_combo)

        btn_scan = QPushButton("🔍  Scan")
        btn_scan.setFixedWidth(80)
        btn_scan.clicked.connect(self._scan_ports)
        row1.addWidget(btn_scan)

        self.btn_conn = QPushButton("⚡  Connect")
        self.btn_conn.setProperty("role", "green")
        self.btn_conn.setMinimumWidth(120)
        self.btn_conn.clicked.connect(self._toggle_connect)
        row1.addWidget(self.btn_conn)

        self.conn_lbl = QLabel("Not connected")
        self.conn_lbl.setStyleSheet("color:#6c7086; font-style:italic;")
        row1.addWidget(self.conn_lbl)
        row1.addStretch()
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Datamodel folder (.dbx / .znc files):"))
        self.dbx_edit = QLineEdit()
        self.dbx_edit.setText(str(APP_DIR / "datamodel_cache"))
        self.dbx_edit.setPlaceholderText("Leave blank to load from device")
        row2.addWidget(self.dbx_edit)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self._browse_dbx)
        row2.addWidget(btn_browse)
        btn_clr = QPushButton("Clear")
        btn_clr.setFixedWidth(55)
        btn_clr.clicked.connect(lambda: self.dbx_edit.clear())
        row2.addWidget(btn_clr)
        outer.addLayout(row2)

        self._scan_ports()
        return grp

    # ── Plot tab ───────────────────────────────────────────────────────────────

    def _build_plot_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        if not _MPL:
            lbl = QLabel(
                "matplotlib not installed — run:  pip install matplotlib\n"
                "Then restart the application.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color:#f38ba8; font-style:italic;")
            lay.addWidget(lbl)
            return w

        self._fig = Figure(tight_layout=True)
        self._fig.patch.set_facecolor('#1e1e2e')
        self._plot_ax = self._fig.add_subplot(111)
        self._canvas  = FigureCanvasQTAgg(self._fig)
        self._canvas.setMinimumHeight(300)
        lay.addWidget(self._canvas)

        ctrl = QHBoxLayout()
        self._plot_pause_btn = QPushButton("⏹  Pause")
        self._plot_pause_btn.setCheckable(True)
        self._plot_pause_btn.setFixedWidth(90)
        self._plot_pause_btn.toggled.connect(self._toggle_plot_pause)
        ctrl.addWidget(self._plot_pause_btn)

        btn_clr = QPushButton("Clear data")
        btn_clr.setFixedWidth(90)
        btn_clr.clicked.connect(self._clear_plot_data)
        ctrl.addWidget(btn_clr)

        ctrl.addWidget(QLabel("  Max samples:"))
        self._max_samples_spin = QSpinBox()
        self._max_samples_spin.setRange(10, 10000)
        self._max_samples_spin.setValue(300)
        self._max_samples_spin.setSingleStep(50)
        self._max_samples_spin.setSuffix(" pts")
        self._max_samples_spin.setFixedWidth(180)
        self._max_samples_spin.setToolTip("Rolling buffer length per variable (10 – 10 000 points)")
        self._max_samples_spin.valueChanged.connect(self._on_max_samples_changed)
        ctrl.addWidget(self._max_samples_spin)

        ctrl.addStretch()
        self._plot_status = QLabel("Toggle 📈 on watchlist rows to enable plotting")
        self._plot_status.setStyleSheet("color:#6c7086; font-style:italic;")
        ctrl.addWidget(self._plot_status)
        lay.addLayout(ctrl)

        self._style_plot_ax()
        self._canvas.draw()
        return w

    def _style_plot_ax(self):
        if not _MPL or not hasattr(self, '_plot_ax'):
            return
        ax = self._plot_ax
        ax.set_facecolor('#313244')
        ax.tick_params(colors='#cdd6f4', labelsize=8)
        ax.grid(True, alpha=0.2, color='#585b70')
        ax.set_xlabel("Time", color='#cdd6f4', fontsize=8)
        ax.set_ylabel("Value", color='#cdd6f4', fontsize=8)
        for spine in ax.spines.values():
            spine.set_color('#585b70')

    def _set_plot_enabled(self, display: str, enabled: bool):
        self._plot_enabled[display] = enabled
        self._save_watchlist()
        if not enabled:
            self._update_plot()

    def _update_plot(self):
        if not _MPL or not hasattr(self, '_plot_ax'):
            return
        ax = self._plot_ax
        ax.clear()
        self._style_plot_ax()

        plotted = 0
        for i, entry in enumerate(self._watchlist):
            if not self._plot_enabled.get(entry.display, False):
                continue
            data = list(self._plot_data.get(entry.display, []))
            if not data:
                continue
            xs = [mdates.date2num(datetime.fromtimestamp(d[0])) for d in data]
            ys = []
            for d in data:
                try:
                    ys.append(float(d[1]))
                except (ValueError, TypeError):
                    ys.append(float('nan'))
            color = _PLOT_COLORS[i % len(_PLOT_COLORS)]
            short = entry.display.split('.')[-1]
            ax.plot(xs, ys, label=short, color=color,
                    linewidth=1.5, marker='o', markersize=2)
            plotted += 1

        if plotted:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            self._fig.autofmt_xdate(rotation=30, ha='right')
            ax.legend(loc='upper left', fontsize=7,
                      facecolor='#313244', edgecolor='#585b70',
                      labelcolor='#cdd6f4')
            self._plot_status.setText(f"Plotting {plotted} variable(s)")
            self._plot_status.setStyleSheet("color:#a6e3a1;")
        else:
            self._plot_status.setText("Toggle 📈 on watchlist rows to enable plotting")
            self._plot_status.setStyleSheet("color:#6c7086; font-style:italic;")

        self._canvas.draw_idle()

    def _clear_plot_data(self):
        for q in self._plot_data.values():
            q.clear()
        self._update_plot()

    # ── Browser tab ───────────────────────────────────────────────────────────

    def _build_browser_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        self._ns_tabs = QTabWidget()
        self._ns_tabs.setDocumentMode(True)
        lay.addWidget(self._ns_tabs)

        bot = QHBoxLayout()
        self._enum_lbl = QLabel("Connect to load registers")
        self._enum_lbl.setStyleSheet("color:#6c7086; font-style:italic;")
        bot.addWidget(self._enum_lbl)
        bot.addStretch()
        btn_upd = QPushButton("🔄  Update Active Tab")
        btn_upd.setFixedWidth(160)
        btn_upd.clicked.connect(self._update_active_tab)
        bot.addWidget(btn_upd)
        lay.addLayout(bot)
        return w

    def _make_ns_tab(self, ns_name: str, entries: List[RegEntry]) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 4, 2, 2)

        filt = QLineEdit()
        filt.setPlaceholderText("Filter registers…")
        filt.setClearButtonEnabled(True)
        lay.addWidget(filt)

        tbl = QTableWidget(len(entries), 3)
        tbl.setHorizontalHeaderLabels(["Register Path", "Value", ""])
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        tbl.setColumnWidth(0, 420)
        tbl.setColumnWidth(1, 180)
        tbl.setColumnWidth(2, 68)
        for col, w in self._browser_col_widths.get(ns_name, {}).items():
            tbl.setColumnWidth(col, w)
        hdr.sectionResized.connect(
            lambda col, _old, new, ns=ns_name: self._on_browser_col_resized(ns, col, new))
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.verticalHeader().setVisible(False)

        for row, e in enumerate(entries):
            path_lbl = _full_label(e.path, e.index)
            path_item = QTableWidgetItem(path_lbl)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item.setData(Qt.ItemDataRole.UserRole, e)
            tbl.setItem(row, 0, path_item)
            val_item = QTableWidgetItem("—")
            val_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_item.setFlags(val_item.flags() | Qt.ItemFlag.ItemIsEditable)
            val_item.setToolTip("Double-click to write a new value")
            tbl.setItem(row, 1, val_item)
            add_btn = QPushButton("Add ⭐")
            add_btn.setFixedWidth(62)
            add_btn.clicked.connect(lambda _, _e=e: self._add_to_watchlist(_e))
            tbl.setCellWidget(row, 2, add_btn)

        tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        tbl.itemChanged.connect(self._on_browser_item_changed)
        filt.textChanged.connect(lambda txt, t=tbl: self._filter_table(t, txt))
        self._browser_tables[ns_name] = tbl
        lay.addWidget(tbl)
        return w

    @staticmethod
    def _filter_table(tbl: QTableWidget, text: str):
        txt = text.lower()
        for row in range(tbl.rowCount()):
            item = tbl.item(row, 0)
            tbl.setRowHidden(row, bool(txt and item and txt not in item.text().lower()))

    # ── Watchlist tab ─────────────────────────────────────────────────────────

    def _build_watchlist_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        self._watch_table = QTableWidget(0, 4)
        self._watch_table.setHorizontalHeaderLabels(["Register", "Value", "📈", ""])
        hdr = self._watch_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        self._watch_table.setColumnWidth(0, 360)
        self._watch_table.setColumnWidth(1, 180)
        self._watch_table.setColumnWidth(2, 58)
        self._watch_table.setColumnWidth(3, 58)
        for col, w in self._watch_col_widths.items():
            self._watch_table.setColumnWidth(col, w)
        hdr.sectionResized.connect(
            lambda col, _old, new: self._on_watch_col_resized(col, new))
        self._watch_table.verticalHeader().setDefaultSectionSize(28)
        self._watch_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._watch_table.setAlternatingRowColors(True)
        self._watch_table.verticalHeader().setVisible(False)
        self._watch_table.itemChanged.connect(self._on_watchlist_item_changed)
        lay.addWidget(self._watch_table)

        ctrl = QHBoxLayout()
        self._live_btn = QPushButton("▶  Start Live")
        self._live_btn.setProperty("role", "blue")
        self._live_btn.setFixedWidth(130)
        self._live_btn.setEnabled(False)
        self._live_btn.clicked.connect(self._toggle_live)
        ctrl.addWidget(self._live_btn)

        ctrl.addWidget(QLabel("Poll:"))
        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(200, 30000)
        self._poll_spin.setValue(1000)
        self._poll_spin.setSingleStep(100)
        self._poll_spin.setSuffix(" ms")
        self._poll_spin.setFixedWidth(180)
        self._poll_spin.setToolTip("Watchlist polling interval (200 ms – 30 s)")
        self._poll_spin.valueChanged.connect(
            lambda ms: self._live_timer.setInterval(ms))
        ctrl.addWidget(self._poll_spin)

        btn_clr = QPushButton("🗑  Clear All")
        btn_clr.setFixedWidth(100)
        btn_clr.clicked.connect(self._clear_watchlist)
        ctrl.addWidget(btn_clr)

        ctrl.addStretch()
        self._watch_count_lbl = QLabel("0 entries  —  logged to Excel during sweep")
        self._watch_count_lbl.setStyleSheet("color:#6c7086; font-style:italic;")
        ctrl.addWidget(self._watch_count_lbl)
        lay.addLayout(ctrl)
        return w

    # ── Ports / connect ───────────────────────────────────────────────────────

    def _scan_ports(self):
        try:
            from serial.tools.list_ports import comports
            ports = sorted(p.device for p in comports())
            self.com_combo.clear()
            self.com_combo.addItems(ports or ["(none found)"])
        except Exception:
            self.com_combo.clear()
            self.com_combo.addItem("(scan error)")

    def _browse_dbx(self):
        path = QFileDialog.getExistingDirectory(self, "Select datamodel folder")
        if path:
            self.dbx_edit.setText(path)

    def _toggle_connect(self):
        if self._pcu is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        com = self.com_combo.currentText()
        if not com or com.startswith("("):
            return
        if self._conn_worker and self._conn_worker.isRunning():
            self._conn_worker.wait(3000)
        self.btn_conn.setEnabled(False)
        dbx = self.dbx_edit.text().strip()
        src = f"local folder" if dbx else "device"
        self.conn_lbl.setText(f"Connecting — loading datamodels from {src}…")
        self.conn_lbl.setStyleSheet("color:#fab387;")
        self._conn_worker = EddieConnectWorker(com, dbx)
        self._conn_worker.done.connect(self._on_connected)
        self._conn_worker.start()

    def _on_connected(self, pcu, err: str):
        if err:
            self.conn_lbl.setText(f"Error: {err}")
            self.conn_lbl.setStyleSheet("color:#f38ba8;")
            self.btn_conn.setEnabled(True)
            return
        self._pcu = pcu
        self.conn_lbl.setText(f"Connected  ✓  {self.com_combo.currentText()}")
        self.conn_lbl.setStyleSheet("color:#a6e3a1;")
        self.btn_conn.setText("⏹  Disconnect")
        self.btn_conn.setProperty("role", "red")
        self.btn_conn.setEnabled(True)
        self.btn_conn.style().unpolish(self.btn_conn)
        self.btn_conn.style().polish(self.btn_conn)
        self._enum_lbl.setText("Enumerating registers…")
        self._enum_worker = EddieEnumWorker(pcu, self._pcu_lock)
        self._enum_worker.done.connect(self._on_enumerated)
        self._enum_worker.start()

    def _on_enumerated(self, ns_entries: Dict[str, List[RegEntry]], err: str):
        if err:
            self._enum_lbl.setText(f"Enumeration error: {err}")
            return
        self._browser_entries = ns_entries
        self._ns_tabs.clear()
        self._browser_tables.clear()
        for ns_name in sorted(ns_entries):
            tab = self._make_ns_tab(ns_name, ns_entries[ns_name])
            self._ns_tabs.addTab(tab, ns_name)
        total = sum(len(v) for v in ns_entries.values())
        self._enum_lbl.setText(
            f"{total} registers in {len(ns_entries)} namespaces — use filter to narrow list")
        self._main_tabs.setEnabled(True)
        self._live_btn.setEnabled(True)

    def _disconnect(self):
        self._live_timer.stop()
        self._live_btn.setText("▶  Start Live")
        self._live_btn.setProperty("role", "blue")
        self._live_btn.setEnabled(False)
        try:
            if self._pcu:
                self._pcu.ecdc.close()
        except Exception:
            pass
        self._pcu = None
        self._main_tabs.setEnabled(False)
        self._ns_tabs.clear()
        self._browser_tables.clear()
        self._browser_entries.clear()
        self._enum_lbl.setText("Connect to load registers")
        self.conn_lbl.setText("Not connected")
        self.conn_lbl.setStyleSheet("color:#6c7086; font-style:italic;")
        self.btn_conn.setText("⚡  Connect")
        self.btn_conn.setProperty("role", "green")
        self.btn_conn.setEnabled(True)
        self.btn_conn.style().unpolish(self.btn_conn)
        self.btn_conn.style().polish(self.btn_conn)

    # ── Browser update ────────────────────────────────────────────────────────

    def _update_active_tab(self):
        if not self._pcu:
            return
        if self._browser_worker and self._browser_worker.isRunning():
            return
        idx     = self._ns_tabs.currentIndex()
        ns_name = self._ns_tabs.tabText(idx)
        tbl     = self._browser_tables.get(ns_name)
        entries = self._browser_entries.get(ns_name, [])
        if not tbl or not entries:
            return
        visible_rows    = [r for r in range(tbl.rowCount()) if not tbl.isRowHidden(r)]
        visible_entries = [entries[r] for r in visible_rows]
        self._browser_worker = EddieBrowserUpdateWorker(self._pcu, visible_entries, self._pcu_lock)
        self._browser_worker.result.connect(
            lambda vals, err, t=tbl, vr=list(visible_rows): self._apply_browser_values(t, vr, vals, err))
        self._browser_worker.start()

    def _apply_browser_values(self, tbl: QTableWidget, visible_rows: list, values: list, err: str):
        if err:
            return
        self._bulk_update_active = True
        try:
            for row, val in zip(visible_rows, values):
                item = tbl.item(row, 1)
                if item:
                    item.setText(val)
                    item.setForeground(QColor("#f38ba8" if val.startswith("Err") else "#a6e3a1"))
        finally:
            self._bulk_update_active = False

    def _on_browser_item_changed(self, item: QTableWidgetItem):
        if self._bulk_update_active or not self._pcu:
            return
        if item.column() != 1:
            return
        tbl = item.tableWidget()
        if not tbl:
            return
        path_item = tbl.item(item.row(), 0)
        if not path_item:
            return
        entry = path_item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        w = EddieWriteWorker(self._pcu, entry.path, entry.index, item.text(), self._pcu_lock)
        w.done.connect(lambda lbl, err: self._on_write_done(item, lbl, err))
        w.finished.connect(lambda _w=w: self._write_workers.remove(_w) if _w in self._write_workers else None)
        self._write_workers.append(w)
        w.start()

    # ── Watchlist management ──────────────────────────────────────────────────

    def _add_to_watchlist(self, entry: RegEntry):
        if any(w.path == entry.path and w.index == entry.index for w in self._watchlist):
            return
        display = _full_label(entry.path, entry.index)
        maxlen  = self._max_samples_spin.value() \
                  if hasattr(self, '_max_samples_spin') else 300
        self._watchlist.append(WatchEntry(display, entry.path, entry.index))
        self._plot_enabled.setdefault(display, False)
        self._plot_data.setdefault(display, deque(maxlen=maxlen))
        self._rebuild_watch_table()
        self._save_watchlist()

    def _remove_from_watchlist(self, idx: int):
        if 0 <= idx < len(self._watchlist):
            d = self._watchlist[idx].display
            del self._watchlist[idx]
            self._plot_enabled.pop(d, None)
            self._plot_data.pop(d, None)
            self._rebuild_watch_table()
            self._update_plot()
            self._save_watchlist()

    def _clear_watchlist(self):
        self._watchlist.clear()
        self._plot_enabled.clear()
        self._plot_data.clear()
        self._rebuild_watch_table()
        self._update_plot()
        self._save_watchlist()

    def _rebuild_watch_table(self):
        self._bulk_update_active = True
        try:
            self._watch_table.setRowCount(len(self._watchlist))
            for row, entry in enumerate(self._watchlist):
                name_item = QTableWidgetItem(entry.display)
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._watch_table.setItem(row, 0, name_item)
                val_item = QTableWidgetItem("—")
                val_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                val_item.setFlags(val_item.flags() | Qt.ItemFlag.ItemIsEditable)
                val_item.setToolTip("Double-click to write a new value")
                self._watch_table.setItem(row, 1, val_item)
                plot_btn = QPushButton("📈")
                plot_btn.setCheckable(True)
                plot_btn.setFixedWidth(48)
                plot_btn.setChecked(self._plot_enabled.get(entry.display, False))
                plot_btn.toggled.connect(
                    lambda checked, d=entry.display: self._set_plot_enabled(d, checked))
                self._watch_table.setCellWidget(row, 2, plot_btn)
                rm = QPushButton("✕")
                rm.setFixedWidth(48)
                rm.clicked.connect(lambda _, i=row: self._remove_from_watchlist(i))
                self._watch_table.setCellWidget(row, 3, rm)
        finally:
            self._bulk_update_active = False
        n = len(self._watchlist)
        self._watch_count_lbl.setText(
            f"{n} {'entry' if n == 1 else 'entries'}  —  logged to Excel during sweep")

    # ── Watchlist persistence ──────────────────────────────────────────────

    def _save_watchlist(self):
        try:
            data = {
                "entries": [
                    {"display": e.display, "path": e.path, "index": e.index}
                    for e in self._watchlist
                ],
                "plot_enabled":       self._plot_enabled,
                "max_samples":        self._max_samples_spin.value()
                                      if hasattr(self, '_max_samples_spin') else 300,
                "watch_col_widths":   {str(k): v for k, v in self._watch_col_widths.items()},
                "browser_col_widths": {
                    ns: {str(k): v for k, v in cols.items()}
                    for ns, cols in self._browser_col_widths.items()
                },
            }
            self._watchlist_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_watchlist(self):
        try:
            if not self._watchlist_path.exists():
                return
            data = json.loads(self._watchlist_path.read_text(encoding="utf-8"))

            # Restore column-width dicts (JSON keys are strings)
            self._watch_col_widths = {
                int(k): v for k, v in data.get("watch_col_widths", {}).items()}
            self._browser_col_widths = {
                ns: {int(k): v for k, v in cols.items()}
                for ns, cols in data.get("browser_col_widths", {}).items()}

            # Restore max-samples spinbox
            if hasattr(self, '_max_samples_spin'):
                self._max_samples_spin.blockSignals(True)
                self._max_samples_spin.setValue(data.get("max_samples", 300))
                self._max_samples_spin.blockSignals(False)

            maxlen = self._max_samples_spin.value() \
                     if hasattr(self, '_max_samples_spin') else 300
            for e in data.get("entries", []):
                entry = WatchEntry(e["display"], e["path"], e["index"])
                self._watchlist.append(entry)
                self._plot_enabled[entry.display] = \
                    data.get("plot_enabled", {}).get(entry.display, False)
                self._plot_data[entry.display] = deque(maxlen=maxlen)
            if self._watchlist:
                self._rebuild_watch_table()

            # Apply saved watchlist column widths
            for col, w in self._watch_col_widths.items():
                self._watch_table.setColumnWidth(col, w)
        except Exception:
            pass

    def _on_watch_col_resized(self, col: int, width: int):
        self._watch_col_widths[col] = width
        self._save_watchlist()

    def _on_browser_col_resized(self, ns: str, col: int, width: int):
        self._browser_col_widths.setdefault(ns, {})[col] = width
        self._save_watchlist()

    def _toggle_plot_pause(self, paused: bool):
        self._plot_paused = paused
        self._plot_pause_btn.setText("▶  Resume" if paused else "⏹  Pause")
        self._plot_pause_btn.setStyleSheet(
            "color:#fab387; font-weight:bold;" if paused else "")

    def _on_max_samples_changed(self, n: int):
        for key in list(self._plot_data):
            self._plot_data[key] = deque(self._plot_data[key], maxlen=n)
        self._save_watchlist()

    # ── Live polling ──────────────────────────────────────────────────────────

    def _toggle_live(self):
        if self._live_timer.isActive():
            self._live_timer.stop()
            self._live_btn.setText("▶  Start Live")
            self._live_btn.setProperty("role", "blue")
        else:
            self._live_timer.start()
            self._live_btn.setText("■  Stop Live")
            self._live_btn.setProperty("role", "red")
            self._poll_watchlist()
        self._live_btn.style().unpolish(self._live_btn)
        self._live_btn.style().polish(self._live_btn)

    def _poll_watchlist(self):
        if not self._pcu or not self._watchlist:
            return
        if self._watch_worker and self._watch_worker.isRunning():
            return
        self._watch_worker = EddieWatchlistPollWorker(
            self._pcu, self._watchlist, self._pcu_lock)
        self._watch_worker.result.connect(self._on_poll_result)
        self._watch_worker.start()

    def _on_watchlist_item_changed(self, item: QTableWidgetItem):
        if self._bulk_update_active or not self._pcu:
            return
        if item.column() != 1:
            return
        row = item.row()
        if row >= len(self._watchlist):
            return
        entry = self._watchlist[row]
        w = EddieWriteWorker(self._pcu, entry.path, entry.index, item.text(), self._pcu_lock)
        w.done.connect(lambda lbl, err: self._on_write_done(item, lbl, err))
        w.finished.connect(lambda _w=w: self._write_workers.remove(_w) if _w in self._write_workers else None)
        self._write_workers.append(w)
        w.start()

    def _on_write_done(self, item: QTableWidgetItem, label: str, err: str):
        self._bulk_update_active = True
        try:
            if err:
                item.setForeground(QColor("#f38ba8"))
                item.setToolTip(f"Write error: {err}")
                self._enum_lbl.setText(f"✕ Write failed [{label}]: {err}")
                self._enum_lbl.setStyleSheet("color:#f38ba8;")
            else:
                item.setForeground(QColor("#89dceb"))
                item.setToolTip("Value written — double-click to write again")
                self._enum_lbl.setText(f"✓ Written [{label}]")
                self._enum_lbl.setStyleSheet("color:#a6e3a1;")
        finally:
            self._bulk_update_active = False

    def _on_poll_result(self, values: dict, err: str):
        if err:
            return
        with self._snap_lock:
            self._last_snapshot = dict(values)
        self._bulk_update_active = True
        ts = time.time()
        try:
            for row, entry in enumerate(self._watchlist):
                val  = values.get(entry.display, "—")
                item = self._watch_table.item(row, 1)
                if not item:
                    item = QTableWidgetItem()
                    self._watch_table.setItem(row, 1, item)
                item.setText(str(val))
                is_err = isinstance(val, str) and val.startswith("Err")
                item.setForeground(QColor("#f38ba8" if is_err else "#a6e3a1"))
                if self._plot_enabled.get(entry.display, False) \
                        and not is_err and not self._plot_paused:
                    try:
                        self._plot_data[entry.display].append((ts, float(val)))
                    except (ValueError, TypeError):
                        pass
        finally:
            self._bulk_update_active = False
        if any(self._plot_enabled.values()) and not self._plot_paused:
            self._update_plot()
