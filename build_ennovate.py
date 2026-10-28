# -*- coding: utf-8 -*-
"""
Build the Ennovate 2026 entry PPTX — updates slides 3-7 only.
Uses the current entry PPTX (with user-edited slides 1-2) as input.
"""

import copy
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from pptx.dml.color import RGBColor
from pptx.shapes.group import GroupShape

INPUT  = r"C:\Users\Dell\CascadeProjects\automated-test-script\Ennovate-2026-Entry.pptx"
OUTPUT = r"C:\Users\Dell\CascadeProjects\automated-test-script\Ennovate-2026-Entry.pptx"

prs = Presentation(INPUT)

# ── Fonts used in template ──────────────────────────────────────────────────
FONT_TITLE   = "Enphase Visuelt"
FONT_BODY    = "Calibri"
FONT_HEADING = "Enphase Visuelt"
WHITE        = RGBColor(0xFF, 0xFF, 0xFF)
DARK         = RGBColor(0x10, 0x0F, 0x0D)
ORANGE       = RGBColor(0xF2, 0x8C, 0x28)


def set_text(shape, lines, font_name=FONT_BODY, font_size=Pt(16),
             color=WHITE, bold=False, alignment=PP_ALIGN.LEFT):
    """Replace all text in a shape with new lines, preserving shape position."""
    tf = shape.text_frame
    tf.clear()
    for i, line in enumerate(lines):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = line
        p.alignment = alignment
        for run in p.runs:
            run.font.name = font_name
            run.font.size = font_size
            run.font.color.rgb = color
            run.font.bold = bold


def set_para_rich(shape, paragraphs):
    """Set paragraphs with mixed formatting.
    paragraphs = [ (text, font_name, font_size, color, bold), ... ]
    """
    tf = shape.text_frame
    tf.clear()
    for i, (text, fn, fs, clr, bld) in enumerate(paragraphs):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = text
        p.alignment = PP_ALIGN.LEFT
        for run in p.runs:
            run.font.name = fn
            run.font.size = fs
            run.font.color.rgb = clr
            run.font.bold = bld


def find_shape(slide, name):
    for s in slide.shapes:
        if s.name == name:
            return s
    return None


def find_group_text(group_shape):
    """Find text shapes inside a group."""
    texts = []
    if isinstance(group_shape, GroupShape):
        for child in group_shape.shapes:
            if child.has_text_frame:
                texts.append(child)
    return texts


def set_group_text(group_shape, new_texts):
    """Set text on text shapes found inside a group.
    new_texts is a list of strings matching the order of text shapes.
    """
    text_shapes = find_group_text(group_shape)
    for i, ts in enumerate(text_shapes):
        if i < len(new_texts):
            for para in ts.text_frame.paragraphs:
                for run in para.runs:
                    run.text = new_texts[i]


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDES 1-2 — SKIPPED (user-edited, do not touch)
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDE 3 — VALUE PROPOSITION
# ═══════════════════════════════════════════════════════════════════════════════
slide3 = prs.slides[2]

value_tb = find_shape(slide3, "TextBox 11")
set_para_rich(value_tb, [
    ("", FONT_BODY, Pt(14), WHITE, False),
    ("Business Impact: Eliminates the manual setup and probing bottleneck. "
     "Engineers spend most of their testing time configuring equipment and "
     "physically handling boards\u200a\u2014\u200athis system automates both. "
     "Test setup that previously took hours now takes minutes through the GUI, "
     "and the 544-channel measurement board scans all 553 test points without "
     "human touch. Tests run faster and far more rigorously.",
     FONT_BODY, Pt(16), WHITE, False),
    ("", FONT_BODY, Pt(14), WHITE, False),
    ("Relevance: Directly supports IQ Battery C80 NPI and future battery "
     "platforms. As BMS complexity grows, manual probing and ad-hoc setups "
     "become untenable. The measurement board scales linearly (add MUX blocks "
     "for more channels), and the GUI already controls 50+ instrument types "
     "from the Enphase equipment library\u200a\u2014\u200aso it adapts to any "
     "test bench configuration.",
     FONT_BODY, Pt(16), WHITE, False),
    ("", FONT_BODY, Pt(14), WHITE, False),
    ("ROI: Significantly reduces engineer and technician time per test station. "
     "Eliminates board damage caused by manual rework and accidental wire "
     "shorts during probing. Enables rigorous regression testing with hundreds "
     "of test cases that would be impractical to run manually. Payback is "
     "estimated within one product cycle.",
     FONT_BODY, Pt(16), WHITE, False),
    ("", FONT_BODY, Pt(14), WHITE, False),
    ("Uniqueness: No COTS solution combines a 544-channel on-board analog MUX "
     "with voltage scaling, 24-bit simultaneous-sampling ADCs, and a desktop "
     "automation GUI with parametric sweep and sequence runners\u200a\u2014\u200a"
     "all in one integrated system. Commercial alternatives (flying-probe, "
     "bed-of-nails) are expensive and not reconfigurable across board revisions.",
     FONT_BODY, Pt(16), WHITE, False),
    ("", FONT_BODY, Pt(14), WHITE, False),
    ("Proof of Concept: The automation GUI is fully functional today, controlling "
     "ITECH, GPP-4323, MDO34, WT5000, GDM-8261A, NGI N83624, DAQ 973A, and "
     "ESPEC chambers. The measurement board schematic (2708 components, 2843 nets, "
     "553 FT pads) is complete with on-board voltage scaling. A live demo of the "
     "GUI driving real instruments is feasible during Ennovate.",
     FONT_BODY, Pt(16), WHITE, False),
])


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDE 4 — SOLUTION DESCRIPTION
# ═══════════════════════════════════════════════════════════════════════════════
slide4 = prs.slides[3]

soln_tb = find_shape(slide4, "TextBox 10")
set_para_rich(soln_tb, [
    ("Problem \u2192 Solution Mapping",
     FONT_HEADING, Pt(20), ORANGE, True),
    ("Manual setup & probing \u2192 GUI-driven instrument control + 544-ch MUX board "
     "scans all 553 FTs without human touch",
     FONT_BODY, Pt(15), WHITE, False),
    ("Board damage from rework/shorts \u2192 No physical probing; signals routed "
     "through on-board voltage dividers and MUX matrix",
     FONT_BODY, Pt(15), WHITE, False),
    ("Limited test cases \u2192 Custom test sequences built in minutes via GUI; "
     "parametric sweeps run hundreds of cases overnight unattended",
     FONT_BODY, Pt(15), WHITE, False),
    ("", FONT_BODY, Pt(12), WHITE, False),
    ("Hardware \u2014 Measurement Board (600-02074, 2708 components)",
     FONT_HEADING, Pt(20), ORANGE, True),
    ("STM32F412 MCU orchestrates 16\u00d732MuxBlock modules (32\u00d774HC4067 + "
     "48\u00d7OPA196 + 16\u00d774HC595) feeding 8\u00d7TMUX4052 group MUXes into "
     "2\u00d7 daisy-chained ADS131E08 24-bit ADCs. On-board voltage scaling "
     "(1M/330K dividers) handles HV inputs up to 80 V. Shifted 5\u201310 V analog "
     "domain for MUX/buffer stage; ADC operates 0\u20135 V unipolar. "
     "553 SMD test pads + 10 keystone TPs. Isolated CAN bus for host communication.",
     FONT_BODY, Pt(15), WHITE, False),
    ("", FONT_BODY, Pt(12), WHITE, False),
    ("Software \u2014 Automation GUI",
     FONT_HEADING, Pt(20), ORANGE, True),
    ("PyQt6 desktop app with sidebar nav, detachable instrument panels, real-time "
     "dashboard with live readings. Instrument Library panel connects 50+ instrument "
     "types via NI-VISA. Engineers build custom test sequences in the GUI quickly "
     "\u2014 no scripting required. Thread-safe VISA access with per-instrument "
     "locking and persistent SOCKET connections.",
     FONT_BODY, Pt(15), WHITE, False),
    ("", FONT_BODY, Pt(12), WHITE, False),
    ("Key Differentiators",
     FONT_HEADING, Pt(20), ORANGE, True),
    ("\u2022 Parametric Sweep Engine: linear/decade steps across any source, "
     "multi-instrument measurement, Excel output with embedded scope waveforms",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 Sequence Runner: step-table editor with per-step instrument control "
     "(GPP mode, ITECH, ESPEC thermal, DAQ relay switching)",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 E-STOP (Ctrl+Shift+X): instant all-instrument shutdown, hardware-safe",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 On-board voltage scaling: adapts to HV/LV/VLV/DIFF domains without "
     "external fixtures",
     FONT_BODY, Pt(14), WHITE, False),
])


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDE 5 — ADDITIONAL INFO (architecture details)
# ═══════════════════════════════════════════════════════════════════════════════
slide5 = prs.slides[4]

# Remove the old textbox if it exists from a previous run
old_boxes = [s for s in slide5.shapes
             if s.has_text_frame and s.name not in ("Group 2",)
             and not isinstance(s, GroupShape)]
for ob in old_boxes:
    sp = ob._element
    sp.getparent().remove(sp)

from pptx.util import Inches

txBox = slide5.shapes.add_textbox(
    Emu(1200000), Emu(3400000),
    Emu(15500000), Emu(6200000)
)
tf = txBox.text_frame
tf.word_wrap = True

arch_lines = [
    ("SYSTEM ARCHITECTURE", FONT_HEADING, Pt(24), ORANGE, True),
    ("", FONT_BODY, Pt(10), WHITE, False),
    ("Signal Flow:  FT Test Points (553) \u2192 On-board Voltage Dividers (1M/330K) "
     "\u2192 544-ch MUX Matrix (5\u201310 V domain) \u2192 Group MUX (TMUX4052, 0\u201310 V) "
     "\u2192 24-bit ADC (ADS131E08 \u00d72, 0\u20135 V) \u2192 STM32F412 MCU \u2192 "
     "CAN/UART \u2192 Host PC \u2192 Automation GUI",
     FONT_BODY, Pt(15), WHITE, False),
    ("", FONT_BODY, Pt(10), WHITE, False),
    ("MUX Architecture (per 32MuxBlock, \u00d716 instances):",
     FONT_HEADING, Pt(18), ORANGE, True),
    ("  2\u00d7 74HC4067 (16:1 MUX, VCC=10 V, VSS=5 V)  \u2192  "
     "30 regular + 4 differential inputs",
     FONT_BODY, Pt(14), WHITE, False),
    ("  3\u00d7 OPA196 (RRIO op-amp, V+=10 V, V\u2212=5 V)  \u2192  "
     "unity-gain buffer + 2 differential amplifiers",
     FONT_BODY, Pt(14), WHITE, False),
    ("  1\u00d7 74HC595 (shift register, VCC=5 V)  \u2192  "
     "MUX address, enable, and group MUX select",
     FONT_BODY, Pt(14), WHITE, False),
    ("  Voltage scaling per channel: IN0-IN19 direct, IN20-IN29 through 1M/330K dividers",
     FONT_BODY, Pt(14), WHITE, False),
    ("", FONT_BODY, Pt(10), WHITE, False),
    ("GUI Software Stack:", FONT_HEADING, Pt(18), ORANGE, True),
    ("\u2022 Python 3 + PyQt6 + pyqtgraph  |  pyvisa / NI-VISA (TCPIP, GPIB, RS-232, SOCKET)",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 Instrument Registry: 50+ instruments across 15 categories (DC/AC sources, "
     "loads, scopes, DMMs, power meters, chambers, SAS, SMU, etc.)",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 Custom test case builder: engineers design sequences and sweeps in the GUI, "
     "no scripting needed",
     FONT_BODY, Pt(14), WHITE, False),
    ("\u2022 Thread-safe VISA workers, persistent SOCKET connections, E-STOP safety shutdown",
     FONT_BODY, Pt(14), WHITE, False),
    ("", FONT_BODY, Pt(10), WHITE, False),
    ("Voltage Domains Covered:", FONT_HEADING, Pt(18), ORANGE, True),
    ("  HV (10\u201380 V, on-board 1M/330K dividers)  |  LV (1\u201310 V)  |  "
     "VLV (<1 V)  |  DIFF (mV-level shunt signals)",
     FONT_BODY, Pt(14), WHITE, False),
    ("  553 FT pads + 10 keystone TPs  |  2708 components  |  2843 nets",
     FONT_BODY, Pt(14), WHITE, False),
]

for i, (text, fn, fs, clr, bld) in enumerate(arch_lines):
    if i == 0:
        p = tf.paragraphs[0]
    else:
        p = tf.add_paragraph()
    p.text = text
    p.alignment = PP_ALIGN.LEFT
    for run in p.runs:
        run.font.name = fn
        run.font.size = fs
        run.font.color.rgb = clr
        run.font.bold = bld


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDE 6 — ROADMAP
# ═══════════════════════════════════════════════════════════════════════════════
slide6 = prs.slides[5]

roadmap_tb = find_shape(slide6, "TextBox 10")
set_para_rich(roadmap_tb, [
    ("Next Steps: Complete PCB layout \u2192 prototype build \u2192 firmware bring-up \u2192 "
     "integrate with GUI \u2192 validate against known-good BMU boards \u2192 deploy to "
     "validation lab.",
     FONT_BODY, Pt(16), WHITE, False),
    ("", FONT_BODY, Pt(12), WHITE, False),
    ("Resources Needed: PCB fabrication & assembly budget, STM32 firmware development support, "
     "access to BMU golden-unit boards for correlation, and lab bench space for system integration.",
     FONT_BODY, Pt(16), WHITE, False),
])

# Roadmap step groups
step_data = [
    ("Q4 2026", "STEP 1:\nPCB fab + assembly\nFirmware bring-up\nADC calibration"),
    ("Q1 2027", "STEP 2:\nGUI integration\nMUX board driver\nEnd-to-end readback"),
    ("Q2 2027", "STEP 3:\nCorrelation testing\nvs manual probing\nAccuracy validation"),
    ("Q3 2027", "STEP 4:\nLab deployment\nOperator training\nProcess rollout"),
]

step_groups = []
for shape in slide6.shapes:
    if isinstance(shape, GroupShape) and shape.name.startswith("Group ") and shape.name not in ("Group 2",):
        # Check if it has OUTCOME / STEP text
        texts = find_group_text(shape)
        if len(texts) >= 2:
            step_groups.append(shape)

# Sort by left position to get correct order
step_groups.sort(key=lambda s: s.left)

for i, grp in enumerate(step_groups):
    if i < len(step_data):
        timeline, desc = step_data[i]
        set_group_text(grp, [timeline, desc])


# ═══════════════════════════════════════════════════════════════════════════════
# SLIDE 7 — TEAM
# ═══════════════════════════════════════════════════════════════════════════════
slide7 = prs.slides[6]

team_members = [
    "YOUR NAME",
    "TEAM MEMBER 2",
    "TEAM MEMBER 3",
]

member_groups = []
for shape in slide7.shapes:
    if isinstance(shape, GroupShape) and shape.name not in ("Group 2",):
        texts = find_group_text(shape)
        if len(texts) >= 1:
            member_groups.append(shape)

member_groups.sort(key=lambda s: s.left)

for i, grp in enumerate(member_groups):
    if i < len(team_members):
        set_group_text(grp, [team_members[i]])


# ═══════════════════════════════════════════════════════════════════════════════
# SAVE
# ═══════════════════════════════════════════════════════════════════════════════
prs.save(OUTPUT)
print("Saved to:", OUTPUT)
