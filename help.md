# Automated Test Suite — Context Document

> This file is the detailed design reference. `AGENTS.md` instructs the agent to read this before changes and update it after. Keep under 200 lines.

## Running
```
python app.py
```
Deps: PyQt6, pyqtgraph, pyvisa, openpyxl, enphase_equipment (internal)

## Building / Distributing
```
pip install pyinstaller
build.bat            (or: python -m PyInstaller app.spec --noconfirm)
```
Output: `dist\AutomatedTestSuite\` — zip and share the entire folder.
Recipients do **not** need Python or any packages installed.
- `APP_DIR` (`app_logger.py`): resolves to exe's folder when frozen, source dir otherwise
- On first launch the exe copies default JSON configs from `_internal/` to the exe folder
- `results/`, `logs/` are created next to the exe at runtime

## File Map

| File | Role |
|---|---|
| `app.py` | MainWindow (sidebar nav, E-STOP, status bar), DashboardTab (live readings, keepalive, quick-set), AddressesTab, signal wiring, `_on_test_running`, `_on_lib_connected/disconnected` |
| `workers.py` | All VISA access: `PersistentResource/Driver` singletons, `shared_resource/shared_driver`, `invalidate_shared`, `open_resource`, `use_instrument`, `InstrumentWorker` QThread, per-key locks |
| `test_automation.py` | SCPI drivers: `ITechFixed`, `GWGPP4323`, `MDO34Driver`, `WT5000Driver`, `GWInstekGDM8261A`; adapters for Library `load_driver`; `ADDR` dict (global, mutated by AddressesTab) |
| `instrument_registry.py` | `CATEGORIES` dict = single source of truth; helpers: `load_driver`, `address_meta`, `keepalive_keys`, `native_instruments` |
| `sequence_panel.py` | `SequenceRunner(QThread)` step-table runner + `SequencePanel` UI; signal `test_running(bool)` |
| `sweep_panel.py` | `SweepRunner(QThread)` parametric sweep + `SweepPanel` UI; signal `test_running(bool)`, `steps_exported` |
| `panels/_monolith.py` | All panel classes: Itech/Gpp/Gdm/Dmm/Mdo/WT5000/NgiCellSim Panel, `InstrumentLibraryPanel` (connect/disconnect/suspend), `PlotPanel`, `ScpiTerminal` |
| `panels/__init__.py` | Re-exports panel classes |
| `ui_helpers.py` | `TH` class (Catppuccin Mocha palette), `_btn()`, `StatusLabel`, `unique_path()`, `CURVE_COLORS` |
| `app_logger.py` | `APP_DIR`/`BUNDLE_DIR` path resolution, rotating logger `logs/app.log` (5MB x 3), `get_logger(name)` -> `ats.<name>` |
| `persist.py` | Auto-save/restore QWidget state to `panel_state.json` |
| `eddie_panel.py` | EDDIE device watchlist |
| `daq_espec_panel.py` | DAQ 973A + ESPEC SH-662 panels |
| `app.spec` | PyInstaller build spec (hidden imports, data files, excludes) |
| `build.bat` | One-click build script → `dist\AutomatedTestSuite\` |

## UI Architecture
- Sidebar `QTreeWidget` nav (Test Automation / Monitoring / Instruments / Configuration) + `QStackedWidget` content
- E-STOP always visible at sidebar top (Ctrl+Shift+X) — stops runners, ITECH off, GPP all-off, NGI off
- `DetachableTabWidget` for instrument panels — tabs are moveable/pop-out (double-click)
- Status bar: per-instrument connection indicators synced to keepalive
- Shortcuts: Ctrl+1..7 navigate pages, Ctrl+S save, Ctrl+B toggle sidebar
- Theme: all colors via `TH.BLUE`, `TH.GREEN`, etc. Never hardcode hex.

## Connection Patterns (Critical)

### Connection Types
- `use_instrument('key')` context manager: opens VISA resource, yields it, closes on exit (holds per-key lock)
- `use_driver('key', factory)` context manager: creates driver via factory, yields it, closes on exit (holds per-key lock)
- `shared_resource('key')`: for SOCKET instruments (GPP, NGI, MDO, DMM) returns a **persistent** singleton connection (stays open, auto-reconnects on error); for INSTR instruments returns stateless open/close per call
- `shared_driver('key', factory)`: stateless driver wrapper (open/close per call)
- `invalidate_shared(key)`: closes persistent SOCKET connection (called on Library connect/disconnect)
- **SOCKET instruments MUST use `shared_resource`** for repeated access (1s polling etc.) — raw TCP cannot handle rapid open/close cycles (TIME_WAIT exhaustion → firmware lockup)

### Which instrument uses what
| Key | Runner commands | Runner measurements | Dashboard live | Keepalive |
|---|---|---|---|---|
| itech | `shared_driver` | `shared_driver` | `shared_driver` | `shared_driver` |
| gpp | `shared_resource` | `shared_resource` | `shared_resource` | `shared_resource` |
| gdm | `shared_resource` | `shared_resource` | `shared_resource` | `shared_resource` |
| dmm | `use_instrument` | `use_instrument` | `use_instrument` | `open_resource` |
| mdo | `shared_driver` | `shared_driver` | — | `open_resource` |
| wt5000 | `shared_driver` | `shared_driver` | — | `open_resource` |
| ngi | `shared_resource` | `shared_resource` | — | `open_resource` |
| espec | `use_instrument` | — | — | `open_resource` |

### Session Management
- SOCKET instruments (GPP, NGI, MDO, DMM) use persistent singleton connections via `_PersistentResourceConn`
- INSTR instruments (ITECH, WT5000, ESPEC, GDM) use stateless open/close per call
- Per-key `threading.Lock` serialises all VISA access to the same instrument
- Library panel holds its own driver (`_drivers` dict) when user clicks Connect
- `invalidate_shared(key)` on Library connect/disconnect closes the persistent connection to avoid dual-session conflicts
- Keepalive has 30s per-key cooldown (`_ka_err_until`) after failures

## Test Lifecycle (Sweep & Sequence)

1. **Pre-flight**: `check_instruments(keys)` verifies addresses; EDDIE warning if enabled but no device
2. **Start**: `test_running.emit(True)` -> DashboardTab pauses keepalive + live readings
3. **Run**: Runner thread uses `shared_driver`/`shared_resource` wrappers (open/close per call); GPP commands batched in single `shared_resource('gpp').run(fn)` call
4. **End**: `test_running.emit(False)` -> keepalive + live readings resume

## Instrument Registry

`instrument_registry.py` `CATEGORIES` dict. Adding a new instrument:
1. Add entry to `CATEGORIES` with `panel_cls`, `addr_hint`, `ka_label`
2. Create panel in `panels/_monolith.py` (or new file under `panels/`)
3. Re-export in `panels/__init__.py` and add to `app.py` `_panel_classes`

Auto-generates: AddressesTab form, keepalive indicators, instrument tab map.

## Key Classes

### workers.py
- `use_instrument(key)` / `use_driver(key, factory)`: context managers for VISA access (open/close per call, per-key lock)
- `shared_resource(key)` / `shared_driver(key, factory)`: return wrappers with `.run(fn)` API (open/close per call internally)
- `InstrumentWorker(QThread)`: runs `fn()` in thread, emits `result(value, err_str)`
- Per-key `threading.Lock` (`_instr_lock`) serialises all VISA access

### test_automation.py
- `GWGPP4323(res)`: full GPP driver — measure, output, OVP/OCP, tracking, load modes (`set_load_mode`, `set_load_resistance`)
- `ITechFixed(addr)`: IT6005C driver (tolerates firmware err 170)
- `MDO34Driver(addr)`: MDO34 scope — `measure_immediate`, `capture_waveform`, DVM
- `WT5000Driver(addr)`: Yokogawa WT5000 — `measure_element`, `read_numeric`, integration
- `ADDR` dict: global key->VISA address; `'XXX'` = unconfigured

### panels/_monolith.py
- `InstrumentLibraryPanel`: `_connect_key(key)`, `_disconnect_key(key)`, `connected_keys()`, signals `instrument_connected(str, obj)` / `instrument_disconnected(str)`
- `WT5000Panel`: `_int_poll` 2s QTimer -> `_poll_int_state()` (auto-starts)
- `GppPanel`: per-channel tabs + overview + protection tab (`get_protection_config()`)

## GPP Mode Control
- CH1/CH2 support Source and Load (CV/CC/CR) modes; CH3/CH4 always Source
- Unified across all surfaces: Mode combo + V/I fields
- `set_load_mode(ch, mode)` only sends ON for target mode (GPP auto-disables others)
- V = voltage (Source/CV), resistance (CR); I = current limit (Source/CV), setpoint (CC)

## Sequence Panel
- Post-test state = first row of table (no separate config)
- Column group toggles: ITECH, GPP CH1-4, Tracking, ESPEC
- GPP CH1/CH2 columns include Mode combo
- Measurement functions: `_meas_itech_v/i` (shared_driver), `_meas_gpp_v/i` (shared_resource), `_meas_gdm` (shared_resource), `_meas_dmm` (use_instrument), `_meas_mdo` (shared_driver), `_build_wt5000_meas` (shared_driver)

## Sweep Panel
- Mirror of sequence measurement patterns
- `steps_exported` signal -> sequence panel `load_steps`
- NGI cell voltage sweep support (16 cells via shared_resource)

## Logging
- Namespace: `ats.*` (ats.app, ats.sweep, ats.sequence, ats.workers, ats.panels)
- Global exception hooks: `sys.excepthook` + `qInstallMessageHandler`
- Runners route UI log through `_log`/`_emit` -> file log (DEBUG for normal, WARNING for errors)
- Usage: `from app_logger import get_logger; _log = get_logger("mymodule")`

## NGI N83624 Cell Simulator
- 17 channels: CH1-8 (cells 1-8), CH9 (busbar), CH10-17 (cells 9-16)
- TCP port 7000 SOCKET; system commands need front panel/RS232
