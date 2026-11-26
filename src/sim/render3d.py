"""3D view of the EnergyPlus model geometry (read from out/sim/AIGH_IP.idf).

Colours surfaces by unit (zone name prefix), outside walls/roofs only, glazing in blue.
Usage: python3 -m src.sim.render3d [--out out/sim/AIGH_IP_3d.png] [--az -60 --el 25]
"""
import argparse
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

IDF = Path("out/sim/AIGH_IP.idf")


def objects(text, cls):
    for m in re.finditer(r"^\s*" + re.escape(cls) + r",(.*?);", text, re.S | re.M):
        body = re.sub(r"!.*", "", m.group(1))
        yield [f.strip() for f in body.split(",")]


def _verts(fields, start):
    nums = [float(x) for x in fields[start:] if x not in ("", "autocalculate")]
    return [tuple(nums[i:i + 3]) for i in range(0, len(nums) - 2, 3)]


def load(text):
    # BuildingSurface:Detailed: Name, Type, Construction, Zone, Space, OBC, OBC obj, Sun, Wind, VF, NVerts, X1...
    surf = {}
    for f in objects(text, "BuildingSurface:Detailed"):
        surf[f[0]] = dict(type=f[1].lower(), zone=f[3], obc=f[5].lower(), v=_verts(f, 11))
    win = []
    for f in objects(text, "FenestrationSurface:Detailed"):
        # Name, Type, Construction, Building Surface, OBC obj, VF, Frame, Multiplier, NVerts, X1...
        win.append(dict(host=f[3], v=_verts(f, 9)))
    return surf, win


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/sim/AIGH_IP_3d.png")
    ap.add_argument("--az", type=float, default=-60)
    ap.add_argument("--el", type=float, default=25)
    a = ap.parse_args()
    surf, win = load(IDF.read_text(encoding="utf-8", errors="replace"))
    units = sorted({s["zone"].split(" ")[0] if " " in s["zone"] else s["zone"] for s in surf.values()})
    cmap = plt.get_cmap("tab20")
    col = {}
    for s in surf.values():
        key = re.sub(r"^(\S+\s+\S+).*", r"\1", s["zone"])
        col.setdefault(key, cmap(len(col) % 20))
    polys, cols, edges = [], [], []
    for s in surf.values():
        if s["obc"] not in ("outdoors", "adiabatic", "ground") or s["type"] == "floor" and s["obc"] != "ground":
            continue
        key = re.sub(r"^(\S+\s+\S+).*", r"\1", s["zone"])
        polys.append(s["v"])
        cols.append((*col[key][:3], 0.85))
    wp = [w["v"] for w in win]
    fig = plt.figure(figsize=(16, 11), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(polys, facecolors=cols, edgecolors=(0, 0, 0, 0.25), linewidths=0.2))
    if wp:
        ax.add_collection3d(Poly3DCollection(wp, facecolors=(0.35, 0.6, 0.9, 0.9), edgecolors="none"))
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    zs = [p[2] for pl in polys for p in pl]
    ax.set_xlim(min(xs), max(xs))
    ax.set_ylim(min(ys), max(ys))
    ax.set_zlim(min(zs), max(zs))
    ax.set_box_aspect((max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)))
    ax.view_init(elev=a.el, azim=a.az)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m, north)")
    ax.set_zlabel("z (m)")
    ax.set_title(f"AIGH Inpatient LB3–L07 thermal model: {len({s['zone'] for s in surf.values()})} zones, "
                 f"{len(surf)} surfaces, {len(win)} windows (colour = storey/unit, blue = glazing)")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, bbox_inches="tight")
    print(a.out, len(polys), len(wp))


if __name__ == "__main__":
    main()
