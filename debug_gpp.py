"""
debug_gpp.py  —  Step-by-step GPP-4323 connection diagnostic
Run from the project folder:  python debug_gpp.py
"""

import socket
import time
import sys
import json
from pathlib import Path

# ── Load saved address ────────────────────────────────────────────────────────
ADDR_FILE = Path(__file__).parent / "instrument_addresses.json"
try:
    saved = json.loads(ADDR_FILE.read_text(encoding="utf-8"))
    RAW_ADDR = saved.get("gpp", "")
except Exception as e:
    RAW_ADDR = ""
    print(f"[WARN] Could not load instrument_addresses.json: {e}")

# Allow override from command line:  python debug_gpp.py 169.254.X.Y 1026
import re
CLI_IP   = sys.argv[1] if len(sys.argv) > 1 else None
CLI_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else None

# Parse IP / port from saved address  (TCPIP0::169.254.110.87::1026::SOCKET)
m_ip   = re.search(r'TCPIP\d*::([^:]+)', RAW_ADDR)
m_port = re.search(r'::(\d+)::SOCKET', RAW_ADDR, re.IGNORECASE)

IP   = CLI_IP   or (m_ip.group(1)   if m_ip   else "169.254.110.87")
PORT = CLI_PORT or (int(m_port.group(1)) if m_port else 1026)

TIMEOUT = 5  # seconds

print("=" * 60)
print(f"  GPP-4323 Debug Diagnostic")
print(f"  Saved address : {RAW_ADDR or '(none)'}")
print(f"  Testing IP    : {IP}")
print(f"  Testing port  : {PORT}")
print("=" * 60)

# ── STEP 1: Ping-style TCP connect on port 1026 ───────────────────────────────
print(f"\n[STEP 1]  Raw TCP connect  →  {IP}:{PORT}  (timeout {TIMEOUT}s)")
raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
raw_sock.settimeout(TIMEOUT)
t0 = time.time()
err_code = raw_sock.connect_ex((IP, PORT))
elapsed = time.time() - t0

if err_code != 0:
    print(f"  ✗  FAILED  (OS error {err_code}, took {elapsed:.2f}s)")
    print(f"     Port {PORT} is CLOSED or unreachable.")
    print(f"     ► On the GPP-4323 front panel:")
    print(f"       SHIFT + LAN  →  Socket  →  Server: ON")
    print(f"       Note the port number shown and update the address in the app.")
    raw_sock.close()

    # Scan other ports so we know if the instrument is on a different port
    print(f"\n  Scanning other common ports on {IP} ...")
    for p in [1026, 3000, 5025, 1234, 3490, 8000, 8080]:
        if p == PORT:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        r = s.connect_ex((IP, p))
        s.close()
        tag = "OPEN ← try this!" if r == 0 else "closed"
        print(f"    port {p:5d}  {tag}")
    print()
    sys.exit(1)
else:
    print(f"  ✓  TCP port {PORT} is OPEN  (connected in {elapsed:.3f}s)")

# ── STEP 2: Send *IDN? and read response ─────────────────────────────────────
print(f"\n[STEP 2]  Send  *IDN?\\n  and read response ...")
raw_sock.settimeout(TIMEOUT)
try:
    raw_sock.sendall(b"*IDN?\n")
    print(f"  →  Sent b'*IDN?\\n'")
    time.sleep(0.15)
    data = b""
    raw_sock.settimeout(3)
    try:
        while True:
            chunk = raw_sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    except socket.timeout:
        pass
    if data:
        print(f"  ←  Raw bytes  : {data!r}")
        print(f"  ←  Decoded    : {data.decode(errors='replace').strip()}")
    else:
        print(f"  ✗  No response to *IDN? (instrument may not support it on this socket,")
        print(f"     or it needs \\r\\n termination — we'll try that next)")
except Exception as e:
    print(f"  ✗  Exception: {e}")

# ── STEP 2b: Retry *IDN? with \\r\\n termination ─────────────────────────────
print(f"\n[STEP 2b] Retry  *IDN?\\r\\n  (CR+LF termination) ...")
raw_sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
raw_sock2.settimeout(TIMEOUT)
raw_sock2.connect((IP, PORT))
try:
    raw_sock2.sendall(b"*IDN?\r\n")
    print(f"  →  Sent b'*IDN?\\r\\n'")
    time.sleep(0.15)
    data2 = b""
    raw_sock2.settimeout(3)
    try:
        while True:
            chunk = raw_sock2.recv(4096)
            if not chunk:
                break
            data2 += chunk
            if b"\n" in data2:
                break
    except socket.timeout:
        pass
    if data2:
        print(f"  ←  Raw bytes  : {data2!r}")
        print(f"  ←  Decoded    : {data2.decode(errors='replace').strip()}")
        print(f"  ★  Instrument DOES respond to CR+LF — write_termination should be '\\r\\n'")
    else:
        print(f"  ✗  Still no response")
except Exception as e:
    print(f"  ✗  Exception: {e}")
finally:
    raw_sock2.close()

# ── STEP 3: Send VOLT/CURR command via raw socket ─────────────────────────────
print(f"\n[STEP 3]  Send voltage/current commands (raw socket, LF termination) ...")
for cmd in ["CH1:VOLT 5.0000", "CH1:CURR 1.0000"]:
    try:
        payload = (cmd + "\n").encode()
        raw_sock.sendall(payload)
        print(f"  →  Sent {payload!r}")
        time.sleep(0.05)
    except Exception as e:
        print(f"  ✗  Failed: {e}")

# ── STEP 3b: with \\r\\n ─────────────────────────────────────────────────────
print(f"\n[STEP 3b] Retry via a fresh socket with CR+LF termination ...")
raw_sock3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
raw_sock3.settimeout(TIMEOUT)
try:
    raw_sock3.connect((IP, PORT))
    for cmd in ["CH1:VOLT 5.0000", "CH1:CURR 1.0000"]:
        payload = (cmd + "\r\n").encode()
        raw_sock3.sendall(payload)
        print(f"  →  Sent {payload!r}")
        time.sleep(0.05)
    print(f"  ✓  Commands sent  (check instrument display for voltage/current change)")
except Exception as e:
    print(f"  ✗  Exception: {e}")
finally:
    raw_sock3.close()

raw_sock.close()

# ── STEP 4: pyvisa SOCKET open ────────────────────────────────────────────────
print(f"\n[STEP 4]  pyvisa ResourceManager → open_resource  (SOCKET)")
try:
    import pyvisa
    rm = pyvisa.ResourceManager()
    visa_addr = f"TCPIP0::{IP}::{PORT}::SOCKET"
    print(f"  address : {visa_addr}")
    res = rm.open_resource(visa_addr, timeout=5000)
    res.write_termination = "\n"
    res.read_termination  = "\n"
    print(f"  ✓  pyvisa resource opened")
    print(f"     resource_name : {getattr(res,'resource_name','?')}")
    print(f"     resource_class: {getattr(res,'resource_class','?')}")

    # STEP 5: IDN via pyvisa
    print(f"\n[STEP 5]  pyvisa  res.query('*IDN?') ...")
    try:
        idn = res.query("*IDN?")
        print(f"  ✓  IDN response: {idn!r}")
    except Exception as e:
        print(f"  ✗  *IDN? failed: {e}")

    # STEP 6: SCPI write via pyvisa
    print(f"\n[STEP 6]  Set CH1 voltage + current (write-only, no setpoint query) ...")
    try:
        res.write("CH1:VOLT 5.0000")
        print(f"  ✓  CH1:VOLT 5.0000  written")
        res.write("CH1:CURR 1.0000")
        print(f"  ✓  CH1:CURR 1.0000  written")
        res.write("*CLS")              # clear error queue
        print(f"  ✓  *CLS sent (clears error queue)")
    except Exception as e:
        print(f"  ✗  Write failed: {e}")

    print(f"\n[STEP 7]  Enable CH1 output ...")
    try:
        res.write("OUTP CH1,ON")
        print(f"  ✓  OUTP CH1,ON  written  — check front panel CH1 LED")
        time.sleep(0.2)
    except Exception as e:
        print(f"  ✗  Write failed: {e}")

    print(f"\n[STEP 8]  Probing measurement query syntax (1 s timeout each) ...")
    print(f"         CH1 output was just enabled — output should be active now")
    VOLT_QUERIES = [
        "MEAS:VOLT? CH1",       # pyvisa driver default — times out on V1.22
        "MEAS1:VOLT?",          # channel as prefix number
        "CH1:MEAS:VOLT?",       # channel-root form
        "VOUT1?",               # old GW Instek form
        "MEAS:VOLT:DC? CH1",    # explicit DC
        "MEAS:VOLT?",           # no channel (reads selected channel)
        "FETCH:VOLT? CH1",      # FETCH variant
        "READ:VOLT? CH1",       # READ variant
        "VOLT? CH1",            # short form
    ]
    CURR_QUERIES = [
        "MEAS:CURR? CH1",
        "MEAS1:CURR?",
        "CH1:MEAS:CURR?",
        "IOUT1?",
        "MEAS:CURR:DC? CH1",
        "MEAS:CURR?",
        "CURR? CH1",
    ]
    working_volt = None
    working_curr = None
    visa_addr_inner = f"TCPIP0::{IP}::{PORT}::SOCKET"
    for q in VOLT_QUERIES:
        try:
            r2 = rm.open_resource(visa_addr_inner, timeout=1500)
            r2.write_termination = "\n"; r2.read_termination = "\n"
            val = r2.query(q).strip()
            r2.close()
            print(f"  ✓  {q:<30s}  →  {val!r}  ← USE THIS")
            working_volt = q
            break
        except Exception:
            print(f"  ✗  {q:<30s}  →  timeout / error")
    if not working_volt:
        print(f"  ✗  No voltage query format worked — instrument may need output ON")

    for q in CURR_QUERIES:
        try:
            r2 = rm.open_resource(visa_addr_inner, timeout=1500)
            r2.write_termination = "\n"; r2.read_termination = "\n"
            val = r2.query(q).strip()
            r2.close()
            print(f"  ✓  {q:<30s}  →  {val!r}  ← USE THIS")
            working_curr = q
            break
        except Exception:
            print(f"  ✗  {q:<30s}  →  timeout / error")
    if not working_curr:
        print(f"  ✗  No current query format worked")

    print(f"\n[STEP 9]  SYST:ERR? — instrument error queue ...")
    try:
        res.write("*CLS")
        time.sleep(0.05)
        err = res.query("SYST:ERR?").strip()
        print(f"  ←  SYST:ERR? = {err!r}  (empty = no errors)")
    except Exception as e:
        print(f"     (not supported or timed out: {e})")

    if working_volt and working_curr:
        print(f"\n★  Working query commands for GWGPP4323 driver:")
        print(f"     measure_voltage: {working_volt!r}")
        print(f"     measure_current: {working_curr!r}")

    res.close()
    rm.close()

except Exception as e:
    print(f"  ✗  pyvisa open_resource failed: {e}")
    print(f"     This means pyvisa-py or the NI-VISA backend can't open a SOCKET resource.")
    print(f"     Check:  pip install pyvisa-py")
    print(f"     Or set  VISA_LIBRARY=@py  environment variable")

print("\n" + "=" * 60)
print("  Diagnostic complete.")
print("  ► Look for ✓ / ✗ above to identify which step fails.")
print("=" * 60)
