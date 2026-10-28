# -*- coding: utf-8 -*-
"""
app_logger.py — Centralised rotating file logger for the Automated Test Suite.

Creates a single logger that writes to ``logs/app.log`` with:
- RotatingFileHandler: max 5 MB per file, 3 backup files (total ~20 MB max)
- Console handler: WARNING and above echoed to stderr
- Format: ``YYYY-MM-DD HH:MM:SS.mmm  LEVEL  [source]  message``

Usage in any module::

    from app_logger import log
    log.info("Sweep started with 42 steps")
    log.warning("GPP CH1 returned unexpected mode")
    log.error("ITECH connection failed", exc_info=True)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import shutil

def _app_dir() -> Path:
    """Return the writable application directory.

    When running from source: the directory containing this file.
    When running from a PyInstaller --onedir bundle: the directory
    containing the .exe (one level above sys._MEIPASS).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

def _bundle_dir() -> Path:
    """Return the directory where PyInstaller unpacked read-only assets.

    Same as APP_DIR when running from source.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

APP_DIR    = _app_dir()
BUNDLE_DIR = _bundle_dir()

def _seed_config_files():
    """On first run from a frozen bundle, copy default config files
    from the bundle into the writable APP_DIR so the user can edit them."""
    if not getattr(sys, "frozen", False):
        return
    for name in (
        "instrument_addresses.json",
        "app_settings.json",
        "instrument_config.json",
    ):
        target = APP_DIR / name
        source = BUNDLE_DIR / name
        if not target.exists() and source.exists():
            shutil.copy2(source, target)

_seed_config_files()

_LOG_DIR  = APP_DIR / "logs"
_LOG_FILE = _LOG_DIR / "app.log"
_MAX_BYTES  = 5 * 1024 * 1024   # 5 MB per log file
_BACKUP_CNT = 3                  # keep 3 old files  (total ~20 MB)

_LOG_FMT  = "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(name)s]  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_LOG_DIR.mkdir(exist_ok=True)

# ── Root logger for the app ──────────────────────────────────────────────────

_root = logging.getLogger("ats")          # "ats" = Automated Test Suite
_root.setLevel(logging.DEBUG)

# File handler — DEBUG and above
_fh = RotatingFileHandler(
    str(_LOG_FILE), maxBytes=_MAX_BYTES, backupCount=_BACKUP_CNT,
    encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_DATE_FMT))
_root.addHandler(_fh)

# Console handler — WARNING and above (keeps console clean)
_ch = logging.StreamHandler(sys.stderr)
_ch.setLevel(logging.WARNING)
_ch.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_DATE_FMT))
_root.addHandler(_ch)

# ── Public convenience logger ────────────────────────────────────────────────

log = _root   # modules can do: from app_logger import log


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'ats' namespace.

    Example::

        log = get_logger("sweep")   # → logger named "ats.sweep"
        log.info("Starting sweep…")
    """
    return logging.getLogger(f"ats.{name}")
