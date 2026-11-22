"""Zoning pilot (Sections 7.1 to 7.4): calibration, room polygons, AHU zones, PDF writer.

Conventions used throughout the package:

* page view: the rotated, landscape page frame in pt, origin top-left, y down
  (``unrotated_point * page.rotation_matrix``), as in the registers;
* building frame: millimetres, X along the numbered grids (1 -> 23, page right),
  Y along the lettered grids (A -> W, page up), origin at grid 2 / A
  (``out/registers/grid_model.json``);
* areas are in m2 and lengths in m unless a name says otherwise.
"""
