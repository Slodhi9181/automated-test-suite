# Automated Test Suite — Agent Rules

## Context
Read `help.md` at project root before making any changes. It contains the full architecture, connection patterns, and design decisions. **After any code change, update `help.md`** to reflect what changed (add/remove/modify entries). Keep `help.md` under 200 lines.

## Run
```
python app.py
```

## Build / Distribute
```
pip install pyinstaller
build.bat
```
Output: `dist\AutomatedTestSuite\` — zip the folder to share. No Python needed on target machines.

## Architecture (Quick Reference)

**PyQt6 desktop app** controlling lab instruments via NI-VISA (pyvisa). Sidebar nav + QStackedWidget pages. All VISA access serialised through `workers.py`.

### File Map
| File | Role |
|---|---|
| `app.py` | MainWindow, DashboardTab (live readings + keepalive), AddressesTab, E-STOP, signal wiring |
| `workers.py` | use_instrument/use_driver context managers, shared_resource (persistent for SOCKET, stateless for INSTR), InstrumentWorker QThread |
| `test_automation.py` | SCPI drivers (ITechFixed, GWGPP4323, MDO34Driver, WT5000Driver, GDM8261A), ADDR dict |
| `instrument_registry.py` | Single source of truth for all instruments — drives Library panel, addresses, keepalive |
| `sequence_panel.py` | Step-table test runner (SequenceRunner QThread) |
| `sweep_panel.py` | Parametric sweep runner (SweepRunner QThread) |
| `panels/_monolith.py` | All instrument panels + InstrumentLibraryPanel (connect/disconnect) |
| `ui_helpers.py` | TH (Catppuccin Mocha theme), _btn(), StatusLabel |
| `app_logger.py` | `APP_DIR`/`BUNDLE_DIR` path resolution, rotating logger, `get_logger(name)` |
| `app.spec` | PyInstaller build config |
| `build.bat` | One-click build script |
| `persist.py` | Auto-save/restore widget state to `panel_state.json` |

### Critical Rules
- **VISA connections**: SOCKET instruments (GPP, NGI, MDO, DMM) use **persistent** connections via `shared_resource()`. INSTR instruments use stateless open/close. **NEVER use `use_instrument()` in 1s polling loops for SOCKET instruments** — causes TCP lockup.
- **Per-key locking**: `_instr_lock(key)` serialises all VISA access per instrument. No two threads touch the same instrument at the same time.
- **invalidate_shared(key)**: closes persistent SOCKET connections. Called on Library connect/disconnect to avoid dual sessions.
- **Test lifecycle**: `test_running` signal from Sweep/Sequence panels pauses keepalive and live readings.
- **Theme**: Use `TH.BLUE`, `TH.GREEN` etc. from `ui_helpers.py`. Never hardcode hex colors.
- **File paths**: Use `APP_DIR` from `app_logger.py` for all runtime data files (JSON configs, logs, results). Never use `Path(__file__).parent` for data files — it breaks in PyInstaller bundles.
- **No comments added/removed** unless explicitly asked.
- **Git updates**: This project is version-controlled (branch `main`, hosted on GitHub). After completing any task that changes tracked files, **ask the user** whether to commit and push the changes to git before doing so. Never commit or push without explicit approval.
