# -*- coding: utf-8 -*-
"""
ui_helpers.py — Shared UI widgets, theme constants, and helpers.

Centralises small helpers that were previously duplicated in panels.py,
sweep_panel.py, sequence_panel.py, and daq_espec_panel.py.
"""

from pathlib import Path

from PyQt6.QtWidgets import QPushButton, QLabel
from PyQt6.QtCore import QTimer


# ── Theme colour constants  (Catppuccin Mocha) ───────────────────────────────

class TH:
    """Central colour and style constants — single source of truth."""
    # Surfaces
    BASE       = '#1e1e2e'
    MANTLE     = '#181825'
    CRUST      = '#11111b'
    SURFACE0   = '#313244'
    SURFACE1   = '#45475a'
    SURFACE2   = '#585b70'
    OVERLAY0   = '#6c7086'
    OVERLAY1   = '#7f849c'

    # Text
    TEXT       = '#cdd6f4'
    SUBTEXT0   = '#a6adc8'
    SUBTEXT1   = '#bac2de'

    # Accent
    BLUE       = '#89b4fa'
    GREEN      = '#a6e3a1'
    RED        = '#f38ba8'
    ORANGE     = '#fab387'
    YELLOW     = '#f9e2af'
    MAUVE      = '#cba6f7'
    TEAL       = '#94e2d5'
    PINK       = '#f5c2e7'
    ROSEWATER  = '#f5e0dc'
    FLAMINGO   = '#f2cdcd'
    PEACH      = '#fab387'
    SKY        = '#89dceb'
    LAVENDER   = '#b4befe'
    MAROON     = '#eba0ac'

    # Semantic
    OK         = GREEN
    ERR        = RED
    WARN       = ORANGE
    INFO       = SUBTEXT0
    ACCENT     = BLUE
    MUTED      = OVERLAY0
    BUSBAR     = ORANGE  # NGI busbar highlight

    # Common style fragments
    MONO       = "'Courier New', monospace"
    SANS       = "'Segoe UI', Arial, sans-serif"
    FONT_SM    = '9pt'
    FONT_MD    = '10pt'
    FONT_LG    = '13pt'


# ── Colour palette for plots ─────────────────────────────────────────────────

CURVE_COLORS = [
    TH.BLUE, TH.GREEN, TH.RED, TH.ORANGE,
    TH.MAUVE, TH.YELLOW, TH.TEAL, TH.MAROON,
]


# ── Button factory ────────────────────────────────────────────────────────────

def _btn(text, role=None, width=None):
    """Create a QPushButton with optional CSS role and fixed width."""
    b = QPushButton(text)
    if role:
        b.setProperty("role", role)
    if width:
        b.setFixedWidth(width)
    return b


# ── Status label ──────────────────────────────────────────────────────────────

class StatusLabel(QLabel):
    """Coloured status label with ok / err / info convenience methods."""

    def ok(self, msg):
        self.setText(f"✓  {msg}")
        self.setStyleSheet(f"color:{TH.OK}; font-size:{TH.FONT_SM};")

    def err(self, msg):
        self.setText(f"❌  {msg}")
        self.setStyleSheet(f"color:{TH.ERR}; font-size:{TH.FONT_SM};")

    def info(self, msg):
        self.setText(msg)
        self.setStyleSheet(f"color:{TH.INFO}; font-size:{TH.FONT_SM};")

    def clear_after(self, ms=4000):
        QTimer.singleShot(ms, self.clear)

    def clear(self):
        self.setText("")


# ── Unique file path ─────────────────────────────────────────────────────────

def unique_path(fp: str) -> str:
    """Return *fp* unchanged if it doesn't exist; otherwise append _1, _2, …
    until a free name is found."""
    p = Path(fp)
    if not p.exists():
        return str(p)
    stem, suf, par = p.stem, p.suffix, p.parent
    for n in range(1, 9999):
        c = par / f"{stem}_{n}{suf}"
        if not c.exists():
            return str(c)
    return str(p)
