# -*- coding: utf-8 -*-
"""
Generic panel state persistence.

Saves / restores the values of all QSpinBox, QDoubleSpinBox, QComboBox,
QCheckBox, and QLineEdit instances found in a panel's Python-level instance
attributes (``vars(panel)``).  Dict-valued attributes are also walked one
level deep — this handles cases like ``GppPanel._ch_panels[ch]._v_sp``.

Usage::

    import persist

    panels = {
        'sweep': sweep_panel,
        'itech': itech_panel,
        ...
    }
    persist.load_all(panels)   # call once after building the UI
    persist.save_all(panels)   # call in QMainWindow.closeEvent
"""

import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QLineEdit, QWidget,
)

from app_logger import APP_DIR

# Signals emitted by each leaf widget type
_CHANGE_SIG = {
    QSpinBox:        lambda w: w.valueChanged,
    QDoubleSpinBox:  lambda w: w.valueChanged,
    QComboBox:       lambda w: w.currentIndexChanged,
    QCheckBox:       lambda w: w.toggled,
    QLineEdit:       lambda w: w.textChanged,
}

STATE_FILE = APP_DIR / "panel_state.json"

_LEAF = (QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QLineEdit)


# ── low-level read / write ────────────────────────────────────────────────────

def _get(w):
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return w.value()
    if isinstance(w, QComboBox):
        return w.currentIndex()
    if isinstance(w, QCheckBox):
        return w.isChecked()
    if isinstance(w, QLineEdit):
        return w.text()
    return None


def _set(w, val):
    try:
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.setValue(val)
        elif isinstance(w, QComboBox):
            idx = int(val)
            if 0 <= idx < w.count():
                w.setCurrentIndex(idx)
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(val))
        elif isinstance(w, QLineEdit):
            w.setText(str(val))
    except Exception:
        pass


# ── collect / restore ─────────────────────────────────────────────────────────

def _collect(obj, prefix: str, out: dict):
    try:
        attrs = vars(obj)
    except TypeError:
        return
    for attr, val in attrs.items():
        key = f"{prefix}.{attr}"
        if isinstance(val, _LEAF):
            v = _get(val)
            if v is not None:
                out[key] = v
        elif isinstance(val, dict):
            for k2, w2 in val.items():
                sub = f"{key}[{k2}]"
                if isinstance(w2, _LEAF):
                    v = _get(w2)
                    if v is not None:
                        out[sub] = v
                elif isinstance(w2, QWidget):
                    _collect(w2, sub, out)


def _restore(obj, prefix: str, state: dict):
    try:
        attrs = vars(obj)
    except TypeError:
        return
    for attr, val in attrs.items():
        key = f"{prefix}.{attr}"
        if isinstance(val, _LEAF):
            if key in state:
                _set(val, state[key])
        elif isinstance(val, dict):
            for k2, w2 in val.items():
                sub = f"{key}[{k2}]"
                if isinstance(w2, _LEAF):
                    if sub in state:
                        _set(w2, state[sub])
                elif isinstance(w2, QWidget):
                    _restore(w2, sub, state)


# ── public API ────────────────────────────────────────────────────────────────

def save_all(panels: dict):
    """Collect widget values from every panel and write to *panel_state.json*.

    Args:
        panels: ``{name: panel_widget}`` mapping.
    """
    state: dict = {}
    for name, obj in panels.items():
        _collect(obj, name, state)
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[persist] save error: {e}")


def load_all(panels: dict):
    """Read *panel_state.json* and restore widget values in every panel.

    Args:
        panels: ``{name: panel_widget}`` mapping.  Unknown keys in the file
                are silently ignored (forward-compatible).
    """
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[persist] load error: {e}")
        return
    for name, obj in panels.items():
        _restore(obj, name, state)


# ── auto-save helpers ─────────────────────────────────────────────────────────

def _connect_widget(w, cb):
    """Connect *w*'s change signal to *cb* (ignores widgets not in _CHANGE_SIG)."""
    for cls, sig_fn in _CHANGE_SIG.items():
        if isinstance(w, cls):
            try:
                sig_fn(w).connect(lambda *_: cb())
            except Exception:
                pass
            break


def _connect_obj(obj, cb, _seen=None):
    """Walk *obj*'s instance attributes and connect all leaf widgets to *cb*."""
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return
    _seen.add(obj_id)
    try:
        attrs = vars(obj)
    except TypeError:
        return
    for val in attrs.values():
        if isinstance(val, _LEAF):
            _connect_widget(val, cb)
        elif isinstance(val, dict):
            for w2 in val.values():
                if isinstance(w2, _LEAF):
                    _connect_widget(w2, cb)
                elif isinstance(w2, QWidget):
                    _connect_obj(w2, cb, _seen)


def has_panel_state(key: str) -> bool:
    """Return True if panel_state.json contains at least one key for *key*."""
    if not STATE_FILE.exists():
        return False
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        prefix = f"{key}."
        return any(k.startswith(prefix) for k in state)
    except Exception:
        return False


def connect_autosave(panels: dict, callback):
    """Walk every widget in *panels* and connect its change signal to *callback*.

    Use a debounce timer as the callback so rapid changes don't hammer the disk::

        timer = QTimer(); timer.setSingleShot(True); timer.setInterval(2000)
        timer.timeout.connect(lambda: persist.save_all(panels))
        persist.connect_autosave(panels, timer.start)
    """
    for obj in panels.values():
        _connect_obj(obj, callback)
