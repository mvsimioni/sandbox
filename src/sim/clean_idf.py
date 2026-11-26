"""Clean the translated IDF geometry for EnergyPlus (D-109).

Per BuildingSurface:Detailed: merge vertices closer than TOL, drop collinear vertices; delete the surface (and any window on it)
when fewer than 3 vertices remain or its area is below MIN_AREA. The model has no interzone surfaces (boundaries are Outdoors,
Adiabatic or Ground), so deleting a sliver leaves no orphan reference; the zone loses a sliver of wall (< MIN_AREA each).
Optionally sets the Building Solar Distribution.

``python3 -m src.sim.clean_idf out/sim/AIGH_IP.idf out/sim/AIGH_IP_clean.idf [--solar MinimalShadowing]``
"""
from __future__ import annotations

import argparse
import re

TOL = 0.01       # m
MIN_AREA = 0.05  # m2


def _area(v):
    nx = ny = nz = 0.0
    for i in range(len(v)):
        x1, y1, z1 = v[i]
        x2, y2, z2 = v[(i + 1) % len(v)]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return 0.5 * (nx * nx + ny * ny + nz * nz) ** 0.5


def _clean(v):
    out = []
    for p in v:
        if not out or max(abs(a - b) for a, b in zip(p, out[-1])) > TOL:
            out.append(p)
    if len(out) > 1 and max(abs(a - b) for a, b in zip(out[0], out[-1])) <= TOL:
        out.pop()
    changed = True
    while changed and len(out) >= 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            ab = [b[k] - a[k] for k in range(3)]
            bc = [c[k] - b[k] for k in range(3)]
            cr = (ab[1] * bc[2] - ab[2] * bc[1], ab[2] * bc[0] - ab[0] * bc[2], ab[0] * bc[1] - ab[1] * bc[0])
            lab = sum(x * x for x in ab) ** 0.5
            lbc = sum(x * x for x in bc) ** 0.5
            if lab * lbc == 0 or (sum(x * x for x in cr) ** 0.5) / (lab * lbc) < 1e-3:
                out.pop(i)
                changed = True
                break
    return out


def clean(text: str, solar: str | None = None):
    objs = list(re.finditer(r"(?m)^BuildingSurface:Detailed,\n(.*?;)[^\n]*\n", text, re.S))
    dropped, fixed, keep = set(), 0, []
    pieces, last = [], 0
    for m in objs:
        lines = m.group(0).splitlines()
        code = " ".join(ln.split("!")[0] for ln in lines[1:]).replace(";", ",")
        vals = [x.strip() for x in code.split(",")]
        name = vals[0]
        nv_i = 10   # Number of Vertices field (index after name)
        nums = [float(x) for x in vals[nv_i + 1:] if x != ""]
        verts = [tuple(nums[i:i + 3]) for i in range(0, len(nums) - 2, 3)]
        cv = _clean(verts)
        pieces.append(text[last:m.start()])
        last = m.end()
        if len(cv) < 3 or _area(cv) < MIN_AREA:
            dropped.add(name.upper())
            continue
        if len(cv) != len(verts):
            fixed += 1
        head = lines[:1 + nv_i]
        body = [f"  {len(cv)},                                      !- Number of Vertices"]
        for i, (x, y, z) in enumerate(cv):
            end = ";" if i == len(cv) - 1 else ","
            body.append(f"  {x:.4f}, {y:.4f}, {z:.4f}{end}  !- Vertex {i + 1}")
        pieces.append("\n".join(head + body) + "\n")
    pieces.append(text[last:])
    out = "".join(pieces)
    # windows on dropped hosts
    wins = 0

    def _win(m):
        nonlocal wins
        host = m.group(0).splitlines()[4].split("!")[0].strip().rstrip(",").upper()
        if host in dropped:
            wins += 1
            return ""
        return m.group(0)
    out = re.sub(r"(?m)^FenestrationSurface:Detailed,\n.*?;[^\n]*\n", _win, out, flags=re.S)
    # ideal loads: point at the zone's DSOA space list (the translator writes the first space's DSOA only, i.e. one share of the OA)
    lists = set(x.upper() for x in re.findall(r"(?m)^DesignSpecification:OutdoorAir:SpaceList,\n\s*([^,]+),", out))
    nlinks = 0

    def _ideal(m):
        nonlocal nlinks
        body = m.group(0)

        def _f(mm):
            nonlocal nlinks
            name = mm.group(2).strip()
            if name.upper().endswith(" OA") and (name[:-3] + " DSOA Space List").upper() in lists:
                nlinks += 1
                return mm.group(1) + name[:-3] + " DSOA Space List" + mm.group(3)
            return mm.group(0)
        return re.sub(r"(?m)^(  )([^,\n]+)(,\s*!- Design Specification Outdoor Air Object Name)", _f, body)
    out = re.sub(r"(?m)^ZoneHVAC:IdealLoadsAirSystem,\n.*?;[^\n]*\n", _ideal, out, flags=re.S)
    if solar:
        out = re.sub(r"(?m)^(  )[^,\n]*(,\s*!- Solar Distribution)", r"\g<1>" + solar + r"\2", out, count=1)
    return out, dict(surfaces=len(objs), dropped=len(dropped), vertices_fixed=fixed, windows_dropped=wins, ideal_oa_space_lists=nlinks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--solar")
    a = ap.parse_args()
    out, info = clean(open(a.src, encoding="utf-8").read(), a.solar)
    open(a.dst, "w", encoding="utf-8").write(out)
    print(info)


if __name__ == "__main__":
    main()
