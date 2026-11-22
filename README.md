# AIGH IP ACMV pipeline

Alexandra Integrated General Hospital redevelopment, Contract 2, Inpatient building. ACMV document
conversion, AHU zoning, department thermal-load workbooks and schematic updates, built to the brief
`AIGH_IP_ACMV_Claude_Code_Prompt.md` (Rev D, 22 November 2026). Everything produced here is a draft
for the reviewing engineer's approval at the gates defined in the brief.

## Layout

| Path | Holds |
|---|---|
| `data/source/` | Read-only copy of the Drive folders `DD Sign-OFF`, `Template`, `Basis` (locked after each pull; not in git) |
| `out/markdown/` | Markdown corpus mirroring the source tree (uploaded to Drive `MarkDown`; not in git) |
| `out/sidecar/` | Coordinates, tables and raw reads as JSON and CSV (not in git) |
| `out/registers/` | Manifest, page census, sheet index, rooms, AHUs, equipment, envelope, comments, conflicts, assumptions |
| `out/zoning/`, `out/loads/`, `out/schematics/` | Phase 2, 3 and 4 deliverables |
| `out/qa/` | Check reports, evidence crops, council minutes, model allocation, decisions needed, forensic report |
| `src/` | Pipeline code. `config/` configuration. `state/` listings, ledgers, progress. `logs/` run logs |

## Entry points

```text
python -m src.drive_io doctor | list | pull | push-markdown      # the only module that touches Drive
python -m src.manifest build                                     # manifest, skipped zips, anomaly report
python -m src.census run                                         # page census by script
python -m src.pressure --selftest                                # pressure-offset method, known-answer tests
python -m src.checks phase0 … phase5                             # pass/fail per phase
python -m src.decisions add|list                                 # out/qa/decisions_needed.md
```

## Deviations from the brief in this environment

Recorded in `out/qa/decisions_needed.md` for the engineer to confirm:

- **Drive transport.** This cloud container has no rclone credential. The Google Drive connector of the
  session lists metadata and saves files up to 10 MB to disk; files above that come by upload or by a
  transport the engineer chooses (D-001). `drive_io.py` keeps the rclone verbs and commands from the
  brief and uses them unchanged when the remotes `aigh_src` and `aigh_md` exist.
- **Appendices A to E** of the brief and Section 11 from item 24 onward were not received. The bootstrap
  files, register columns and Markdown layout are reconstructed from the body of the brief (D-002).
- **Segmentation model weights** cannot be downloaded here (hosts blocked); method B falls back to
  region growing unless the hosts are allowed (D-003).

## Rules that hold everywhere

Sources are read-only. No zip files. MarkItDown first. Scripts before models, text layer before vision.
Provenance on every value. Unknown stays unknown. No silent conflict resolution. The engineering method
belongs to the engineer. Data stays in this session. Evidence, not claims. Issued deliverables do not
name tools. Stop at gates.
