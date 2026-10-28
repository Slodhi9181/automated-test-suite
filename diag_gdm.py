"""
diag_gdm.py — GDM-8261A timing diagnostic.
Measures elapsed time for every VISA operation to pinpoint the 5-second delay.

Run: python diag_gdm.py
"""
import time, sys, pyvisa

ADDR = "ASRL5::INSTR"   # adjust if needed

TIMEOUT_MS = 5000        # match app default

def t(label, fn):
    t0 = time.perf_counter()
    try:
        result = fn()
        elapsed = time.perf_counter() - t0
        print(f"  [{elapsed*1000:7.1f} ms]  OK   {label}: {str(result)[:80]}")
        return result
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"  [{elapsed*1000:7.1f} ms]  FAIL {label}: {e}")
        return None

print("=" * 70)
print("GDM-8261A TIMING DIAGNOSTIC")
print("=" * 70)

# ── Step 1: open resource manager ─────────────────────────────────────────
rm = t("Open ResourceManager", lambda: pyvisa.ResourceManager())
if rm is None:
    sys.exit(1)

# ── Step 2: list resources ─────────────────────────────────────────────────
t("List resources", lambda: rm.list_resources())

# ── Step 3: open ASRL resource ────────────────────────────────────────────
res = t(f"Open {ADDR}", lambda: rm.open_resource(ADDR, timeout=TIMEOUT_MS))
if res is None:
    sys.exit(1)

res.write_termination = "\n"
res.read_termination  = "\n"

# ── Step 4: probe termination characters ──────────────────────────────────
# GDM-8261A may use \r\n or \r — try both
print("\n--- Probing termination characters ---")
for wt, rt in [("\n","\n"), ("\r\n","\r\n"), ("\n","\r\n"), ("","\n"), ("","\r\n")]:
    res.write_termination = wt
    res.read_termination  = rt
    res.timeout = 2000
    r = t(f"*IDN? wt={repr(wt)} rt={repr(rt)}",
          lambda: res.query("*IDN?").strip())
    if r:
        print(f"  *** GOOD termination: write={repr(wt)} read={repr(rt)}")
        break

# ── Step 5: SYST:ERR? support ─────────────────────────────────────────────
print("\n--- Error queue support ---")
res.timeout = 1000
t("*CLS",      lambda: res.write("*CLS"))
t("SYST:ERR?", lambda: res.query("SYST:ERR?").strip())

# ── Step 6: CONF command ──────────────────────────────────────────────────
print("\n--- Configuration commands ---")
res.timeout = 2000
t("CONF:VOLT:DC",             lambda: res.write("CONF:VOLT:DC"))
t("RATE S",                   lambda: res.write("RATE S"))
t("RATE M",                   lambda: res.write("RATE M"))
t("RATE F",                   lambda: res.write("RATE F"))

# ── Step 7: probe measurement queries ─────────────────────────────────────
print("\n--- Measurement query variants (VOLT:DC, RATE=F first) ---")
res.timeout = 3000
t("RATE F", lambda: res.write("RATE F"))

for q in [
    "MEAS:VOLT:DC?",
    "MEAS:VOLT:DC? AUTO,DEF",
    "READ?",
    "FETCH?",
    "MEAS:VOLT:DC? AUTO",
]:
    res.timeout = 3000
    r = t(q, lambda qq=q: res.query(qq).strip())
    if r is not None:
        print(f"  *** Working query: {q!r}  result={r}")
        break

# ── Step 8: repeated fast measurements with winner ────────────────────────
winner = None
for q in ["MEAS:VOLT:DC?", "READ?", "FETCH?"]:
    res.timeout = 3000
    r = t(f"Probe {q}", lambda qq=q: res.query(qq).strip())
    if r is not None:
        winner = q
        break

if winner:
    print(f"\n--- 5x repeated measurements using {winner!r} ---")
    for i in range(5):
        res.timeout = 3000
        t(f"Meas #{i+1}", lambda qq=winner: res.query(qq).strip())

# ── Step 9: close ─────────────────────────────────────────────────────────
print("\n--- Cleanup ---")
t("res.close()", lambda: res.close())
t("rm.close()",  lambda: rm.close())

print("\n" + "=" * 70)
print("Done. Look for the longest elapsed times above to find the bottleneck.")
print("=" * 70)
