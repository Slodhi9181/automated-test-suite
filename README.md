# Automated Instrument Sweep Script

Cycles through every combination of source outputs and captures measurements from instruments, saving results to a timestamped CSV. Features both CLI and web UI interfaces.

---

## Instruments

| Role        | Instrument                        | Interface       |
|-------------|-----------------------------------|-----------------|
| Source 1    | ITECH IT6005C-80-150 (Bi-dir PSU) | USB / LAN / RS-232 |
| Source 2    | GW INSTEK GPP-4323 (4-ch PSU)    | USB / LAN / RS-232 |
| Measure 1   | Tektronix MDO34 (Oscilloscope)    | USB / LAN / GPIB  |
| Measure 2   | Keysight 34461A (DMM)             | USB / LAN / GPIB  |
| Measure 3   | GW Instek GDM-8261A (DMM)         | USB / LAN / GPIB  |

---

## Prerequisites

### 1. Python 3.8 or later
Download from https://www.python.org/downloads/

### 2. VISA backend
Install **one** of the following before the Python packages:

- **NI-VISA** (recommended) — https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html  
- **Keysight IO Libraries Suite** — https://www.keysight.com/find/iosuite  
- **pyvisa-py only** (no driver install, limited USB support) — pure-Python fallback included in requirements.txt

### 3. Python packages
```
pip install -r requirements.txt
```

---

## Usage Options

### Option 1: Web UI (Recommended)
Start the web interface for a graphical dashboard:
```
python web_ui.py
```
Then open http://localhost:5000 in your browser.

**Web UI Features:**
- Real-time sweep progress and measurements
- Interactive configuration editor
- Instrument connection status
- CSV result download
- Live status updates via WebSocket
- Sweep start/stop controls

### Option 2: Command Line Interface
Run sweeps directly from the command line:
```
# Run with all instruments
python test_automation.py

# Run with specific instruments only
python test_automation.py --instruments itech gpp dmm

# List available VISA resources
python test_automation.py --list-resources
```

---

## Web UI Setup

### Step 1 — Install dependencies
```powershell
pip install -r requirements.txt
```
This includes Flask and Flask-SocketIO for the web interface.

### Step 2 — Discover VISA addresses
```
python test_automation.py --list-resources
```
This prints every detected resource and its `*IDN?` response.

### Step 3 — Set addresses in the script
Open `test_automation.py` and replace the placeholder strings in the `ADDR` dict (Section 1) with the addresses discovered in Step 2.

### Step 4 — Start the web UI
```powershell
python web_ui.py
```
Open http://localhost:5000 in your browser.

### Step 5 — Configure via web interface
- Use the **Configuration** page to set sweep parameters
- Discover instruments automatically via the web interface
- Save configurations for later use
- Start sweeps with instrument selection

---

## CLI Setup (Traditional)

## Instrument Selection

Both CLI and web UI support selective instrument usage:

### CLI Selection
```bash
# Use only ITECH and GPP-4323
python test_automation.py --instruments itech gpp

# Use only measurement instruments
python test_automation.py --instruments mdo dmm gdm

# Use all instruments (default)
python test_automation.py
```

### Web UI Selection
In the web UI, click "Start Sweep" and select which instruments to include in the modal.

Available instrument keys:
- `itech` - ITECH IT6005C-80-150
- `gpp` - GW INSTEK GPP-4323
- `mdo` - Tektronix MDO34
- `dmm` - Keysight 34461A
- `gdm` - GW Instek GDM-8261A

---

## Output

Each row in the CSV corresponds to one combination of source settings and contains:

| Column group              | Description                                        |
|---------------------------|----------------------------------------------------|
| `step`, `timestamp`       | Step number and wall-clock time                    |
| `itech_*_set_*`           | ITECH programmed setpoints                         |
| `itech_*_meas_*`          | ITECH back-panel voltage / current readings        |
| `gpp_ch<n>_*_set_*`       | GPP-4323 programmed setpoints per channel          |
| `gpp_ch<n>_*_meas_*`      | GPP-4323 back-panel readings per channel           |
| `mdo_<ch>_<type>`         | Oscilloscope scalar measurements                   |
| `dmm_<function>`          | Keysight 34461A measurements                       |
| `gdm_<function>`          | GW Instek GDM-8261A measurements                  |

---

## Web UI Features

### Dashboard
- **Real-time Status**: Live sweep progress with percentage and elapsed time
- **Measurements Display**: Current readings from all active instruments
- **Instrument Status**: Connection status and identification for all devices
- **Results Management**: Download CSV files directly from the interface
- **Control Panel**: Start/stop sweeps with instrument selection

### Configuration Page
- **Sweep Matrix Editor**: Interactive editors for ITECH and GPP-4323 sweep points
- **Measurement Configuration**: Set up oscilloscope and multimeter measurements
- **VISA Address Management**: Discover and configure instrument connections
- **Advanced Settings**: Settling time, import/export configurations
- **Validation**: Check configuration before running sweeps

### Real-time Updates
- WebSocket-based live updates during sweep execution
- Progress bars and status indicators
- Automatic refresh of measurement displays
- Error notifications and status changes

---

## Sweep Size

```
Total steps = len(ITECH_SWEEP) × len(GPP_SWEEP)
```

With the default configuration: **6 ITECH × 4 GPP = 24 steps**.

---

## Safety Notes

- The ITECH IT6005C-80-150 can deliver up to **80 V / 150 A**.  Verify wiring and fusing before running.
- The script turns **all outputs OFF** on exit, including on `Ctrl+C` or errors.
- Keep `SETTLING_TIME` (default 0.5 s) long enough for your load to stabilise before measurements are taken.
- For `CURR:DC` on the 34461A, ensure the test leads are in the **current input terminals** (max 3 A on fused terminal, 10 A on unfused).
