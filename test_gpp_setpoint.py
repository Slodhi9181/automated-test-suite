"""
test_gpp_setpoint.py
====================
End-to-end test:
  1. Open VISA SOCKET connection on port 1026
  2. Read *IDN?  (confirms connection)
  3. Set CH1 voltage + current setpoint
  4. Turn CH1 output ON
  5. Wait for output to settle
  6. Read back measured voltage + current
  7. Compare measured vs. setpoint  → PASS / FAIL
  8. Turn CH1 output OFF

Usage:
    python test_gpp_setpoint.py
    python test_gpp_setpoint.py 169.254.110.87 1026 5.0 1.0 1

Arguments (all optional):
    IP       instrument IP  (default: from instrument_addresses.json)
    PORT     socket port    (default: 1026)
    VOLT     target voltage (default: 5.0 V)
    CURR     target current (default: 1.0 A)
    CHANNEL  channel 1-4   (default: 1)
"""

import sys, json, time, re
from pathlib import Path

# ── Command-line / defaults ───────────────────────────────────────────────────
ADDR_FILE = Path(__file__).parent / "instrument_addresses.json"
try:
    saved = json.loads(ADDR_FILE.read_text(encoding="utf-8"))
    raw   = saved.get("gpp", "")
except Exception:
    raw = ""

m_ip   = re.search(r'TCPIP\d*::([^:]+)', raw)
m_port = re.search(r'::(\d+)::SOCKET', raw, re.IGNORECASE)
DEF_IP   = m_ip.group(1)   if m_ip   else "169.254.110.87"
DEF_PORT = int(m_port.group(1)) if m_port else 1026

IP      = sys.argv[1] if len(sys.argv) > 1 else DEF_IP
PORT    = int(sys.argv[2]) if len(sys.argv) > 2 else DEF_PORT
VOLT    = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
CURR    = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
CHANNEL = int(sys.argv[5])   if len(sys.argv) > 5 else 1

TOLERANCE_V = 0.5   # V — acceptable measurement error
TOLERANCE_A = 0.5   # A

SETTLE_S    = 1.0   # seconds to wait after enabling output
TIMEOUT_MS  = 5000  # pyvisa timeout

print("=" * 58)
print("  GPP-4323  Setpoint End-to-End Test")
print(f"  IP={IP}  PORT={PORT}  CH={CHANNEL}")
print(f"  Target: {VOLT:.3f} V  /  {CURR:.3f} A")
print("=" * 58)

import pyvisa

VISA_ADDR = f"TCPIP0::{IP}::{PORT}::SOCKET"
PASS = True


def step(n, title):
    print(f"\n[STEP {n}]  {title}")


def ok(msg):
    print(f"  ✓  {msg}")


def fail(msg):
    global PASS
    PASS = False
    print(f"  ✗  {msg}")


def info(msg):
    print(f"  →  {msg}")


# ── STEP 1: Open pyvisa SOCKET resource ──────────────────────────────────────
step(1, f"Open pyvisa SOCKET  {VISA_ADDR}")
try:
    rm  = pyvisa.ResourceManager()
    res = rm.open_resource(VISA_ADDR, timeout=TIMEOUT_MS)
    res.write_termination = "\n"
    res.read_termination  = "\n"
    ok(f"Resource opened  (class={getattr(res,'resource_class','?')})")
except Exception as e:
    fail(f"Cannot open resource: {e}")
    sys.exit(1)

# ── STEP 2: *IDN? ─────────────────────────────────────────────────────────────
step(2, "*IDN?  — confirm connection")
try:
    idn = res.query("*IDN?").strip()
    ok(f"IDN = {idn!r}")
except Exception as e:
    fail(f"*IDN? failed: {e}")
    res.close(); rm.close(); sys.exit(1)

# helper: write a command then immediately check SYST:ERR?
def write_check(r, cmd):
    """Write cmd, then query SYST:ERR? — returns (accepted:bool, err_str)."""
    r.write("*CLS")
    time.sleep(0.05)
    r.write(cmd)
    time.sleep(0.05)
    try:
        err = r.query("SYST:ERR?").strip()
    except Exception:
        err = "timeout"
    accepted = "no error" in err.lower() or err in ("0", "+0", "0,\"No error\"")
    return accepted, err


# ── STEP 3: Probe VOLT set-command syntax ─────────────────────────────────────
step(3, f"Probe: which voltage-set command does V1.22 accept?")
VOLT_CMDS = [
    f"CH{CHANNEL}:VOLT {VOLT:.4f}",         # CHn:VOLT style (newer firmware)
    f"VOLT{CHANNEL} {VOLT:.4f}",             # VOLTn style
    f"VSET{CHANNEL}:{VOLT:.4f}",             # VSETn: style (older GW Instek)
    f"VSET{CHANNEL} {VOLT:.4f}",             # VSETn space style
    f":CH{CHANNEL}:VOLT {VOLT:.4f}",         # rooted form
    f"SOUR:VOLT {VOLT:.4f}",                 # SOURce form (single-channel)
    f"SOUR{CHANNEL}:VOLT {VOLT:.4f}",        # SOURce channelled
]
GOOD_VOLT_CMD = None
for cmd in VOLT_CMDS:
    accepted, err = write_check(res, cmd)
    if accepted:
        ok(f"ACCEPTED: {cmd!r}  (SYST:ERR? = {err!r})")
        GOOD_VOLT_CMD = cmd
        break
    else:
        info(f"rejected: {cmd!r}  →  SYST:ERR? = {err!r}")

if GOOD_VOLT_CMD is None:
    fail("No voltage-set command was accepted — check instrument SCPI manual for V1.22")
else:
    ok(f"Voltage set command confirmed: {GOOD_VOLT_CMD!r}")

# ── STEP 4: Probe CURR set-command syntax ─────────────────────────────────────
step(4, f"Probe: which current-set command does V1.22 accept?")
CURR_CMDS = [
    f"CH{CHANNEL}:CURR {CURR:.4f}",
    f"CURR{CHANNEL} {CURR:.4f}",
    f"ISET{CHANNEL}:{CURR:.4f}",
    f"ISET{CHANNEL} {CURR:.4f}",
    f":CH{CHANNEL}:CURR {CURR:.4f}",
    f"SOUR:CURR {CURR:.4f}",
    f"SOUR{CHANNEL}:CURR {CURR:.4f}",
]
GOOD_CURR_CMD = None
for cmd in CURR_CMDS:
    accepted, err = write_check(res, cmd)
    if accepted:
        ok(f"ACCEPTED: {cmd!r}  (SYST:ERR? = {err!r})")
        GOOD_CURR_CMD = cmd
        break
    else:
        info(f"rejected: {cmd!r}  →  SYST:ERR? = {err!r}")

if GOOD_CURR_CMD is None:
    fail("No current-set command was accepted")
else:
    ok(f"Current set command confirmed: {GOOD_CURR_CMD!r}")

# ── STEP 5: Probe OUTPUT enable syntax ────────────────────────────────────────
step(5, "Probe: which output-enable command does V1.22 accept?")
OUTP_CMDS = [
    f"OUTP CH{CHANNEL},ON",
    f"OUTPUT{CHANNEL} ON",
    f"OUTP{CHANNEL} ON",
    f"OUTP CH{CHANNEL} ON",
    f":OUTP{CHANNEL} ON",
    f"OUT{CHANNEL} ON",
    f"SOUR:OUTP{CHANNEL} ON",
]
GOOD_OUTP_CMD = None
for cmd in OUTP_CMDS:
    accepted, err = write_check(res, cmd)
    if accepted:
        ok(f"ACCEPTED: {cmd!r}  (SYST:ERR? = {err!r})")
        GOOD_OUTP_CMD = cmd
        break
    else:
        info(f"rejected: {cmd!r}  →  SYST:ERR? = {err!r}")

if GOOD_OUTP_CMD is None:
    fail("No output-enable command was accepted")
else:
    ok(f"Output enable command confirmed: {GOOD_OUTP_CMD!r}")

if not (GOOD_VOLT_CMD and GOOD_CURR_CMD and GOOD_OUTP_CMD):
    print("\n  ✗  Cannot continue — at least one command format unknown.")
    print("     Update the GWGPP4323 driver with the correct commands shown above.")
    res.close(); rm.close(); sys.exit(1)

# ── STEP 6: Apply setpoint with confirmed commands ────────────────────────────
step(6, f"Apply setpoint using confirmed commands")
res.write("*CLS")
res.write(GOOD_VOLT_CMD)
info(f"Wrote: {GOOD_VOLT_CMD!r}")
res.write(GOOD_CURR_CMD)
info(f"Wrote: {GOOD_CURR_CMD!r}")
err6 = res.query("SYST:ERR?").strip()
info(f"SYST:ERR? = {err6!r}")
if "no error" in err6.lower():
    ok("Setpoints accepted — no errors")
else:
    fail(f"Error after setpoints: {err6!r}")

# ── STEP 7: Enable output ─────────────────────────────────────────────────────
step(7, f"Enable output using confirmed command: {GOOD_OUTP_CMD!r}")
res.write(GOOD_OUTP_CMD)
info(f"Wrote: {GOOD_OUTP_CMD!r}")
info(f"Waiting {SETTLE_S:.1f}s for output to settle ...")
time.sleep(SETTLE_S)

# ── STEP 8: Measure voltage ───────────────────────────────────────────────────
step(8, f"Read back voltage  MEAS{CHANNEL}:VOLT?")
v_meas = None
try:
    raw_v = res.query(f"MEAS{CHANNEL}:VOLT?").strip()
    v_meas = float(raw_v)
    ok(f"MEAS{CHANNEL}:VOLT? = {raw_v!r}  →  {v_meas:.4f} V")
except Exception as e:
    fail(f"MEAS{CHANNEL}:VOLT? failed: {e}")

# ── STEP 9: Measure current ───────────────────────────────────────────────────
step(9, f"Read back current  MEAS{CHANNEL}:CURR?")
i_meas = None
try:
    raw_i = res.query(f"MEAS{CHANNEL}:CURR?").strip()
    i_meas = float(raw_i)
    ok(f"MEAS{CHANNEL}:CURR? = {raw_i!r}  →  {i_meas:.4f} A")
except Exception as e:
    fail(f"MEAS{CHANNEL}:CURR? failed: {e}")

# ── STEP 10: Compare vs. setpoint ────────────────────────────────────────────
step(10, "Compare measured vs. setpoint")
if v_meas is not None:
    diff_v = abs(v_meas - VOLT)
    info(f"Voltage: target={VOLT:.3f} V  measured={v_meas:.3f} V  diff={diff_v:.3f} V  tol={TOLERANCE_V} V")
    if diff_v <= TOLERANCE_V:
        ok(f"Voltage within tolerance  ✓")
    else:
        fail(f"Voltage out of tolerance!  diff={diff_v:.3f} V > {TOLERANCE_V} V")
        if v_meas < 0.1:
            info("  Measured ~0V despite output ON.")
            info("  Possible causes:")
            info("  1. No load connected — some PSUs show 0V without a load")
            info("  2. OUTP CHn,ON command was not accepted (check SYST:ERR?)")
            info("  3. Instrument in REMOTE+LOCK — setpoint may be ignored")
else:
    info("Voltage comparison skipped (readback failed)")

if i_meas is not None:
    diff_i = abs(i_meas - 0.0)   # with no load, current should be ~0
    info(f"Current: target={CURR:.3f} A (limit)  measured={i_meas:.3f} A")
    if i_meas < CURR + TOLERANCE_A:
        ok(f"Current within limit  ✓")
    else:
        fail(f"Current exceeds limit!  {i_meas:.3f} A > {CURR + TOLERANCE_A:.3f} A")

# ── STEP 11: Turn output OFF ──────────────────────────────────────────────────
step(11, f"Turn output OFF  OUTPUT{CHANNEL} OFF")
try:
    res.write(f"OUTPUT{CHANNEL} OFF")
    ok(f"Written: 'OUTPUT{CHANNEL} OFF'")
except Exception as e:
    fail(f"Write failed: {e}")

res.close()
rm.close()

# ── Result ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 58)
if PASS:
    print("  RESULT:  PASS  ✓")
else:
    print("  RESULT:  FAIL  ✗  — see ✗ lines above for details")
print("=" * 58)
