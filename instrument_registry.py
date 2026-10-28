# -*- coding: utf-8 -*-
"""
Enphase testlib instrument registry.

Each entry describes one driver available from enphase_equipment.
Fields
------
name       : Display name shown in the UI
cls        : Class name inside the module
module     : Full dotted import path
key        : Short unique key used for JSON config persistence
tab_label  : Abbreviated label for the instrument tab
init_args  : Extra __init__ kwargs (beyond resource_name) that need defaults
panel_cls  : (optional) Name of the dedicated panel widget class in panels/
addr_hint  : (optional) Placeholder hint for the Addresses tab
ka_label   : (optional) Short label for the keepalive / dashboard status display
"""

from __future__ import annotations
from typing import Optional
import importlib
import logging

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES: dict[str, list[dict]] = {

    "DC Source": [
        {"name": "ITECH IT6005C-80-150",
         "cls": "ITechFixed",  "module": "test_automation",
         "key": "itech",       "tab_label": "ITECH DC",
         "panel_cls": "ItechPanel",
         "addr_hint": "e.g. TCPIP::192.168.1.10::INSTR",
         "ka_label": "ITECH"},
        {"name": "Agilent N5746A",
         "cls": "AgilentN5746A", "module": "enphase_equipment.dc_source.agilent",
         "key": "agilent_n5746", "tab_label": "Agilent N5746"},
        {"name": "Agilent 6030A",
         "cls": "Agilent6030A",  "module": "enphase_equipment.dc_source.agilent",
         "key": "agilent_6030", "tab_label": "Agilent 6030"},
        {"name": "Agilent E3631A",
         "cls": "AgilentE3631A", "module": "enphase_equipment.dc_source.agilent",
         "key": "agilent_e3631", "tab_label": "Agilent E3631"},
        {"name": "Amrel SPS100-60",
         "cls": "Amrel_SPS100_60_K022", "module": "enphase_equipment.dc_source.amrel",
         "key": "amrel_sps100", "tab_label": "Amrel SPS100"},
        {"name": "Amrel SPS60-20",
         "cls": "Amrel_SPS60_20_V020",  "module": "enphase_equipment.dc_source.amrel",
         "key": "amrel_sps60",  "tab_label": "Amrel SPS60"},
        {"name": "Amtek XG 60-14",
         "cls": "Amtek_XG_60_14", "module": "enphase_equipment.dc_source.amtek",
         "key": "amtek_xg60",  "tab_label": "Amtek XG60"},
        {"name": "Kikusui DC Source",
         "cls": "Kikusui",     "module": "enphase_equipment.dc_source.kikusui",
         "key": "kikusui_dc",  "tab_label": "Kikusui DC"},
        {"name": "Rigol DC Load DL3000",
         "cls": "RigolDL3000", "module": "enphase_equipment.dc_load.rigol",
         "key": "rigol_dl",    "tab_label": "Rigol DL"},
    ],

    "AC Source": [
        {"name": "Chroma 61815  (15 kVA)",
         "cls": "Chroma61815",  "module": "enphase_equipment.ac_source.chroma",
         "key": "chroma_61815", "tab_label": "Chroma 61815"},
        {"name": "Chroma 61830  (30 kVA)",
         "cls": "Chroma61830",  "module": "enphase_equipment.ac_source.chroma",
         "key": "chroma_61830", "tab_label": "Chroma 61830"},
        {"name": "Chroma 61845  (45 kVA)",
         "cls": "Chroma61845",  "module": "enphase_equipment.ac_source.chroma",
         "key": "chroma_61845", "tab_label": "Chroma 61845"},
        {"name": "Chroma 61860  (60 kVA)",
         "cls": "Chroma61860",  "module": "enphase_equipment.ac_source.chroma",
         "key": "chroma_61860", "tab_label": "Chroma 61860"},
        {"name": "Ametek CSW AC Source",
         "cls": "AmetekCSW",    "module": "enphase_equipment.ac_source.ametek",
         "key": "ametek_csw",   "tab_label": "Ametek CSW"},
        {"name": "NHR AC Source",
         "cls": "NHR",          "module": "enphase_equipment.ac_source.nhr",
         "key": "nhr_ac",       "tab_label": "NHR AC"},
    ],

    "DC Load": [
        {"name": "Agilent N3300A DC Load",
         "cls": "AgilentN3300A",  "module": "enphase_equipment.dc_load.agilent",
         "key": "agilent_dcload", "tab_label": "Agilent DC Load"},
        {"name": "Agilent N3301A DC Load",
         "cls": "AgilentN3301A",  "module": "enphase_equipment.dc_load.agilent",
         "key": "agilent_n3301",  "tab_label": "Agilent N3301"},
        {"name": "Agilent 6051A DC Load",
         "cls": "Agilent6051A",   "module": "enphase_equipment.dc_load.agilent",
         "key": "agilent_6051",   "tab_label": "Agilent 6051"},
        {"name": "Rigol DL3000 DC Load",
         "cls": "RigolDL3000",    "module": "enphase_equipment.dc_load.rigol",
         "key": "rigol_dl3000",   "tab_label": "Rigol DL3000"},
    ],

    "AC Load": [
        {"name": "Chroma 63802  (2 kVA)",
         "cls": "Chroma63802",     "module": "enphase_equipment.ac_load.chroma",
         "key": "chroma_63802",    "tab_label": "Chroma 63802"},
        {"name": "Chroma 63804  (4 kVA)",
         "cls": "Chroma63804",     "module": "enphase_equipment.ac_load.chroma",
         "key": "chroma_63804",    "tab_label": "Chroma 63804"},
        {"name": "Chroma 63815R  (15 kVA)",
         "cls": "Chroma63815R",    "module": "enphase_equipment.ac_load.chroma",
         "key": "chroma_63815",    "tab_label": "Chroma 63815"},
        {"name": "NHR 9430-12KW",
         "cls": "NHR9430_12KW",   "module": "enphase_equipment.ac_load.nhr",
         "key": "nhr_9430_12kw",  "tab_label": "NHR 9430 12kW"},
        {"name": "NHR 9430-24KW",
         "cls": "NHR9430_24KW",   "module": "enphase_equipment.ac_load.nhr",
         "key": "nhr_9430_24kw",  "tab_label": "NHR 9430 24kW"},
    ],

    "Oscilloscope": [
        {"name": "Tektronix MDO34",
         "cls": "MDO34Driver",       "module": "test_automation",
         "key": "mdo",               "tab_label": "MDO34",
         "panel_cls": "MdoPanel",
         "addr_hint": "e.g. TCPIP::192.168.1.13::INSTR",
         "ka_label": "MDO34"},
        {"name": "Tektronix 4000 series  (MDO4034 / DPO4034 …)",
         "cls": "Tektronix4000",     "module": "enphase_equipment.oscilloscope.tektronix4000",
         "key": "tek_4000",          "tab_label": "Tektronix 4k"},
        {"name": "Tektronix MSO4034",
         "cls": "TektronixMSO4034",  "module": "enphase_equipment.oscilloscope.tektronix4000",
         "key": "tek_mso4034",       "tab_label": "Tek MSO4034"},
        {"name": "Tektronix DPO4034",
         "cls": "TektronixDPO4034",  "module": "enphase_equipment.oscilloscope.tektronix4000",
         "key": "tek_dpo4034",       "tab_label": "Tek DPO4034"},
        {"name": "Tektronix DPO3034",
         "cls": "TektronixDPO3034",  "module": "enphase_equipment.oscilloscope.tektronix",
         "key": "tek_dpo3034",       "tab_label": "Tek DPO3034"},
        {"name": "Agilent DSOX 3034A",
         "cls": "AgilentDSOX3034A",  "module": "enphase_equipment.oscilloscope.agilent",
         "key": "agilent_dso3034",   "tab_label": "Agilent DSO"},
        {"name": "R&S RTE Oscilloscope",
         "cls": "RandSRTE",          "module": "enphase_equipment.oscilloscope.r_and_s",
         "key": "rands_rte",         "tab_label": "R&S RTE"},
    ],

    "DMM / Multimeter": [
        {"name": "Keysight 34461A",
         "cls": "Agilent34401A",  "module": "enphase_equipment.multi_meter.agilent",
         "key": "dmm",            "tab_label": "Keysight DMM",
         "panel_cls": "DmmPanel",
         "addr_hint": "e.g. TCPIP::192.168.1.12::INSTR",
         "ka_label": "DMM"},
    ],

    "Power Meter": [
        {"name": "Yokogawa WT5000",
         "cls": "YokogawaWT5000", "module": "enphase_equipment.power_meter.yokogawa",
         "key": "wt5000",         "tab_label": "Yoko WT5000",
         "panel_cls": "WT5000Panel",
         "addr_hint": "e.g. TCPIP::192.168.1.XXX::INSTR",
         "ka_label": "WT5000"},
        {"name": "Yokogawa WT3000",
         "cls": "YokogawaWT3000", "module": "enphase_equipment.power_meter.yokogawa",
         "key": "yoko_wt3000",    "tab_label": "Yoko WT3000"},
        {"name": "Yokogawa WT1800",
         "cls": "YokogawaWT1800", "module": "enphase_equipment.power_meter.yokogawa",
         "key": "yoko_wt1800",    "tab_label": "Yoko WT1800"},
        {"name": "Yokogawa WT500",
         "cls": "YokogawaWT500",  "module": "enphase_equipment.power_meter.yokogawa",
         "key": "yoko_wt500",     "tab_label": "Yoko WT500"},
        {"name": "Hioki 3193",
         "cls": "Hioki3193",      "module": "enphase_equipment.power_meter.hioki",
         "key": "hioki_3193",     "tab_label": "Hioki 3193"},
        {"name": "Radian RD20",
         "cls": "RD20",           "module": "enphase_equipment.power_meter.radian",
         "key": "radian_rd20",    "tab_label": "Radian RD20"},
    ],

    "Power Analyzer": [
        {"name": "Hioki PW3335",
         "cls": "PW3335",         "module": "enphase_equipment.power_analyzer.hioki",
         "key": "hioki_pw3335",   "tab_label": "Hioki PW3335"},
    ],

    "Thermal Chamber": [
        {"name": "ESPEC SH-662",
         "cls": "Espec",           "module": "enphase_equipment.thermal_chamber.espec",
         "key": "espec",           "tab_label": "ESPEC",
         "addr_hint": "e.g. ASRL6::INSTR  (RS-232 COM port)",
         "ka_label": "ESPEC"},
        {"name": "Thermotron",
         "cls": "Thermotron",      "module": "enphase_equipment.thermal_chamber.thermotron_v2",
         "key": "thermotron",      "tab_label": "Thermotron"},
    ],

    "Solar Array Simulator": [
        {"name": "Agilent E4350A SAS",
         "cls": "AgilentE4350A",   "module": "enphase_equipment.solar_array_simulator.agilent",
         "key": "sas_e4350",       "tab_label": "Agilent E4350"},
        {"name": "Agilent E4360A SAS",
         "cls": "AgilentE4360A",   "module": "enphase_equipment.solar_array_simulator.agilent",
         "key": "sas_e4360",       "tab_label": "Agilent E4360"},
        {"name": "Keysight MP4300 SAS",
         "cls": "KeysightMP4300",  "module": "enphase_equipment.solar_array_simulator.agilent",
         "key": "sas_mp4300",      "tab_label": "Keysight MP4300"},
        {"name": "ITECH N2131 SAS",
         "cls": "N2131",           "module": "enphase_equipment.solar_array_simulator.itech",
         "key": "sas_n2131",       "tab_label": "ITECH N2131"},
    ],

    "LCR Meter": [
        {"name": "Agilent 4284A LCR Meter",
         "cls": "Agilent4284A",   "module": "enphase_equipment.lcr_meter.agilent",
         "key": "agilent_lcr",    "tab_label": "Agilent LCR"},
        {"name": "Microtest 6632 LCR Meter",
         "cls": "Microtest6632",  "module": "enphase_equipment.lcr_meter.microtest",
         "key": "microtest_6632", "tab_label": "Microtest LCR"},
    ],

    "Arb Waveform Gen": [
        {"name": "Agilent 33220A AWG",
         "cls": "Agilent33220A",  "module": "enphase_equipment.arb_wave_gen.agilent",
         "key": "awg_33220",      "tab_label": "Agilent 33220"},
        {"name": "Agilent 33522A AWG",
         "cls": "Agilent33522A",  "module": "enphase_equipment.arb_wave_gen.agilent",
         "key": "awg_33522",      "tab_label": "Agilent 33522"},
    ],

    "Source Measure Unit": [
        {"name": "Keithley 2600 SMU",
         "cls": "Keithley26XXA",  "module": "enphase_equipment.source_measurement_unit.keithley",
         "key": "keithley_2600",  "tab_label": "Keithley SMU"},
    ],

    "Spectrum Analyzer": [
        {"name": "Agilent N9010A Spectrum Analyzer",
         "cls": "AgilentN9010A",  "module": "enphase_equipment.spectrum_analyzer.agilent",
         "key": "agilent_sa",     "tab_label": "Agilent SA"},
    ],

    "Impedance Analyzer": [
        {"name": "Agilent 4294A Impedance Analyzer",
         "cls": "Agilent4294A",   "module": "enphase_equipment.impedance_analyzer.agilent",
         "key": "agilent_ia",     "tab_label": "Agilent IA"},
    ],

    "Relay / Switch": [
        {"name": "Keysight DAQ 973A",
         "cls": "Keysight34980A", "module": "enphase_equipment.relay.keysight",
         "key": "daq",            "tab_label": "Keysight DAQ",
         "addr_hint": "e.g. TCPIP::192.168.1.14::INSTR",
         "ka_label": "DAQ"},
    ],

    "Multimeter (GW Instek)": [
        {"name": "GW Instek GDM-8261A  (6.5-digit Dual DMM)",
         "cls": "GDM8261AAdapter", "module": "test_automation",
         "key": "gdm",             "tab_label": "GDM-8261A",
         "panel_cls": "GdmPanel",
         "addr_hint": "e.g. ASRL5::INSTR  or  TCPIP::...",
         "ka_label": "GDM"},
    ],

    "Battery Simulator": [
        {"name": "NGI N83624  (24-CH Cell Simulator)",
         "cls": "NgiN83624Adapter", "module": "test_automation",
         "key": "ngi",              "tab_label": "NGI N83624",
         "panel_cls": "NgiCellSimPanel",
         "addr_hint": "e.g. TCPIP::10.73.77.200::7000::SOCKET",
         "ka_label": "NGI"},
    ],

    "DC Power Supply (GW Instek)": [
        {"name": "GW Instek GPP-4323  (4-Channel DC Supply)",
         "cls": "GPP4323Adapter",  "module": "test_automation",
         "key": "gpp",             "tab_label": "GPP-4323",
         "panel_cls": "GppPanel",
         "addr_hint": "e.g. TCPIP::192.168.1.11::INSTR",
         "ka_label": "GPP"},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def flat_list() -> list[dict]:
    """Return a flat list of all instrument entries with 'category' field added."""
    out = []
    for cat, entries in CATEGORIES.items():
        for e in entries:
            out.append({**e, "category": cat})
    return out


def find_by_key(key: str) -> Optional[dict]:
    """Return the registry entry for *key*, or None."""
    for entry in flat_list():
        if entry["key"] == key:
            return entry
    return None


def load_driver(entry: dict, resource_name: str):
    """
    Instantiate the instrument driver for *entry* using *resource_name*.

    Returns the driver object or raises on import/init failure.
    """
    mod = importlib.import_module(entry["module"])
    cls = getattr(mod, entry["cls"])
    return cls(resource_name)


def native_instruments() -> list[dict]:
    """Return entries that have dedicated panel widgets (have 'panel_cls' set)."""
    return [e for e in flat_list() if e.get("panel_cls")]


def address_meta() -> dict[str, tuple[str, str]]:
    """Return {key: (display_name, addr_hint)} for instruments with addr_hint.

    Used by AddressesTab to auto-generate its address form.
    """
    out = {}
    for e in flat_list():
        if e.get("addr_hint"):
            out[e["key"]] = (e["name"], e["addr_hint"])
    return out


def keepalive_keys() -> list[tuple[str, str]]:
    """Return [(key, ka_label), ...] for instruments with ka_label.

    Used by DashboardTab for keepalive status indicators.
    """
    return [(e["key"], e["ka_label"])
            for e in flat_list() if e.get("ka_label")]


def instrument_tab_map() -> dict[str, str]:
    """Return {key: panel_cls_name} for instruments with dedicated panels.

    Used by MainWindow to map instrument keys to panel tab names.
    """
    return {e["key"]: e["panel_cls"]
            for e in flat_list() if e.get("panel_cls")}
