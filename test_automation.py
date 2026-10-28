"""
Automated Test Sweep Script
============================
Sources  : ITECH IT6005C-80-150  (Bi-directional DC Power Supply)
           GW INSTEK GPP-4323    (4-Channel Programmable DC Power Supply)
Measures : Tektronix MDO34       (Mixed Domain Oscilloscope)
           Keysight 34461A       (6.5-Digit Digital Multimeter)
           GW Instek GDM-8261A  (6.5-Digit Digital Multimeter)

Iterates over every combination of ITECH × GPP-4323 output settings,
applies them, waits for settling, captures measurements from both
instruments, and writes all results to a timestamped CSV file.
"""

import pyvisa
import itertools
import csv
import time
import logging
import sys
from datetime import datetime
from pathlib import Path

# ── enphase_equipment drivers ───────────────────────────────────────────────
from enphase_equipment.dc_source.itech        import ITech600x
from enphase_equipment.multi_meter.agilent    import Agilent34401A
from enphase_equipment.oscilloscope.tektronix import TektronixDPO3034
from enphase_equipment.thermal_chamber.espec  import Espec
from enphase_equipment.power_meter.yokogawa   import YokogawaWT5000

class ITechFixed(ITech600x):
    """ITech600x that tolerates the ITECH IT6005C firmware quirk where the
    instrument returns error 170 (Invalid command) after receiving SYSTem:REM.

    Root cause: some ITECH IT6005C firmware versions do not support SYSTem:REM
    and immediately push error 170 into the error queue.  The base-class
    enable_remote_mode() then checks SYST:ERR?, reads that 170, and raises —
    aborting the connection even though the instrument is otherwise fully
    operational.

    Fix: send *CLS before AND after the base-class call.  If the base-class
    raises and the message contains '170' or 'invalid command', swallow the
    exception (the instrument is already in remote mode or ignores the command
    gracefully) and continue.  Any other exception is re-raised.
    """
    def enable_remote_mode(self, enabled: bool = True):
        try:
            self.visa.write('*CLS')   # drain any stale pre-existing errors
        except Exception:
            pass
        try:
            super().enable_remote_mode(enabled)
        except Exception as exc:
            msg = str(exc).lower()
            if '170' in msg or 'invalid command' in msg:
                # SYSTem:REM not recognised by this firmware — clear the queue
                # and proceed; instrument works fine without the command.
                try:
                    self.visa.write('*CLS')
                except Exception:
                    pass
            else:
                raise


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — VISA RESOURCE ADDRESSES
#  Run the discovery helper at the bottom of this file to find your addresses:
#      python test_automation.py --list-resources
# ═══════════════════════════════════════════════════════════════════════════════
ADDR = {
    "itech": "TCPIP::192.168.1.XXX::INSTR",   # ITECH IT6005C-80-150  — replace XXX with actual IP
    "gpp":   "TCPIP::192.168.1.XXX::INSTR",   # GW INSTEK GPP-4323   — replace XXX with actual IP
    "mdo":   "TCPIP::10.73.77.136::4000::SOCKET",  # Tektronix MDO34 — port 4000 SCPI socket
    "dmm":   "TCPIP::192.168.1.XXX::INSTR",   # Keysight 34461A      — replace XXX with actual IP
    "gdm":   "ASRL5::INSTR",                   # GW Instek GDM-8261A (USB)
    "daq":   "TCPIP::192.168.1.XXX::INSTR",   # Keysight DAQ 973A    — replace XXX with actual IP
    "espec": "ASRL6::INSTR",                   # ESPEC SH-662         — RS-232 (adjust COM port)
    "wt5000": "TCPIP::10.73.92.108::INSTR",    # Yokogawa WT5000
    "ngi":    "TCPIP::10.73.77.200::7000::SOCKET",   # NGI N83624 Cell Simulator (TCP port 7000)
}

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — SWEEP MATRIX
#  Edit these lists to define your test space.
#  Total steps = len(ITECH_SWEEP) × len(GPP_SWEEP)
# ═══════════════════════════════════════════════════════════════════════════════

# ITECH IT6005C-80-150 sweep points
# Tuple: (voltage_V, current_limit_A, mode)
#   mode = "SOURCE" → supply power to load  (positive current out)
#   mode = "SINK"   → absorb power from DUT (regenerative / electronic load)
ITECH_SWEEP = [
    ( 0,   0,  "SOURCE"),   # baseline / off
    (12,   5,  "SOURCE"),
    (24,  10,  "SOURCE"),
    (48,  20,  "SOURCE"),
    (12,   5,  "SINK"),
    (24,  10,  "SINK"),
]

# GW INSTEK GPP-4323 sweep points
# Each entry is a full 4-channel config: {ch: (voltage_V, current_A, enable)}
GPP_SWEEP = [
    # CH1 only
    {1: (5.0,  1.0, True),  2: (0.0, 0.5, False), 3: (0.0, 0.5, False), 4: (0.0, 0.5, False)},
    # CH1 + CH2
    {1: (5.0,  1.0, True),  2: (12.0, 1.0, True), 3: (0.0, 0.5, False), 4: (0.0, 0.5, False)},
    # CH1 + CH2 + CH3
    {1: (5.0,  1.0, True),  2: (12.0, 1.0, True), 3: (3.3, 0.5, True),  4: (0.0, 0.5, False)},
    # All four channels
    {1: (5.0,  1.0, True),  2: (12.0, 1.0, True), 3: (3.3, 0.5, True),  4: (15.0, 0.5, True)},
]

# Seconds to wait after applying outputs before reading measurements
SETTLING_TIME = 0.5

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — MEASUREMENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Tektronix MDO34 — (oscilloscope_channel, measurement_type) pairs
# Supported types: MEAN, RMS, FREQuency, PK2pk, MINimum, MAXimum, RISE, FALL
MDO_MEAS_CONFIG = [
    ("CH1", "MEAN"),
    ("CH1", "RMS"),
    ("CH1", "FREQuency"),
    ("CH2", "MEAN"),
    ("CH2", "RMS"),
]

# Keysight 34461A — SCPI function strings
# Supported: VOLT:DC, VOLT:AC, CURR:DC, CURR:AC, RES, FREQ, DIOD, CONT
DMM_MEAS_CONFIG = [
    "VOLT:DC",
    "CURR:DC",
]

# GW Instek GDM-8261A — SCPI function strings
# Supported: VOLT:DC, VOLT:AC, CURR:DC, CURR:AC, RES, FREQ, DIOD, CONT
GDM_MEAS_CONFIG = [
    "VOLT:DC",
    "CURR:DC",
]

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — SCPI ERROR HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

class ScpiError(RuntimeError):
    """Raised when an instrument returns a non-zero SYST:ERR? response."""


_SCPI_RESOLUTIONS = {
    "undefined header":      "Command syntax is wrong for this firmware version. "
                             "Check the programming manual (e.g. VOLT1 not CH1:VOLT).",
    "parameter not allowed": "Invalid parameter for this command — check value format and units.",
    "data out of range":     "Value is outside the allowed range for this instrument setting.",
    "command protected":     "Instrument is in local mode or protected. "
                             "Press LOCAL on the front panel to return to remote control.",
    "execution error":       "Hardware or state error — check connections, "
                             "then reset the instrument with *RST.",
    "queue overflow":        "Error queue overflowed (too many unread errors). "
                             "Send *CLS to clear the queue.",
    "illegal parameter":     "Invalid parameter value — check units and format.",
    "not allowed":           "Operation not allowed in the current instrument state.",
    "hardware missing":      "Required hardware option is not installed in this instrument.",
    "settings conflict":     "Conflicting settings — review the instrument configuration.",
    "trigger ignored":       "Trigger command ignored — ensure instrument is in REM mode.",
}


def _scpi_resolution(err_str: str) -> str:
    """Return a human-readable resolution hint for a SCPI error string."""
    low = err_str.lower()
    for key, hint in _SCPI_RESOLUTIONS.items():
        if key in low:
            return hint
    return "Check the instrument front panel for alerts and consult the programming manual."


def check_error(res, context: str = "", timeout_ms: int = 500) -> None:
    """
    Query SYST:ERR? and raise ScpiError if the instrument reports an error.

    Uses a short timeout (default 500 ms) so that instruments which do not
    support SYST:ERR? fail silently without blocking the UI.
    """
    old = getattr(res, 'timeout', 5000)
    try:
        res.timeout = timeout_ms
        err = res.query("SYST:ERR?").strip()
    except Exception:
        return  # instrument doesn't support SYST:ERR?, or timed out — skip
    finally:
        try:
            res.timeout = old
        except Exception:
            pass

    low = err.lower()
    is_ok = (
        "no error" in low
        or err in ("0", "+0")
        or err.startswith("0,")
        or err.startswith("+0,")
    )
    if not is_ok:
        hint = _scpi_resolution(err)
        prefix = f"[{context}] " if context else ""
        raise ScpiError(
            f"{prefix}Instrument error: {err!r}\n"
            f"  Fix: {hint}"
        )


def write_checked(res, cmd: str, context: str = "") -> None:
    """Write a SCPI command then verify no error via SYST:ERR?."""
    res.write(cmd)
    check_error(res, context or cmd)


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — INSTRUMENT DRIVERS
#
#  enphase_equipment provides drivers for:
#    ITech600x       (ITECH IT6005C)      dc_source.itech
#    Agilent34401A   (Keysight 34461A)    multi_meter.agilent
#    TektronixDPO3034 (MDO34)             oscilloscope.tektronix
#    Espec           (ESPEC SH-662)       thermal_chamber.espec
#
#  Custom drivers (not in enphase_equipment) are kept below:
#    GWGPP4323, GWInstekGDM8261A, and their Adapters
# ═══════════════════════════════════════════════════════════════════════════════


class GWGPP4323:
    """Full SCPI driver for GW INSTEK GPP-4323 4-Channel Programmable DC Power Supply.

    Implements the complete command set from GPP-Series User Manual Rev G (2024):
        Measurement   : per-channel VOLTage/CURRent/POWEr, ALL?, VOUT/IOUT shortcuts
        Output        : per-channel ON/OFF, ALLOUTON/ALLOUTOFF, OVP, OCP
        Source        : SOURce voltage/current setpoints + readback, CC-limit state
        Tracking      : TRACK (independent/series/parallel), SERies/PARallel mode
        Display       : ENABle, BRIGhtness, TYPE (1–7)
        System        : beeper, save/recall (0–9), error query, remote/local, version
        IEEE 488.2    : *IDN?, *RST, *CLS, *SAV, *RCL, *OPC?, *STB?
    """

    CHANNELS          = (1, 2, 3, 4)
    MAX_VOLT          = 32.0    # V — GPP-4323 all 4 channels
    MAX_CURR          = 3.2     # A — GPP-4323 all 4 channels
    BRIGHTNESS_LEVELS = ["Low", "Middle", "High"]

    def __init__(self, resource):
        self._r                   = resource
        self._r.timeout           = 5000
        self._r.write_termination = "\n"
        self._r.read_termination  = "\n"
        self._r.write("*CLS")

    # ── IEEE 488.2 / System ──────────────────────────────────────────────────

    def identify(self) -> str:
        return self._r.query("*IDN?").strip()

    def reset(self):
        self._r.write("*RST"); self._r.write("*CLS"); time.sleep(0.5)

    def clear(self):
        self._r.write("*CLS")

    def save_state(self, n: int):
        """Save current configuration to internal memory slot n (0–9).  :*SAV n"""
        self._r.write(f"*SAV {n}")

    def recall_state(self, n: int):
        """Recall configuration from internal memory slot n (0–9).  :*RCL n"""
        self._r.write(f"*RCL {n}"); time.sleep(0.15)

    def get_error(self) -> str:
        """:SYSTem:ERRor? — returns instrument error string."""
        return self._r.query(":SYSTem:ERRor?").strip()

    def get_version(self) -> str:
        """:SYSTem:VERSion? — returns SCPI version string."""
        return self._r.query(":SYSTem:VERSion?").strip()

    def set_beeper(self, enable: bool):
        """:SYSTem:BEEPer:STATe {ON|OFF}"""
        self._r.write(f":SYSTem:BEEPer:STATe {'ON' if enable else 'OFF'}")

    def get_beeper(self) -> bool:
        return self._r.query(":SYSTem:BEEPer:STATe?").strip() in ("1", "ON")

    def set_remote(self):
        """:SYSTem:REMote — enter remote control mode."""
        self._r.write(":SYSTem:REMote")

    def set_local(self):
        """:SYSTem:LOCal — return to local (front panel) mode."""
        self._r.write(":SYSTem:LOCal")

    # ── Display ─────────────────────────────────────────────────────────────

    def set_display(self, enable: bool):
        """:DISPlay:ENABle {ON|OFF}"""
        self._r.write(f":DISPlay:ENABle {'ON' if enable else 'OFF'}")

    def get_display(self) -> bool:
        return self._r.query(":DISPlay:ENABle?").strip() in ("1", "ON")

    def set_brightness(self, level: str):
        """:DISPlay:BRIGhtness {Low|Middle|High}"""
        self._r.write(f":DISPlay:BRIGhtness {level}")

    def get_brightness(self) -> str:
        return self._r.query(":DISPlay:BRIGhtness?").strip()

    def set_display_type(self, t: int):
        """:DISPlay:TYPE {1..7} — select display layout."""
        self._r.write(f":DISPlay:TYPE {t}")

    def get_display_type(self) -> int:
        return int(self._r.query(":DISPlay:TYPE?").strip())

    # ── Output state ────────────────────────────────────────────────────────

    def output_on(self, ch: int):
        """:OUTPut{ch}:STATe ON"""
        self._r.write(f":OUTPut{ch}:STATe ON")

    def output_off(self, ch: int):
        """:OUTPut{ch}:STATe OFF"""
        self._r.write(f":OUTPut{ch}:STATe OFF")

    def enable_channel(self, ch: int, enable: bool):
        """:OUTPut{ch}:STATe {ON|OFF}"""
        self._r.write(f":OUTPut{ch}:STATe {'ON' if enable else 'OFF'}")

    def get_output_state(self, ch: int) -> bool:
        """:OUTPut{ch}:STATe? — True = output is ON."""
        return self._r.query(f":OUTPut{ch}:STATe?").strip() in ("1", "ON")

    def all_outputs_on(self):
        """:ALLOUTON — turn all four channel outputs ON."""
        self._r.write(":ALLOUTON")

    def all_outputs_off(self):
        """:ALLOUTOFF — turn all four channel outputs OFF."""
        self._r.write(":ALLOUTOFF")

    # ── OVP (Over-Voltage Protection) ────────────────────────────────────────

    def set_ovp(self, ch: int, voltage: float):
        """:OUTPut{ch}:OVP <value> — set OVP trip level in volts."""
        self._r.write(f":OUTPut{ch}:OVP {voltage:.4f}")

    def get_ovp(self, ch: int) -> float:
        """:OUTPut{ch}:OVP? — query OVP trip level."""
        return float(self._r.query(f":OUTPut{ch}:OVP?").strip())

    def enable_ovp(self, ch: int, enable: bool):
        """:OUTPut{ch}:OVP:STATe {ON|OFF}"""
        self._r.write(f":OUTPut{ch}:OVP:STATe {'ON' if enable else 'OFF'}")

    def get_ovp_state(self, ch: int) -> bool:
        return self._r.query(f":OUTPut{ch}:OVP:STATe?").strip() in ("1", "ON")

    # ── OCP (Over-Current Protection) ────────────────────────────────────────

    def set_ocp(self, ch: int, current: float):
        """:OUTPut{ch}:OCP <value> — set OCP trip level in amps."""
        self._r.write(f":OUTPut{ch}:OCP {current:.4f}")

    def get_ocp(self, ch: int) -> float:
        """:OUTPut{ch}:OCP? — query OCP trip level."""
        return float(self._r.query(f":OUTPut{ch}:OCP?").strip())

    def enable_ocp(self, ch: int, enable: bool):
        """:OUTPut{ch}:OCP:STATe {ON|OFF}"""
        self._r.write(f":OUTPut{ch}:OCP:STATe {'ON' if enable else 'OFF'}")

    def get_ocp_state(self, ch: int) -> bool:
        return self._r.query(f":OUTPut{ch}:OCP:STATe?").strip() in ("1", "ON")

    # ── Source (voltage/current setpoints) ───────────────────────────────────

    def set_voltage(self, ch: int, voltage: float):
        """:SOURce{ch}:VOLTage <NRf> — set output voltage setpoint."""
        self._r.write(f":SOURce{ch}:VOLTage {voltage:.4f}")

    def get_voltage_set(self, ch: int) -> float:
        """:SOURce{ch}:VOLTage? — query voltage setpoint."""
        return float(self._r.query(f":SOURce{ch}:VOLTage?").strip())

    def set_current(self, ch: int, current: float):
        """:SOURce{ch}:CURRent <NRf> — set current limit."""
        self._r.write(f":SOURce{ch}:CURRent {current:.4f}")

    def get_current_set(self, ch: int) -> float:
        """:SOURce{ch}:CURRent? — query current limit setting."""
        return float(self._r.query(f":SOURce{ch}:CURRent?").strip())

    def get_cc_state(self, ch: int) -> bool:
        """True when channel is in constant-current (current limit reached).
        :SOURce{ch}:CURRent:LIMit:STATe? returns 1 when CC."""
        try:
            return self._r.query(
                f":SOURce{ch}:CURRent:LIMit:STATe?").strip() in ("1", "ON")
        except Exception:
            return False

    def set_channel(self, ch: int, voltage: float, current: float):
        """Program voltage setpoint and current limit for channel ch (1–4)."""
        self.set_voltage(ch, voltage)
        self.set_current(ch, current)

    def apply_config(self, config: dict):
        """Apply a full config dict: {ch: (volt, curr, enable)}."""
        for ch, (volt, curr, en) in config.items():
            self.set_channel(ch, volt, curr)
            self.enable_channel(ch, en)

    # ── Measurement ─────────────────────────────────────────────────────────

    def measure_voltage(self, ch: int) -> float:
        """:MEASure{ch}:VOLTage? — actual output voltage."""
        return float(self._r.query(f":MEASure{ch}:VOLTage?").strip())

    def measure_current(self, ch: int) -> float:
        """:MEASure{ch}:CURRent? — actual output current."""
        return float(self._r.query(f":MEASure{ch}:CURRent?").strip())

    def measure_power(self, ch: int) -> float:
        """:MEASure{ch}:POWEr? — actual output power."""
        return float(self._r.query(f":MEASure{ch}:POWEr?").strip())

    def measure_all(self, ch: int) -> dict:
        """Query V, I and P for channel ch in one :MEASure{ch}:ALL? call.
        Returns {'V': float, 'I': float, 'P': float}."""
        raw   = self._r.query(f":MEASure{ch}:ALL?").strip()
        parts = [p.strip() for p in raw.split(",")]
        try:
            v, i, p = float(parts[0]), float(parts[1]), float(parts[2])
        except (ValueError, IndexError):
            v = self.measure_voltage(ch)
            i = self.measure_current(ch)
            p = v * i
        return {'V': v, 'I': i, 'P': p}

    def measure_all_channels(self) -> dict:
        """Measure all 4 channels. Returns {ch: {'V': v, 'I': i, 'P': p}}."""
        result = {}
        for ch in self.CHANNELS:
            try:
                result[ch] = self.measure_all(ch)
            except Exception:
                result[ch] = {'V': float('nan'), 'I': float('nan'), 'P': float('nan')}
        return result

    # ── Tracking / Series / Parallel ────────────────────────────────────────

    def set_tracking_mode(self, mode: str):
        """TRACK{0|1|2}  —  0=independent, 1=series, 2=parallel (CH1/CH2)."""
        code = {"independent": 0, "series": 1, "parallel": 2}.get(mode.lower(), 0)
        self._r.write(f"TRACK{code}")

    def set_series_mode(self, enable: bool):
        """:OUTPut:SERies {ON|OFF} — CH1+CH2 series (2× voltage)."""
        self._r.write(f":OUTPut:SERies {'ON' if enable else 'OFF'}")

    def set_parallel_mode(self, enable: bool):
        """:OUTPut:PARallel {ON|OFF} — CH1+CH2 parallel (2× current)."""
        self._r.write(f":OUTPut:PARallel {'ON' if enable else 'OFF'}")

    # ── Load Mode (CH1 and CH2 only) ────────────────────────────────────────
    # The GPP-4323 supports electronic load operation on CH1 and CH2.
    # Three modes: CV (constant voltage), CC (constant current), CR (constant resistance).
    # Only one mode should be active at a time; enabling one automatically implies
    # the others are OFF (send OFF to all others before enabling the desired mode).

    def set_load_cv(self, ch: int, enable: bool):
        """:LOAD{ch}:CV {ON|OFF} — constant-voltage load mode (ch 1 or 2)."""
        self._r.write(f":LOAD{ch}:CV {'ON' if enable else 'OFF'}")

    def set_load_cc(self, ch: int, enable: bool):
        """:LOAD{ch}:CC {ON|OFF} — constant-current load mode (ch 1 or 2)."""
        self._r.write(f":LOAD{ch}:CC {'ON' if enable else 'OFF'}")

    def set_load_cr(self, ch: int, enable: bool):
        """:LOAD{ch}:CR {ON|OFF} — constant-resistance load mode (ch 1 or 2)."""
        self._r.write(f":LOAD{ch}:CR {'ON' if enable else 'OFF'}")

    def set_load_mode(self, ch: int, mode: str):
        """Enable one load mode (GPP auto-disables the others).
        mode: 'CV' | 'CC' | 'CR' | 'OFF'  (ch must be 1 or 2).

        IMPORTANT: The GPP-4323 firmware reverts load mode if you send
        :LOAD{ch}:xx OFF *after* enabling a mode. So we only send the
        single ON command for the desired mode. To return to source mode
        ('OFF'), we turn off whichever mode is currently active.
        """
        m = mode.upper()
        if m == 'CV':
            self._r.write(f":LOAD{ch}:CV ON")
        elif m == 'CC':
            self._r.write(f":LOAD{ch}:CC ON")
        elif m == 'CR':
            self._r.write(f":LOAD{ch}:CR ON")
        else:
            # OFF — disable all (order doesn't matter when turning off)
            self._r.write(f":LOAD{ch}:CV OFF")
            self._r.write(f":LOAD{ch}:CC OFF")
            self._r.write(f":LOAD{ch}:CR OFF")

    def get_mode(self, ch: int) -> str:
        """:MODE{ch}? — returns current operating mode string (CV/CC/CR)."""
        return self._r.query(f":MODE{ch}?").strip()

    def set_load_resistance(self, ch: int, ohms: float):
        """:LOAD{ch}:RESistor <NRf> — set resistance for CR mode (ch 1 or 2)."""
        self._r.write(f":LOAD{ch}:RESistor {ohms:.4f}")

    def get_load_resistance(self, ch: int) -> float:
        """:LOAD{ch}:RESistor? — query CR mode resistance."""
        return float(self._r.query(f":LOAD{ch}:RESistor?").strip())

    def set_source_resistance(self, ch: int, ohms: float):
        """:SOURce{ch}:RESistor <NRf> — set source output resistance (ch 1 or 2)."""
        self._r.write(f":SOURce{ch}:RESistor {ohms:.4f}")

    def get_source_resistance(self, ch: int) -> float:
        """:SOURce{ch}:RESistor? — query source output resistance."""
        return float(self._r.query(f":SOURce{ch}:RESistor?").strip())


# ── MDO34 / Tektronix helpers (using enphase TektronixDPO3034) ──────────────

def _mdo_measure(drv: TektronixDPO3034, channel: str, meas_type: str) -> float:
    """Immediate scalar measurement via enphase_equipment TektronixDPO3034."""
    drv.write("*CLS")
    drv.write(f"MEASUrement:IMMed:SOUrce1 {channel}")
    drv.write(f"MEASUrement:IMMed:TYPe {meas_type}")
    raw = drv.ask("MEASUrement:IMMed:VALue?").strip()
    try:
        return float(raw)
    except ValueError:
        return float("nan")


def _mdo_apply_channel(drv: TektronixDPO3034, channel: str, scale: float,
                       pos: float, coupling: str, term: str, probe: str,
                       bw: str, invert: str) -> str:
    drv.write("*CLS")
    for cmd in [
        f"{channel}:SCALE {scale}",
        f"{channel}:POSITION {pos}",
        f"{channel}:COUPLING {coupling}",
        f"{channel}:TERMINATION {term}",
        f"{channel}:PROBE:GAIN {probe}",
        f"{channel}:BANDWIDTH {bw}",
        f"{channel}:INVERT {invert}",
    ]:
        drv.write(cmd)
    return f"{channel} configured: {scale} V/div, {coupling}, {bw} BW"


class MDO34Driver:
    """Raw-SCPI driver for Tektronix 3 Series MDO oscilloscope.

    Wraps the enphase_equipment TektronixDPO3034 for connection management
    and exposes the full command set from the 3 Series MDO Programmer Manual
    (Tektronix 077-1498-06).  Designed for use with use_driver().

    Key command groups implemented:
        Acquisition (RUN/STOP/SINGLE, mode, averages)
        Channel (scale, position, coupling, termination, probe, BW, invert, label)
        Timebase (scale, position, record length)
        Trigger (edge: source, slope, level, coupling)
        Immediate measurements (all 22 supported types)
        Persistent measurement slots MEAS1–MEAS4 (value + statistics)
        DVM – Digital Voltmeter (ACRMS / ACDCRMS / DC / Frequency)
    """

    CHANNELS       = ["CH1", "CH2", "CH3", "CH4"]
    COUPLINGS      = ["DC", "AC", "GND"]
    TERMINATIONS   = ["1e6", "50"]
    BW_LIMITS      = ["FULL", "250", "20"]
    PROBE_GAINS    = ["1", "10", "100", "1000"]
    ACQ_MODES      = ["SAMple", "PEAKdetect", "HIRes", "AVErage", "ENVelope"]
    TRIG_SLOPES    = ["RISE", "FALL", "EITHER"]
    TRIG_COUPLINGS = ["DC", "AC", "HFREJ", "LFREJ", "NOISEREJ"]
    DVM_MODES      = ["ACRMS", "ACDCRMS", "DC", "FREQuency", "OFF"]
    RECORD_LENGTHS = ["1000", "10000", "100000", "1000000"]
    TB_SCALES      = [
        "1ns","2ns","5ns","10ns","20ns","50ns","100ns","200ns","500ns",
        "1us","2us","5us","10us","20us","50us","100us","200us","500us",
        "1ms","2ms","5ms","10ms","20ms","50ms","100ms","200ms","500ms",
        "1s","2s","5s","10s",
    ]
    MEAS_TYPES     = [
        ("Mean",          "MEAN"),
        ("RMS",           "RMS"),
        ("AC RMS",        "ACRms"),
        ("Frequency",     "FREQuency"),
        ("Period",        "PERIod"),
        ("Peak-Peak",     "PK2pk"),
        ("Maximum",       "MAXimum"),
        ("Minimum",       "MINimum"),
        ("Amplitude",     "AMPlitude"),
        ("High Level",    "HIGH"),
        ("Low Level",     "LOW"),
        ("Rise Time",     "RISe"),
        ("Fall Time",     "FALL"),
        ("Pos Width",     "PWIdth"),
        ("Neg Width",     "NWIdth"),
        ("Pos Duty",      "PDUty"),
        ("Neg Duty",      "NDUty"),
        ("Pos Overshoot", "POVershoot"),
        ("Neg Overshoot", "NOVershoot"),
        ("RMS Cycle",     "CRMs"),
        ("Phase",         "PHAse"),
        ("Delay",         "DELay"),
    ]

    def __init__(self, visa_address: str):
        self._rm  = pyvisa.ResourceManager()
        self._res = self._rm.open_resource(visa_address, timeout=5000)
        if '::SOCKET' in visa_address.upper():
            self._res.write_termination = '\n'
            self._res.read_termination  = '\n'
        self._res.write('*CLS')
        self._res.write('HEADer OFF')

    def close(self):
        try: self._res.close()
        except Exception: pass
        try: self._rm.close()
        except Exception: pass

    def write(self, cmd: str):
        self._res.write(cmd)

    def ask(self, cmd: str) -> str:
        try:
            return self._res.query(cmd)
        except pyvisa.errors.VisaIOError:
            self._res.clear()
            raise

    def identify(self) -> str:
        return self.ask("*IDN?").strip()

    # ── Acquisition control ──────────────────────────────────────────────────

    def acq_run(self):
        self.write("ACQuire:STATE RUN")

    def acq_stop(self):
        self.write("ACQuire:STATE STOP")

    def acq_single(self):
        self.write("ACQuire:STOPAfter SEQ")
        self.write("ACQuire:STATE RUN")

    def autoset(self) -> str:
        self.write("AUTOSet EXECute")
        time.sleep(3)
        return "Autoset complete"

    # ── Channel configuration ────────────────────────────────────────────────

    def configure_channel(self, ch: str, scale: float, pos: float,
                          coupling: str, term: str, probe: str,
                          bw: str, invert: str, label: str = "") -> str:
        self.write("*CLS")
        for cmd in [
            f"{ch}:SCAle {scale}",
            f"{ch}:POSition {pos}",
            f"{ch}:COUPling {coupling}",
            f"{ch}:TERmination {term}",
            f"{ch}:PRObe:GAIN {probe}",
            f"{ch}:BANdwidth {bw}",
            f"{ch}:INVert {invert}",
        ]:
            self.write(cmd)
        if label:
            self.write(f"{ch}:LABel '{label}'")
        return f"{ch}: {scale} V/div  {coupling}  {bw} BW"

    def channel_select(self, ch: str, on: bool):
        self.write(f"SELect:{ch} {'ON' if on else 'OFF'}")

    # ── Timebase ─────────────────────────────────────────────────────────────

    def configure_timebase(self, scale_s: float, position: float = 0,
                           record_length: str = None) -> str:
        self.write("*CLS")
        self.write(f"HORizontal:SCAle {scale_s}")
        self.write(f"HORizontal:POSition {position}")
        if record_length:
            self.write(f"HORizontal:RECOrdlength {record_length}")
        try:
            sr = self.ask("HORizontal:SAMPLERate?").strip()
        except Exception:
            sr = "?"
        return f"{scale_s} s/div  pos={position}%  SR={sr}"

    # ── Acquisition mode ─────────────────────────────────────────────────────

    def configure_acquisition(self, mode: str, num_avg: int = 8) -> str:
        self.write("*CLS")
        self.write(f"ACQuire:MODe {mode}")
        if mode.upper().startswith("AVE"):
            self.write(f"ACQuire:NUMAVg {num_avg}")
        return "Acq: " + mode + (f"  N={num_avg}" if mode.upper().startswith("AVE") else "")

    # ── Trigger ──────────────────────────────────────────────────────────────

    def configure_trigger(self, src: str, slope: str,
                          level: float, coupling: str) -> str:
        self.write("*CLS")
        self.write("TRIGger:A:TYPE EDGE")
        self.write(f"TRIGger:A:EDGE:SOUrce {src}")
        self.write(f"TRIGger:A:EDGE:SLOpe {slope}")
        self.write(f"TRIGger:A:LEVel {level}")
        self.write(f"TRIGger:A:EDGE:COUPling {coupling}")
        return f"Trigger: {src}  {slope} edge  @ {level:.3f} V  ({coupling})"

    # ── Immediate measurement ─────────────────────────────────────────────────

    def measure_immediate(self, ch: str, meas_type: str) -> float:
        self.write("*CLS")
        self.write(f"MEASUrement:IMMed:SOUrce1 {ch}")
        self.write(f"MEASUrement:IMMed:TYPe {meas_type}")
        raw = self.ask("MEASUrement:IMMed:VALue?").strip()
        try:
            return float(raw)
        except ValueError:
            return float("nan")

    # ── Persistent measurement slots (MEAS1–MEAS4) ────────────────────────────

    def setup_meas_slot(self, slot: int, ch: str, meas_type: str):
        """Enable and configure one of the four persistent measurement slots."""
        self.write(f"MEASUrement:MEAS{slot}:SOUrce1 {ch}")
        self.write(f"MEASUrement:MEAS{slot}:TYPe {meas_type}")
        self.write(f"MEASUrement:MEAS{slot}:STATE ON")

    def read_meas_slot(self, slot: int) -> float:
        raw = self.ask(f"MEASUrement:MEAS{slot}:VALue?").strip()
        try:    return float(raw)
        except: return float("nan")

    def read_meas_stats(self, slot: int) -> dict:
        def _f(cmd):
            try:    return float(self.ask(cmd).strip())
            except: return float("nan")
        return {
            "mean":   _f(f"MEASUrement:MEAS{slot}:MEAN?"),
            "min":    _f(f"MEASUrement:MEAS{slot}:MINimum?"),
            "max":    _f(f"MEASUrement:MEAS{slot}:MAXimum?"),
            "stddev": _f(f"MEASUrement:MEAS{slot}:STDdev?"),
        }

    def enable_statistics(self, on: bool = True):
        self.write(f"MEASUrement:STATIstics:MODe {'ON' if on else 'OFF'}")

    # ── Waveform capture ─────────────────────────────────────────────────────

    def get_acq_mode(self) -> str:
        """Return current acquisition mode (SAMple / AVErage / HIRes …)."""
        try:
            return self.ask("ACQuire:MODe?").strip()
        except Exception:
            return "SAMple"

    def capture_waveform(self, ch: str, max_points: int = 10000):
        """Capture one waveform from *ch*.

        Returns ``(t_array, v_array)`` as numpy float64 arrays (seconds / volts).
        Returns ``(None, None)`` on any error.
        """
        try:
            import numpy as np
        except ImportError:
            return None, None
        try:
            self.write(f"DATa:SOUrce {ch}")
            self.write("DATa:ENCdg ASCIi")
            self.write("DATa:STARt 1")
            self.write(f"DATa:STOP {max_points}")
            xi = float(self.ask("WFMOutpre:XINcr?"))
            x0 = float(self.ask("WFMOutpre:XZEro?"))
            ym = float(self.ask("WFMOutpre:YMUlt?"))
            yo = float(self.ask("WFMOutpre:YOFf?"))
            yz = float(self.ask("WFMOutpre:YZEro?"))
            raw = self.ask("CURVe?")
            samples = np.array([float(x) for x in raw.split(",")])
            t = x0 + xi * np.arange(len(samples))
            v = (samples - yo) * ym + yz
            return t, v
        except Exception:
            return None, None

    # ── DVM (Digital Voltmeter) ───────────────────────────────────────────────

    def is_dvm_available(self) -> bool:
        """Return True if the DVM option is installed on this unit."""
        try:
            opts = self.ask("*OPT?").upper()
            return "DVM" in opts
        except Exception:
            return False

    def dvm_configure(self, mode: str, source: str) -> str:
        try:
            self._res.write(f"DVM:SOUrce {source}")
            self._res.write(f"DVM:MODe {mode}")
            self._res.write("*CLS")   # clear CME if DVM not installed
        except Exception:
            pass
        return f"DVM: {mode} on {source}"

    def dvm_read(self) -> float:
        try:
            raw = self.ask("DVM:MEASUrement:VALue?").strip()
            return float(raw)
        except Exception:
            return float("nan")

    def dvm_read_all(self) -> dict:
        def _f(cmd):
            try:    return float(self.ask(cmd).strip())
            except: return float("nan")
        return {
            "value": _f("DVM:MEASUrement:VALue?"),
            "freq":  _f("DVM:MEASUrement:FREQuency?"),
            "avg":   _f("DVM:MEASUrement:HIStory:AVErage?"),
            "min":   _f("DVM:MEASUrement:HIStory:MINImum?"),
            "max":   _f("DVM:MEASUrement:HIStory:MAXimum?"),
        }


# ── Keysight 34461A helpers (using enphase Agilent34401A) ─────────────────────

def _dmm_configure(drv: Agilent34401A, function: str, nplc: str = "1",
                   rng: str = "AUTO", autozero: str = "ON") -> str:
    drv.write("*CLS")
    rng_part = "" if rng == "AUTO" else f" {rng}"
    drv.write(f"CONF:{function}{rng_part}")
    if ":" in function:
        drv.write(f"{function}:NPLC {nplc}")
        drv.write(f"{function}:ZERO:AUTO {autozero}")
    return f"Configured: {function}, NPLC={nplc}, Range={rng}, AZ={autozero}"


def _dmm_measure(drv: Agilent34401A, function: str) -> float:
    """
    Single-shot measurement using MEASure:<function>? AUTO,DEF.
        function examples: 'VOLT:DC', 'VOLT:AC', 'CURR:DC', 'CURR:AC', 'RES'
        Returns float('nan') on parse error.
        """
    raw = drv.ask(f"MEAS:{function}? AUTO,DEF").strip()
    try:
        return float(raw)
    except ValueError:
        return float("nan")


class RawTCPResource:
    """Minimal VISA-like wrapper over a plain TCP socket.

    Allows the same .query() / .write() / .close() API that pyvisa resources
    expose so drivers (like GWInstekGDM8261A) work without changes.
    """

    def __init__(self, ip: str, port: int, timeout: float = 5.0):
        import socket as _socket
        self._ip = ip
        self._port = port
        self.timeout = int(timeout * 1000)  # keep in ms like pyvisa
        self.write_termination = "\n"
        self.read_termination  = "\n"
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((ip, port))

    def write(self, cmd: str):
        self._sock.sendall((cmd + self.write_termination).encode())

    def read(self) -> str:
        buf = b""
        term = self.read_termination.encode()
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if term in buf:
                break
        return buf.decode().strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        time.sleep(0.05)
        return self.read()

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


def open_tcpip_resource(rm, address: str, timeout: int = 5000):
    """Try multiple VISA address formats + raw socket fallback for LAN instruments.

    Parameters
    ----------
    rm      : pyvisa ResourceManager
    address : user-supplied string, e.g. "TCPIP::10.73.76.95::INSTR"
    timeout : milliseconds

    Returns a pyvisa resource **or** a RawTCPResource wrapper.
    Raises RuntimeError if nothing works.
    """
    import re, socket as _socket

    # Extract the IP from any TCPIP address format
    ip_match = re.search(r'TCPIP\d*::([^:]+)', address)
    ip = ip_match.group(1) if ip_match else address

    # Extract explicit port from SOCKET address (e.g. 1026 from ::1026::SOCKET)
    sock_port_match = re.search(r'::(\d+)::SOCKET', address, re.IGNORECASE)
    configured_port = int(sock_port_match.group(1)) if sock_port_match else None

    # 1) Try the address as given (INSTR / VXI-11 or SOCKET)
    formats_to_try = [address]
    # 2) Try explicit SOCKET on port 5025
    if "SOCKET" not in address.upper():
        formats_to_try.append(f"TCPIP::{ip}::5025::SOCKET")
    # 3) Try SOCKET on port 1234 (some GW Instek instruments)
    formats_to_try.append(f"TCPIP::{ip}::1234::SOCKET")

    for addr in formats_to_try:
        try:
            res = rm.open_resource(addr, timeout=timeout)
            res.write_termination = "\n"
            res.read_termination  = "\n"
            _ = res.query("*IDN?")
            return res
        except Exception:
            try:
                res.close()
            except Exception:
                pass

    # 4) Raw socket fallback — always try the instrument's own port first,
    #    then common SCPI ports
    raw_ports = [7655, 5025, 1234, 3490, 80, 8080]  # 7655 = Yokogawa WT5000 SCPI port
    if configured_port and configured_port not in raw_ports:
        raw_ports.insert(0, configured_port)
    for port in raw_ports:
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(2)
            if s.connect_ex((ip, port)) == 0:
                s.close()
                raw = RawTCPResource(ip, port, timeout=timeout / 1000)
                idn = raw.query("*IDN?")
                if idn and len(idn) > 2:
                    return raw
                raw.close()
            else:
                s.close()
        except Exception:
            pass

    raise RuntimeError(
        f"Cannot connect to {address} (IP {ip}). "
        "Check that the instrument's LAN socket server is enabled: "
        "Menu → I/O → LAN → Socket Server → ON, then note the port number."
    )


class GWInstekGDM8261A:
    """
    Full SCPI driver for GW Instek GDM-8261A 6.5-Digit Dual-Display Multimeter.

    Implements the complete remote command set from the GDM-8261A User Manual:
    • All 13 measurement functions (primary + secondary display)
    • Range / rate / filter / auto-range
    • Math: REL/NULL, Stats, dBm, dB, MX+B, %
    • Dual display (CONF2 / VAL1? / VAL2?)
    • Trigger: IMM/EXT/BUS, sample count, INIT / FETCH
    • Trace storage
    • SENSE commands for fine configuration
    • System: remote/local, beeper, error query
    """

    FUNCTIONS = {
        "VOLT:DC":   {"ranges": ["AUTO","0.1","1","10","100","1000"]},
        "VOLT:AC":   {"ranges": ["AUTO","0.1","1","10","100","750"]},
        "CURR:DC":   {"ranges": ["AUTO","100e-6","1e-3","10e-3","100e-3","1","10"]},
        "CURR:AC":   {"ranges": ["AUTO","1e-3","10e-3","100e-3","1","10"]},
        "RES":       {"ranges": ["AUTO","100","1e3","10e3","100e3","1e6","10e6","100e6"]},
        "FRES":      {"ranges": ["AUTO","100","1e3","10e3","100e3","1e6","10e6","100e6"]},
        "FREQ":      {"ranges": ["AUTO"]},
        "PER":       {"ranges": ["AUTO"]},
        "DIOD":      {"ranges": ["AUTO"]},
        "CONT":      {"ranges": ["AUTO"]},
        "TEMP:TCO":  {"ranges": ["AUTO"]},
        "TEMP:RTD":  {"ranges": ["AUTO"]},
        "TEMP:FRTD": {"ranges": ["AUTO"]},
    }

    FUNC2_FUNCTIONS = ["OFF", "VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC",
                       "RES", "FRES", "FREQ", "PER"]

    RATES        = {"S": "Slow (6.5d)", "M": "Med (5.5d)", "F": "Fast (4.5d)"}
    AC_BANDWIDTHS = [3, 20, 200]
    MATH_FUNCS   = ["NULL", "DB", "DBM", "STAT", "MXB", "PCNT", "INV"]

    def __init__(self, resource):
        self._r = resource
        self._r.timeout = 10000
        self._r.write_termination = "\n"
        self._r.read_termination  = "\n"

    # ── Identification / system ───────────────────────────────────────────────

    def identify(self) -> str:
        return self._r.query("*IDN?").strip()

    def reset(self):
        self._r.write("*RST")
        self._r.write("*CLS")
        time.sleep(0.5)

    def clear_status(self):
        self._r.write("*CLS")

    def set_remote(self):
        try: self._r.write("SYST:REM")
        except Exception: pass

    def set_local(self):
        try: self._r.write("SYST:LOC")
        except Exception: pass

    def set_rwlock(self):
        try: self._r.write("SYST:RWL")
        except Exception: pass

    def get_error(self) -> str:
        try: return self._r.query("SYST:ERR?").strip()
        except Exception: return ""

    def get_version(self) -> str:
        return self._r.query("SYST:VER?").strip()

    def set_beeper(self, on: bool):
        self._r.write(f"SYST:BEEP:STAT {'ON' if on else 'OFF'}")

    def set_display(self, on: bool):
        self._r.write(f"SYST:DISP {'ON' if on else 'OFF'}")

    # ── Primary display configuration ─────────────────────────────────────────

    def configure(self, function: str, rng: str = "AUTO",
                  rate: str = "M", rel: bool = False) -> str:
        """Configure primary display: function, range, rate, REL/NULL."""
        self._r.write("*CLS")
        rng_part = "" if rng in ("AUTO", "") else f" {rng}"
        write_checked(self._r, f"CONF:{function}{rng_part}", f"CONF:{function}")
        try:
            self._r.write(f"SENS:DET:RATE {rate}")
        except Exception:
            pass
        try:
            if rel:
                self._r.write("CALC:FUNC NULL")
                self._r.write("CALC:STAT ON")
            else:
                self._r.write("CALC:STAT OFF")
        except Exception:
            pass
        return f"Configured: {function}{rng_part}, Rate={rate}, REL={'ON' if rel else 'OFF'}"

    def set_function(self, function: str):
        self._r.write(f'SENS:FUNC1 "{function}"')

    def set_range(self, function: str, rng: str):
        """Set range for function, AUTO or numeric value."""
        _sense_key = {
            "VOLT:DC": "VOLT:DC", "VOLT:AC": "VOLT:AC",
            "CURR:DC": "CURR:DC", "CURR:AC": "CURR:AC",
            "RES": "RES", "FRES": "FRES",
            "FREQ": "FREQ:VOLT", "PER": "PER:VOLT",
        }.get(function)
        if _sense_key:
            if rng == "AUTO":
                self._r.write(f"SENS:{_sense_key}:RANG:AUTO ON")
            else:
                self._r.write(f"SENS:{_sense_key}:RANG:AUTO OFF")
                self._r.write(f"SENS:{_sense_key}:RANG {rng}")

    def set_auto_range(self, on: bool):
        self._r.write(f"CONF:AUTO {'ON' if on else 'OFF'}")

    def get_auto_range(self) -> bool:
        return self._r.query("CONF:AUTO?").strip() == "1"

    def set_rate(self, rate: str):
        """rate: S | M | F"""
        self._r.write(f"SENS:DET:RATE {rate}")

    def get_rate(self) -> str:
        return self._r.query("SENS:DET:RATE?").strip()

    def set_ac_bandwidth(self, hz: int):
        """AC bandwidth filter: 3 | 20 | 200 Hz."""
        self._r.write(f"SENS:DET:BAND {hz}")

    def get_function(self) -> str:
        return self._r.query("CONF:FUNC?").strip()

    def get_range(self) -> str:
        return self._r.query("CONF:RANG?").strip()

    def set_nplc(self, function: str, nplc: float):
        """Set NPLC for DC voltage, current or resistance."""
        _nplc_key = {
            "VOLT:DC": "VOLT:DC", "CURR:DC": "CURR:DC",
            "RES": "RES", "FRES": "FRES",
        }.get(function)
        if _nplc_key:
            self._r.write(f"SENS:{_nplc_key}:NPLC {nplc}")

    def set_input_impedance_auto(self, on: bool):
        """High-Z input mode (for DCV on 100mV/1V/10V ranges)."""
        self._r.write(f"INP:IMP:AUTO {'ON' if on else 'OFF'}")

    # ── Secondary display configuration ───────────────────────────────────────

    def configure_display2(self, function: str, rng: str = "AUTO") -> str:
        """Configure secondary display. function='OFF' disables it."""
        if function == "OFF":
            self._r.write("CONF2:OFF")
            return "Display 2: OFF"
        rng_part = "" if rng in ("AUTO", "") else f" {rng}"
        self._r.write(f"CONF2:{function}{rng_part}")
        return f"Display 2: {function}{rng_part}"

    def disable_display2(self):
        self._r.write("CONF2:OFF")

    def get_function2(self) -> str:
        return self._r.query("CONF2:FUNC?").strip()

    def set_auto_range2(self, on: bool):
        self._r.write(f"CONF2:AUTO {'ON' if on else 'OFF'}")

    # ── Measurements ──────────────────────────────────────────────────────────

    def read(self) -> float:
        """READ? — trigger measurement on primary display."""
        raw = self._r.query("READ?").strip()
        try: return float(raw.split(",")[0])
        except ValueError: return float("nan")

    def fetch(self) -> float:
        """FETC? — return last triggered reading without re-triggering."""
        raw = self._r.query("FETC?").strip()
        try: return float(raw.split(",")[0])
        except ValueError: return float("nan")

    def measure(self, function: str) -> float:
        """One-shot MEAS:{function}? (auto-configures and reads)."""
        raw = self._r.query(f"MEAS:{function}?").strip()
        try: return float(raw.split(",")[0])
        except ValueError: return float("nan")

    def read_primary(self) -> float:
        """VAL1? — primary display value (honoring output format setting)."""
        raw = self._r.query("VAL1?").strip()
        try: return float(raw.split(",")[0])
        except ValueError: return float("nan")

    def read_secondary(self) -> float:
        """VAL2? — secondary display value."""
        raw = self._r.query("VAL2?").strip()
        try: return float(raw.split(",")[0])
        except ValueError: return float("nan")

    def read_both(self) -> tuple:
        """Return (primary, secondary) float tuple."""
        return self.read_primary(), self.read_secondary()

    # ── Math / CALC ───────────────────────────────────────────────────────────

    def enable_rel(self, ref: float = None):
        """REL/NULL: optionally set reference, then activate."""
        self._r.write("CALC:FUNC NULL")
        if ref is not None:
            self._r.write(f"CALC:NULL:OFFS {ref}")
        self._r.write("CALC:STAT ON")

    def enable_stats(self):
        self._r.write("CALC:FUNC STAT")
        self._r.write("CALC:STAT ON")

    def enable_dbm(self, ref_ohm: float = 600.0):
        self._r.write(f"CALC:DBM:REF {ref_ohm}")
        self._r.write("CALC:FUNC DBM")
        self._r.write("CALC:STAT ON")

    def enable_db(self, ref_v: float = None):
        if ref_v is not None:
            self._r.write(f"CALC:DB:REF {ref_v}")
        self._r.write("CALC:FUNC DB")
        self._r.write("CALC:STAT ON")

    def enable_mxb(self, m: float = 1.0, b: float = 0.0):
        self._r.write(f"CALC:MATH:MMFACT {m}")
        self._r.write(f"CALC:MATH:MBFACT {b}")
        self._r.write("CALC:FUNC MXB")
        self._r.write("CALC:STAT ON")

    def disable_math(self):
        try: self._r.write("CALC:STAT OFF")
        except Exception: pass

    def get_min(self) -> float:
        try: return float(self._r.query("CALC:MIN?").strip().split(",")[0])
        except Exception: return float("nan")

    def get_max(self) -> float:
        try: return float(self._r.query("CALC:MAX?").strip().split(",")[0])
        except Exception: return float("nan")

    def get_stats(self) -> dict:
        """Return stats dict: count, min, max, avg, ptpeak, stdev."""
        try:
            cnt  = int(float(self._r.query("CALC:AVG:COUN?").strip()))
            mn   = float(self._r.query("CALC:AVG:MIN?").strip().split(",")[0])
            mx   = float(self._r.query("CALC:AVG:MAX?").strip().split(",")[0])
            avg  = float(self._r.query("CALC:AVG:AVG?").strip().split(",")[0])
            ptp  = float(self._r.query("CALC:AVG:PTP?").strip().split(",")[0])
            sdev = float(self._r.query("CALC:AVG:SDEV?").strip().split(",")[0])
            return {"count": cnt, "min": mn, "max": mx,
                    "avg": avg, "ptpeak": ptp, "stdev": sdev}
        except Exception:
            return {}

    def reset_stats(self):
        try: self._r.write("TRAC:CLR")
        except Exception: pass

    # ── Filtering ─────────────────────────────────────────────────────────────

    def set_digital_filter(self, on: bool, count: int = 10,
                            filter_type: str = "MOV"):
        """Digital filter. filter_type: MOV (moving) | REP (repeating)."""
        self._r.write(f"SENS:AVER:TCON {filter_type}")
        self._r.write(f"SENS:AVER:COUN {count}")
        self._r.write(f"SENS:AVER:STAT {'ON' if on else 'OFF'}")

    def get_digital_filter(self) -> dict:
        return {
            "state": self._r.query("SENS:AVER:STAT?").strip(),
            "type":  self._r.query("SENS:AVER:TCON?").strip(),
            "count": self._r.query("SENS:AVER:COUN?").strip(),
        }

    def set_analog_filter(self, on: bool):
        self._r.write(f"SENS:FILT:STAT {'ON' if on else 'OFF'}")

    def set_continuity_threshold(self, ohms: float):
        self._r.write(f"SENS:CONT:THR {ohms}")

    def set_zero_auto(self, mode: str = "ON"):
        """mode: ON | OFF | ONCE"""
        self._r.write(f"SENS:ZERO:AUTO {mode}")

    # ── Trigger / Trace ───────────────────────────────────────────────────────

    def set_trigger(self, source: str = "IMM", delay: float = 0.0,
                    auto: bool = True):
        """source: IMM | EXT | BUS"""
        self._r.write(f"TRIG:SOUR {source}")
        self._r.write(f"TRIG:DEL {delay}")
        self._r.write(f"TRIG:AUTO {'ON' if auto else 'OFF'}")

    def set_sample_count(self, count: int):
        self._r.write(f"SAMP:COUN {count}")

    def set_trigger_count(self, count: int):
        self._r.write(f"TRIG:COUN {count}")

    def initiate(self):
        self._r.write("INIT")

    def get_data_points(self) -> int:
        try: return int(float(self._r.query("DATA:POIN?").strip()))
        except Exception: return 0

    def get_trace(self) -> list:
        """Return stored trace data as list of floats via TRAC:DATA?."""
        try:
            raw = self._r.query("TRAC:DATA?").strip()
            result = []
            for token in raw.replace("\n", ",").split(","):
                t = token.strip()
                if t:
                    try: result.append(float(t))
                    except ValueError: pass
            return result
        except Exception:
            return []

    def clear_trace(self):
        self._r.write("TRAC:CLR")

    def set_store_count(self, count: int):
        self._r.write(f"CALC:STOR:COUN {count}")

    # ── Thermocouple / RTD ────────────────────────────────────────────────────

    def set_tc_type(self, tc_type: str):
        """Thermocouple type: B|E|J|K|N|R|S|T"""
        self._r.write(f"SENS:TEMP:TCO:TYPE {tc_type}")

    def get_tc_type(self) -> str:
        return self._r.query("SENS:TEMP:TCO:TYPE?").strip()

    def set_temp_unit(self, unit: str):
        """C | F"""
        self._r.write(f"SENS:UNIT {unit}")

    def set_rtd_type(self, rtd_type: str, four_wire: bool = False):
        """RTD type: PT100|D100|F100|PT385|PT3916|USER"""
        cmd = "FRTD" if four_wire else "RTD"
        self._r.write(f"SENS:TEMP:{cmd}:TYPE {rtd_type}")


class GDM8261AAdapter:
    """
    Adapter wrapping GWInstekGDM8261A so it accepts a VISA address string,
    making it compatible with the Instrument Library's load_driver() interface.
    """

    def __init__(self, resource_name: str):
        import pyvisa as _pyvisa
        self._rm  = _pyvisa.ResourceManager()
        self._res = self._rm.open_resource(resource_name)
        self._drv = GWInstekGDM8261A(self._res)

    def ask(self, cmd: str) -> str:
        return self._res.query(cmd).strip()

    def write(self, cmd: str):
        self._res.write(cmd)

    def close(self):
        try: self._res.close()
        except Exception: pass
        try: self._rm.close()
        except Exception: pass

    def __getattr__(self, name: str):
        return getattr(self._drv, name)


class GPP4323Adapter:
    """
    Adapter wrapping GWGPP4323 so it accepts a VISA address string,
    making it compatible with the Instrument Library's load_driver() interface.

    Uses open_tcpip_resource() which tries VXI-11 / SOCKET / raw-TCP in order,
    so it works regardless of which protocol the GPP firmware exposes.
    The GPP-4323 typically uses a raw TCP socket on port 1026 (configurable
    via SHIFT+LAN → Socket → Server: ON on the front panel).
    """

    def __init__(self, resource_name: str):
        import pyvisa as _pyvisa
        self._rm  = _pyvisa.ResourceManager()
        self._res = open_tcpip_resource(self._rm, resource_name, timeout=5000)
        self._drv = GWGPP4323(self._res)

    def ask(self, cmd: str) -> str:
        return self._res.query(cmd).strip()

    def write(self, cmd: str):
        self._res.write(cmd)

    def close(self):
        try: self._res.close()
        except Exception: pass
        try: self._rm.close()
        except Exception: pass

    def __getattr__(self, name: str):
        return getattr(self._drv, name)


class NgiN83624Adapter:
    """
    Adapter for the NGI N83624 Cell Simulator so it accepts a VISA address
    string, making it compatible with the Instrument Library's load_driver().

    The N83624 uses raw TCP SOCKET on port 7000 with LF terminators.
    """

    def __init__(self, resource_name: str):
        import pyvisa as _pyvisa
        self._rm  = _pyvisa.ResourceManager()
        self._res = self._rm.open_resource(resource_name, timeout=5000)
        self._res.write_termination = "\n"
        self._res.read_termination  = "\n"

    def ask(self, cmd: str) -> str:
        return self._res.query(cmd).strip()

    def write(self, cmd: str):
        self._res.write(cmd)

    def query(self, cmd: str) -> str:
        return self._res.query(cmd)

    def close(self):
        try: self._res.close()
        except Exception: pass
        try: self._rm.close()
        except Exception: pass


class WT5000Driver:
    """Adapter that wraps enphase_equipment YokogawaWT5000.

    Provides the panel-compatible API (constants + old-style methods) while
    delegating to the enphase library driver internally.  Raw SCPI is used
    for settings the enphase API does not expose directly.

    Usage (programmatic):
        wt = WT5000Driver(ADDR["wt5000"])
        wt.set_wiring("P1W2")
        wt.configure_element(1, v_range="150V", i_range="5A")
        data = wt.measure_element(1)
        wt.close()

    Usage (high-level via enphase):
        wt.enphase.set_channel_mapping({"ac_line_1": {"channel": "ELEMENT1"}})
        wt.enphase.set_measurement_items({"ac_line_1": {"rms_voltage", "real_power"}})
        result = wt.enphase.get_last_measurement()
    """

    V_RANGES  = ["AUTO","1.5V","3V","6V","10V","15V","30V","60V",
                 "100V","150V","300V","600V","1000V"]
    I_RANGES  = ["AUTO","500MA","1A","2A","5A","10A","20A","30A"]
    SYNC_SRC  = ["U1","U2","U3","U4","U5","U6","U7",
                 "I1","I2","I3","I4","I5","I6","I7",
                 "EXTernal","NONE"]
    FREQ_FILT = ["OFF","0.1KHZ","0.5KHZ","1KHZ","5KHZ","10KHZ","100KHZ","300KHZ"]
    WIRINGS   = ["P1W2","P1W3","P3W3","P3W4","V3A3","V3A3R"]
    AVG_TYPES = ["LINear","EXPonential"]
    INT_MODES = ["NORMal","CONTinuous","RNORmal","RCONtinuous","PULSe"]

    ELEM_ITEMS = [
        ("URMS",   "Vrms (V)"),
        ("IRMS",   "Arms (A)"),
        ("UDC",    "Udc (V)"),
        ("IDC",    "Idc (A)"),
        ("P",      "Power (W)"),
        ("S",      "Apparent (VA)"),
        ("Q",      "Reactive (VAR)"),
        ("LAMBda", "Power Factor"),
        ("FU",     "V-Freq (Hz)"),
        ("FI",     "I-Freq (Hz)"),
        ("CFU",    "V Crest Factor"),
        ("CFI",    "I Crest Factor"),
        ("PHI",    "Phase (°)"),
        ("WH",     "Wh (Wh)"),
        ("AH",     "Ah (Ah)"),
    ]

    SIGMA_ITEMS = [
        ("P",      "Σ Power (W)"),
        ("S",      "Σ Apparent (VA)"),
        ("Q",      "Σ Reactive (VAR)"),
        ("LAMBda", "Σ PF"),
    ]

    def __init__(self, visa_address: str):
        self.enphase = YokogawaWT5000(visa_address)
        self._write("*CLS")

    # ── raw SCPI helpers ─────────────────────────────────────────────────────
    def _write(self, cmd: str):
        self.enphase.write(cmd, check_for_error=False)

    def _query(self, cmd: str) -> str:
        return self.enphase.query(cmd)

    # ── basic ────────────────────────────────────────────────────────────────
    def identify(self) -> str:
        info = self.enphase.get_info()
        return f"{info.make},{info.model},{info.sn},{info.fw}"

    def reset(self):
        self.enphase.reset()

    def close(self):
        self.enphase.close()

    # ── wiring ───────────────────────────────────────────────────────────────
    def set_wiring(self, element_or_wiring, wiring=None) -> str:
        """Accepts set_wiring("P1W2") or the legacy set_wiring(elem, "P1W2")."""
        pattern = wiring if wiring is not None else element_or_wiring
        self.enphase.set_wiring(pattern)
        return f"Wiring → {pattern}"

    # ── element configuration ────────────────────────────────────────────────
    def configure_element(self, element: int, v_range: str = "AUTO",
                          i_range: str = "AUTO", sync: str = None,
                          line_filter="OFF", freq_filter: str = "OFF") -> str:
        if sync is None:
            sync = f"I{element}"
        if v_range == "AUTO":
            self._write(f":INPut:VOLTage:AUTO:ELEMent{element} ON")
        else:
            self._write(f":INPut:VOLTage:AUTO:ELEMent{element} OFF")
            self._write(f":INPut:VOLTage:RANGe:ELEMent{element} {v_range}")
        if i_range == "AUTO":
            self._write(f":INPut:CURRent:AUTO:ELEMent{element} ON")
        else:
            self._write(f":INPut:CURRent:AUTO:ELEMent{element} OFF")
            self._write(f":INPut:CURRent:RANGe:ELEMent{element} {i_range}")
        lf_param = ("0.5KHZ" if line_filter else "OFF") if isinstance(line_filter, bool) else line_filter
        self._write(f":INPut:FILTer:LINE:ELEMent{element} {lf_param}")
        self._write(f":INPut:FILTer:FREQuency:ELEMent{element} {freq_filter}")
        self._write(f":INPut:SYNChronize:ELEMent{element} {sync}")
        return f"E{element}: V={v_range} I={i_range} Sync={sync} LF={lf_param} BW={freq_filter}"

    # ── averaging ────────────────────────────────────────────────────────────
    def configure_averaging(self, enabled: bool, avg_type: str = "LINear",
                            count: int = 8) -> str:
        s = "ON" if enabled else "OFF"
        self._write(f":MEASure:AVERaging:STATe {s}")
        if enabled:
            self._write(f":MEASure:AVERaging:TYPE {avg_type}")
            self._write(f":MEASure:AVERaging:COUNt {count}")
        return f"Averaging {s}, Type={avg_type}, Count={count}"

    # ── integration ──────────────────────────────────────────────────────────
    def configure_integration(self, mode: str = "NORMal",
                              hours: int = 0, minutes: int = 0,
                              seconds: int = 0) -> str:
        self._write(f":INTEGrate:MODE {mode}")
        self._write(f":INTEGrate:TIMer1 {hours},{minutes},{seconds}")
        return f"Integration: Mode={mode}, Timer={hours:02d}:{minutes:02d}:{seconds:02d}"

    def start_integration(self):
        self._write(":INTEGrate:STARt")

    def stop_integration(self):
        self._write(":INTEGrate:STOP")

    def reset_integration(self):
        self.enphase.reset_integration()

    def integration_state(self) -> str:
        return self._query(":INTEGrate:STATe?").strip()

    # ── numeric output list ──────────────────────────────────────────────────
    def setup_numeric(self, items: list) -> None:
        """items: list of (func, element) or (func,) for sigma quantities."""
        self._write(":NUMeric:FORMat ASCii")
        self._write(f":NUMeric:NORMal:NUMber {len(items)}")
        for idx, item in enumerate(items, 1):
            if len(item) == 2:
                func, elem = item
                self._write(f":NUMeric:NORMal:ITEM{idx} {func},{elem}")
            else:
                self._write(f":NUMeric:NORMal:ITEM{idx} {item[0]},SIGMa")

    def read_numeric(self) -> list:
        raw = self._query(":NUMeric:NORMal:VALue?").strip()
        vals = []
        for x in raw.split(","):
            try:
                vals.append(float(x))
            except ValueError:
                vals.append(float("nan"))
        return vals

    def measure_element(self, element: int, items: list = None) -> dict:
        if items is None:
            items = ["URMS", "IRMS", "P", "S", "Q", "LAMBda", "FU"]
        self.setup_numeric([(f, element) for f in items])
        vals = self.read_numeric()
        lbl  = dict(self.ELEM_ITEMS)
        return {lbl.get(f, f): v for f, v in zip(items, vals)}

    def measure_sigma(self) -> dict:
        funcs = [s[0] for s in self.SIGMA_ITEMS]
        self.setup_numeric([(f, "SIGMa") for f in funcs])
        vals = self.read_numeric()
        lbl  = dict(self.SIGMA_ITEMS)
        return {lbl[f]: v for f, v in zip(funcs, vals)}


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def setup_logging() -> logging.Logger:
    log = logging.getLogger("test_sweep")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    log.addHandler(handler)
    return log


def build_csv_header() -> list:
    """Construct the ordered list of column names for the results CSV."""
    header = [
        "step",
        "timestamp",
        # ITECH programmed setpoints
        "itech_volt_set_V",
        "itech_curr_set_A",
        "itech_mode",
        # ITECH back-plane measurements
        "itech_volt_meas_V",
        "itech_curr_meas_A",
        # GPP-4323 programmed setpoints (all 4 channels)
        *[f"gpp_ch{c}_volt_set_V" for c in range(1, 5)],
        *[f"gpp_ch{c}_curr_set_A" for c in range(1, 5)],
        *[f"gpp_ch{c}_enabled"    for c in range(1, 5)],
        # GPP-4323 back-panel measurements
        *[f"gpp_ch{c}_volt_meas_V" for c in range(1, 5)],
        *[f"gpp_ch{c}_curr_meas_A" for c in range(1, 5)],
        # Oscilloscope measurements
        *[f"mdo_{ch}_{mtype.lower()}" for ch, mtype in MDO_MEAS_CONFIG],
        # DMM measurements
        *[f"dmm_{fn.replace(':', '_').lower()}" for fn in DMM_MEAS_CONFIG],
        # GDM measurements
        *[f"gdm_{fn.replace(':', '_').lower()}" for fn in GDM_MEAS_CONFIG],
    ]
    return header


def safe_measure(func, label: str, log: logging.Logger):
    """Call func(); return float('nan') and log a warning on any exception."""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001
        log.warning("  Measurement failed [%s]: %s", label, exc)
        return float("nan")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — MAIN SWEEP
# ═══════════════════════════════════════════════════════════════════════════════

def run_sweep(instruments=None):
    """
    Run the sweep with optional instrument selection.
    instruments: list of instrument keys to include, e.g. ['itech', 'gpp', 'mdo', 'dmm', 'gdm']
                If None, all instruments are used.
    """
    log = setup_logging()
    rm  = pyvisa.ResourceManager()

    # ── Connect ──────────────────────────────────────────────────────────────
    log.info("Opening VISA connections …")
    
    # Determine which instruments to use
    if instruments is None:
        instruments = ["itech", "gpp", "mdo", "dmm", "gdm"]
    
    log.info("Using instruments: %s", ", ".join(instruments))
    
    # Initialize selected instruments
    # For TCPIP addresses, open_tcpip_resource tries multiple protocols
    # (VXI-11, SOCKET on 5025/1234, raw TCP) with automatic fallback.
    inst_refs = {}
    
    def _open(addr):
        if addr.startswith("TCPIP"):
            return open_tcpip_resource(rm, addr)
        return rm.open_resource(addr)
    
    try:
        if "itech" in instruments:
            inst_refs["itech"] = ItechIT6005C(_open(ADDR["itech"]))
        if "gpp" in instruments:
            inst_refs["gpp"] = GWGPP4323(_open(ADDR["gpp"]))
        if "mdo" in instruments:
            inst_refs["mdo"] = TektronixMDO34(_open(ADDR["mdo"]))
        if "dmm" in instruments:
            inst_refs["dmm"] = Keysight34461A(_open(ADDR["dmm"]))
        if "gdm" in instruments:
            inst_refs["gdm"] = GWInstekGDM8261A(_open(ADDR["gdm"]))
    except (pyvisa.VisaIOError, RuntimeError) as exc:
        log.error("Could not open instrument: %s", exc)
        log.error("Run  python test_automation.py --list-resources  to check connections.")
        rm.close()
        sys.exit(1)

    # ── Identify ─────────────────────────────────────────────────────────────
    log.info("Instrument identification:")
    for label, key in [("ITECH", "itech"), ("GPP-4323", "gpp"),
                       ("MDO34", "mdo"), ("34461A", "dmm"), ("GDM-8261A", "gdm")]:
        if key in inst_refs:
            log.info("  %-10s → %s", label, inst_refs[key].identify())

    # ── Reset to known state ─────────────────────────────────────────────────
    log.info("Resetting instruments to factory defaults …")
    if "itech" in inst_refs:
        inst_refs["itech"].reset(); inst_refs["itech"].output_off()
    if "gpp" in inst_refs:
        inst_refs["gpp"].reset(); inst_refs["gpp"].all_outputs_off()
    if "mdo" in inst_refs:
        inst_refs["mdo"].reset()
    if "dmm" in inst_refs:
        inst_refs["dmm"].reset()
    if "gdm" in inst_refs:
        inst_refs["gdm"].reset()

    # ── Prepare output directory and CSV ─────────────────────────────────────
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        from app_logger import APP_DIR
        out_dir = APP_DIR / "results"
    except ImportError:
        out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"sweep_{ts}.csv"
    header   = build_csv_header()

    total = len(ITECH_SWEEP) * len(GPP_SWEEP)
    log.info("Sweep: %d ITECH points × %d GPP points = %d total steps",
             len(ITECH_SWEEP), len(GPP_SWEEP), total)
    log.info("Results → %s", csv_path)

    step = 0
    try:
        with open(csv_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=header)
            writer.writeheader()

            # ── Outer product: every ITECH setting × every GPP setting ───────
            for (iv, ic, imode), gpp_cfg in itertools.product(ITECH_SWEEP, GPP_SWEEP):
                step += 1
                log.info("━━━ Step %d / %d ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                         step, total)

                # Apply ITECH
                if "itech" in inst_refs:
                    log.info("  [ITECH]   VOLT=%.2f V  CURR=%.2f A  MODE=%s",
                             iv, ic, imode)
                    inst_refs["itech"].set_output(iv, ic, imode)
                    inst_refs["itech"].output_on()

                # Apply GPP-4323
                if "gpp" in inst_refs:
                    inst_refs["gpp"].apply_config(gpp_cfg)
                    for ch, (v, c, en) in gpp_cfg.items():
                        log.info("  [GPP CH%d] VOLT=%.2f V  CURR=%.2f A  %s",
                                 ch, v, c, "ON" if en else "OFF")

                # Wait for outputs to settle
                time.sleep(SETTLING_TIME)

                # ── Gather row ────────────────────────────────────────────────
                row: dict = {
                    "step":              step,
                    "timestamp":         datetime.now().isoformat(timespec="milliseconds"),
                    "itech_volt_set_V":  iv,
                    "itech_curr_set_A":  ic,
                    "itech_mode":        imode,
                }
                
                if "itech" in inst_refs:
                    row["itech_volt_meas_V"] = safe_measure(inst_refs["itech"].measure_voltage,
                                                          "ITECH VOLT", log)
                    row["itech_curr_meas_A"] = safe_measure(inst_refs["itech"].measure_current,
                                                          "ITECH CURR", log)
                else:
                    row["itech_volt_meas_V"] = float("nan")
                    row["itech_curr_meas_A"] = float("nan")

                for ch in range(1, 5):
                    v_s, c_s, en = gpp_cfg[ch]
                    row[f"gpp_ch{ch}_volt_set_V"]  = v_s
                    row[f"gpp_ch{ch}_curr_set_A"]  = c_s
                    row[f"gpp_ch{ch}_enabled"]     = en
                    if "gpp" in inst_refs:
                        row[f"gpp_ch{ch}_volt_meas_V"] = safe_measure(
                            lambda c=ch: inst_refs["gpp"].measure_voltage(c), f"GPP CH{ch} VOLT", log)
                        row[f"gpp_ch{ch}_curr_meas_A"] = safe_measure(
                            lambda c=ch: inst_refs["gpp"].measure_current(c), f"GPP CH{ch} CURR", log)
                    else:
                        row[f"gpp_ch{ch}_volt_meas_V"] = float("nan")
                        row[f"gpp_ch{ch}_curr_meas_A"] = float("nan")

                if "mdo" in inst_refs:
                    for osc_ch, mtype in MDO_MEAS_CONFIG:
                        key = f"mdo_{osc_ch}_{mtype.lower()}"
                        val = safe_measure(
                            lambda oc=osc_ch, mt=mtype: inst_refs["mdo"].measure(oc, mt),
                            f"MDO {osc_ch} {mtype}", log)
                        row[key] = val
                        log.info("  [MDO34]   %s %-10s = %g", osc_ch, mtype, val)
                else:
                    for osc_ch, mtype in MDO_MEAS_CONFIG:
                        key = f"mdo_{osc_ch}_{mtype.lower()}"
                        row[key] = float("nan")

                if "dmm" in inst_refs:
                    for fn in DMM_MEAS_CONFIG:
                        key = f"dmm_{fn.replace(':', '_').lower()}"
                        val = safe_measure(
                            lambda f=fn: inst_refs["dmm"].measure(f), f"DMM {fn}", log)
                        row[key] = val
                        log.info("  [34461A]  %-10s = %g", fn, val)
                else:
                    for fn in DMM_MEAS_CONFIG:
                        key = f"dmm_{fn.replace(':', '_').lower()}"
                        row[key] = float("nan")
                
                if "gdm" in inst_refs:
                    for fn in GDM_MEAS_CONFIG:
                        key = f"gdm_{fn.replace(':', '_').lower()}"
                        val = safe_measure(
                            lambda f=fn: inst_refs["gdm"].measure(f), f"GDM {fn}", log)
                        row[key] = val
                        log.info("  [GDM-8261A]  %-10s = %g", fn, val)
                else:
                    for fn in GDM_MEAS_CONFIG:
                        key = f"gdm_{fn.replace(':', '_').lower()}"
                        row[key] = float("nan")

                writer.writerow(row)
                csvfile.flush()   # write to disk incrementally

    except KeyboardInterrupt:
        log.warning("Sweep interrupted by user at step %d / %d.", step, total)

    finally:
        # ── Safe shutdown regardless of how we exit ───────────────────────────
        log.info("Turning off all outputs …")
        try:
            if "itech" in inst_refs:
                inst_refs["itech"].output_off()
        except Exception:  # noqa: BLE001
            pass
        try:
            if "gpp" in inst_refs:
                inst_refs["gpp"].all_outputs_off()
        except Exception:  # noqa: BLE001
            pass
        rm.close()

    log.info("Sweep complete.  %d / %d steps finished.", step, total)
    log.info("Results saved to: %s", csv_path)
    return str(csv_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def list_resources():
    """Print all VISA resources visible to the system."""
    rm = pyvisa.ResourceManager()
    resources = rm.list_resources()
    if not resources:
        print("No VISA resources found.  Check USB / LAN connections and drivers.")
    else:
        print(f"Found {len(resources)} VISA resource(s):")
        for addr in resources:
            try:
                res = rm.open_resource(addr)
                idn = res.query("*IDN?").strip()
                res.close()
                print(f"  {addr}\n    IDN: {idn}")
            except Exception:  # noqa: BLE001
                print(f"  {addr}\n    IDN: <query failed>")
    rm.close()


import argparse

def parse_args():
    parser = argparse.ArgumentParser(description="Automated instrument sweep")
    parser.add_argument("--list-resources", action="store_true", help="List VISA resources")
    parser.add_argument("--instruments", nargs="*", 
                       choices=["itech", "gpp", "mdo", "dmm", "gdm"],
                       help="Select instruments to use (default: all)")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.list_resources:
        list_resources()
    else:
        run_sweep(instruments=args.instruments)
