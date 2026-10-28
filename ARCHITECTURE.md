# App Architecture — Automated Instrument Test Suite

## Module Map

```
app.py                  Entry point, MainWindow, DashboardTab, AddressesTab
├── panels.py           All instrument control panels + PlotPanel + ScpiTerminal
├── sweep_panel.py      Sweep tab — automated multi-instrument parametric sweep
├── sequence_panel.py   Sequence tab — step-by-step manual test runner
├── eddie_panel.py      EDDIE Device tab — PCU register read/write + watchlist
├── daq_espec_panel.py  DAQ 973A + ESPEC SH-662 panels
├── workers.py          Connection layer — persistent resources, drivers, threads
├── test_automation.py  ADDR dict, instrument drivers (GWGPP4323, GWInstekGDM8261A,
│                       ITechFixed, WT5000Driver), SCPI helpers, sweep config
├── persist.py          UI state snapshot/restore → panel_state.json
└── instrument_registry.py  InstrumentLibraryPanel — connect/disconnect registry
```

---

## Tabs & Panels

```
MainWindow (QMainWindow)
│
├── 📈  Sweep          SweepPanel (sweep_panel.py)
├── 📋  Sequence       SequencePanel (sequence_panel.py)
├── 📊  Dashboard      DashboardTab (app.py)
├── 🔌  EDDIE Device   EddiePanel (eddie_panel.py)
├── 🔧  Instruments    QTabWidget (hidden tabs, shown on connect)
│   ├── ITECH IT6005C  ItechPanel
│   ├── GPP-4323       GppPanel
│   ├── GDM-8261A      GdmPanel
│   ├── Keysight 34461A DmmPanel
│   ├── Tektronix MDO34 MdoPanel
│   ├── DAQ 973A       KeysightDAQ973APanel
│   ├── ESPEC SH-662   EspecSH662Panel
│   ├── Yokogawa WT5000 WT5000Panel
│   └── 📚 Instrument Library  InstrumentLibraryPanel
└── 🌐  Addresses & Terminal
    ├── AddressesTab   (VISA address config + port scan)
    └── ScpiTerminal   (raw SCPI command prompt)
```

---

## Connection Layer (`workers.py`)

```
                     ┌───────────────────────────────────┐
                     │         workers.py                │
                     │                                   │
  _INSTR_LOCKS ──────┤  per-key threading.Lock()         │
                     │  (serialises all raw-VISA access) │
                     │                                   │
  PersistentResource ┤  key → single pyvisa resource     │
  (raw VISA)         │  • open on first run()            │
                     │  • reconnect on error             │
                     │  • on_connect hook (e.g. *CLS)    │
                     │                                   │
  PersistentDriver   ┤  factory → enphase_equipment drv  │
  (driver owns VISA) │  • ITechFixed, Agilent34401A, …   │
                     │  • reconnect on error             │
                     │                                   │
  shared_resource(k) ┤  process-wide singleton           │
                     │  PersistentResource per key       │
                     │                                   │
  shared_driver(k,f) ┤  process-wide singleton           │
                     │  PersistentDriver per key         │
                     │                                   │
  InstrumentWorker   ┤  QThread wrapper for any callable │
                     │  → emits result(val, err) signal  │
                     │                                   │
  SweepWorker        ┤  QThread for full sweep execution │
                     │  → emits progress, log, done      │
                     └───────────────────────────────────┘
```

### Connection singletons per instrument

| Key      | Type                | Driver / Protocol           | on_connect |
|----------|---------------------|-----------------------------|------------|
| `itech`  | `PersistentDriver`  | `ITechFixed` (enphase_eq)   | *CLS via ITechFixed.enable_remote_mode |
| `gpp`    | `PersistentResource`| raw VISA (GWGPP4323 wraps)  | `*CLS`     |
| `gdm`    | `PersistentResource`| raw VISA (GWInstekGDM8261A) | `*CLS`     |
| `dmm`    | `PersistentDriver`  | `Agilent34401A` (enphase_eq)| —          |
| `mdo`    | `PersistentDriver`  | `TektronixDPO3034`          | —          |
| `wt5000` | `PersistentDriver`  | `WT5000Driver`              | —          |
| `daq`    | `PersistentResource`| raw VISA (Keysight DAQ)     | —          |
| `espec`  | `PersistentResource`| raw VISA (ESPEC RS-232)     | —          |

---

## Data Flow

### Dashboard — live readings every 1 s

```
QTimer (1 s)
  └─ DashboardTab._tick()
       ├─ _read_itech()  ──► shared_driver('itech').run(drv.measure, skip_if_busy)
       │                       └─ updates _it_v / _it_i / _it_p labels
       ├─ _read_gpp()    ──► shared_resource('gpp').run(MEAS1-4, skip_if_busy)
       │                       └─ updates _gv[ch] / _gi[ch] labels
       ├─ _read_gdm()    ──► shared_resource('gdm').run(READ?, skip_if_busy)
       │                       CONF: sent only when function changes
       │                       └─ updates _gdm_val label
       ├─ _read_dmm()    ──► use_instrument('dmm') MEAS:VOLT:DC?
       │                       └─ updates _dmm_val label
       └─ _read_eddie()  ──► EddiePanel.get_watchlist_snapshot() [no VISA]
                               └─ updates _eddie_tbl rows
```

### Keepalive — instrument status ping every 10 s

```
QTimer (10 s)  [toggleable via "Auto-ping every 10 s" checkbox]
  └─ DashboardTab._tick_keepalive()
       For each instrument key:
         ├─ itech   ──► shared_driver('itech').run(*IDN?, skip_if_busy)
         ├─ gpp     ──► shared_resource('gpp').run(*IDN?, skip_if_busy)
         ├─ gdm     ──► shared_resource('gdm').run(*IDN?, skip_if_busy)
         └─ others  ──► open_resource(key, timeout=2000) → *IDN? → close
           → _ka_done() sets ● green / ● red / ● gray per instrument
```

### Instrument Panel — continuous plot (e.g. ItechPanel)

```
PlotPanel QTimer (user-set interval, default 1 s)
  └─ PlotPanel._tick()
       └─ ItechPanel._read_all()
            └─ shared_driver('itech').run(drv.measure(), skip_if_busy=True)
                 └─ PlotPanel._on_result()
                      ├─ appends to deque per channel
                      └─ updates pyqtgraph PlotDataItem
```

### Sweep execution

```
SweepPanel "Run Sweep" button
  └─ SweepWorker (QThread)
       Per step:
         ├─ Set ITECH  ──► shared_driver('itech').run(set_volts+set_amps+on)
         ├─ Set GPP    ──► use_instrument('gpp') GWGPP4323.set_channel()
         ├─ sleep(settling_time)
         └─ Measure:
              ├─ _meas_gdm()    ──► shared_resource('gdm').run(READ?)
              │                     CONF: only on function change
              ├─ _meas_itech_v() ─► shared_driver('itech').run(ask MEAS:VOLT?)
              ├─ _meas_itech_i() ─► shared_driver('itech').run(ask MEAS:CURR?)
              ├─ _meas_gpp_v/i() ─► use_instrument('gpp') MEAS?
              ├─ _meas_dmm()    ──► use_instrument('dmm') MEAS:?
              ├─ _meas_wt5000() ──► shared_driver('wt5000').run(batch read)
              └─ _meas_daq()    ──► daq_scan() via DAQ panel reference
       └─ SweepWorker.done signal → save .xlsx, turn outputs off
```

### Sequence execution

```
SequencePanel "Run" button
  └─ SequenceRunner (QThread)  [identical data-flow to SweepWorker]
       Per row in the step table:
         ├─ ITECH ON/OFF  ──► shared_driver('itech').run(set+on/off)
         ├─ GPP channels  ──► use_instrument('gpp') set_channel + enable
         ├─ sleep(dwell)
         └─ Measurements  ──► same callables as SweepWorker
       └─ Excel export on finish
```

### SCPI Terminal

```
User types command → ScpiTerminal._send()
  └─ selected instrument via shared_resource(key).run(query/write)
       └─ response displayed in terminal log
```

---

## Signal / Event Wiring

```
InstrumentLibraryPanel
  instrument_connected(key, driver)  ──► MainWindow._on_lib_connected(key)
  │                                        └─ inst_tabs.setTabVisible(idx, True)
  instrument_connected(key, driver)  ──► SweepPanel.notify_instrument_connected(key)
  │                                        └─ shows/hides WT5000 measurement inputs
  instrument_connected(key, driver)  ──► SequencePanel.notify_instrument_connected(key)
  instrument_disconnected(key)       ──► MainWindow._on_lib_disconnected(key)
  │                                        └─ inst_tabs.setTabVisible(idx, False)
  instrument_disconnected(key)       ──► SweepPanel.notify_instrument_disconnected(key)
  instrument_disconnected(key)       ──► SequencePanel.notify_instrument_disconnected(key)

SweepPanel
  steps_exported(list[dict])  ──► SequencePanel.load_steps()
                                    └─ fills step table from sweep matrix

EddiePanel
  (reference passed to DashboardTab, SweepPanel, SequencePanel)
  └─ get_watchlist_snapshot() polled by Dashboard._read_eddie() every 1 s
```

---

## State Persistence

| File                        | Contents                                  | When saved/loaded        |
|-----------------------------|-------------------------------------------|--------------------------|
| `panel_state.json`          | All widget values (spinboxes, checkboxes, combos, lineedits) | `closeEvent` / `__init__` |
| `instrument_addresses.json` | ADDR dict (VISA addresses)                | Addresses tab Save / startup |
| `app_settings.json`         | Main tab order                            | tab drag / startup       |
| `instrument_config.json`    | Instrument Library connections            | Library panel            |
| `eddie_watchlist.json`      | EDDIE watchlist register list             | EDDIE panel              |

---

## Instrument Drivers

```
enphase_equipment library (external)
  ├── ITech600x          → ITECH IT6005C  (ask/write auto-check SYSTEM:ERROR?)
  ├── Agilent34401A      → Keysight 34461A
  ├── TektronixDPO3034   → Tektronix MDO34
  └── YokogawaWT5000     → WT5000 (via WT5000Driver wrapper in test_automation.py)

Custom drivers (test_automation.py)
  ├── ITechFixed(ITech600x)  — subclass: sends *CLS before enable_remote_mode
  │                            fixes stale SCPI error-queue startup failures
  ├── GWGPP4323              — wraps raw pyvisa resource; SCPI for GPP-4323
  └── GWInstekGDM8261A       — wraps raw pyvisa resource; full GDM-8261A SCPI

Custom drivers (daq_espec_panel.py)
  ├── Keysight DAQ 973A     — channel scan, temperature measurement
  └── ESPEC SH-662          — temperature/humidity chamber RS-232 control
```

---

## Lock & Concurrency Model

```
All VISA access is serialised per-instrument via the PersistentResource /
PersistentDriver internal threading.Lock.

Priority model (skip_if_busy):
  HIGH   — sweep/sequence runner    — blocks until lock free (blocking=True)
  MEDIUM — panel plot timers        — skip if busy          (skip_if_busy=True)
  LOW    — dashboard readings       — skip if busy          (skip_if_busy=True)
  LOW    — keepalive pings          — skip if busy          (skip_if_busy=True)

→ Running a sweep never starves the UI thread; panel plots and dashboard
  silently show "—" while the sweep holds the instrument.
→ Two concurrent VISA sessions to the same TCP instrument are impossible
  because all callers share the same singleton resource object.
```

---

## Key Features per Tab

### 📈 Sweep
- Linear / decade step generation for ITECH (V + I) and GPP-4323 (4 ch V + I)
- Configurable measurement inputs: GDM, DMM, ITECH readback, GPP readback, WT5000, DAQ
- N readings per step, step interval, dwell time preview
- Background `SweepWorker` thread — UI stays responsive
- Interrupt button stops mid-sweep cleanly
- Export steps → Sequence tab
- Excel (.xlsx) output with timestamps

### 📋 Sequence
- Table-based step editor (add, delete, duplicate, reorder rows)
- Each row: ITECH setpoint, GPP per-channel setpoint, dwell, measurement selector
- Import from Sweep export or load `.xlsx`
- Background `SequenceRunner` thread
- Per-row result colouring (pass/fail threshold)
- Excel export with per-step measurements

### 📊 Dashboard
- Live readings every 1 s: ITECH V/I/P, GPP 4-ch V/I, GDM value, DMM value, EDDIE watchlist
- **Keepalive**: auto-ping every 10 s (toggleable checkbox + "Ping Now" button)
  - ● green = responsive, ● red = error, ● gray = not configured
- Live readings start/stop toggle

### 🔌 EDDIE Device
- PCU (Power Control Unit) TCP connection
- Read/write individual registers
- Watchlist: continuous polling of selected registers
- Snapshot fed to Dashboard live table

### 🔧 Instruments (per-instrument panel features)
| Panel | Features |
|-------|---------|
| ItechPanel | V/I set, output ON/OFF, OVP/OCP, mode SOURCE/SINK, live plot V+I+P |
| GppPanel | Per-channel V/I set + ON/OFF, all-OFF, live plot per-ch and all-ch |
| GdmPanel | Front-panel style function buttons, range/rate/filter, math (REL/Stats/dBm), live plot |
| DmmPanel | Function config, NPLC, range, zero, live plot |
| MdoPanel | Channel config, scalar measurement, live plot |
| WT5000Panel | Element select, function select, live batch measurement |
| DAQ973APanel | Channel scan, thermocouple/voltage measurement |
| EspecSH662Panel | Temp/humidity setpoint, program run/stop |
| ScpiTerminal | Raw SCPI send/receive to any instrument |
