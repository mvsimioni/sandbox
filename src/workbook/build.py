"""Orchestrator: department input -> workbook with live formulas."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from openpyxl import Workbook

from . import sheets_basic as SB
from . import sheets_pressure as SP
from . import sheets_rooms as SR
from . import sheets_systems as SS
from .ctx import Ctx
from .layouts import FIRST, build_layouts
from .model import InputError, Layout, blank, load_input
from .styles import install_styles


def _prepare(ctx: Ctx) -> None:
    d = ctx.data
    ctx.room_eng = {}
    ctx.eng_total_row = {}
    ctx.room_ai = {}
    # envelope rows
    ctx.env_rows = []
    for r in ctx.rooms:
        for e in r.get("envelope", []):
            ctx.env_rows.append(dict(e, room=r["number"]))
    # pressure nodes
    nodes: List[Dict[str, Any]] = []
    ctx.ps_index = {}
    for gi, r in enumerate(ctx.rooms):
        pr = r.get("pressure")
        if not pr:
            continue
        ctrl = pr.get("mode", "controlled") == "controlled"
        if ctrl:
            c = r.get("criteria") or {}
            if all(blank(c.get(k)) for k in ("pressure_pa_booklet", "pressure_pa_tender", "pressure_pa_working")):
                raise InputError(f"room {r['number']}: controlled on Pressure but no pressure value in its criteria")
        if pr.get("leaks_to", "REF") != "REF":
            raise InputError(f"room {r['number']}: leaks_to other than REF is not supported by the workbook")
        nodes.append(dict(tag=r["number"], kind="ROOM", room=r, gi=gi, controlled=ctrl, suite=pr.get("suite") or r["number"]))
    ext = list(d.get("pressure_nodes", []))
    if not any(e["tag"] == "REF" for e in ext):
        ext.insert(0, dict(tag="REF", description="Reference space (corridor outside the suite)", pressure_pa=0, code="S", source="GEN-PRESSURE"))
    for e in ext:
        if blank(e.get("source")):
            raise InputError(f"pressure node {e['tag']} has no source id")
        nodes.append(dict(tag=e["tag"], kind="EXTERNAL", room=None, controlled=False, suite="", description=e.get("description"),
                          pressure_pa=e["pressure_pa"], code=e.get("code", "S"), source=e["source"]))
    for q, nd in enumerate(nodes):
        nd["index"] = q
        if nd["kind"] == "ROOM":
            ctx.ps_index[nd["tag"]] = dict(index=q, controlled=nd["controlled"])
    ctx.ps_nodes = nodes
    tags = {n["tag"] for n in nodes}
    seen = set()
    links = []
    for r in ctx.rooms:
        for door in (r.get("pressure") or {}).get("doors", []):
            key = (door["id"], frozenset((r["number"], door["to"])))
            if key in seen:
                continue
            seen.add(key)
            if door["to"] not in tags:
                raise InputError(f"door {door['id']}: node '{door['to']}' is not a room on Pressure or a pressure node")
            links.append(dict(door, room=r["number"], a=r["number"], b=door["to"]))
    ctx.pl_links = links
    suites = []
    for n in nodes:
        if n["kind"] == "ROOM" and n["controlled"] and n["suite"] not in suites:
            suites.append(n["suite"])
    ctx.suites = suites
    # AHUs
    info = {a["tag"]: a for a in d.get("ahus", [])}
    ahus = []
    for r in ctx.rooms:
        t = r.get("ahu")
        if not blank(t) and t not in [a["tag"] for a in ahus]:
            ahus.append(dict(info.get(t, {}), tag=t))
    ctx.ahus = ahus
    ctx.fans = list(d.get("fans", []))
    terminals = list(d.get("terminals", []))
    have = {t["tag"] for t in terminals}
    for r in ctx.rooms:
        t = r.get("terminal_tag")
        if not blank(t) and t not in have:
            code, src = (r.get("prov") or {}).get("zoning") or ["D", ""]
            terminals.append(dict(tag=t, type=None, rooms=[], source=src, code=code))
            have.add(t)
    for t in terminals:
        if not t.get("rooms"):
            t["rooms"] = [r["number"] for r in ctx.rooms if r.get("terminal_tag") == t["tag"]]
    ctx.terminals = terminals
    ctx.valves = list(d.get("valves", []))
    ctx.references = list(d.get("references", []))
    n_sys_fcu = sum(1 for r in ctx.rooms if str(r.get("system_type") or "").upper() == "FCU")
    ctx.n_fcu_rows = min(len(ctx.rooms), n_sys_fcu + 4 * max(1, len(ctx.ahus)))   # 4 = outdoor-air rule rounds


def build_workbook(data: Union[Dict[str, Any], Path, str], out: Optional[Path] = None) -> Ctx:
    if not isinstance(data, dict):
        data = load_input(Path(data))
    data = copy.deepcopy(data)
    ctx = Ctx(data)
    _prepare(ctx)
    wb = Workbook()
    wb.remove(wb.active)
    ctx.wb = wb
    install_styles(wb)
    names = ["README", "Basis of Design", "Room Information", "Criteria", "Envelope"] + [ctx.sheet_for(s) for s in ctx.storeys] + \
            ["Airflow", "Pressure", "AHU", "FCU", "Fans", "VAV", "Venturi Valves", "Flow Balance", "Airflow Comparison", "Checks",
             "Sources", "Flags"]
    for n in names:
        wb.create_sheet(n)
    build_layouts(ctx)
    SB.write_basis(ctx)
    SR.write_room_information(ctx)
    SR.write_criteria(ctx)
    SR.write_envelope(ctx)
    SR.write_engines(ctx)
    SR.write_airflow(ctx)
    SP.write_pressure(ctx)
    SS.write_ahu(ctx)
    SS.write_fcu(ctx)
    SS.write_fans(ctx)
    SS.write_vav(ctx)
    SS.write_venturi(ctx)
    SS.write_flow_balance(ctx)
    SS.write_comparison(ctx)
    SB.write_sources(ctx)
    rows = SB.build_flag_rows(ctx)
    ctx.n_live = min(len(ctx.rooms), 4 * len(ctx.ahus))       # outdoor-air rule: one live flag per separated or listed room
    ctx.n_flags = len(rows) + ctx.n_live
    ctx.lay["FL"] = Layout("Flags", ["id", "type", "ref", "desc", "status", "owner", "src", "note"], 7, ctx.n_flags)
    SB.write_checks(ctx)
    SB.write_readme(ctx)
    SB.write_flags(ctx, rows)
    SB.write_basis_tail(ctx)
    wb.properties.creator = str((data.get("project", {}).get("revision") or {}).get("by") or "")
    wb.properties.title = f"{ctx.project}: {data['department'].get('name', '')} thermal load calculation"
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        wb.save(out)
    ctx.out = out
    return ctx
