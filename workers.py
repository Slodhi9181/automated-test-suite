# -*- coding: utf-8 -*-
"""
workers.py — Background workers and VISA resource management.
"""

import pyvisa
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, QObject

from test_automation import (
    run_sweep, ADDR, open_tcpip_resource,
    GWGPP4323, GWInstekGDM8261A,
)
from app_logger import get_logger, APP_DIR

_log = get_logger("workers")


# ─────────────────────────────────────────────────────────────────────────────
# Per-instrument global lock
# Serialises ALL threads (live-readings panel, sweep runner, etc.) so no two
# threads ever touch the same physical instrument at the same time.
# ─────────────────────────────────────────────────────────────────────────────

_INSTR_LOCKS:     dict             = {}
_INSTR_LOCKS_MTX: threading.Lock  = threading.Lock()


def _instr_lock(key: str) -> threading.Lock:
    with _INSTR_LOCKS_MTX:
        if key not in _INSTR_LOCKS:
            _INSTR_LOCKS[key] = threading.Lock()
        return _INSTR_LOCKS[key]


@contextmanager
def use_instrument(key: str, timeout: int = 5000):
    """Open *key*, yield the resource, close it — holding the global
    per-instrument lock for the duration so no concurrent VISA access occurs."""
    with _instr_lock(key):
        rm, res = open_resource(key, timeout)
        try:
            yield res
        finally:
            try:
                res.close()
            except Exception:
                pass
            try:
                rm.close()
            except Exception:
                pass


@contextmanager
def use_driver(key: str, factory):
    """Create a driver via *factory()*, yield it, close it — holding the global
    per-instrument lock for the duration so no concurrent VISA access occurs.
    For enphase_equipment drivers that open their own VISA session."""
    with _instr_lock(key):
        drv = factory()
        try:
            yield drv
        finally:
            try:
                drv.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Working-address cache  (avoids full rediscovery on every button press)
# ─────────────────────────────────────────────────────────────────────────────

_ADDR_CACHE: dict = {}  # key → VISA address string that connected last time


def invalidate_cache(key: str = None):
    """Clear one or all cached working addresses (call if instrument is re-IP'd)."""
    if key:
        _ADDR_CACHE.pop(key, None)
    else:
        _ADDR_CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# VISA resource opener with full ASRL/serial support
# ─────────────────────────────────────────────────────────────────────────────

def open_resource(key: str, timeout: int = 5000):
    """
    Open a VISA resource by instrument key.
    Handles TCPIP (LAN), ASRL (USB/Serial), and generic resources.
    For ASRL the GDM-8261A USB-CDC default is 115200 baud, 8N1.

    TCPIP: first attempt uses the cached working address (fast, ~100 ms).
    If the cache is stale the full open_tcpip_resource discovery runs once
    and the new working address is cached for next time.
    """
    addr = ADDR.get(key, '')
    if not addr or 'XXX' in addr:
        _log.warning("open_resource(%s): no valid address", key)
        raise ValueError(
            f"No valid address for '{key}'. "
            "Set it in the Addresses tab first."
        )
    _log.debug("open_resource(%s): addr=%s timeout=%d", key, addr, timeout)
    rm = pyvisa.ResourceManager()

    if addr.upper().startswith("TCPIP"):
        # ── Direct path: address already names a port and protocol ──
        # e.g. TCPIP0::169.254.110.87::1026::SOCKET
        if "::SOCKET" in addr.upper():
            try:
                res = rm.open_resource(addr, timeout=timeout)
                res.write_termination = "\n"
                res.read_termination  = "\n"
                _ADDR_CACHE[key] = addr
                return rm, res
            except Exception:
                pass  # fall through to full discovery

        # ── Fast path: reuse the previously-successful address format ──
        cached = _ADDR_CACHE.get(key)
        if cached:
            try:
                res = rm.open_resource(cached, timeout=timeout)
                res.write_termination = "\n"
                res.read_termination  = "\n"
                return rm, res
            except Exception:
                _ADDR_CACHE.pop(key, None)   # stale — fall through

        # ── Slow path: full discovery (first time or after stale cache) ──
        res = open_tcpip_resource(rm, addr, timeout)
        rname = getattr(res, 'resource_name', None)
        if rname:
            _ADDR_CACHE[key] = rname
        return rm, res

    elif addr.upper().startswith("ASRL"):
        # USB-CDC / RS-232 serial resource
        res = rm.open_resource(addr)
        res.timeout = timeout
        # Apply standard serial settings — safe to ignore if USBTMC
        try:
            res.baud_rate   = 115200
            res.data_bits   = 8
            res.stop_bits   = pyvisa.constants.StopBits.one
            res.parity      = pyvisa.constants.Parity.none
            res.flow_control = pyvisa.constants.VI_ASRL_FLOW_NONE
        except (AttributeError, pyvisa.errors.VisaIOError):
            pass
        # GW Instek instruments expect CR+LF write, plain LF read
        res.write_termination = '\r\n'
        res.read_termination  = '\n'

    else:
        res = rm.open_resource(addr, timeout=timeout)
        res.write_termination = '\n'
        res.read_termination  = '\n'

    return rm, res


# ─────────────────────────────────────────────────────────────────────────────
# Connection wrappers with .run(fn) API
#
# SOCKET instruments (GPP, NGI, DMM, MDO) use a persistent TCP connection
# that stays open across calls — their firmware can't handle rapid TCP
# open/close cycles (TIME_WAIT exhaustion → lockup).
#
# INSTR instruments (ITECH, WT5000, ESPEC) use VXI-11 which handles
# reconnects cleanly, so they open/close per call.
# ─────────────────────────────────────────────────────────────────────────────

def _is_socket(key: str) -> bool:
    addr = ADDR.get(key, '')
    return '::SOCKET' in addr.upper()


class _PersistentResourceConn:
    """Keeps ONE VISA resource alive across run() calls.
    Reconnects automatically on error."""
    def __init__(self, key):
        self._key  = key
        self._rm   = None
        self._res  = None
        self._lock = threading.Lock()

    def _open(self):
        self._rm, self._res = open_resource(self._key)

    def _close(self):
        for obj in (self._res, self._rm):
            try:
                if obj is not None: obj.close()
            except Exception:
                pass
        self._res = self._rm = None

    def run(self, fn, skip_if_busy=False):
        if skip_if_busy:
            acquired = self._lock.acquire(blocking=False)
        else:
            acquired = self._lock.acquire()
        if not acquired:
            return None
        try:
            if self._res is None:
                self._open()
            try:
                return fn(self._res)
            except Exception as e1:
                _log.warning("PersistentConn(%s): %s — reconnecting", self._key, e1)
                self._close()
                time.sleep(0.3)
                self._open()
                return fn(self._res)
        except Exception as e2:
            _log.error("PersistentConn(%s): retry failed: %s", self._key, e2)
            self._close()
            raise
        finally:
            self._lock.release()

    def invalidate(self):
        with self._lock:
            self._close()


class _StatelessResourceConn:
    """Opens and closes a VISA resource per run() call."""
    def __init__(self, key):
        self._key = key
    def run(self, fn, skip_if_busy=False):
        with use_instrument(self._key) as res:
            return fn(res)
    def invalidate(self):
        pass


class _DriverConn:
    """Creates and closes a driver per run() call."""
    def __init__(self, key, factory):
        self._key = key
        self._factory = factory
    def run(self, fn, skip_if_busy=False):
        with use_driver(self._key, self._factory) as drv:
            return fn(drv)
    def invalidate(self):
        pass


# Process-wide singletons for persistent SOCKET connections
_PERSISTENT: dict = {}
_PERSISTENT_MTX = threading.Lock()


def shared_resource(key: str, on_connect=None):
    """Return a resource wrapper for *key*.
    SOCKET instruments get a persistent connection (singleton).
    INSTR instruments get a fresh open/close per call."""
    if _is_socket(key):
        with _PERSISTENT_MTX:
            if key not in _PERSISTENT:
                _PERSISTENT[key] = _PersistentResourceConn(key)
            return _PERSISTENT[key]
    return _StatelessResourceConn(key)


def shared_driver(key: str, factory):
    """Return a driver wrapper for *key* (open/close per call)."""
    return _DriverConn(key, factory)


def invalidate_shared(key: str):
    """Close persistent SOCKET connection for *key* (if any)."""
    with _PERSISTENT_MTX:
        conn = _PERSISTENT.pop(key, None)
        if conn is not None:
            _log.info("invalidate_shared: closing persistent conn for %s", key)
            conn.invalidate()


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight instrument check
# ─────────────────────────────────────────────────────────────────────────────

_INSTR_NAMES = {
    'itech': 'ITECH IT6005C',
    'gpp':   'GPP-4323',
    'gdm':   'GDM-8261A',
    'dmm':   'Keysight 34461A',
    'mdo':   'Tektronix MDO34',
    'wt5000':'Yokogawa WT5000',
    'espec': 'ESPEC SH-662',
    'ngi':   'NGI N83624',
    'daq':   'Keysight DAQ 973A',
}


def check_instruments(keys: list) -> list:
    """Check which instruments have valid addresses configured.

    Parameters
    ----------
    keys : list of instrument key strings (e.g. ['itech', 'gpp'])

    Returns
    -------
    list of (key, friendly_name) tuples for instruments that are NOT configured.
    """
    missing = []
    for key in keys:
        addr = ADDR.get(key, '')
        if not addr or 'XXX' in addr:
            missing.append((key, _INSTR_NAMES.get(key, key)))
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Logging bridge for sweep output → Qt signals
# ─────────────────────────────────────────────────────────────────────────────

class _LogSignals(QObject):
    message = pyqtSignal(str)


class QLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.signals = _LogSignals()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        self.signals.message.emit(self.format(record))


# ─────────────────────────────────────────────────────────────────────────────
# Sweep worker
# ─────────────────────────────────────────────────────────────────────────────

class SweepWorker(QThread):
    log_msg   = pyqtSignal(str)
    finished  = pyqtSignal(str)   # csv path
    error_sig = pyqtSignal(str)

    def __init__(self, instruments: list):
        super().__init__()
        self.instruments = instruments

    def run(self):
        handler = QLogHandler()
        handler.signals.message.connect(self.log_msg)
        logger = logging.getLogger("test_sweep")
        logger.addHandler(handler)
        try:
            run_sweep(instruments=self.instruments)
            results_dir = APP_DIR / "results"
            csv_files = sorted(results_dir.glob("sweep_*.csv"))
            self.finished.emit(str(csv_files[-1]) if csv_files else "")
        except SystemExit as e:
            self.error_sig.emit(f"Sweep exited with code {e.code}")
        except Exception as e:
            self.error_sig.emit(str(e))
        finally:
            logger.removeHandler(handler)


# ─────────────────────────────────────────────────────────────────────────────
# Universal instrument worker
# ─────────────────────────────────────────────────────────────────────────────

class InstrumentWorker(QThread):
    """
    Executes any callable (fn) in a background thread.
    fn must return a value (or None) and may raise on error.
    Emits result(value, error_string).
    """
    result = pyqtSignal(object, str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            val = self._fn()
            self.result.emit(val, "")
        except Exception as e:
            self.result.emit(None, str(e))
