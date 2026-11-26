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
    org = {}
    for f in objects(text, "Zone"):
        org[f[0]] = tuple(float(x) if x else 0.0 for x in f[2:5])
    surf = {}
    for f in objects(text, "BuildingSurface:Detailed"):
        o = org.get(f[3], (0.0, 0.0, 0.0))
        surf[f[0]] = dict(type=f[1].lower(), zone=f[3], obc=f[5].lower(),
                          v=[(x + o[0], y + o[1], z + o[2]) for x, y, z in _verts(f, 11)])
    win = []
    for f in objects(text, "FenestrationSurface:Detailed"):
        # Name, Type, Construction, Building Surface, OBC obj, VF, Frame, Multiplier, NVerts, X1...
        win.append(dict(host=f[3], v=_verts(f, 9)))
    for w in win:
        h = surf.get(w["host"])
        o = org.get(h["zone"], (0.0, 0.0, 0.0)) if h else (0.0, 0.0, 0.0)
        w["v"] = [(x + o[0], y + o[1], z + o[2]) for x, y, z in w["v"]]
    return surf, win


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/sim/AIGH_IP_3d.png")
    ap.add_argument("--az", type=float, default=-60)
    ap.add_argument("--el", type=float, default=25)
    a = ap.parse_args()
    surf, win = load(IDF.read_text(encoding="utf-8", errors="replace"))
    storeys = ["LB3", "B3", "B2", "B1", "L01", "L02", "L03", "L04", "L05", "L06", "L07"]
    cmap = plt.get_cmap("viridis")
    scol = {st: cmap(i / (len(storeys) - 1)) for i, st in enumerate(storeys)}
    floors, fcol, walls, wcol = [], [], [], []
    for s in surf.values():
        st = s["zone"].split(" ")[0]
        if s["type"] == "floor":
            floors.append(s["v"])
            fcol.append((*scol.get(st, (0.5, 0.5, 0.5, 1))[:3], 0.95))
        elif s["type"] == "wall" and s["obc"] == "outdoors":
            walls.append(s["v"])
            wcol.append((0.6, 0.6, 0.65, 0.22) if s["obc"] == "outdoors" else (0.35, 0.3, 0.25, 0.25))
    wp = [w["v"] for w in win]
    fig = plt.figure(figsize=(16, 12), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(floors, facecolors=fcol, edgecolors=(0, 0, 0, 0.15), linewidths=0.15))
    ax.add_collection3d(Poly3DCollection(walls, facecolors=wcol, edgecolors="none"))
    if wp:
        ax.add_collection3d(Poly3DCollection(wp, facecolors=(0.15, 0.45, 0.95, 0.55), edgecolors="none"))
    polys = floors + walls
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=scol[s], label=f"{s} floor") for s in storeys]
              + [Patch(color=(0.15, 0.45, 0.95), label="glazing"), Patch(color=(0.55, 0.55, 0.6), label="external wall"),
                 ], loc="upper left", fontsize=8)
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
                 f"{len(surf)} surfaces, {len(win)} windows")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, bbox_inches="tight")
    print(a.out, len(floors), len(walls), len(wp))


if __name__ == "__main__":
    main()
