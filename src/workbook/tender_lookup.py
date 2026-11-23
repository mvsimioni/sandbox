"""One tender airflow lookup for every deliverable (department workbooks, AHU master summary, zone schedule).

Order of sources: Equipment Schedule (Corr 5) rows (air handling units, pre-cooled units and fan coil units), then the MOT NSC Overall AC Equipment Schedule
(Corr 2), then the tender air-side schematics (sheet totals and the duty printed under each unit tag). A tag is looked up as written, without the suffix
A, B or C, with the suffix, and, for a list tag such as 'FCU-B-01-01RETAIL-01/02/03' or 'PAHU-B-B1-B2HOSP-01A/B/C', for each unit of the list.

Where a source gives different values for the units of one tag, or where lower sources give values that differ from the value taken, the value is not
chosen silently: the first case leaves the value blank and names the values, the second names the other values in the source text.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import common

_UNIT_RE = re.compile(r"^(?:AHU|PAHU|FCU|DXFCU)-")


def _num(x: Any) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def expand_tag(tag: str) -> List[str]:
    """Units named by a tag: 'X-01/02/03' gives X-01, X-02, X-03; 'X-01A/B/C' gives X-01A, X-01B, X-01C; any other tag gives itself."""
    t = (tag or "").strip()
    m = re.fullmatch(r"(.*-)(\d{2})((?:/\d{2})+)", t)
    if m:
        return [m.group(1) + n for n in [m.group(2)] + re.findall(r"\d{2}", m.group(3))]
    m = re.fullmatch(r"(.*\d)([A-C])((?:/[A-C])+)", t)
    if m:
        return [m.group(1) + x for x in [m.group(2)] + re.findall(r"[A-C]", m.group(3))]
    return [t]


class Tender:
    """Tender airflow of a unit; blank (no information) when no source gives one or the sources disagree."""

    def __init__(self, root: Optional[Path] = None) -> None:
        P = common.resolve_paths() if root is None else {"registers": Path(root) / "out" / "registers"}
        self.sched: Dict[str, dict] = {}
        self.sched_dups: Dict[str, List[dict]] = defaultdict(list)       # a second row of the same tag in the schedule (a different value is listed, not chosen)
        for r in common.read_csv_dicts(P["registers"] / "ahu_schedule.csv"):
            v = _num(r.get("supply_airflow_l_s [Supply Airflow (l/s)]", ""))
            if v is not None:
                if r.get("tag_normalised", "").strip() in self.sched:
                    self.sched_dups[r.get("tag_normalised", "").strip()].append({"v": v, "pages": r.get("page(s)", ""), "written": r.get("tag_as_written", "")})
                self.sched.setdefault(r.get("tag_normalised", "").strip(), {"v": v, "sheet": r.get("sheet_code", ""), "pages": r.get("page(s)", ""), "title": r.get("schedule_title", ""),
                                                                      "written": r.get("tag_as_written", ""), "oa": _num(r.get("outdoor_air_min_l_s [Outdoor Airflow / Min (l/s)]", "")), "type": r.get("type") or "AHU",
                                                                      "reheat": _num(r.get("reheat_air_flow_l_s", "")), "kind": "ahu"})
        for r in common.read_csv_dicts(P["registers"] / "equipment_schedule_air_handling.csv"):     # fan coil units: same schedule document
            if r.get("type") != "FCU":
                continue
            v = _num(r.get("Min Airflow (l/s)", ""))
            if v is not None:
                self.sched.setdefault(r.get("tag_normalised", "").strip(), {"v": v, "sheet": r.get("sheet_code", ""), "pages": r.get("page(s)", ""), "title": r.get("schedule_title", ""),
                                                                      "written": r.get("tag_as_written", ""), "oa": None, "type": "FCU", "kind": "fcu"})
        self.mot: Dict[str, dict] = {}                 # MOT NSC Overall AC Equipment Schedule (Corr 2): AHU and PAHU rows only
        for r in common.read_csv_dicts(P["registers"] / "mot_ahu_schedule.csv"):
            if r.get("equipment_type") not in ("AHU", "PAHU"):
                continue
            v = _num(r.get("supply_airflow_l_s", ""))
            if v is not None:
                self.mot.setdefault(r.get("tag", "").strip().replace("lSONP", "ISONP"), {"v": v, "issue": r.get("issue", ""), "pages": r.get("page", ""), "title": r.get("schedule_title", ""),
                                                                                  "written": r.get("tag", "").strip(), "oa": _num(r.get("outdoor_air_min_l_s", "")), "type": r.get("equipment_type") or "AHU"})
        self.schem: Dict[str, List[dict]] = defaultdict(list)
        for r in common.read_csv_dicts(P["registers"] / "schematic_ahu_totals.csv"):
            if r.get("issue_set", "") != "tender" or r.get("scope", "sheet") != "sheet":
                continue
            v = _num(r.get("ahu_supply_L_s", ""))
            if v is not None:
                self.schem[r.get("ahu_tag", "").strip()].append({"v": v, "sheet": r.get("sheet", ""), "issue": r.get("issue", ""), "text": r.get("ahu_supply_as_written", "")})
        for r in common.read_csv_dicts(P["registers"] / "schematic_equipment.csv"):       # the duty printed under the unit tag
            if r.get("issue_set") != "tender" or r.get("type") not in ("AHU", "PAHU", "FCU"):
                continue
            m = re.match(r"\s*([\d,]+)\s*L/s", r.get("duty_as_written") or "")
            if m:
                t = re.sub(r"/([A-C])$", r"\1", r.get("tag", "").strip())
                v = float(m.group(1).replace(",", ""))
                if not any(x["sheet"] == r.get("sheet", "") and x["v"] == v for x in self.schem[t]):
                    self.schem[t].append({"v": v, "sheet": r.get("sheet", ""), "issue": r.get("issue", ""), "text": r["duty_as_written"], "page": r.get("page", "")})

    # ---- tags ------------------------------------------------------------------------------------
    @staticmethod
    def candidates(tags: List[str]) -> List[str]:
        """Tags to look up for a zone tag: each unit of a list tag, each as written, then the duty and standby units (suffix A and B) and the tag without the
        suffix. A tag that ends in a number ('...-01') can be scheduled as '...-01A' and '...-01B'."""
        out: List[str] = []
        for t0 in tags:
            for t in expand_tag(t0):
                t = t.strip()
                out.append(t)
                if re.search(r"\d[A-C]$", t):
                    base = t[:-1]
                elif t.endswith("A/B"):
                    base = t[:-3]
                elif re.search(r"\d$", t):
                    base = t
                else:
                    base = None
                if base is not None:
                    out += [base + "A", base + "B", base + "C", base] if base != t else [base + "A", base + "B", base + "C"]
        return list(dict.fromkeys(out))

    @staticmethod
    def level_variants(cands: List[str]) -> List[str]:
        """Same tags with the plant-level part of the tag replaced by the served-level part ('AHU-B-07-04CATH-01' -> 'AHU-B-04-04CATH-01'). Used only after the tag as written has no row."""
        out = []
        for c in cands:
            m = re.match(r"^((?:AHU|PAHU|FCU)-B-)(\d\d)-(\d\d)(.*)$", c)
            if m and m.group(2) != m.group(3):
                out.append("%s%s-%s%s" % (m.group(1), m.group(3), m.group(3), m.group(4)))
        return out

    def similar_tag_note(self, tag: str, hit: str) -> str:
        """A schedule row whose tag differs from this one only in the name part, where one name begins with the other ('ICUSTAFF' and 'ICUSTA'): named, not taken."""
        m = re.match(r"^((?:AHU|PAHU|FCU)-B-\d\d-\d\d)([A-Z]+)-(\d\d)", hit)
        if not m:
            return ""
        out = []
        for t, row in {**self.sched, **self.mot}.items():
            m2 = re.match(r"^((?:AHU|PAHU|FCU)-B-\d\d-\d\d)([A-Z]+)-(\d\d)", t)
            if m2 and m2.group(1) == m.group(1) and m2.group(3) == m.group(3) and m2.group(2) != m.group(2) and (m2.group(2).startswith(m.group(2)) or m.group(2).startswith(m2.group(2))):
                out.append("%s %g L/s" % (re.sub(r"(?<=\d)[AB]$", "A/B", t), row["v"]))
        return ("; a schedule prints a similar tag: %s. Confirm whether it is this unit" % ", ".join(sorted(set(out))[:2])) if out else ""

    @staticmethod
    def _look(table: Dict[str, Any], cands: List[str]):
        return [(c, table[c]) for c in cands if c in table]

    def _all_values(self, cands: List[str]) -> List[Tuple[float, str]]:
        """Every value that any source gives for the candidate tags, with a short source text."""
        out: List[Tuple[float, str]] = []
        for c, h in self._look(self.sched, cands):
            out.append((h["v"], "Equipment Schedule (Corr 5), page %s, %s" % (h["pages"], h["written"] or c)))
        for c in cands:
            for h in self.sched_dups.get(c, []):
                out.append((h["v"], "Equipment Schedule (Corr 5), page %s, %s" % (h["pages"], h["written"] or c)))
        for c, h in self._look(self.mot, cands):
            out.append((h["v"], "MOT NSC Overall AC Equipment Schedule (%s), page %s, %s" % (h["issue"], h["pages"], c)))
        for c in cands:
            for x in self.schem.get(c, []):
                out.append((x["v"], "tender air-side schematic %s, %s" % (x["sheet"], c)))
        return out

    # ---- lookup ----------------------------------------------------------------------------------
    def lookup(self, tags: List[str]) -> Dict[str, Any]:
        """{value, text, kind, doc, issue, page, location, text_as_read, others, tag_as_written} for the tags of one unit. value is None when no source gives a value or the units differ."""
        cands = self.candidates(tags)
        alias = False
        eq5 = self._look(self.sched, cands)
        mot = self._look(self.mot, cands)
        hits2 = [(c, x) for c in cands for x in self.schem.get(c, [])]
        if not eq5 and not mot:
            vcands = self.level_variants(cands)
            vcands = list(dict.fromkeys(c for v in vcands for c in self.candidates([v])))
            eq5v, motv = self._look(self.sched, vcands), self._look(self.mot, vcands)
            if eq5v or motv:
                eq5, mot, alias = eq5v, motv, True
                cands = cands + vcands
        note = ""
        if alias:
            note = "; the schedule prints the tag as %s" % ", ".join(sorted({h[0] for h in (eq5 or mot)}))
        res: Dict[str, Any] = dict(value=None, text="", kind="", others=[], tag_as_written=tags[0] if tags else "")

        def others_text(taken: float, tag_src: str) -> str:
            diff = []
            for v, s in self._all_values(cands):
                if abs(v - taken) > 1e-6 and (v, s) not in diff and s != tag_src:
                    diff.append((v, s))
            res["others"] = diff
            return ("; other values for this unit: " + "; ".join("%s L/s (%s)" % (format(v, ",.0f"), s) for v, s in diff[:4])) if diff else ""

        if eq5:
            vals = {h[1]["v"] for h in eq5}
            if len(vals) == 1:
                h = eq5[0][1]
                extra = others_text(h["v"], "")
                res.update(value=h["v"], kind="eq5" if h["kind"] == "ahu" else "eq5fcu", doc="Equipment Schedule, %s" % ("air handling unit schedule" if h["kind"] == "ahu" else "fan coil unit schedule"),
                           issue="Corr 5", page=str(h["pages"]), sheet=h["sheet"], tag_as_written=h["written"] or eq5[0][0], oa=h.get("oa"), type=h.get("type"), cand=eq5[0][0],
                           text="Equipment Schedule SCH/1 (Corr 5), %s, page %s%s%s" % (h["sheet"], h["pages"], note, extra))
                return res
            res["text"] = "No information: the units of this tag show different airflows in Equipment Schedule (Corr 5) (%s L/s)" % ", ".join("%s %g" % (c, h["v"]) for c, h in eq5)
            return res
        if mot:
            vals = {h[1]["v"] for h in mot}
            if len(vals) == 1:
                h = mot[0][1]
                extra = others_text(h["v"], "")
                res.update(value=h["v"], kind="mot", doc="MOT NSC Overall AC Equipment Schedule", issue=h["issue"], page=str(h["pages"]), tag_as_written=mot[0][0], oa=h.get("oa"), type=h.get("type"), cand=mot[0][0],
                           text="MOT NSC Overall AC Equipment Schedule (%s), page %s%s%s" % (h["issue"], h["pages"], note, extra))
                return res
            res["text"] = "No information: the units of this tag show different airflows in the MOT NSC Overall AC Equipment Schedule (%s L/s)" % ", ".join("%s %g" % (c, h["v"]) for c, h in mot)
            return res
        if hits2:
            vals = {h[1]["v"] for h in hits2}
            if len(vals) == 1:
                h = hits2[0][1]
                res.update(value=h["v"], kind="schem", doc="Air-side schematic %s" % h["sheet"], issue=h["issue"], page=str(h.get("page", "")), sheet=h["sheet"], cand=hits2[0][0],
                           tag_as_written=hits2[0][0], text_as_read=h.get("text", ""),
                           text="Tender air-side schematic %s (%s)%s%s" % (h["sheet"], h["issue"], note, self.similar_tag_note(tags[0], hits2[0][0])))
                return res
            lst = sorted({(v, h["sheet"]) for _, h in hits2 for v in [h["v"]]})
            res["others"] = [(v, "tender air-side schematic %s" % s) for v, s in lst]
            res["text"] = ("No information: tender air-side schematics show different airflows (%s L/s)" % ", ".join(sorted("%g" % v for v in vals)) +
                           " (" + "; ".join("%s on %s" % (format(v, ",.0f"), s) for v, s in lst[:4]) + ")")
            return res
        res["text"] = "No information: the unit is not in Equipment Schedule (Corr 5), the MOT NSC Overall AC Equipment Schedule (Corr 2) or the tender air-side schematics"
        return res

    def find(self, tags: List[str]) -> Tuple[Optional[float], str]:
        r = self.lookup(tags)
        return r["value"], r["text"]

    def schematic_value(self, tags: List[str]) -> Optional[Dict[str, Any]]:
        """The tender air-side schematic figure alone (for the comparison with the schematics): {value, sheet, issue, text_as_read, cand} or None when there is none or the figures differ."""
        cands = self.candidates(tags)
        hits = [(c, x) for c in cands for x in self.schem.get(c, [])]
        if hits and len({h[1]["v"] for h in hits}) == 1:
            c, h = hits[0]
            return dict(value=h["v"], sheet=h["sheet"], issue=h["issue"], text_as_read=h.get("text", ""), page=str(h.get("page", "")), cand=c)
        return None


_SHARED: Dict[str, "Tender"] = {}


def shared(root: Optional[Path] = None) -> "Tender":
    """The one lookup object of a process (one per project root)."""
    key = "" if root is None else str(Path(root).resolve())
    if key not in _SHARED:
        _SHARED[key] = Tender(root)
    return _SHARED[key]


def record_conflicts(units: List[Dict[str, Any]]) -> int:
    """Append to out/registers/conflicts.csv one row for each unit whose tender airflow sources differ (the value taken and the other values, or a blank value and
    the values that disagree). A unit already listed under the same topic is not listed again. Returns the number of rows added."""
    import csv
    P = common.resolve_paths()
    path = P["registers"] / "conflicts.csv"
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0].keys()) if rows else ["conflict_id", "topic", "item", "value_a", "source_a", "value_b", "source_b", "precedence_applied", "resolution", "status", "flag"]
    topic = "tender airflow of a unit differs between sources"
    have = {(r["topic"], r["item"]) for r in rows}
    last = max((int(re.sub(r"\D", "", r["conflict_id"]) or 0) for r in rows), default=0)
    new = []
    for u in units:
        if (topic, u["tag"]) in have or not u.get("others"):
            continue
        last += 1
        taken = ("%s L/s" % format(u["value"], ",.0f")) if u.get("value") is not None else "blank (no value chosen)"
        new.append({"conflict_id": "CF-%04d" % last, "topic": topic, "item": u["tag"], "value_a": taken, "source_a": u.get("text", "")[:240],
                    "value_b": "; ".join("%s L/s" % format(v, ",.0f") for v, _ in u["others"][:5]), "source_b": "; ".join(s for _, s in u["others"][:5]),
                    "precedence_applied": "Equipment Schedule (Corr 5), then MOT NSC Overall AC Equipment Schedule (Corr 2), then tender air-side schematic" if u.get("value") is not None else "none applies",
                    "resolution": "value taken as stated; other values named in the Remarks" if u.get("value") is not None else "left blank; the values are named in the Remarks",
                    "status": "open", "flag": "VERIFY"})
    if new:
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            for r in new:
                w.writerow({k: r.get(k, "") for k in fields})
    return len(new)
