"""New air-side schematics for the FCUs that the zone schedule lists with no tag (brief 9.3 item 5, 9.4).

The zone schedule holds 98 lines "FCU (tag to confirm) [room]" with one room each and no department workbook row.
Each line is a system that no sheet shows.  The sheets here draw, per storey, one FCU box ("tag to confirm") for each line, its room
box, and the supply and return ducts.  Airflows are "not given": no workbook carries the room.  The outdoor-air source is a query.
Style, title block, legend and symbols are those of the approved pilot (newdwg.py); the group layout follows the separated-room
groups of newdwg_missing.py.  The numbers are the next free numbers of the 06 series, provisional.

    python -m src.schematics.newdwg_fcu run
    python -m src.schematics.newdwg_fcu check
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openpyxl
from openpyxl.utils import get_column_letter

from . import newdwg as nd
from . import newdwg_missing as nm
from .newdwg import H, HS, HT, LP, BW, Value, Y_MIN, Y_TOP, SHELF_GAP, X_MAX, X0_PAGE, insert, wrap
from .newdwg_missing import Stack, draw_card, emit, fnum1

ROOT = nd.ROOT
NEWDIR = nd.NEWDIR
CHUNK = 6
COVERAGE = NEWDIR / "fcu_notag_coverage.csv"
FCU_RE = re.compile(r"^FCU( duty/standby)? \(tag to confirm\)")


class FcuData:
    def __init__(self):
        self.d = nm.Data()
        wb = openpyxl.load_workbook(ROOT / "out/zoning/zone_schedule.xlsx", data_only=True, read_only=True)
        self.col = {h: get_column_letter(i + 1) for i, h in enumerate(next(wb["AHU summary"].iter_rows(values_only=True)))}
        self.tabcol: Dict[str, Dict[str, str]] = {}
        for st in nm.STOREYS:
            self.tabcol[st] = {h: get_column_letter(i + 1) for i, h in enumerate(next(wb[st].iter_rows(values_only=True)))}
        wb.close()

    def rows(self) -> List[Dict[str, str]]:
        return [r for r in self.d.mrows if r["category"] == "FCU, tag to confirm"]

    def build(self, row: Dict[str, str]) -> Optional[Dict[str, Any]]:
        d = self.d
        tag = row["system"]
        n, zr = d.zs_rows[tag]
        head = d.zs_head
        storey = str(zr[head["Storeys served"]] or "")
        rooms = d.zrooms.get(tag, [])
        if storey not in nm.STOREYS or len(rooms) != 1:
            return None
        rm = rooms[0]
        c = self.col
        tc = self.tabcol[rm["storey"]]
        return dict(kind="fcu", chg=row["change_id"], tag=tag, zs_row=n, storey=storey, room=rm,
                    cells=dict(tag="A%d" % n, type=c["Type"] + str(n), storeys=c["Storeys served"] + str(n),
                               rooms=c["Rooms"] + str(n), area=c["Area (m²)"] + str(n),
                               calc=c["Calculated airflow (L/s)"] + str(n), calc_oa=c["Calculated outdoor air (L/s)"] + str(n),
                               rid=tc["Room ID"] + str(rm["row"]), rname=tc["Room name"] + str(rm["row"]),
                               rarea=tc["Area (m²)"] + str(rm["row"]), tab=rm["storey"]),
                    type=str(zr[head["Type"]]), n_rooms=zr[head["Rooms"]], area=zr[head["Area (m²)"]])

    def items(self) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, str]]]:
        by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        not_drawn: List[Dict[str, str]] = []
        for r in self.rows():
            F = self.build(r)
            if F is None:
                not_drawn.append(dict(change_id=r["change_id"], item=r["system"],
                                      reason="no room and no storey in the zone schedule: the FCU cannot be placed; it stays on the list of missing systems"))
            else:
                by[F["storey"]].append(F)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for st, fs in by.items():
            fs.sort(key=lambda f: f["room"]["room_id"])
            groups[st] = [dict(kind="fcugrp", storey=st, bid="FCUS:%s:%d" % (st, i // CHUNK + 1), members=fs[i:i + CHUNK])
                          for i in range(0, len(fs), CHUNK)]
        return groups, not_drawn


def zsrc(tab: str, cell: str) -> str:
    return "Zone schedule, tab %s, cell %s" % (tab, cell)


def room_stack(F: Dict[str, Any]) -> Stack:
    chg, cl, rm = F["chg"], F["cells"], F["room"]
    owner = "box:fcu:%s" % rm["room_id"]
    st = Stack(BW - 3.2)
    st.owner = owner
    st.gap(1.2)
    st.text(str(rm["room_id"]), HS, value=Value("room_no", str(rm["room_id"]), chg, zsrc(cl["tab"], cl["rid"]), owner), gap=0.9)
    st.text(str(rm["name"]), H + 0.1, value=Value("name", str(rm["name"]), chg, zsrc(cl["tab"], cl["rname"]), owner), gap=1.1)
    cnt = Value("count", "1", chg, zsrc("AHU summary", cl["rooms"]), owner)
    area = Value("area_m2", nm.numtxt(rm["area"]), chg, zsrc(cl["tab"], cl["rarea"]), owner)
    st.values += [cnt, area]
    st.y -= H
    st.ops.append((0, st.y, "COUNT", HS, "IP-ROOMTEXT", None, (0, 1), owner))
    st.ops.append((14, st.y, cnt.text, H, "IP-ROOMTEXT", cnt, (0, 1), owner))
    st.ops.append((22, st.y, "AREA", HS, "IP-ROOMTEXT", None, (0, 1), owner))
    st.ops.append((33, st.y, area.text + " m2", H, "IP-ROOMTEXT", area, (0, 1), owner))
    st.y -= H * LP - H + 0.4

    def nv(field, cell):
        return Value(field, "not given", chg, zsrc("AHU summary", cell), owner)
    sa, ra, ea, oa = nv("supply_L_s", cl["calc"]), nv("return_L_s", cl["calc"]), nv("exhaust_L_s", cl["calc"]), nv("outdoor_L_s", cl["calc_oa"])
    st.pair([(0, "SA L/S", sa, ""), (36, "RA L/S", ra, "")])
    st.pair([(0, "EA L/S", ea, ""), (36, "OA L/S", oa, "")])
    st.gap(2.0)
    return st


def room_height(F: Dict[str, Any]) -> float:
    n0 = Value._n
    st = room_stack(F)
    Value._n = n0
    return round(st.height + 2 * HS * LP + 3.4, 1)


def draw_room(sh, F: Dict[str, Any], x: float, y_top: float, h_box: float) -> None:
    owner = "box:fcu:%s" % F["room"]["room_id"]
    insert(sh, "ROOMBOX", x, y_top - h_box, BW, h_box, "IP-ROOMBOX")
    emit(sh, room_stack(F), x + 1.6, y_top)
    v = Value("change_id", F["chg"], F["chg"], zsrc("AHU summary", F["cells"]["tag"]), owner)
    sh.values.append(v)
    sh.text(x + 1.6, y_top - h_box + 1.2 + HS * LP, "CHG " + F["chg"], HS, "IP-ANNO-ID", v, owner=owner)
    sh.text(x + 1.6, y_top - h_box + 1.2, "ZONE ROOM " + str(F["room"]["room_id"]), HS, "IP-ANNO-ID", owner=owner)


def fcu_stack(F: Dict[str, Any], w: float) -> Stack:
    chg, cl = F["chg"], F["cells"]
    owner = "fcu:%s" % F["room"]["room_id"]
    st = Stack(w - 3.2)
    st.owner = owner
    st.gap(1.2)
    st.text("FCU, TAG TO CONFIRM", HT, "IP-EQUIPTEXT", dx=1.6)
    st.text(F["type"], H, "IP-EQUIPTEXT", Value("unit_type", F["type"], chg, zsrc("AHU summary", cl["type"]), owner), dx=1.6,
            prefix="TYPE ", gap=0.2)
    st.text(nm.numtxt(F["n_rooms"]), H, "IP-EQUIPTEXT", Value("zone_rooms", nm.numtxt(F["n_rooms"]), chg, zsrc("AHU summary", cl["rooms"]), owner),
            dx=1.6, prefix="ROOMS IN THE ZONE SCHEDULE ", gap=0.2)
    st.text(nm.numtxt(F["area"]), H, "IP-EQUIPTEXT", Value("zone_area_m2", nm.numtxt(F["area"]), chg, zsrc("AHU summary", cl["area"]), owner),
            dx=1.6, prefix="ZONED AREA ", suffix=" m2", gap=0.2)
    st.gap(1.0)
    for ln in wrap("QUERY %s: NO WORKBOOK CARRIES THIS ROOM, SO NO AIRFLOW IS GIVEN. NO DOCUMENT GIVES THE TAG OR THE OUTDOOR-AIR SOURCE." % chg,
                   HS, w - 3.2 - 1.6):
        st.y -= HS
        st.ops.append((1.6, st.y, ln, HS, "IP-QUERY", None, (0, 1), owner))
        st.y -= HS * LP - HS
    st.gap(1.0)
    return st


def draw_fcugrp(sh, G: Dict[str, Any], gx: float, ytop: float) -> None:
    owner = "fcugrp:" + G["bid"]
    members = G["members"]
    head = "FCUS WITH NO TAG IN THE ZONE SCHEDULE, %s (%d OF THIS GROUP)" % (nm.LEVEL_NAME[G["storey"]], len(members))
    sh.text(gx, ytop - 3.0, head, 3.0, "IP-NOTES", owner=owner)
    fw, off = 104.0, 18.0
    x_f = gx + 14.0
    x_r = x_f + fw + off
    oa_x = gx + 4.0
    yy = ytop - 8.0
    rows = []
    for F in members:
        h_room = room_height(F)
        n0 = Value._n
        st = fcu_stack(F, fw)
        Value._n = n0
        h_f = max(st.height + 2 * HS * LP + 2.4, 22.0)
        hrow = max(h_room, h_f)
        rows.append((F, yy, hrow))
        yy -= hrow + 10.0
    for F, yt, hrow in rows:
        st = fcu_stack(F, fw)
        hh = draw_card(sh, st, "SYM_FCU", x_f, yt, fw, F["chg"], "ZONE SCHEDULE ROW %d" % F["zs_row"], "fcu:%s" % F["room"]["room_id"],
                       foot_src=zsrc("AHU summary", F["cells"]["tag"]))
        draw_room(sh, F, x_r, yt, max(hrow, hh))
        sh.line((oa_x, yt - 4.0), (x_f, yt - 4.0), nd.DUCT_LAYER["OA"])
        sh.text(gx - 0.0, yt - 2.4, "OA", HS, nd.DUCT_LAYER["OA"], owner=owner)
        sh.line((x_f + fw, yt - 6.0), (x_r, yt - 6.0), nd.DUCT_LAYER["SA"])
        sh.line((x_r, yt - 11.0), (x_f + fw, yt - 11.0), nd.DUCT_LAYER["RA"])
        sh.text(x_f + fw + 2.0, yt - 4.6, "SA", HS, nd.DUCT_LAYER["SA"], owner=owner)
        sh.text(x_f + fw + 2.0, yt - 9.8, "RA", HS, nd.DUCT_LAYER["RA"], owner=owner)
    sh.last_bottom = rows[-1][1] - rows[-1][2] - 1.0


def build_storey(storey: str, groups: List[Dict[str, Any]]) -> List[Tuple[nm.SheetX, Dict[str, Any]]]:
    sym = nd.legend_symbols()
    scratch = nm.SheetX(storey, "SCRATCH", 1, 1, ["SCRATCH"])
    nd.make_blocks(scratch, sym)
    blocks, by_id = [], {}
    for G in groups:
        blk = nd.Block(G["bid"], "ahu", lambda sh, gx, yt, G=G: draw_fcugrp(sh, G, gx, yt), [], [])
        nd.measure(scratch, blk)
        blocks.append(blk)
        by_id[blk.bid] = G
    layout = nd.paginate(blocks)
    numbers = nm.provisional_numbers(storey, len(layout))
    out = []
    for i, bands in enumerate(layout):
        Value._n = 0
        sh = nm.SheetX(storey, numbers[i], i + 1, len(layout), numbers)
        sh.caption_kind = "FCUS WITH NO TAG"
        sh.title_kind = ["FCUS WITH NO TAG IN THE", "ZONE SCHEDULE"]
        sh.note_kind = ("THE SHEET SHOWS THE FCUS OF THE %s THAT THE ZONE SCHEDULE LISTS WITH NO TAG AND THAT NO WORKING SHEET DRAWS. EACH "
                        "FCU HAS ONE ROOM. THE TAG IS TO BE CONFIRMED.")
        nd.make_blocks(sh, sym)
        nd.frame(sh)
        y_top = Y_TOP
        level_ys: List[float] = []
        for band in bands:
            hmax = max(b.h for b, _x in band)
            for b, bx0 in band:
                b.draw(sh, bx0 - b.xmin, y_top)
                sh.items.append(by_id[b.bid])
            level_ys.append(y_top - hmax - 6.0)
            y_top = y_top - hmax - SHELF_GAP
        sh.band_bottom = level_ys[-1] + 6.0
        nm.levels2(sh, level_ys)
        nm.strip2(sh)
        nm.key_plan2(sh)
        nm.legend_list2(sh)
        nm.caption2(sh)
        out.append((sh, dict(blocks=[b for band in bands for b, _ in band])))
    return out


def reserve_numbers() -> None:
    """Numbers of the sheets that exist are taken, except the sheets of this job from an earlier run (they are replaced)."""
    mine = set()
    if COVERAGE.exists():
        with open(COVERAGE, encoding="utf-8", newline="") as fh:
            mine = {r["drawing"] for r in csv.DictReader(fh) if r["drawing"]}
    for p in NEWDIR.glob("IP-BH-*_P1.dxf"):
        if p.name[:11] not in mine:
            nm._TAKEN.add(p.name[6:11])


def run(storeys: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    NEWDIR.mkdir(parents=True, exist_ok=True)
    reserve_numbers()
    fd = FcuData()
    groups, not_drawn = fd.items()
    results = []
    for st in (storeys or nm.STOREYS):
        if st not in groups:
            continue
        for sh, ctx in build_storey(st, groups[st]):
            paths = nd.write_sheet(sh)
            results.append(dict(storey=st, number=sh.number, sheet=sh, ctx=ctx, **paths))
            print(sh.number, st, "%d FCUs" % sum(len(g["members"]) for g in sh.items))
    run.not_drawn = not_drawn          # type: ignore[attr-defined]
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "check"])
    ap.add_argument("--storey", action="append", default=None)
    ns = ap.parse_args(argv)
    res = run(ns.storey)
    if ns.cmd == "check":
        from . import newdwg_fcu_check as chk
        return chk.main(res, getattr(run, "not_drawn", []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
