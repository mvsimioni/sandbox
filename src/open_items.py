"""Write out/qa/open_items_for_marcus.md from one list, and check it against out/qa/decisions_needed.md.

``python3 -m src.open_items``

The decisions that are open in decisions_needed.md (D-020 to D-024 and any later one) are read from that file, so the two files agree. The items found in the
final verification are listed here with their options and the interim state of the deliverables.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

DECISIONS = Path("out/qa/decisions_needed.md")
OUT = Path("out/qa/open_items_for_marcus.md")

# every decision that is open in decisions_needed.md is listed. The rulings file names some of them with an OI id: both ids are printed.
ALIASES = {"D-020": "OI-04-5"}

HEAD = """# Open items for Marcus

Engineering-method and criteria decisions that the verification did not decide. Each item names its options and the interim state.
This file is written from one list (`src/open_items.py`). The open decisions of `out/qa/decisions_needed.md` are read from that file.
"""

# (id, title, text)
VERIFICATION_ITEMS: List[tuple] = [
    ("OI-04-1", "Space-type defaults table (lighting and small power).", "Brief 8.2/8.3 needs your approval before the table is used. The Rev A table has 7 outpatient-pharmacy rows only (12/15, 12/10, 10/5, 10/15, 12/25, 10/2, 8/0 W/m²). Options: (a) approve the 7 Rev A rows and extend them with rows for the hospital room types (bed room, ICU/HDU, theatre, procedure, imaging, laboratory, kitchen, plant, store, data room), values and source to be given by you; (b) approve the 7 rows only, and leave other rooms at occupants only; (c) issue with the thermal case marked \"occupants only\" (interim state). Every applied value stays code A, yellow."),
    ("OI-04-2", "Space-type classification.", "If (a) or (b): approve classification by room name. Proposal: the room-use rules already used for the D-017 occupancy default (`USE_RULES`), extended to the approved space types; a room with no match stays UNCLASSIFIED and occupants only."),
    ("OI-04-3", "Equipment gains.", "D-008 excludes the SoE, so no equipment load is known. Options: (a) keep 0 with a VERIFY flag on equipment-led rooms (interim); (b) allow a provisional W/m² per equipment-led use (data centre, UPS/battery, ASRS/automation, kitchen, imaging, laboratory), flagged for vendor data; (c) add the SoE for those rooms only, as a narrow exception to D-008 (as D-019 did for heights)."),
    ("OI-04-4", "Envelope.", "No Envelope rows in any workbook (brief 8.4 not carried out). Confirm whether the envelope work goes ahead before issue, or the workbooks are issued with \"envelope not assessed\"."),
    ("OI-05-1", "(optional) Tolerance for acceptance checks.", "The acceptance checks now FAIL above 0 (no tolerance in brief 8.6): rooms on no AHU or class, supply on no unit, rooms without criteria, rooms without area or height. Every workbook shows FAIL until zoning links and criteria are complete. Say if you want a tolerance (for example a share of rooms or of supply)."),
    ("OI-13-1", "Supply floor from exhaust.", "Brief 8.3 \"not less than the room's exhaust and pressure regime require\". Options: (a) supply not below the required exhaust in every room (the recomputation's rule); (b) no floor where there is no pressure offset: the exhaust is made up by transfer air, as Basis of Design states (the workbook rule, interim); (c) (b) for toilets, showers and change rooms only, (a) for other rooms. The compared values that differ stay listed until you decide; both engines are then changed together."),
    ("OI-13-2", "Duct leakage on ducted FCUs.", "D-005 sets 10 % on AHU supply. Non-ducted (hi-wall) FCUs are ruled at 0 %. Options for ducted FCUs: (a) 10 % as AHUs (interim); (b) 0 %; (c) another value."),
    ("OI-13-3", "Minimum for the occupancy round-up.", "D-017 rounds the default count up to a whole person with no limit. Hand-wash bays are ruled as circulation (0 persons). Options for other small rooms: (a) no limit (interim); (b) no round-up below a set area (for example 2 m²); (c) round up only when the calculated count is at least a set value (for example 0.5)."),
    ("OI-13-4", "RDS \"n/a\" in occupied rooms.", "D-018 reads \"n/a\" as 0 persons. Rooms with an occupied use and \"n/a\" or \"0\" are flagged VERIFY with the text \"RDS occupancy taken as 0 persons\". Options: (a) keep D-018 and flag them VERIFY (interim); (b) where the room use is occupied, treat \"n/a\" as empty and use the ASHRAE default (red, Remarks note)."),
    ("OI-13-5", "Innovation Lab (D44 ITEC 05.07.01) criteria.", "No booklet exists for 43/44 (D-010) and the RDS gives no air-change rate, so the room has no air-change airflow. Options: (a) give a criteria row (for example the Admin Workspace office row, as D-009 did for AN-ED, or a laboratory row); (b) leave it with no criteria, flagged (interim)."),
    ("OI-R103-1", "Department circulation: D-011 default (final verification R1-03).",
     "Rooms named Circulation (D04 2,228 m², D11 1,689 m², D23 1,044 m² and others) have no booklet row in most departments. D-011 gives a default for the common areas (neutral, 2 outdoor air ACH, 6 total ACH, class 2) and says nothing about department circulation. Options: (a) apply the D-011 default to department circulation that no booklet row covers (code A, flagged); (b) leave it with no criteria and listed (interim). Room types that a booklet row names are mapped (code A, \"Confirm the mapping\") and the ones that no row names are listed on Basis of Design and Flags."),
    ("OI-R108-1", "Plant-room criteria (final verification R1-08).",
     "Plant, electrical, comms and riser rooms have no booklet row, so they have no criteria row and no air-change airflow. Options: (a) give a plant-room criteria row (ventilation or cooling by the plant list, code A); (b) leave them with no criteria and listed (interim); (c) take their loads from the schedule of equipment as a narrow exception to D-008."),
    ("OI-R108-2", "Unit of the D00 spaces that lie over an open zoning conflict (final verification R1-08).",
     "CA-L03-023 takes AHU-B-03-ELECRM-01A/B from the plan room L03-R011 (the later dated document, IP-BH-06031 Rev 2; conflict CF-0034 is open, VERIFY). CA-L04-020 has no unit: the plan room L04-R267 is 'no information' (App D draws the fans OAF/EAF-B-04-04AHU-02A/B only; conflict CF-0044 is open). Options: (a) keep both as they are (interim); (b) confirm or give the unit when the conflicts are closed. The D00 workbook and the zone schedule must keep agreeing."),
    ("OI-R110-1", "Single-pass room in the AHU outdoor-air fraction (final verification R1-10).",
     "A room with single-pass air (100 % outdoor air, for example a negative-pressure utility at 20 air changes) sets the highest outdoor-air fraction of its unit, and the outdoor-air rule takes that fraction times the whole unit supply. Interim state (a): the room and its share of the supply are named in the Remarks of the unit, and the column 'Outdoor air, mass-balance basis' on AHU and AHU Master shows the effect of (b) for information only; it feeds nothing. Options: (a) keep the rule as built (interim; the room is named on AHU); (b) leave single-pass rooms out of the fraction and add their outdoor air as a separate duty; (c) separate the room onto its own unit or FCU under the outdoor-air rule."),
    ("OI-R111-1", "Outdoor air per area as a supply floor (data centre rooms).",
     "The booklet gives the outdoor air of the data-centre rooms as L/s per m². Both engines now take it as a floor of the room supply. Confirm that the floor is the intended reading, or give the other: the outdoor air as a separate duty on the FCU tab."),
    ("OI-R112-1", "Anaesthesia preparation rooms of the emergency theatres.",
     "MSOT 04.01.05 is on AHU-B-07-05MOTE-01A/B and MSOT 04.01.06 is on -02A/B, each with the theatre that it serves (04.02.07 and 04.02.08, IP-BH-16054 and Appendix J). Confirm that the drawing agrees."),
    ("OI-R114-1", "Tag AHU-B-07-05MOTHYB-02.",
     "The MOT NSC schedule of modes (Appendix J, theatre MSOT 05.02.12) names this unit. The MOT NSC Overall AC Equipment Schedule lists only AHU-B-07-05MOTHYB-01, and the zone schedule does not show it. Options: (a) the tag is correct and the schedule is incomplete; (b) the tag is an error for AHU-B-07-05MOTHYB-01. No tender airflow is used until you confirm."),
    ("OI-R206-1", "Reduction against tender for a unit whose zone is one small room (round 2, R2-06).",
     "The partial-list rule of D-023 compares the rooms counted in the department workbooks with the rooms of the unit in the zone schedule. Both lists come from the same plan rooms, so a unit that the zoning gives only one or two rooms passes the test even when the tender airflow shows that the unit serves far more (for example AHU-B-05-05CORE-01: tender 9,000 L/s, one zoned room of 35 m², reduction 97.9 %). The units that still show a reduction are on AHU Master (Against tender = REDUCTION AGAINST TENDER). Interim state (a): the reduction cells and the AHU Master column Against tender show the reduction as D-023 gives it, and the Remarks of each such unit (zone of 2 rooms or fewer) say that it is not a confirmed result (OI-R206-1 open). Options: (a) keep the rule of D-023 (interim); (b) also require, for a unit with 2 rooms or fewer, a second source for the room list (the rooms printed for the unit on the tender air-side schematic); (c) state no reduction for a unit with fewer than a set number of zoned rooms."),
    ("OI-R211-1", "Rooms with no room use for the default occupancy (round 2, R2-11).",
     "The room-use rules (D-017) now map more names (for example change rooms, nursing rooms, command centres, reporting rooms, recycling rooms, multipurpose rooms and the Appendix D abbreviation M&E). The rooms that remain with occupancy 'NOT SET' are listed on Flags with the reason: locker rooms (the density table has no row for them) and names that do not say what the room is used for. Options: (a) keep them listed (interim); (b) add a density row for lockers; (c) give the number of persons for each listed room."),
    ("OI-R212-1", "Three L06 units that have rooms and no zone polygon (round 2, R2-12).",
     "AHU-B-07-06DSOT-11, AHU-B-07-06DSOT-12A/B and AHU-B-07-06STER-03A/B have room tags in an open region of the L06 plan (no closed outline). They have a zoned area of 0 m², so they have no zone on the zoning PDF and no legend or index row there. The zone schedule keeps them with the reason and shows no reduction. Options: (a) keep as it is (interim); (b) measure the open region on the plan to give the zones an outline."),
]


def parse_decisions(text: str) -> List[Dict[str, str]]:
    out = []
    for m in re.finditer(r"^### (D-\d+) (.*?)\n(.*?)(?=^### |\Z)", text, flags=re.S | re.M):
        body = m.group(3)
        fields = dict(re.findall(r"^- (Status|Context|Options|Provisional value): (.*)$", body, flags=re.M))
        out.append(dict(id=m.group(1), title=m.group(2).strip(), **fields))
    return out


def render(decisions: List[Dict[str, str]]) -> str:
    lines = [HEAD]
    lines.append("## Open decisions of `decisions_needed.md`\n")
    open_d = [d for d in decisions if d.get("Status", "").strip() == "open"]
    for d in open_d:
        lines.append(f"**{d['id']}{(' / ' + ALIASES[d['id']]) if d['id'] in ALIASES else ''} {d['title']}** (open). {d.get('Context', '')} Options: {d.get('Options', '')}. Interim state: {d.get('Provisional value', '')}.\n")
    lines.append("## From the final verification (22 and 23 Nov 2026)\n")
    lines.append("Id scheme: OI-<finding>-<n>, as in `out/qa/final_verification_rulings.md` (F-04 is OI-04-n, R1-08 is OI-R108-n, R2-11 is OI-R211-n). Decisions keep their D id. "
                 "Rulings and evidence: `out/qa/final_verification_rulings.md`. Each item gives the options and the interim state of the deliverables until you decide.\n")
    for i, t, x in VERIFICATION_ITEMS:
        lines.append(f"**{i} {t}** {x}\n")
    return "\n".join(lines)


def main() -> int:
    decisions = parse_decisions(DECISIONS.read_text(encoding="utf-8"))
    text = render(decisions)
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
