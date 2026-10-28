# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Automated Test Suite.

Build:
    pyinstaller app.spec

Output:
    dist/AutomatedTestSuite/AutomatedTestSuite.exe
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ── Paths ────────────────────────────────────────────────────────────────────
SRC = Path(SPECPATH)

# ── Hidden imports ───────────────────────────────────────────────────────────
# PyInstaller cannot detect these because they are imported dynamically or
# lazily, or because they live in internal (non-PyPI) packages.
hidden = [
    # enphase internal packages used by test_automation.py / eddie_panel.py
    "enphase_equipment",
    "enphase_equipment.dc_source",
    "enphase_equipment.dc_source.itech",
    "enphase_equipment.multi_meter",
    "enphase_equipment.multi_meter.agilent",
    "enphase_equipment.oscilloscope",
    "enphase_equipment.oscilloscope.tektronix",
    "enphase_equipment.thermal_chamber",
    "enphase_equipment.thermal_chamber.espec",
    "enphase_equipment.power_meter",
    "enphase_equipment.power_meter.yokogawa",
    # enphase_ecdc used lazily by eddie_panel.py
    "enphase_ecdc",
    # pyvisa backends
    "pyvisa_py",
    # openpyxl used for Excel export in sweep/sequence panels
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.drawing.image",
    # pyqtgraph internals
    "pyqtgraph.graphicsItems",
    "pyqtgraph.graphicsItems.PlotItem",
]

# Also collect all sub-modules of enphase_equipment so any driver works
try:
    hidden += collect_submodules("enphase_equipment")
except Exception:
    pass

# ── Data files ───────────────────────────────────────────────────────────────
# JSON config files and help docs that the app reads at runtime via
# Path(__file__).parent / "filename"
datas = [
    (str(SRC / "instrument_addresses.json"), "."),
    (str(SRC / "app_settings.json"), "."),
    (str(SRC / "help.md"), "."),
    (str(SRC / "instrument_config.json"), "."),
]
# Include panel-level config if it exists
if (SRC / "panels" / "instrument_config.json").exists():
    datas.append((str(SRC / "panels" / "instrument_config.json"), "panels"))
# Include session file if it exists (optional — recreated at runtime)
if (SRC / "sequence_session.json").exists():
    datas.append((str(SRC / "sequence_session.json"), "."))
if (SRC / "eddie_watchlist.json").exists():
    datas.append((str(SRC / "eddie_watchlist.json"), "."))
if (SRC / "panel_state.json").exists():
    datas.append((str(SRC / "panel_state.json"), "."))

# Collect pyqtgraph data files (icons, templates, etc.)
try:
    datas += collect_data_files("pyqtgraph")
except Exception:
    pass

# ── Analysis ─────────────────────────────────────────────────────────────────
a = Analysis(
    [str(SRC / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy packages not used by this app
        "matplotlib",
        "scipy",
        "pandas",
        "tkinter",
        "_tkinter",
        "IPython",
        "jupyter",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutomatedTestSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # No terminal window — it's a GUI app
    icon=None,               # Add .ico path here if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutomatedTestSuite",
)
