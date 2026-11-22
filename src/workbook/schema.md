# Department input schema (version 1)

One JSON file per department drives `python3 -m src.workbook build --input <json> --out <xlsx>`. Rule: every value has a source ID that
resolves on `sources`. A value that is unknown is `null` and stays blank in the workbook (it is listed on Flags); it is never invented.
Codes: `D` value taken from a document, `M` measured from a drawing, `S` fixed by a specification or set by the engineer,
`A` assumed, to confirm (shown yellow and listed on Basis of Design and Flags).

Provenance is given as `[code, source_id]`. A room carries `prov`, a map from input group to that pair, with an optional
`prov_default`; a key that is a field name (for example `terminal_tag`) overrides its group for that field. Room Information shows a code and source ID beside every input (identity and zoning fields included); a blank input shows neither. A value without provenance is rejected (`InputError`).

## Top level

| Key | Content |
|---|---|
| `schema_version` | 1 |
| `project` | `name`, `code`, `revision {rev, date, description, by, checked, approved}`; `by` (author), `checked`, `approved` come from the project input (`config/project.yaml` `workbook.people`), blank when not given, and `by` is also the file's author property |
| `department` | `number`, `name` |
| `scope`, `purpose`, `criteria_origin[]`, `key_flags[]`, `documents[]` | text for README and Basis of Design; `documents` rows `{document, role, issue, taken, source}` |
| `storeys[]`, `storey_labels{}` | order of the storey codes and their tab labels (`L01` -> `Level 1`); one `<Level> Room Zones` tab per storey |
| `constants{}` | design constants by workbook name (below); each `{label, value, unit, code, source, remark}` |
| `space_types[]` | `{name, room_t_c, room_h_kjkg, code, source}`: design room condition per space type |
| `activities[]` | `{name, sensible_w, latent_w, code, source}`: occupant gains |
| `orientations[]` | `{name, window_w_m2, wall_w_m2, code, source}`: indicative peak gains per orientation (assumption until facade data) |
| `space_type_defaults[]` | `{name, lighting_w_m2, small_power_w_m2, density_per_100m2, source}`: yellow fallback table |
| `sources[]` | `{id, document, issue, page, location, text_as_read, method, confidence}` |
| `rooms[]` | see below |
| `ahus[]` | `{tag, type, sched_supply_lps, sched_oa_lps, sched_code, sched_source, schematic_supply_lps, schematic_code, schematic_source, schematic_remark}` |
| `fans[]` | `{tag, type (RETURN, EXHAUST, ISOLATION EXHAUST, OUTDOOR AIR, PAHU), serves, scheduled_lps, code, source, remark}`; `serves` is an AHU tag, or for an exhaust fan its own tag (rooms name it in `exhaust_fan`) |
| `terminals[]` | `{tag, type (VAV, CAV, VENTURI, NONE), rooms[], code, source}`; a room's `terminal_tag` not listed here gets a row with a blank type |
| `valves[]` | `{tag, room, role (SUPPLY, EXHAUST), airflow_mode2_lps, code, source}` |
| `pressure_nodes[]` | spaces with a known pressure outside the rooms: `{tag, description, pressure_pa, code, source}`; `REF` (0 Pa) is added if absent |
| `references[]` | information-only airflows for Airflow Comparison: `{label, flow_lps, oa_lps, code, source, remark}` |
| `flags[]` | `{type (CONFIRM, VERIFY, ASSUMPTION), ref, text, status, owner, source}` |

Required `constants` (no default, never invented): `SupplyTemp`, `OutdoorDB`, `OutdoorWB`, `OutdoorH`, `FanPressure`, `FanEfficiency`,
`RoofSolarGain`. Every other constant (safety factor 0.10, duct leakage 0.10, air constants 1.21, 1.2 and 1.18, the pressure method,
the outdoor-air rule 5 % and 5 percentage points, check tolerances) has a default with a built-in source (`GEN-ENGINE`,
`GEN-CONFIG`, `GEN-PRESSURE`) and can be overridden. All are named cells on Basis of Design.

## Room

| Key | Group in `prov` | Note |
|---|---|---|
| `number`, `name`, `storey` | `identity` | `number` is the key used everywhere |
| `ahu`, `zone_tag`, `system_type`, `terminal_tag`, `exhaust_fan`, `oa_source`, `oa_condition` | `zoning` | `oa_source`: PAHU or fan that serves the room's outdoor air if it moves to an FCU (blank: the FCU tab takes the first PAHU, else the first outdoor-air fan, on `fans[]` whose `serves` is the room's AHU, and flags it; none found: NOT SET, coil on untreated air); `oa_condition`: TREATED or UNTREATED (blank: TREATED for a PAHU) |
| `space_type`, `room_condition` | `space_type` | must be a name in `space_types`; `room_condition` Comfort or Critical |
| `soa_area_m2` | `soa_area` | |
| `measured_area_m2` | `measured_area` | normally code M |
| `area_selector` | `area_selector` | AUTO (default), SOA, MEASURED, LARGER |
| `ceiling_height_m` | `ceiling_height` | |
| `occupancy_people`, `occupancy_per_100m2` | `occupancy_people`, `occupancy_density` | stated count wins over density |
| `iof`, `activity`, `operation_hours` | `iof`, `activity`, `operation_hours` | |
| `lighting_w_m2`, `small_power_w_m2`, `equipment_sensible_w`, `equipment_latent_w` | `lighting`, `small_power`, `equipment_sensible`, `equipment_latent` | |
| `criteria` | one `source` and `code` for the whole row | `criteria_row`, `page`, `min_oa_ach`, `oa_lps_per_person`, `oa_specified_lps`, `min_total_ach`, `supply_lps_m2`, `supply_specified_lps`, `exhaust_ach`, `exhaust_lps_m2`, `exhaust_specified_lps`, `temp_min_c`, `temp_max_c`, `rh_max_pct`, `pressure_regime` (POSITIVE, NEGATIVE, NEUTRAL), `pressure_pa_booklet`, `pressure_pa_tender`, `pressure_pa_working`, `air_class`, `filter`, `single_pass` (Y or N), `afterhours`, `remark`. Specified values (MOT theatres) use code `S`. The pressure target used is the tender value, then the working-drawing value, then the booklet value. |
| `envelope[]` | per row `source`, `code` | `{segment, building_part, face, azimuth_deg, orientation (a name in `orientations`), glazing_m2, wall_m2, roof_m2, shading, method, remark}`; a room with no rows is internal |
| `pressure` | `prov` for the perimeter | `{mode: controlled or free, suite, perimeter_m, surface_override_m2, surface_prov, doors[]}`. `controlled`: the room has a pressure target in its criteria. `free`: no set pressure; the tab solves it in fixed Newton steps. Door `{id, to, kind, seal (hinged, sliding, bed_width, shaft), gap_cm2, leaf_w_m, leaf_h_m, fixed_flow_m3h, code, source}`; `to` is a room number on Pressure or a `pressure_nodes` tag; a door is one air path between the room and `to` and acts on both spaces it joins: enter it once, under either room (the same `id` listed from both sides counts once; the workbook and `src/recompute` both apply this, including across suites); `fixed_flow_m3h` is positive from the room to `to`. Precedence of the gap area: fixed flow, then `gap_cm2`, then `seal`, then the leaf size with the unsealed gap widths. |
| `schematic` | `source`, `code` | `{supply_lps, return_lps, exhaust_lps, pressure_pa, remark}` read from the air-side schematics |
| `remark` | | plain-language remark on the source reference, e.g. "SOA V16 row 42; plan area 21.4 m2, agrees" |

## Rules the generator keeps

* The structure of `pressure` (controlled, free, none) is fixed at build time; Airflow links the offset only for controlled rooms.
* Only `leaks_to: REF` is supported (the whole envelope leaks to the reference space).
* A controlled room needs a pressure value in `criteria`; a direction only (POSITIVE, NEGATIVE) with no value is listed on Flags, not calculated.
* Rooms with no AHU are listed on Flags; the AHU tab lists AHUs found on the rooms.

## Outdoor-air rule, MOT rooms and outputs

* Outdoor air of a room is the larger of its minimum and what exhaust and pressurisation need, never more than its supply.
* A room whose `criteria.code` is `S` (MOT theatre) is fixed: specified supply, outdoor air and exhaust stand, and Airflow and Pressure report the offset needed beside the specified one (VERIFY on Checks if it is short). Such a room is listed for Marcus, never separated.
* Every room the rule separates (CONFIRM) or lists (VERIFY) has a live row on Flags with status OPEN, and `python3 -m src.workbook build` writes it to `out/zoning/zoning_change_proposals.csv` (rows keyed by department; `--no-proposals` skips it).
* Coil duty reference condition: the first supply room of the AHU, as the example's AHU Summary takes the first space type.
