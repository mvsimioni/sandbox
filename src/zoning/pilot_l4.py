"""Launcher for the 4th-storey pilot that works under ``python3 -I`` (isolated mode ignores PYTHONPATH and the cwd).

Usage: ``python3 -I src/zoning/pilot_l4.py [--stages calibrate,vector,raster,compare,zones,pdf,report]``
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.zoning.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
