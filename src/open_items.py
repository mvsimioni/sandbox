"""Write out/qa/open_items_for_marcus.md from one list, and check it against out/qa/decisions_needed.md.

``python3 -m src.open_items``

The decisions that are open in decisions_needed.md are read from that file, so the two files agree. The items found in the final verification are listed here with
their options and the interim state of the deliverables. Items that a decision of Marcus has settled (D-025 to D-028) are not open: they are listed in one short
section with the decision that settles them. The open conflicts between documents are read from out/registers/conflicts.csv. The common areas outside Appendix D and the
unit links that the area test refused are read from the registers and the department inputs.
"""

from __future__ import annotations

import csv
import glob
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

DECISIONS = Path("out/qa/decisions_needed.md")
OUT = Path("out/qa/open_items_for_marcus.md")

# Items that a decision of Marcus has settled: (id, title, decision that settles it, how it stands now)
SETTLED: List[tuple] = [
    ("D-021", "Door data for the Pressure tab", "D-028", "No door data is needed. Every room with a pressure target in Pa is calculated from its internal surface at 2.5 m³/h per m² at 50 Pa (n = 0.65; sensitivity 0.50 and 0.75). No room is flagged for missing door data."),
    ("OI-04-1", "Space-type defaults table (lighting and small power)", "D-025", "Plug load and lighting come from the ASHRAE value of the room use where no project document gives one (red cells, Remarks note)."),
    ("OI-04-2", "Space-type classification", "D-025", "The room use comes from the room name, by the room-use rules of the occupancy default. A name that gives no room use is listed on Flags."),
    ("OI-04-3", "Equipment gains", "D-025", "Equipment gain stays 0 W. Equipment-led rooms have a VERIFY flag."),
    ("OI-04-4", "Envelope", "D-025", "Envelope gain is not assessed. The workbooks state it plainly."),
    ("OI-R308-1", "D47 offices on L06 and L07", "D-026", "The Admin Workspace booklet prints its own rows for these levels (L6 and L7 Admin Workspace Office, Meeting Room, Collab, Pantry, Toilet; Neutral, 6 air changes). D-026 applies only where the own department has no row, so these rooms take the own rows (mapped by name, code A, Remarks say so). D-009 stays limited to the AN-ED offices on L02."),
    ("OI-R103-1", "Department circulation: default criteria", "D-026", "A room with no criteria row in its own department takes the row of the same space in the closest other department (italic green, Remarks note)."),
]

# every decision that is open in decisions_needed.md is listed. The rulings file names some of them with an OI id: both ids are printed.
ALIASES = {"D-020": "OI-04-5"}

HEAD = """# Open items for Marcus

Engineering-method and criteria decisions that the verification did not decide. Each item names its options and the interim state.
This file is written from one list (`src/open_items.py`). The open decisions of `out/qa/decisions_needed.md` are read from that file.
"""

# (id, title, text)
VERIFICATION_ITEMS: List[tuple] = [
    ("OI-05-1", "(optional) Tolerance for acceptance checks.", "The acceptance checks now FAIL above 0 (no tolerance in brief 8.6): rooms on no AHU or class, supply on no unit, rooms without criteria, rooms without area or height. Every workbook shows FAIL until zoning links and criteria are complete. Say if you want a tolerance (for example a share of rooms or of supply)."),
    ("OI-13-1", "Supply floor from exhaust.", "Brief 8.3 \"not less than the room's exhaust and pressure regime require\". Options: (a) supply not below the required exhaust in every room (the recomputation's rule); (b) no floor where there is no pressure offset: the exhaust is made up by transfer air, as Basis of Design states (the workbook rule, interim); (c) (b) for toilets, showers and change rooms only, (a) for other rooms. The recomputation reads this rule as the workbook does for the comparison; the values that the other reading would change are listed in out/qa/recompute_sensitivity.csv until you decide, and both engines are then changed together."),
    ("OI-13-2", "Duct leakage on ducted FCUs.", "D-005 sets 10 % on AHU supply. Non-ducted (hi-wall) FCUs are ruled at 0 %. Options for ducted FCUs: (a) 10 % as AHUs (interim); (b) 0 %; (c) another value."),
    ("OI-13-3", "Minimum for the occupancy round-up.", "D-017 rounds the default count up to a whole person with no limit. Hand-wash bays are ruled as circulation (0 persons). Options for other small rooms: (a) no limit (interim); (b) no round-up below a set area (for example 2 m²); (c) round up only when the calculated count is at least a set value (for example 0.5)."),
    ("OI-13-4", "RDS \"n/a\" in occupied rooms.", "D-018 reads \"n/a\" as 0 persons. Rooms with an occupied use and \"n/a\" or \"0\" are flagged VERIFY with the text \"RDS occupancy taken as 0 persons\". Options: (a) keep D-018 and flag them VERIFY (interim); (b) where the room use is occupied, treat \"n/a\" as empty and use the ASHRAE default (red, Remarks note)."),
    ("OI-13-5", "Innovation Lab (D44 ITEC 05.07.01) criteria.", "No booklet exists for 43/44 (D-010) and the RDS gives no air-change rate. No other department prints the same space (D-026 takes a row only for the same space, not for one shared word such as 'Lab'), so the room has no criteria row and no airflow from criteria. The Flags tab lists it. Options: (a) leave the room with no criteria row (interim); (b) give another criteria row (for example the Admin Workspace office row, as D-009 did for AN-ED, or a laboratory row)."),
    ("OI-R108-1", "Plant-room criteria (final verification R1-08).",
     "Plant, electrical, comms and riser rooms have no booklet row. D-026 gives a presumed row only where another department prints the same room type name or code (for example UPS room, compressor room); it gives none from the use class, so the other plant rooms have no criteria row and no air-change airflow. Options: (a) give a plant-room criteria row (ventilation or cooling by the plant list, code A); (b) leave them with no criteria and listed (interim); (c) take their loads from the schedule of equipment as a narrow exception to D-008."),
    ("OI-R108-2", "Unit of the D00 spaces that lie over an open zoning conflict (final verification R1-08).",
     "CA-L03-023 takes AHU-B-03-ELECRM-01A/B from the plan room L03-R011 (the later dated document, IP-BH-06031 Rev 2; conflict CF-0034 is open, VERIFY). CA-L04-020 has no unit: the plan room L04-R267 is 'no information' (App D draws the fans OAF/EAF-B-04-04AHU-02A/B only; conflict CF-0044 is open). Options: (a) keep both as they are (interim); (b) confirm or give the unit when the conflicts are closed. The D00 workbook and the zone schedule must keep agreeing."),
    ("OI-R110-1", "Single-pass room in the AHU outdoor-air fraction (final verification R1-10).",
     "A room with single-pass air (100 % outdoor air, for example a negative-pressure utility at 20 air changes) sets the highest outdoor-air fraction of its unit, and the outdoor-air rule takes that fraction times the whole unit supply. Interim state (a): the room and its share of the supply are named in the Remarks of the unit, and the column 'Outdoor air, mass-balance basis' on AHU and AHU Master shows the effect of (b) for information only; it feeds nothing. Options: (a) keep the rule as built (interim; the room is named on AHU); (b) leave single-pass rooms out of the fraction and add their outdoor air as a separate duty; (c) separate the room onto its own unit or FCU under the outdoor-air rule."),
    ("OI-R111-1", "Outdoor air per area or per person as a supply floor.",
     "The booklet gives the outdoor air of some room types as L/s per m² or L/s per person (for example 0.6 L/s per m² for offices and meeting rooms, 10 L/s per person for lecture rooms, or the higher of the two). Both engines take the highest of these values, the air-change value and the specified value as the outdoor air of the room, and take that outdoor air as a floor of the room supply. Confirm that the floor is the intended reading, or give the other: the outdoor air as a separate duty on the FCU tab."),
    ("OI-R112-1", "Anaesthesia preparation rooms of the emergency theatres.",
     "MSOT 04.01.05 is on AHU-B-07-05MOTE-01A/B and MSOT 04.01.06 is on -02A/B, each with the theatre that it serves (04.02.07 and 04.02.08, IP-BH-16054 and Appendix J). Confirm that the drawing agrees."),
    ("OI-R114-1", "Tag AHU-B-07-05MOTHYB-02.",
     "The MOT NSC schedule of modes (Appendix J, theatre MSOT 05.02.12) names this unit. The MOT NSC Overall AC Equipment Schedule lists only AHU-B-07-05MOTHYB-01, and the zone schedule does not show it. Options: (a) the tag is correct and the schedule is incomplete; (b) the tag is an error for AHU-B-07-05MOTHYB-01. Interim state: the D22 workbook, the AHU master summary and the zone schedule all list the unit as AHU-B-07-05MOTHYB-02 with the rooms that carry it and with no tender airflow; the conflict is listed on Flags of D22 and the AHU master summary. No tender airflow is used until you confirm."),
    ("OI-R206-1", "Reduction against tender for a unit whose zone is one small room (round 2, R2-06).",
     "The completeness rule of D-027 compares the rooms counted in the department workbooks with the rooms of the unit in the zone schedule. Both lists come from the same plan rooms, so a unit that the zoning gives only one or two rooms can be complete even when the tender airflow shows that the unit serves far more (for example AHU-B-05-05CORE-01: tender 9,000 L/s, one zoned room of 35 m²). Interim state (a): the reduction cells show the reduction that D-027 gives, and the Remarks of each such unit (zone of 2 rooms or fewer) say that it is not a confirmed result (OI-R206-1 open). Options: (a) keep the rule of D-027 (interim); (b) also require, for a unit with 2 rooms or fewer, a second source for the room list (the rooms printed for the unit on the tender air-side schematic); (c) state no reduction for a unit with fewer than a set number of zoned rooms."),
    ("OI-R211-1", "Rooms with no room use for the default occupancy (round 2, R2-11).",
     "The room-use rules (D-017) now map more names (for example change rooms, nursing rooms, command centres, reporting rooms, recycling rooms, multipurpose rooms and the Appendix D abbreviation M&E). The rooms that remain with occupancy 'NOT SET' are listed on Flags with the reason: locker rooms (the density table has no row for them) and names that do not say what the room is used for. Options: (a) keep them listed (interim); (b) add a density row for lockers; (c) give the number of persons for each listed room."),
    ("OI-R212-1", "Three L06 units that have rooms and no zone polygon (round 2, R2-12).",
     "AHU-B-07-06DSOT-11, AHU-B-07-06DSOT-12A/B and AHU-B-07-06STER-03A/B have room tags in an open region of the L06 plan (no closed outline). They have a zoned area of 0 m², so they have no zone on the zoning PDF and no legend or index row there. The zone schedule keeps them with the reason and shows no reduction. Options: (a) keep as it is (interim); (b) measure the open region on the plan to give the zones an outline."),
    ("OI-R305-1", "CT and UCV theatre: supply airflow for the future UCV (round 3, R3-05).",
     "The MOT NSC air-side schematic IP-BH-16057 (Corr 2, rev 0A) carries a note for the CT and UCV theatre (MSOT 05.02.10, unit AHU-B-07-05MOTCT-01): an approximate supply airflow of 5,320 L/s shall be provided and achieved to the room to accommodate the future UCV, and the ductwork sizing shall account for the pressure loss. The room table of the same schematic gives 3,600 L/s (normal mode and single pass), and the MOT NSC equipment schedule gives the unit 6,400 L/s. The note is stated in the Remarks of the room, of the unit and of the zone schedule. Options: (a) keep the 3,600 L/s of the room table (interim; the workbooks do not resize specified rooms); (b) take 5,320 L/s for this room, so that the unit total follows the future UCV; (c) show both and decide at the UCV change."),
    ("OI-R306-1", "MOT rooms: unit tags that the documents print differently (round 3, R3-06).",
     "For some theatres and anaesthesia preparation rooms the MOT NSC schedule of modes, the air-side schematic and the zoning of the plan name different units. The Remarks of each room now name only the unit that the workbook uses (the unit of the room). Each difference is a VERIFY flag on Flags of D22 with the tags that each document prints. Options: (a) keep the unit of the zoning where the plan gives one, else the unit of the schedule of modes (interim); (b) give the unit for each listed room."),
    ("OI-R401-1", "Exhaust and outdoor air of a room with a pressure offset (round 4).",
     "D-028 gives every room with a pressure target in Pa a Pressure row, so the offset now enters the airflow of about 300 rooms. The two engines read the exhaust of such a room differently. "
     "The workbook exhausts the required exhaust and, for a negative room, the offset air (supply plus offset is the extract; the rest returns to the AHU), so the outdoor air of the room is the larger of its air-change value and its exhaust. "
     "The independent recomputation exhausts the whole extract (supply plus offset for a negative room, supply minus offset for a positive room) and takes the outdoor air as the supply. Supply and loads agree; only the room outdoor air (and the outdoor air of the AHU that carries it) differs. "
     "Options: (a) the workbook reading (interim, as stated on Basis of Design); (b) the recomputation reading (more outdoor air for negative and positive rooms); (c) (b) for clinical rooms with air class 1 or 2 only. "
     "Interim state: no change to either engine. The comparison (out/qa/recompute_comparison.csv) reads both engines under the reading of the workbook, so that each engine applies the same stated rule on its own: the room outdoor air of a room with a pressure offset is then confirmed independently under the interim reading. "
     "The values that the other reading would change are listed in out/qa/recompute_sensitivity.csv. They are not counted as explained. Same precedent as OI-13-1."),
]


def parse_decisions(text: str) -> List[Dict[str, str]]:
    out = []
    for m in re.finditer(r"^### (D-\d+) (.*?)\n(.*?)(?=^### |\Z)", text, flags=re.S | re.M):
        body = m.group(3)
        fields = dict(re.findall(r"^- (Status|Context|Options|Provisional value): (.*)$", body, flags=re.M))
        out.append(dict(id=m.group(1), title=m.group(2).strip(), **fields))
    return out


def _read(path: str) -> List[Dict[str, str]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


INTERIM = {"D-023": "The completeness rule of D-027 applies: a reduction is stated only where the calculated airflow is below tender and every zoned room of the unit is counted "
                    "(rooms counted with a criteria row at least as many as the zoned rooms, no counted room without a criteria row, counted area at least the share 0.9 of the zoned area). "
                    "A unit that other departments share is judged on the AHU master summary only: the department tab says so and states no reduction. "
                    "The Remarks of a unit that is not complete say nothing about a reduction. The share 0.9 is the open value"}


def refused_links() -> str:
    """R3-02: the unit links that the area test refused, from the link audit of each department."""
    per: Dict[str, List[str]] = defaultdict(list)
    for f in sorted(glob.glob("out/qa/dpa_link_audit_D*.csv")):
        d = re.search(r"D(\d+)\.csv", f).group(1)
        for r in _read(f):
            if r.get("plan_basis") == "no link: area not plausible":
                per[d].append(r["ref"])
    if not per:
        return "No unit link was refused by the area test."
    lines = [f"- Department {d}: {len(v)} room(s): {', '.join(v)}." for d, v in sorted(per.items())]
    return ("A plan room much smaller than the DPA room or rooms that fit its name gives no unit link (the total design area must be within a factor of 2 of the plan area). "
            "Each refused room is a VERIFY flag on Flags of its workbook and a row of the link audit (out/qa/dpa_link_audit_D<nn>.csv, plan_basis 'no link: area not plausible').\n" + "\n".join(lines))


def common_areas_outside() -> str:
    """R3-07: the storeys below L02 have common areas, and Appendix D covers storeys 2 to 7 only."""
    rows = []
    for st in ("LB3", "B3", "B2", "B1", "L01"):
        za = _read(f"out/registers/zone_assignment_{st}.csv")
        com = [r for r in za if re.search(r"common|core", r.get("department", ""), re.I) or not r.get("department", "").strip()]
        area = sum(float(r.get("area_m2") or 0) for r in com)
        units = sorted({r["class"].strip() for r in com if r.get("class", "").strip().upper().startswith(("AHU", "PAHU", "FCU"))})
        rows.append(f"- {st}: {len(com)} plan room(s) with no department or with a common-area department, {area:,.0f} m². Units in their zoning ({len(units)}): {', '.join(units) if units else 'none'}.")
    return ("Appendix D (ACMV for common areas) covers storeys 2 to 7. The common areas of LB3, B3, B2, B1 and L01 are outside it: no document gives their spaces, so the common-areas "
            "workbook (D00) has no rows for them and no load is calculated for them. They are listed by storey below (the rooms are listed one by one on the storey sheets of the zone schedule). "
            "Options: (a) leave them outside the workbooks and state it on issue (interim); (b) give a source for these spaces, so that D00 covers them.\n" + "\n".join(rows))


def open_conflicts() -> List[str]:
    out = []
    for r in _read("out/registers/conflicts.csv"):
        if r.get("status", "").strip().lower().startswith("open") or "open on" in r.get("status", "").lower() or "open" == r.get("status", "").strip().lower():
            out.append(f"- {r['conflict_id']}: {r['topic']}, {r['item']}. {r['value_a']} ({r['source_a']}) against {r['value_b']} ({r['source_b']}). Applied: {r['precedence_applied']}. Status: {r['status']}.")
    return out


def render(decisions: List[Dict[str, str]]) -> str:
    lines = [HEAD]
    lines.append("## Open decisions of `decisions_needed.md`\n")
    open_d = [d for d in decisions if d.get("Status", "").strip() == "open"]
    for d in open_d:
        lines.append(f"**{d['id']}{(' / ' + ALIASES[d['id']]) if d['id'] in ALIASES else ''} {d['title']}** (open). {d.get('Context', '')} Options: {d.get('Options', '').rstrip('. ')}. "
                     f"Interim state: {INTERIM.get(d['id'], d.get('Provisional value', '')).rstrip('. ')}.\n")
    lines.append("## From the final verification (22 and 23 Nov 2026)\n")
    lines.append("Id scheme: OI-<finding>-<n>, as in `out/qa/final_verification_rulings.md` (F-04 is OI-04-n, R1-08 is OI-R108-n, R2-11 is OI-R211-n, R3-05 is OI-R305-n). Decisions keep their D id. "
                 "Rulings and evidence: `out/qa/final_verification_rulings.md`. Each item gives the options and the interim state of the deliverables until you decide.\n")
    for i, t, x in VERIFICATION_ITEMS:
        lines.append(f"**{i} {t}** {x}\n")
    lines.append("**OI-R302-1 Unit links refused by the area test (round 3, R3-02).** " + refused_links() + "\n")
    lines.append("**OI-R307-1 Common areas of LB3, B3, B2, B1 and L01 are outside Appendix D (round 3, R3-07).** " + common_areas_outside() + "\n")
    lines.append("## Settled by decisions D-025 to D-028 (closed, no action)\n")
    lines.append("These items were open. A decision of Marcus on 23 Nov 2026 settles each one. They are listed so that the earlier ids stay traceable.\n")
    for i, t, dec, now in SETTLED:
        lines.append(f"- {i} {t}: settled by {dec}. {now}")
    lines.append("")
    oc = open_conflicts()
    lines.append(f"## Open conflicts between documents ({len(oc)})\n")
    lines.append("Read from `out/registers/conflicts.csv`. Each conflict is shown on the deliverables where it applies (Remarks, Flags or the zone schedule). No conflict is resolved without a flag: "
                 "the document that the precedence rule gives governs, and the other value stays listed.\n")
    lines.extend(oc)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    decisions = parse_decisions(DECISIONS.read_text(encoding="utf-8"))
    text = render(decisions)
    text = re.sub(r"(?<![.\d])\.\.(?!\.)", ".", text)       # no double full stop (D-015)
    text = re.sub(r"[ \t]{2,}", " ", text)
    OUT.write_text(text, encoding="utf-8")
    open_ids = [d["id"] for d in decisions if d.get("Status", "").strip() == "open"]
    missing = [i for i in open_ids if i not in text]
    rulings = Path("out/qa/final_verification_rulings.md")
    if rulings.exists():                       # one id scheme: every OI id that a ruling names is an item here (a range 'OI-04-1 to OI-04-4' names both ends)
        rid = set(re.findall(r"OI-[A-Z]?\d+-\d+", rulings.read_text(encoding="utf-8")))
        missing += sorted(i for i in rid if i not in text)
    print(f"wrote {OUT}: {len(VERIFICATION_ITEMS)} items and {len(open_ids)} open decisions ({', '.join(open_ids)}); missing {missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
