"""3D view of the OpenStudio model: the ThreeJS scene written by the OpenStudio SDK (ThreeJSForwardTranslator, the format of the
OpenStudio geometry viewer) shown with three.js, coloured as OpenStudio colours the surface types, and a screenshot of it.

``python3 -m src.sim.view3d`` writes out/sim/view3d/index.html (+ scene JSON) and out/sim/AIGH_IP_OpenStudio_3D_*.png.
"""
from __future__ import annotations

import http.server
import shutil
import socketserver
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SIM = ROOT / "out/sim"
VIEW = SIM / "view3d"

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>AIGH IP OpenStudio 3D</title>
<style>html,body{margin:0;height:100%;background:#f4f5f7;font:13px sans-serif}#info{position:absolute;left:12px;top:10px;background:#fffc;
padding:6px 10px;border-radius:4px}</style>
<script src="three.min.js"></script>
<script src="OrbitControls.js"></script></head>
<body><div id="info">AIGH Inpatient LB3–L07 — OpenStudio model (OpenStudio 3.9 ThreeJS scene). Drag to orbit, scroll to zoom.</div><script>
const r=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});r.setPixelRatio(window.devicePixelRatio);
r.setSize(innerWidth,innerHeight);document.body.appendChild(r.domElement);r.setClearColor(0xf4f5f7);
const cam=new THREE.PerspectiveCamera(30,innerWidth/innerHeight,1,5000);const ctl=new THREE.OrbitControls(cam,r.domElement);
fetch('scene.json').then(x=>x.json()).then(j=>{
  const scene=new THREE.ObjectLoader().parse(j);
  scene.add(new THREE.AmbientLight(0xffffff,0.55));const dl=new THREE.DirectionalLight(0xffffff,0.6);dl.position.set(-1,2,1.5);scene.add(dl);
  const box=new THREE.Box3().setFromObject(scene);const c=box.getCenter();const s=box.getSize();
  const VIEW=new URLSearchParams(location.search).get('v')||'iso';
  const d=Math.max(s.x,s.y,s.z)*(VIEW==='iso'?1.55:1.9);
  const dirs={iso:[-0.9,0.75,1.0],sw:[-1,0.6,-0.8],top:[0.001,1,0.001]};const v=dirs[VIEW]||dirs.iso;const n=Math.hypot(...v);
  cam.position.set(c.x+d*v[0]/n,c.y+d*v[1]/n,c.z+d*v[2]/n);ctl.target.copy(c);ctl.update();
  (function loop(){requestAnimationFrame(loop);r.render(scene,cam);})();
  setTimeout(()=>{document.title='READY';},1500);
});
</script></body></html>"""


def main():
    VIEW.mkdir(parents=True, exist_ok=True)
    shutil.copy(SIM / "AIGH_IP_threejs.json", VIEW / "scene.json")
    for f in ("three.min.js", "OrbitControls.js"):            # three.js r87 (npm three@0.87.1, MIT): reads the OpenStudio scene format
        shutil.copy(Path(__file__).with_name("vendor") / f, VIEW / f)
    (VIEW / "index.html").write_text(HTML, encoding="utf-8")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(VIEW), **k)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        js = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const b = await chromium.launch({{ args: ['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'] }});
  const pg = await b.newPage({{ viewport: {{ width: 1600, height: 1000 }}, deviceScaleFactor: 2 }});
  for (const v of ['iso','sw','top']) {{
    await pg.goto('http://127.0.0.1:{port}/index.html?v=' + v);
    pg.on('console', m => console.log('page:', m.text())); pg.on('pageerror', e => console.log('pageerror:', e.message));
    await pg.waitForFunction("document.title==='READY'", null, {{ timeout: 300000 }});
    await pg.waitForTimeout(1500);
    await pg.screenshot({{ path: '{SIM}/AIGH_IP_OpenStudio_3D_' + v + '.png' }});
    console.log('{SIM}/AIGH_IP_OpenStudio_3D_' + v + '.png');
  }}
  await b.close();
}})();
"""
        import subprocess
        res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=1800)
        print(res.stdout[-3000:], res.stderr[-3000:])
        httpd.shutdown()


if __name__ == "__main__":
    main()
