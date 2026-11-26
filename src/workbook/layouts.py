"""Table specifications (keys, headers, units, widths) and layout construction."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .ctx import Ctx
from .model import Layout

FIRST = 7          # first data row of ordinary tables (rows 4 band, 5 header, 6 unit)
ENG_FIRST = 8      # engine: band 4, header 5, unit 6, code 7
NPASS = 14         # Newton iterations in the Pressure tab
NBIS = 24          # bisection steps for the uncontrolled pressure
OA_ROUNDS = 4

Spec = List[Tuple[str, str, str, float]]


def trip(key: str, header: str, unit: str, width: float, with_prov: bool = True) -> Spec:
    out: Spec = [(key, header, unit, width)]
    if with_prov:
        out += [(key + "_c", "Code", "", 5), (key + "_s", "Source ID", "", 11)]
    return out


def ri_spec() -> Spec:
    s: Spec = []
    s += trip("no", "Room number", "", 14) + trip("name", "Room name", "", 28) + trip("storey", "Storey", "", 8)
    s += trip("ahu", "AHU or system tag", "", 24) + trip("zone", "Zone tag", "", 8) + trip("systype", "System type", "", 10)
    s += trip("terminal", "Terminal tag", "", 14) + trip("exfan", "Exhaust fan tag", "", 14)
    s += trip("oasrc", "Outdoor-air source (FCU)", "", 16) + trip("oacond", "Outdoor air to FCU", "", 11)
    s += trip("sptype", "Space type", "", 20)
    s += trip("cond", "Room condition", "", 10)
    s += trip("dpa", "DPA designed area", "m²", 9)
    s += trip("soa", "SOA area (or area label on the drawing)", "m²", 9)
    s += trip("meas", "Measured area", "m²", 9)
    s += trip("sel", "Area-used selector", "", 10)
    s += [("base", "Design area: DPA, else SOA", "m²", 9), ("area", "Area used", "m²", 9), ("diff", "Design − measured", "%", 8),
          ("agree", "Areas agree", "", 8)]
    s += trip("h", "Ceiling height", "m", 8)
    s += [("vol", "Volume", "m³", 9)]
    s += trip("pfix", "Occupancy, stated", "persons", 9)
    s += trip("dens", "Occupancy density", "persons / 100 m²", 10)
    s += [("obasis", "Occupancy source", "", 19), ("people", "Occupancy used", "persons", 9)]
    s += trip("iof", "Intermittent occupancy factor", "–", 10)
    s += trip("act", "Activity", "", 16)
    s += trip("hours", "Operation time", "h / day", 10)
    s += trip("light", "Lighting", "W/m²", 9)
    s += trip("power", "Small power", "W/m²", 9)
    s += trip("eqs", "Equipment, sensible", "W", 9)
    s += trip("eql", "Equipment, latent", "W", 9)
    s += [("glz", "Glazing (Envelope)", "m²", 9), ("wall", "External wall (Envelope)", "m²", 9),
          ("roof", "Roof (Envelope)", "m²", 9), ("face", "Face, azimuth, shading (Envelope)", "", 30),
          ("remark", "Remarks", "", 60)]
    return s


def cr_spec() -> Spec:
    return [("no", "Room number", "", 14), ("name", "Room name", "", 28), ("crit", "Criteria row (as printed)", "", 28),
            ("page", "Booklet page / table", "", 14), ("code", "Code", "", 5), ("src", "Source ID", "", 11),
            ("oa_ach", "Min outdoor air", "ACH", 8), ("oa_lpp", "Outdoor air per person", "L/s per person", 9),
            ("oa_lpm2", "Outdoor air per area", "L/s per m²", 9),
            ("oa_spec", "Outdoor air, specified", "L/s", 9), ("sa_ach", "Min total supply", "ACH", 8),
            ("sa_lpm2", "Supply per area", "L/s per m²", 9), ("sa_spec", "Supply, specified", "L/s", 9),
            ("ex_ach", "Exhaust", "ACH", 8), ("ex_lpm2", "Exhaust per area", "L/s per m²", 9),
            ("ex_spec", "Exhaust, specified", "L/s", 9), ("tmin", "Temperature min", "°C", 8),
            ("tmax", "Temperature max", "°C", 8), ("rh", "Humidity max", "% RH", 8),
            ("regime", "Pressure regime", "", 11), ("pa_bk", "Booklet pressure", "Pa", 8),
            ("pa_td", "Tender drawings pressure", "Pa", 9), ("pa_wd", "Working drawings pressure", "Pa", 9),
            ("pa", "Pressure target used", "Pa", 9), ("pdiff", "Sources differ", "", 8),
            ("cls", "Air class", "", 7), ("filter", "Filter", "", 9), ("single", "Single pass", "", 7),
            ("spc", "Single-pass class (clean or dirty)", "", 10), ("spc_c", "Code", "", 5), ("spc_s", "Source ID", "", 11), ("tset", "Temperature set point", "°C", 8),
            ("td", "Design room temperature", "°C", 9), ("rhd", "Design humidity", "fraction", 8),
            ("pv", "Vapour pressure at design condition", "kPa", 9), ("hd", "Design room enthalpy", "kJ/kg", 9),
            ("trange", "Design temperature inside the range", "", 10),
            ("after", "After-hours", "", 10), ("press", "P / N / –", "", 7), ("clin", "Clinical criteria (not FCU)", "", 9),
            ("ss553", "HFDS row refers to SS 553 for outdoor air", "", 9), ("oascope", "Outdoor-air scope (D-110)", "", 11),
            ("remark", "Remark", "", 50)]


def en_spec() -> Spec:
    return [("room", "Room number", "", 14), ("seg", "Segment", "", 12), ("part", "Building part", "", 12),
            ("face", "Nominal face", "", 10), ("az", "True azimuth", "°", 8), ("ori", "Orientation (table key)", "", 12),
            ("glz", "Glazing", "m²", 9), ("wall", "External wall", "m²", 9), ("roof", "Roof", "m²", 9),
            ("shade", "Shading as drawn", "", 22), ("method", "Method", "", 14), ("code", "Code", "", 5),
            ("src", "Source ID", "", 11), ("win_wm2", "Window gain", "W/m²", 9), ("wall_wm2", "Wall gain", "W/m²", 9),
            ("fen_w", "Fenestration gain", "W", 10), ("wall_w", "Wall gain", "W", 10), ("roof_w", "Roof gain", "W", 10),
            ("remark", "Remark", "", 44)]


# engine: key, header, unit, width, code, group
ENG: List[Tuple[str, str, str, float, str, str]] = [
    ("lvl", "Level", "", 8, "L", "ROOM DETAILS"), ("ahu", "AHU or system", "", 24, "L", "ROOM DETAILS"),
    ("zone", "Zone tag", "", 8, "L", "ROOM DETAILS"), ("no", "Room number", "", 14, "L", "ROOM DETAILS"),
    ("name", "Room name", "", 28, "L", "ROOM DETAILS"), ("sys", "System type", "", 9, "L", "ROOM DETAILS"),
    ("sptype", "Space type", "", 18, "L", "ROOM DETAILS"), ("cond", "Room condition", "", 9, "L", "ROOM DETAILS"),
    ("h", "Ceiling height", "m", 8, "L", "ROOM DETAILS"), ("area", "Room area", "m²", 9, "L", "ROOM DETAILS"),
    ("vol", "Volume", "m³", 9, "C", "ROOM DETAILS"),
    ("glz", "Glazing", "m²", 8, "L", "ROOM DETAILS"), ("wall", "External wall", "m²", 8, "L", "ROOM DETAILS"),
    ("roof", "Roof", "m²", 8, "L", "ROOM DETAILS"), ("nenv", "Envelope rows", "no.", 7, "C", "ROOM DETAILS"),
    ("occ", "Occupancy", "persons", 9, "L", "MINIMUM AIR REQUIREMENTS"), ("iof", "I.O.F.", "–", 7, "L", "MINIMUM AIR REQUIREMENTS"),
    ("oa_ach", "Min outdoor air", "ACH", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("oa_lpp", "Outdoor air per person", "L/s", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("oa_lpm2", "Outdoor air per area", "L/s per m²", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("oa_spec", "Outdoor air, specified", "L/s", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("oa", "Minimum outdoor air", "L/s", 9, "C", "MINIMUM AIR REQUIREMENTS"),
    ("sa_ach", "Min supply air", "ACH", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("sa_lpm2", "Supply per area", "L/s per m²", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("sa_spec", "Supply, specified", "L/s", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("sa_lps", "Air-change airflow", "L/s", 9, "C", "MINIMUM AIR REQUIREMENTS"),
    ("ex_ach", "Exhaust", "ACH", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("ex_lpm2", "Exhaust per area", "L/s per m²", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("ex_spec", "Exhaust, specified", "L/s", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("ex_lps", "Exhaust (calculated)", "L/s", 9, "C", "MINIMUM AIR REQUIREMENTS"),
    ("ex_fin", "Exhaust required", "L/s", 9, "C", "MINIMUM AIR REQUIREMENTS"),
    ("press", "Pressurisation", "P / N / –", 8, "L", "MINIMUM AIR REQUIREMENTS"),
    ("ppl", "People", "persons", 8, "C", "SENSIBLE LOAD"), ("act", "Activity", "", 14, "L", "SENSIBLE LOAD"),
    ("ppl_sw", "Sensible per person", "W", 8, "C", "SENSIBLE LOAD"), ("ppl_w", "People", "W", 9, "C", "SENSIBLE LOAD"),
    ("lgt_wm2", "Lighting", "W/m²", 8, "L", "SENSIBLE LOAD"), ("lgt_w", "Lighting", "W", 9, "C", "SENSIBLE LOAD"),
    ("pwr_wm2", "Small power", "W/m²", 8, "L", "SENSIBLE LOAD"), ("eq_w", "Equipment", "W", 9, "L", "SENSIBLE LOAD"),
    ("pwr_w", "Power", "W", 9, "C", "SENSIBLE LOAD"),
    ("fen_w", "Fenestration", "W", 9, "C", "SENSIBLE LOAD"), ("fen_wm2", "Fenestration", "W/m²", 8, "C", "SENSIBLE LOAD"),
    ("trn_w", "Transmission", "W", 9, "C", "SENSIBLE LOAD"), ("trn_wm2", "Transmission", "W/m²", 8, "C", "SENSIBLE LOAD"),
    ("inf_ach", "Infiltration", "ACH", 8, "C", "SENSIBLE LOAD"), ("inf_lps", "Infiltration", "L/s", 8, "C", "SENSIBLE LOAD"),
    ("inf_w", "Infiltration", "W", 9, "C", "SENSIBLE LOAD"),
    ("lat_ppl_wp", "Latent per person", "W", 8, "C", "LATENT LOAD"), ("lat_ppl", "People", "W", 9, "C", "LATENT LOAD"),
    ("lat_eq", "Equipment", "W", 9, "L", "LATENT LOAD"), ("lat_inf_w", "Infiltration", "W", 9, "C", "LATENT LOAD"),
    ("sens", "Room sensible load", "W", 10, "C", "TOTAL COOLING LOAD"), ("sens_wm2", "Sensible", "W/m²", 8, "C", "TOTAL COOLING LOAD"),
    ("lat", "Room latent load", "W", 10, "C", "TOTAL COOLING LOAD"), ("lat_wm2", "Latent", "W/m²", 8, "C", "TOTAL COOLING LOAD"),
    ("tot", "Total cooling", "W", 10, "C", "TOTAL COOLING LOAD"), ("tot_wm2", "Total", "W/m²", 8, "C", "TOTAL COOLING LOAD"),
    ("troom", "Design room temperature", "°C", 9, "C", "ROOM COOLING SUPPLY AIR CHECK"),
    ("hroom", "Design room enthalpy", "kJ/kg", 9, "C", "ROOM COOLING SUPPLY AIR CHECK"),
    ("tsup", "Supply air temperature", "°C", 9, "L", "ROOM COOLING SUPPLY AIR CHECK"),
    ("th_lps", "Thermal-load airflow", "L/s", 10, "C", "ROOM COOLING SUPPLY AIR CHECK"),
    ("fix", "Fixed by specification (MOT)", "0 / 1", 8, "L", "FINAL ROOM SUPPLY AIR"),
    ("pm", "Exhaust and pressure minimum", "L/s", 10, "L", "FINAL ROOM SUPPLY AIR"),
    ("fin_unr", "Governing airflow, unrounded", "L/s", 10, "C", "FINAL ROOM SUPPLY AIR"),
    ("fin", "Final room supply, rounded up", "L/s", 10, "R", "FINAL ROOM SUPPLY AIR"),
    ("fin_m3h", "Final supply", "m³/h", 10, "C", "FINAL ROOM SUPPLY AIR"),
    ("ach", "Achieved supply", "ACH", 8, "C", "FINAL ROOM SUPPLY AIR"),
    ("voc", "Voc", "L/s", 8, "C", "MULTIPLE SPACE CALC"), ("vsc", "Vsc", "L/s", 8, "C", "MULTIPLE SPACE CALC"),
    ("frac", "Outdoor-air fraction Voc / Vsc", "–", 9, "C", "MULTIPLE SPACE CALC"),
]


def af_spec() -> Spec:
    s: Spec = [("no", "Room number", "", 14), ("name", "Room name", "", 28), ("storey", "Storey", "", 8),
               ("ahu", "AHU or system", "", 24), ("sys", "System type", "", 9), ("area", "Area", "m²", 9),
               ("vol", "Volume", "m³", 9), ("sens", "Room sensible", "W", 10), ("lat", "Room latent", "W", 10),
               ("troom", "Room temperature", "°C", 8), ("hroom", "Room enthalpy", "kJ/kg", 8),
               ("ac", "Air-change airflow", "L/s", 10), ("th", "Thermal-load airflow", "L/s", 10),
               ("gov", "Governing case", "", 12), ("margin", "Margin of the other case", "L/s", 10),
               ("ex_min", "Exhaust required", "L/s", 10), ("kind", "Pressure offset kind", "", 10),
               ("off", "Offset needed", "L/s", 9), ("pm", "Supply minimum from exhaust and pressure", "L/s", 11),
               ("supply", "Final supply", "L/s", 10), ("extract", "Total extract", "L/s", 10),
               ("single", "Single pass", "", 7), ("exhaust", "Exhaust", "L/s", 10), ("ret", "Return", "L/s", 10),
               ("oa_air", "Outdoor air, air-change basis", "L/s", 10), ("oa", "Outdoor air", "L/s", 10),
               ("frac", "Outdoor-air fraction", "–", 9), ("minflow", "Minimum airflow (VAV)", "L/s", 10),
               ("ok", "Supply meets both cases", "", 9), ("spc", "Single-pass class", "", 9),
               ("th_lo", "Thermal airflow at the lower limit of the range (information)", "L/s", 12), ("lo_chk", "Lower-limit check", "", 9),
               ("env_chk", "Envelope room: peak solar check", "", 11), ("fix", "Fixed by specification (MOT)", "0 / 1", 8),
               ("oa_spec", "Outdoor air, specified", "L/s", 9), ("spec_off", "Offset, as specified", "L/s", 9),
               ("off_chk", "Specified offset against offset needed", "", 12), ("terminal", "Terminal tag", "", 14), ("exfan", "Exhaust fan tag", "", 14),
               ("clin", "Clinical criteria", "", 8), ("systype", "System type (input)", "", 9),
               ("oascope", "Outdoor-air scope (D-110)", "", 10)]
    for r in range(1, OA_ROUNDS + 1):
        s += [(f"in{r}", f"Round {r}: in AHU", "0 / 1", 7), (f"tot{r}", f"Round {r}: AHU supply", "L/s", 9),
              (f"max{r}", f"Round {r}: highest fraction", "–", 8), (f"nxt{r}", f"Round {r}: next fraction", "–", 8),
              (f"trg{r}", f"Round {r}: triggers", "0 / 1", 7), (f"sep{r}", f"Round {r}: separate", "0 / 1", 7),
              (f"lst{r}", f"Round {r}: list", "0 / 1", 7)]
    s += [("status", "Outdoor-air rule", "", 16), ("sepf", "Separated", "0 / 1", 8), ("cmark0", "Highest-fraction mark", "", 10),
          ("cmark1", "Highest-fraction mark after separation", "", 10),
          ("ref0", "First supply room of the AHU", "", 10), ("ref1", "First supply room after separation", "", 10), ("fcu", "On an FCU", "0 / 1", 7), ("flgrank", "Outdoor-air flag order", "no.", 7),
          ("fcurank", "FCU list order", "no.", 7)]
    return s


def ps_keys() -> Tuple[List[str], int]:
    """Node table keys: printed block then helpers; returns (keys, n_printed)."""
    printed = ["node", "kind", "name", "suite", "set", "set_c", "set_s", "perim", "perim_c", "perim_s", "area", "h", "ovr",
               "ovr_c", "ovr_s", "surf", "limit", "n", "supply", "exhaust", "pfin", "env_in", "env_out", "gap_in",
               "gap_out", "fix_in", "fix_out", "air_in", "air_out", "kindoff", "needed", "design", "tighter", "ratio", "used", "ratio_used",
               "status", "spec_off", "spec_chk", "off_lps", "design_lps", "needed_n50", "needed_n75", "ratio_n50", "ratio_n75",
               "flag_s", "upa", "resid", "note"]
    helpers = [f"p{k}" for k in range(NPASS + 1)] + ["lk", "negf", "bsum"] + [f"u{k}" for k in range(NBIS + 1)]
    return printed + helpers, len(printed)


def pl_keys(n_printed: int) -> List[str]:
    printed = ["id", "room", "a", "b", "kind", "seal", "gap", "gap_c", "gap_s", "leaf_w", "leaf_h", "fixed", "area",
               "pa", "pb", "dp", "q", "qf"]
    pad = [f"_pad{i}" for i in range(n_printed - len(printed))]
    helpers = ["ia", "ib", "kc"] + [f"f{k}" for k in range(NPASS)] + [f"g{k}" for k in range(NPASS)]
    return printed + pad + helpers


def build_layouts(ctx: Ctx) -> None:
    n = len(ctx.rooms)
    ctx.lay["RI"] = Layout("Room Information", [k for k, *_ in ri_spec()], FIRST, n)
    ctx.lay["CR"] = Layout("Criteria", [k for k, *_ in cr_spec()], FIRST, n)
    ctx.lay["EN"] = Layout("Envelope", [k for k, *_ in en_spec()], FIRST, len(ctx.env_rows) + 50)   # 50 spare rows: rows added later are summed
    ctx.lay["AF"] = Layout("Airflow", [k for k, *_ in af_spec()], FIRST, n)
    keys = [e[0] for e in ENG]
    ctx.eng_layouts = {}
    for st in ctx.storeys:
        rooms_st = [r for r in ctx.rooms if r["storey"] == st]
        ctx.eng_layouts[st] = Layout(ctx.sheet_for(st), keys, ENG_FIRST, len(rooms_st))
    pk, npr = ps_keys()
    ctx.lay["PS"] = Layout("Pressure", pk, FIRST, len(ctx.ps_nodes))
    ps = ctx.lay["PS"]
    link_first = ps.last + 6
    ctx.lay["PL"] = Layout("Pressure", pl_keys(npr), link_first, len(ctx.pl_links))
    suite_first = ctx.lay["PL"].last + 6
    ctx.lay["PU"] = Layout("Pressure", ["suite", "n", "ex_needed", "sup_needed", "ex_design", "sup_design", "needed",
                                          "design", "ratio", "status", "note"], suite_first, len(ctx.suites))
    ctx.ka_first = ctx.lay["PU"].last + 6
    # systems
    ctx.lay["AH"] = Layout("AHU", AHU_KEYS, FIRST, len(ctx.ahus))
    ctx.lay["AH2"] = Layout("AHU", AHU_KEYS, FIRST + len(ctx.ahus) + 6, len(ctx.ahus))
    ctx.lay["FC"] = Layout("FCU", FCU_KEYS, FIRST, ctx.n_fcu_rows)
    ctx.lay["FN"] = Layout("Fans", FAN_KEYS, FIRST, len(ctx.fans))
    ctx.lay["VV"] = Layout("VAV", VAV_KEYS, FIRST, len(ctx.terminals))
    ctx.lay["VN"] = Layout("Venturi Valves", VEN_KEYS, FIRST, len(ctx.valves))
    ctx.lay["FB"] = Layout("Flow Balance", FB_KEYS, FIRST, n)
    ctx.lay["FB2"] = Layout("Flow Balance", FB2_KEYS, FIRST + n + 6, len(ctx.ahus))
    ctx.lay["CP"] = Layout("Airflow Comparison", CP_KEYS, FIRST, n)
    ctx.lay["CP2"] = Layout("Comparison by AHU", CP2_KEYS, FIRST, len(ctx.ahus))
    ctx.lay["CP3"] = Layout("Reference Rows", CP3_KEYS, FIRST, len(ctx.references))
    ctx.lay["SO"] = Layout("Sources", ["id", "doc", "issue", "page", "loc", "text", "method", "conf"], 7, len(ctx.sources))


AHU_KEYS = ["ahu", "type", "n", "area", "area_sup", "vol", "noh", "sa_room", "leak", "sa_ahu", "oa_sum", "crit", "frac", "oa_rule",
            "vou", "vps", "xs", "zd", "ev", "vot", "oa_ms", "oa_mb", "ret", "exh", "relief",
            "sens", "lat", "rt", "rh", "oa_sens", "oa_lat", "fan", "coil_s", "coil_l", "coil", "wm2",
            "oa_sens_ms", "oa_lat_ms", "coil_s_ms", "coil_l_ms", "coil_ms", "req_t", "sched", "sched_oa",
            "sched_c", "sched_s", "tsrc", "zr", "za", "zc", "zs", "oth", "othc", "oths", "cov", "red", "redp", "rem", "nfix", "area_ok", "leadtxt", "nocrit", "area_nocrit",
            "fsp", "spr", "spl", "spy", "spnote", "oaflg", "nok",
            "ducted", "oa_frac", "oa_bal", "offnet", "oa_short", "dn", "dsup", "dexh", "balnote", "second", "second_s", "norr_why",
            "dirsup", "dirn", "spillreq", "ucvadd", "oa_clin", "oa_nc", "ahucls", "oa_min", "oa_how"]
FCU_KEYS = ["k", "no", "name", "ahu", "why", "area", "sens_kw", "lat_kw", "supply", "oa", "oasrc", "oaby", "oacond", "oa_t", "oa_h",
            "oa_sens", "oa_lat", "coil_s", "coil_l", "coil", "cap_sched", "cap_drawn", "cap", "flag",
            "tender", "tsrc", "calc", "red", "redp", "rem"]
FAN_KEYS = ["tag", "type", "serves", "serves2", "req", "leak", "drawn", "sched", "tsrc", "red", "redp", "code", "src", "remark", "status",
            "rem", "pk_p", "pk_o"]
VAV_KEYS = ["tag", "type", "rooms", "n", "max", "min", "turn", "code", "src", "remark"]
VEN_KEYS = ["tag", "room", "role", "d1", "d2", "off", "design", "tight", "ratio", "status", "code", "src", "remark"]
FB_KEYS = ["no", "name", "ahu", "supply", "oa", "exhaust", "ret", "extract", "leak_in", "leak_out", "inn", "out", "net",
           "close", "status", "ach"]
FB2_KEYS = ["ahu", "supply", "oa", "exhaust", "ret", "offnet", "relief", "net_ach", "area", "vol", "sum_net", "close", "status", "note"]
CP_KEYS = ["no", "name", "ahu", "sch_s", "calc_s", "d_s", "dp_s", "sch_r", "calc_r", "d_r", "sch_e", "calc_e", "d_e",
           "pa_calc", "pa_sch", "flag", "code", "src", "tsrc", "remark"]
CP2_KEYS = ["ahu", "sched", "calc", "d_sched", "dp_sched", "tsrc", "sch", "d", "dp", "flag", "code", "src", "remark"]
CP3_KEYS = ["label", "flow", "m3h", "ach", "oa", "diff", "pct", "code", "src", "remark"]
