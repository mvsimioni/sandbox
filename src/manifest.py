"""Build the file manifest, the skipped-zip register and the inventory anomaly report.

Inputs (all read-only):

* the latest consolidated listing ``state/listings/latest`` (or the newest
  ``state/listings/listing_*.json``),
* the pull ledger ``state/pull_ledger.jsonl`` (plus ``state/upload_ledger.jsonl``
  when files were uploaded by hand),
* the pull plan ``state/pull_plan.csv`` (for connector-limit blocks),
* ``state/unstable_departments.json`` when the lister produced one,
* ``drive.transport`` from ``config/project.yaml`` (``--transport`` overrides it):
  the connector size limit only blocks files on the connector transport.

Integrity checks on the way: a file whose Drive size or md5 moved since it
was fetched is reported as a stale pull (``pulled=n``), a Drive id listed
twice only inherits a ledger record whose path matches, and oversize files
that the pull plan never saw are still flagged.

Outputs:

* ``out/registers/manifest.csv``: one row per non-folder file in the listing,
* ``out/registers/skipped_zip.csv``,
* ``out/qa/inventory_anomalies.md``,
* a ``manifest`` item in ``state/progress.json``.

The build is cheap and derived entirely from state, so it is re-run from
scratch every time; no file is ever hashed here (hashes come from the ledger).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (python -m src.manifest, tests)
    from src.common import (
        CONNECTOR_LIMIT_BYTES,
        PATHS,
        ROOT_DD,
        STAGES,
        atomic_write_text,
        classify_document,
        file_ext,
        is_zip,
        load_config,
        parse_department,
        parse_stage,
        project_root,
        read_csv_dicts,
        read_jsonl,
        write_csv_dicts,
    )
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import (  # type: ignore[no-redef]
        CONNECTOR_LIMIT_BYTES,
        PATHS,
        ROOT_DD,
        STAGES,
        atomic_write_text,
        classify_document,
        file_ext,
        is_zip,
        load_config,
        parse_department,
        parse_stage,
        project_root,
        read_csv_dicts,
        read_jsonl,
        write_csv_dicts,
    )
    from state import Progress  # type: ignore[no-redef]

FOLDER_MIME = "application/vnd.google-apps.folder"

MANIFEST_COLUMNS: Tuple[str, ...] = (
    "drive_id", "rel_path", "root", "dept_no", "dept_name", "dept_alias", "stage", "doc_class",
    "ext", "bytes", "drive_modified", "drive_md5", "sha256", "pulled", "duplicate_of", "notes",
)
SKIPPED_ZIP_COLUMNS: Tuple[str, ...] = ("rel_path", "bytes", "parent_folder", "drive_id", "nested")

EXPECTED_DEPARTMENTS = 50
PROBABLE_DUP_NOTE = "probable duplicate (size+name)"
BLOCKED_NOTE = "blocked: connector limit"
OVER_LIMIT_NOTE = "over connector limit (rclone transport: not blocked)"
STALE_NOTE = "stale pull"
DUPLICATE_ID_NOTE = "duplicate Drive id in listing"
TRANSPORTS: Tuple[str, ...] = ("connector", "rclone")
PLAN_BLOCKED_STATUS = "blocked_connector_limit"

_DEPT_FOLDER_RE = re.compile(r"^(?:__)?(\d{2})\s*-\s*(.+)$")
_FILE_NUMBER_RE = re.compile(r"^(\d{1,2})(?=[\s_\-.])")
_ZIP_TWO_RE = re.compile(r"^(.*?)\s*\(2\)\.zip$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _segments(rel_path: str) -> List[str]:
    """Split a Drive-style relative path on forward slashes, dropping empties."""
    return [seg for seg in str(rel_path).replace("\\", "/").split("/") if seg]


def _to_int(value: Any) -> int:
    """Coerce a Drive ``fileSize`` (string, int or missing) to an integer."""
    try:
        return int(str(value).strip()) if value not in (None, "") else 0
    except ValueError:
        return 0


def _fmt_bytes(count: int) -> str:
    """Human-readable byte count alongside the exact figure."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(count)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{count:,} ({value:.1f} {unit})"
        value /= 1024
    return str(count)


def _is_department_folder(name: str) -> bool:
    """True when a folder name matches the department convention (strays included)."""
    return bool(_DEPT_FOLDER_RE.match(name.strip()))


def file_number(title: str) -> str:
    """Leading one- or two-digit number of a file name (``'47_AN Hub.xlsx'`` -> ``'47'``), else ''."""
    match = _FILE_NUMBER_RE.match(title.strip())
    return match.group(1) if match else ""


def dept_alias(title: str, dept_no: str) -> str:
    """The file's leading number when it differs from the folder's department number."""
    number = file_number(title)
    if not number or not dept_no:
        return ""
    return number if int(number) != int(dept_no) else ""


def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    """Render a Markdown table; an empty body yields a single 'none' row."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    body = [list(row) for row in rows]
    if not body:
        body = [["none"] + [""] * (len(headers) - 1)]
    for row in body:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return lines


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def resolve_listing_path(state_dir: Path, explicit: Optional[Path] = None) -> Path:
    """Locate the consolidated listing: explicit path, ``listings/latest``, else the newest ``listing_*.json``."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"listing not found: {explicit}")
        return explicit
    listings = state_dir / "listings"
    latest = listings / "latest"
    if latest.exists():
        target = latest.resolve()
        if target.is_dir():
            inner = sorted(target.glob("listing_*.json"))
            if inner:
                return inner[-1]
        elif target.is_file():
            head = target.read_text(encoding="utf-8", errors="replace")[:4096].lstrip()
            if head.startswith("{"):
                return target
            pointer = (listings / head.strip()).resolve()
            if pointer.is_file():
                return pointer
    candidates = sorted(listings.glob("listing_*.json"))
    if candidates:
        return candidates[-1]
    raise FileNotFoundError(f"no consolidated listing under {listings}; run the lister first")


def load_listing(path: Path) -> Dict[str, Any]:
    """Read a consolidated listing ``{"taken_at", "files", "folders"}`` and normalise its lists."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"listing {path} is not a JSON object")
    files = [f for f in data.get("files", []) if f.get("mimeType") != FOLDER_MIME]
    folders = list(data.get("folders", []))
    folders.extend(f for f in data.get("files", []) if f.get("mimeType") == FOLDER_MIME)
    return {"taken_at": data.get("taken_at", ""), "files": files, "folders": folders, "path": str(path)}


def load_ledger(state_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Index pull-ledger records by ``drive_id`` and by ``rel_path`` (later records win)."""
    index: Dict[str, Dict[str, Any]] = {}
    for name in ("pull_ledger.jsonl", "upload_ledger.jsonl"):
        for record in read_jsonl(state_dir / name):
            if not isinstance(record, dict):
                continue
            if record.get("drive_id"):
                index["id:" + str(record["drive_id"])] = record
            if record.get("rel_path"):
                index["path:" + str(record["rel_path"])] = record
    return index


def ledger_lookup(ledger: Dict[str, Dict[str, Any]], drive_id: str, rel_path: str,
                  unique_id: bool = True) -> Optional[Dict[str, Any]]:
    """Find the ledger record for a file, preferring the Drive id over the path.

    When the listing carries the same Drive id more than once (``unique_id`` is
    False) the id record is accepted only if its ``rel_path`` matches, so a
    second entry never inherits a fetch made for the first. The path fallback
    is used only for records without a Drive id or with the same one.
    """
    record = ledger.get("id:" + str(drive_id)) if drive_id else None
    if record is not None and (unique_id or str(record.get("rel_path") or rel_path) == rel_path):
        return record
    by_path = ledger.get("path:" + rel_path)
    if by_path is None or (by_path.get("drive_id") and str(by_path["drive_id"]) != str(drive_id)):
        return None
    return by_path


def load_plan(state_dir: Path) -> Dict[str, str]:
    """``{drive_id: status}`` from the pull plan (empty when there is no plan)."""
    rows = read_csv_dicts(state_dir / "pull_plan.csv")
    return {str(row["drive_id"]): str(row.get("status") or "") for row in rows if row.get("drive_id")}


def load_blocked_ids(state_dir: Path) -> set[str]:
    """Drive ids the pull plan marks ``blocked_connector_limit`` (empty when there is no plan)."""
    return {drive_id for drive_id, status in load_plan(state_dir).items() if status == PLAN_BLOCKED_STATUS}


def configured_transport(override: Optional[str] = None) -> str:
    """The pull transport: ``override`` when given, else ``drive.transport`` from the config."""
    value = override or str((load_config().get("drive", {}) or {}).get("transport") or "connector")
    value = value.strip().lower()
    if value not in TRANSPORTS:
        raise ValueError(f"unknown transport '{value}' (expected one of {', '.join(TRANSPORTS)})")
    return value


def load_unstable(state_dir: Path) -> Any:
    """Return the parsed ``unstable_departments.json`` or ``None`` when absent/unreadable."""
    path = state_dir / "unstable_departments.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"


# --------------------------------------------------------------------------- #
# Folder tree
# --------------------------------------------------------------------------- #
class FolderTree:
    """Parent lookups over the folders of a listing, by Drive id."""

    def __init__(self, folders: Sequence[Dict[str, Any]]) -> None:
        self.by_id: Dict[str, Dict[str, Any]] = {str(f.get("id")): f for f in folders if f.get("id")}

    def top_folder(self, file_rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The folder directly under the root for ``file_rec`` (walks ``parentId``; ``None`` if unknown)."""
        current = self.by_id.get(str(file_rec.get("parentId", "")))
        seen: set[str] = set()
        while current is not None and str(current.get("id")) not in seen:
            seen.add(str(current.get("id")))
            if len(_segments(current.get("rel_path", ""))) == 2:
                return current
            current = self.by_id.get(str(current.get("parentId", "")))
        return None

    def department_folders(self) -> List[Dict[str, Any]]:
        """Folders directly under ``DD Sign-OFF``."""
        return sorted(
            (f for f in self.by_id.values() if _segments(f.get("rel_path", ""))[:1] == [ROOT_DD]
             and len(_segments(f.get("rel_path", ""))) == 2),
            key=lambda f: (str(f.get("title", "")), str(f.get("id", ""))),
        )


# --------------------------------------------------------------------------- #
# Manifest rows
# --------------------------------------------------------------------------- #
def _base_row(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Columns that depend on the listing record alone."""
    rel_path = str(rec.get("rel_path", ""))
    title = str(rec.get("title") or (_segments(rel_path) or [""])[-1])
    dept = parse_department(rel_path)
    root = dept["root"]
    zipped = is_zip(rel_path)
    notes: List[str] = []
    if dept["stray"]:
        notes.append("stray folder")
    if zipped:
        notes.append("zip skipped (not pulled)")
    if str(rec.get("mimeType", "")).startswith("application/vnd.google-apps."):
        notes.append("Google-native document (no binary size)")
    return {
        "drive_id": str(rec.get("id", "")),
        "rel_path": rel_path,
        "root": root,
        "dept_no": dept["dept_no"],
        "dept_name": dept["dept_name"],
        "dept_alias": dept_alias(title, dept["dept_no"]) if root == ROOT_DD else "",
        "stage": parse_stage(rel_path) if root == ROOT_DD else "",
        "doc_class": "zip" if zipped else classify_document(rel_path),
        "ext": str(rec.get("fileExtension") or file_ext(rel_path)).lower(),
        "bytes": _to_int(rec.get("fileSize")),
        "drive_modified": str(rec.get("modifiedTime", "")),
        "drive_md5": str(rec.get("md5Checksum") or ""),
        "sha256": "",
        "pulled": "n",
        "duplicate_of": "",
        "notes": notes,
        "_title": title,
        "_stray": dept["stray"],
        "_blocked": False,
        "_over_limit": False,
        "_has_size": rec.get("fileSize") not in (None, ""),
        "_stale": "",
        "_moved_from": "",
        "_id_count": 1,
    }


def stale_reasons(row: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    """Why the listing no longer matches the fetched copy: Drive size or md5 moved since the fetch.

    The ledger ``bytes`` equal Drive's size at fetch time only when ``size_match``
    was not False, and ``drive_md5`` (else the local ``md5`` when ``md5_match``
    was not False) stands for Drive's md5 at fetch time; nothing else is compared.
    """
    reasons: List[str] = []
    fetched_bytes = record.get("bytes")
    if row["_has_size"] and fetched_bytes not in (None, "") and record.get("size_match") is not False:
        if _to_int(fetched_bytes) != row["bytes"]:
            reasons.append(f"Drive size {_to_int(fetched_bytes)} at fetch, now {row['bytes']}")
    fetched_md5 = str(record.get("drive_md5") or "")
    if not fetched_md5 and record.get("md5_match") is not False:
        fetched_md5 = str(record.get("md5") or "")
    if row["drive_md5"] and fetched_md5 and row["drive_md5"] != fetched_md5:
        reasons.append("Drive md5 changed since fetch")
    return reasons


def _apply_ledger(row: Dict[str, Any], record: Optional[Dict[str, Any]], root_dir: Path) -> None:
    """Fill ``sha256``/``pulled`` from the ledger record and note any integrity problems."""
    if record is None or row["doc_class"] == "zip":
        return
    ledger_path = str(record.get("rel_path") or "")
    if ledger_path and ledger_path != row["rel_path"]:
        row["_moved_from"] = ledger_path
        row["notes"].append(f"moved or renamed since fetch (ledger path: {ledger_path})")
    stale = stale_reasons(row, record)
    if stale:
        # The local copy is a superseded version: report it unpulled, keep its hash out of
        # the duplicate matching and say what moved so the pull can be repeated.
        row["_stale"] = "; ".join(stale)
        row["pulled"] = "n"
        row["sha256"] = ""
        row["notes"].append(f"{STALE_NOTE} ({row['_stale']}; local sha256 "
                            f"{str(record.get('sha256') or '')[:12]} superseded, re-pull needed)")
        return
    row["sha256"] = str(record.get("sha256") or "")
    row["pulled"] = "y"
    if not row["drive_md5"] and record.get("drive_md5"):
        row["drive_md5"] = str(record["drive_md5"])
    if record.get("size_match") is False:
        row["notes"].append("size mismatch against Drive")
    if record.get("md5_match") is False:
        row["notes"].append("md5 mismatch against Drive")
    transport = record.get("transport")
    if transport and transport not in ("connector", "rclone"):
        row["notes"].append(f"transport {transport}")
    local = record.get("local_path")
    if local and not (root_dir / str(local)).exists():
        row["notes"].append("local file missing")


def _apply_blocked(row: Dict[str, Any], plan: Dict[str, str], transport: str) -> None:
    """Mark unpulled files over the connector limit (per plan or per listing size).

    The size rule runs for every unpulled file, so a file that grew or appeared
    after the plan was written is still caught. On the rclone transport nothing
    is blocked; oversize files only get an informational note.
    """
    if row["doc_class"] == "zip" or row["pulled"] == "y":
        return
    if plan and row["drive_id"] not in plan:
        row["notes"].append("not in pull plan")
    plan_blocked = plan.get(row["drive_id"]) == PLAN_BLOCKED_STATUS
    too_big = row["bytes"] > CONNECTOR_LIMIT_BYTES
    if not (plan_blocked or too_big):
        return
    if transport == "connector":
        row["_blocked"] = True
        row["notes"].append(BLOCKED_NOTE)
    else:
        row["_over_limit"] = True
        row["notes"].append(OVER_LIMIT_NOTE)


def assign_duplicates(rows: List[Dict[str, Any]]) -> None:
    """Fill ``duplicate_of``: by sha256 when pulled, else by title+bytes(+md5) for unpulled files."""
    first_by_sha: Dict[str, Dict[str, Any]] = {}
    for row in rows:  # rows are sorted by rel_path
        if row["sha256"] and row["sha256"] not in first_by_sha:
            first_by_sha[row["sha256"]] = row
    for row in rows:
        if row["sha256"]:
            first = first_by_sha[row["sha256"]]
            if first is not row:
                row["duplicate_of"] = first["rel_path"]
    by_name_size: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["doc_class"] != "zip":
            by_name_size[(row["_title"].lower(), row["bytes"])].append(row)
    for row in rows:
        if row["sha256"] or row["duplicate_of"] or row["doc_class"] == "zip":
            continue
        for candidate in by_name_size[(row["_title"].lower(), row["bytes"])]:
            if candidate is row:
                break
            if row["drive_md5"] and candidate["drive_md5"] and row["drive_md5"] != candidate["drive_md5"]:
                continue
            row["duplicate_of"] = candidate["rel_path"]
            row["notes"].append(PROBABLE_DUP_NOTE)
            break


def build_rows(listing: Dict[str, Any], ledger: Dict[str, Dict[str, Any]], plan: Dict[str, str],
               root_dir: Path, transport: str = "connector") -> List[Dict[str, Any]]:
    """Assemble every manifest row (with private ``_`` helper keys) sorted by ``rel_path``."""
    rows = [_base_row(rec) for rec in listing["files"]]
    rows.sort(key=lambda r: (r["rel_path"], r["drive_id"]))
    id_counts = Counter(r["drive_id"] for r in rows if r["drive_id"])
    for row in rows:
        row["_id_count"] = id_counts.get(row["drive_id"], 1)
        if row["_id_count"] > 1:
            row["notes"].append(f"{DUPLICATE_ID_NOTE} ({row['_id_count']} entries)")
        record = ledger_lookup(ledger, row["drive_id"], row["rel_path"], unique_id=row["_id_count"] == 1)
        _apply_ledger(row, record, root_dir)
        _apply_blocked(row, plan, transport)
    assign_duplicates(rows)
    return rows


def public_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip helper keys and join notes, ready for the CSV writer."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        clean = {k: row[k] for k in MANIFEST_COLUMNS if k != "notes"}
        clean["notes"] = "; ".join(dict.fromkeys(row["notes"]))
        out.append(clean)
    return out


# --------------------------------------------------------------------------- #
# Skipped zips
# --------------------------------------------------------------------------- #
def zip_is_nested(rel_path: str) -> bool:
    """A zip is nested unless it sits directly under ``DD Sign-OFF`` or in a department folder."""
    segs = _segments(rel_path)
    if segs[:1] != [ROOT_DD]:
        return True
    if len(segs) == 2:
        return False
    return not (len(segs) == 3 and _is_department_folder(segs[1]))


def skipped_zip_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows for ``skipped_zip.csv`` from the manifest rows."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if row["doc_class"] != "zip":
            continue
        out.append({
            "rel_path": row["rel_path"],
            "bytes": row["bytes"],
            "parent_folder": "/".join(_segments(row["rel_path"])[:-1]),
            "drive_id": row["drive_id"],
            "nested": "y" if zip_is_nested(row["rel_path"]) else "n",
        })
    return out


# --------------------------------------------------------------------------- #
# Anomaly report sections
# --------------------------------------------------------------------------- #
def _section_counts(rows: List[Dict[str, Any]]) -> List[str]:
    """Counts by root, by department and by stage."""
    lines = ["## 1. Counts by root, department and stage", ""]
    by_root = Counter(r["root"] for r in rows)
    bytes_root = Counter()
    for r in rows:
        bytes_root[r["root"]] += r["bytes"]
    lines += _md_table(["root", "files", "bytes"], [(k, by_root[k], _fmt_bytes(bytes_root[k])) for k in sorted(by_root)])
    lines += ["", "### By department (DD Sign-OFF)", ""]
    dept_rows: Dict[Tuple[str, str, bool], Counter] = defaultdict(Counter)
    for r in rows:
        if r["root"] == ROOT_DD:
            key = (r["dept_no"] or "--", r["dept_name"] or "(no department folder)", r["_stray"])
            dept_rows[key]["files"] += 1
            dept_rows[key]["pulled"] += r["pulled"] == "y"
            dept_rows[key]["zips"] += r["doc_class"] == "zip"
            dept_rows[key][r["stage"] or "other"] += 1
    headers = ["dept_no", "dept_name", "stray", "files", "pulled", "zips", *STAGES, "other"]
    lines += _md_table(headers, [
        (no, name, "y" if stray else "", c["files"], c["pulled"], c["zips"], *(c[s] for s in STAGES), c["other"])
        for (no, name, stray), c in sorted(dept_rows.items())
    ])
    lines += ["", "### By stage (DD Sign-OFF)", ""]
    by_stage = Counter(r["stage"] or "blank" for r in rows if r["root"] == ROOT_DD)
    lines += _md_table(["stage", "files"], [(k, by_stage[k]) for k in sorted(by_stage)])
    return lines + [""]


def _section_departments(tree: FolderTree) -> List[str]:
    """Department folders found, numbering gaps, duplicates, extras and unnumbered folders."""
    lines = ["## 2. Department folders", ""]
    found: List[Tuple[str, str, str]] = []
    strays: List[Tuple[str, str]] = []
    unnumbered: List[str] = []
    for folder in tree.department_folders():
        title = str(folder.get("title", ""))
        match = _DEPT_FOLDER_RE.match(title.strip())
        if not match:
            unnumbered.append(title)
        elif title.startswith("__"):
            strays.append((title, str(folder.get("id", ""))))
        else:
            found.append((match.group(1), match.group(2).strip(), str(folder.get("id", ""))))
    numbers = Counter(no for no, _, _ in found)
    top = max([EXPECTED_DEPARTMENTS] + [int(no) for no in numbers])
    gaps = [f"{n:02d}" for n in range(1, EXPECTED_DEPARTMENTS + 1) if f"{n:02d}" not in numbers]
    extras = sorted(no for no in numbers if int(no) > EXPECTED_DEPARTMENTS or int(no) < 1)
    repeated = sorted(no for no, count in numbers.items() if count > 1)
    lines.append(f"Numbered department folders found: {len(found)} (expected {EXPECTED_DEPARTMENTS}; "
                 f"distinct numbers {len(numbers)}, highest {top:02d}).")
    lines.append(f"- Numbering gaps (01-{EXPECTED_DEPARTMENTS}): {', '.join(gaps) if gaps else 'none'}")
    lines.append(f"- Numbers above {EXPECTED_DEPARTMENTS}: {', '.join(extras) if extras else 'none'}")
    lines.append(f"- Numbers used by more than one folder: {', '.join(repeated) if repeated else 'none'}")
    lines.append(f"- Stray folders (leading '__'): {', '.join(t for t, _ in strays) if strays else 'none'}")
    lines.append(f"- Unnumbered folders under {ROOT_DD}: {', '.join(unnumbered) if unnumbered else 'none'}")
    lines.append("")
    lines += _md_table(["dept_no", "dept_name", "drive_id"], sorted(found))
    return lines + [""]


def _section_zips(zips: List[Dict[str, Any]]) -> List[str]:
    """Zip count, bytes and the nested ones."""
    lines = ["## 3. Zips (excluded from the pull)", ""]
    total = sum(int(z["bytes"]) for z in zips)
    nested = [z for z in zips if z["nested"] == "y"]
    lines.append(f"Zips in the listing: {len(zips)}, total {_fmt_bytes(total)}; nested: {len(nested)}.")
    lines.append("")
    lines += _md_table(["rel_path", "bytes", "parent_folder"], [(z["rel_path"], z["bytes"], z["parent_folder"]) for z in nested])
    return lines + [""]


def _section_zip_twins(rows: List[Dict[str, Any]]) -> List[str]:
    """Zips whose names differ only by ' (2)'."""
    lines = ["## 4. Duplicate zips named ' (2)'", ""]
    zips = [r for r in rows if r["doc_class"] == "zip"]
    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for z in zips:
        parent = "/".join(_segments(z["rel_path"])[:-1])
        by_key[(parent, z["_title"].lower())].append(z)
    table: List[Tuple[Any, ...]] = []
    for z in zips:
        match = _ZIP_TWO_RE.match(z["_title"])
        if not match:
            continue
        parent = "/".join(_segments(z["rel_path"])[:-1])
        originals = by_key.get((parent, (match.group(1) + ".zip").lower()), [])
        for original in originals:
            same = "same size" if original["bytes"] == z["bytes"] else "sizes differ"
            table.append((z["rel_path"], z["bytes"], original["rel_path"], original["bytes"], same))
        if not originals:
            table.append((z["rel_path"], z["bytes"], "(no original beside it)", "", ""))
    lines += _md_table(["zip ' (2)'", "bytes", "original", "bytes", "size check"], table)
    return lines + [""]


def _compare_groups(a: Dict[str, Dict[str, Any]], b: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Compare two ``{sub_path: row}`` groups; identical means equal sha256 when both pulled, else equal size."""
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    identical: List[str] = []
    different: List[str] = []
    for key in sorted(set(a) & set(b)):
        ra, rb = a[key], b[key]
        if ra["sha256"] and rb["sha256"]:
            (identical if ra["sha256"] == rb["sha256"] else different).append(f"{key} (sha256)")
        elif ra["drive_md5"] and rb["drive_md5"]:
            (identical if ra["drive_md5"] == rb["drive_md5"] else different).append(f"{key} (md5)")
        else:
            (identical if ra["bytes"] == rb["bytes"] else different).append(f"{key} (name+size)")
    return {"only in A": only_a, "only in B": only_b, "in both and identical": identical, "in both and different": different}


def _group_by_top_folder(rows: List[Dict[str, Any]], tree: FolderTree,
                         file_recs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{top_folder_id: {sub_path: row}}`` for DD files; falls back to the path when ids are unknown."""
    groups: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["root"] != ROOT_DD or row["doc_class"] == "zip":
            continue
        segs = _segments(row["rel_path"])
        if len(segs) < 3:
            continue
        top = tree.top_folder(file_recs.get(row["drive_id"], {}))
        key = str(top.get("id")) if top else "path:" + segs[1]
        groups[key]["/".join(segs[2:])] = row
    return groups


def _section_strays(rows: List[Dict[str, Any]], tree: FolderTree, listing: Dict[str, Any]) -> List[str]:
    """Compare each stray folder with the governing department folder, and the strays with each other."""
    cfg = (load_config().get("precedence", {}) or {}).get("stray_folders", {}) or {}
    governs = str(cfg.get("governs", "29 - Lab"))
    stray_names = {str(n) for n in cfg.get("compare_by_hash", ["__29 - Lab"])}
    lines = [f"## 5. Stray folders compared with '{governs}'", ""]
    file_recs = {str(f.get("id")): f for f in listing["files"]}
    groups = _group_by_top_folder(rows, tree, file_recs)
    folders = tree.department_folders()
    strays = [f for f in folders if str(f.get("title", "")) in stray_names]
    govern = [f for f in folders if str(f.get("title", "")) == governs]
    lines.append(f"Stray folders found: {len(strays)} ({', '.join(str(f.get('id')) for f in strays) or 'none'}); "
                 f"governing folder '{governs}': {'found' if govern else 'not found'}.")
    lines.append("")
    labelled: List[Tuple[str, Dict[str, Dict[str, Any]]]] = []
    for idx, folder in enumerate(strays):
        fid = str(folder.get("id"))
        labelled.append((f"{folder.get('title')} #{idx + 1} ({fid})", groups.get(fid) or groups.get("path:" + str(folder.get("title")), {})))
    govern_group = {}
    if govern:
        govern_group = groups.get(str(govern[0].get("id"))) or groups.get("path:" + governs, {})
    pairs = [(label, f"{governs} ({govern[0].get('id')})" if govern else governs, grp, govern_group) for label, grp in labelled]
    if len(labelled) >= 2:
        pairs.append((labelled[0][0], labelled[1][0], labelled[0][1], labelled[1][1]))
    for label_a, label_b, grp_a, grp_b in pairs:
        result = _compare_groups(grp_a, grp_b)
        lines.append(f"### A = {label_a}; B = {label_b}")
        lines.append("")
        lines += _md_table(["category", "count", "files"], [(k, len(v), "; ".join(v)) for k, v in result.items()])
        lines.append("")
    if not pairs:
        lines += ["No stray folders in this listing.", ""]
    return lines


def _section_aliases(rows: List[Dict[str, Any]]) -> List[str]:
    """Files whose leading number differs from the folder's department number."""
    lines = ["## 6. File names whose number differs from the folder", ""]
    table = [(r["dept_no"], r["dept_alias"], r["rel_path"]) for r in rows if r["dept_alias"]]
    lines += _md_table(["folder dept_no", "file number", "rel_path"], table)
    return lines + [""]


def _section_cross_stage(rows: List[Dict[str, Any]]) -> List[str]:
    """Hash-identical files that appear in more than one stage."""
    lines = ["## 7. Hash-identical duplicates across stages", ""]
    by_sha: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["sha256"] and r["root"] == ROOT_DD:
            by_sha[r["sha256"]].append(r)
    table: List[Tuple[Any, ...]] = []
    for sha, group in sorted(by_sha.items(), key=lambda kv: kv[1][0]["rel_path"]):
        stages = sorted({g["stage"] for g in group})
        if len(stages) > 1:
            table.append((sha[:12], ", ".join(stages), "; ".join(g["rel_path"] for g in group)))
    lines += _md_table(["sha256 (prefix)", "stages", "files"], table)
    return lines + [""]


def _section_blocked(rows: List[Dict[str, Any]], transport: str = "connector") -> List[str]:
    """Files the connector transport cannot fetch, by root and department."""
    lines = [f"## 8. Files blocked by the connector limit ({CONNECTOR_LIMIT_BYTES:,} bytes)", ""]
    blocked = [r for r in rows if r["_blocked"]]
    over = [r for r in rows if r["_over_limit"]]
    lines.append(f"Transport: {transport}. Blocked files: {len(blocked)}, "
                 f"total {_fmt_bytes(sum(r['bytes'] for r in blocked))}.")
    if transport != "connector":
        lines.append(f"rclone has no size limit: {len(over)} unpulled files over the connector limit "
                     f"({_fmt_bytes(sum(r['bytes'] for r in over))}) would be blocked only if the transport "
                     "reverted to the connector.")
    lines.append("")
    agg: Dict[Tuple[str, str, str], List[int]] = defaultdict(lambda: [0, 0])
    for r in blocked:
        cell = agg[(r["root"], r["dept_no"], r["dept_name"])]
        cell[0] += 1
        cell[1] += r["bytes"]
    lines += _md_table(["root", "dept_no", "dept_name", "files", "bytes"],
                       [(k[0], k[1], k[2], v[0], _fmt_bytes(v[1])) for k, v in sorted(agg.items())])
    return lines + [""]


def _section_unstable(unstable: Any) -> List[str]:
    """Departments whose two listings disagreed."""
    lines = ["## 9. Unstable departments (changed between listings)", ""]
    if unstable is None:
        return lines + ["state/unstable_departments.json not present; the lister has not run twice.", ""]
    if unstable == "unreadable":
        return lines + ["state/unstable_departments.json could not be parsed.", ""]
    entries: List[Tuple[str, str]] = []
    if isinstance(unstable, dict):
        inner = unstable.get("departments", unstable.get("unstable", unstable))
        if isinstance(inner, list):
            entries = [(str(x), "") for x in inner]
        elif isinstance(inner, dict):
            entries = [(str(k), json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v) for k, v in inner.items()]
    elif isinstance(unstable, list):
        entries = [(str(x) if isinstance(x, str) else json.dumps(x, ensure_ascii=False), "") for x in unstable]
    lines.append(f"Unstable departments: {len(entries)}.")
    lines.append("")
    lines += _md_table(["department", "detail"], entries)
    return lines + [""]


def _section_integrity(rows: List[Dict[str, Any]], plan: Dict[str, str]) -> List[str]:
    """Duplicate Drive ids, stale pulls, moved files and files the pull plan never saw."""
    lines = ["## 10. Listing integrity (duplicate ids, stale pulls, plan coverage)", ""]
    dup_ids = [r for r in rows if r["_id_count"] > 1]
    stale = [r for r in rows if r["_stale"]]
    moved = [r for r in rows if r["_moved_from"]]
    unplanned = [r for r in rows if plan and r["doc_class"] != "zip" and r["drive_id"] not in plan]
    lines.append(f"Drive ids listed more than once: {len({r['drive_id'] for r in dup_ids})} "
                 f"({len(dup_ids)} entries); stale pulls: {len(stale)}; moved or renamed since fetch: "
                 f"{len(moved)}; files absent from the pull plan: "
                 f"{len(unplanned) if plan else 'n/a (no plan)'}.")
    lines += ["", "### Duplicate Drive ids", ""]
    lines += _md_table(["drive_id", "rel_path", "bytes", "pulled"],
                       [(r["drive_id"], r["rel_path"], r["bytes"], r["pulled"]) for r in dup_ids])
    lines += ["", "### Stale pulls (Drive size or md5 moved since the fetch)", ""]
    lines += _md_table(["rel_path", "bytes", "reason"], [(r["rel_path"], r["bytes"], r["_stale"]) for r in stale])
    lines += ["", "### Moved or renamed since the fetch", ""]
    lines += _md_table(["rel_path", "ledger path"], [(r["rel_path"], r["_moved_from"]) for r in moved])
    lines += ["", "### Files absent from the pull plan", ""]
    lines += _md_table(["rel_path", "bytes", "pulled"], [(r["rel_path"], r["bytes"], r["pulled"]) for r in unplanned])
    return lines + [""]


def render_report(rows: List[Dict[str, Any]], zips: List[Dict[str, Any]], tree: FolderTree,
                  listing: Dict[str, Any], unstable: Any, transport: str = "connector",
                  plan: Optional[Dict[str, str]] = None) -> str:
    """Assemble ``inventory_anomalies.md``."""
    head = [
        "# Inventory anomalies",
        "",
        f"Listing: `{listing['path']}` taken at {listing['taken_at'] or 'unknown'}; "
        f"{len(rows)} files, {len(tree.by_id)} folders. Generated from state; re-run `python -m src.manifest build` to refresh.",
        "",
    ]
    body = (_section_counts(rows) + _section_departments(tree) + _section_zips(zips) + _section_zip_twins(rows)
            + _section_strays(rows, tree, listing) + _section_aliases(rows) + _section_cross_stage(rows)
            + _section_blocked(rows, transport) + _section_unstable(unstable) + _section_integrity(rows, plan or {}))
    return "\n".join(head + body).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Build / summary
# --------------------------------------------------------------------------- #
def build(state_dir: Path, registers_dir: Path, qa_dir: Path, root_dir: Path,
          listing_path: Optional[Path] = None, transport: Optional[str] = None) -> Dict[str, Any]:
    """Build manifest, skipped-zip register and anomaly report; return a small summary."""
    mode = configured_transport(transport)
    listing = load_listing(resolve_listing_path(state_dir, listing_path))
    tree = FolderTree(listing["folders"])
    plan = load_plan(state_dir)
    rows = build_rows(listing, load_ledger(state_dir), plan, root_dir, mode)
    zips = skipped_zip_rows(rows)
    manifest_path = write_csv_dicts(registers_dir / "manifest.csv", public_rows(rows), MANIFEST_COLUMNS)
    write_csv_dicts(registers_dir / "skipped_zip.csv", zips, SKIPPED_ZIP_COLUMNS)
    report_path = atomic_write_text(qa_dir / "inventory_anomalies.md",
                                    render_report(rows, zips, tree, listing, load_unstable(state_dir), mode, plan))
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    Progress(state_dir / "progress.json").set("manifest", "done", hash=digest, listing=listing["path"],
                                              files=len(rows), zips=len(zips), transport=mode)
    return {"files": len(rows), "zips": len(zips), "pulled": sum(r["pulled"] == "y" for r in rows),
            "duplicates": sum(bool(r["duplicate_of"]) for r in rows), "blocked": sum(r["_blocked"] for r in rows),
            "stale": sum(bool(r["_stale"]) for r in rows), "transport": mode,
            "manifest": str(manifest_path), "report": str(report_path)}


def summary(registers_dir: Path) -> Dict[str, Any]:
    """Counts from an existing ``manifest.csv`` (an empty manifest is valid and yields zeros)."""
    path = registers_dir / "manifest.csv"
    if not path.is_file():
        raise FileNotFoundError(f"no manifest at {path}; run 'build' first")
    rows = read_csv_dicts(path)
    return {
        "files": len(rows),
        "by_root": dict(sorted(Counter(r["root"] for r in rows).items())),
        "by_stage": dict(sorted(Counter(r["stage"] or "blank" for r in rows).items())),
        "by_doc_class": dict(sorted(Counter(r["doc_class"] for r in rows).items())),
        "pulled": sum(r["pulled"] == "y" for r in rows),
        "duplicates": sum(bool(r["duplicate_of"]) for r in rows),
        "zips": sum(r["doc_class"] == "zip" for r in rows),
        "departments": len({r["dept_no"] for r in rows if r["dept_no"]}),
    }


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Argument parser; ``build`` is the default subcommand."""
    parser = argparse.ArgumentParser(description="Build the manifest, skipped-zip register and inventory anomaly report.")
    parser.add_argument("--state-dir", type=Path, default=PATHS["state"], help="state directory (default: state/)")
    parser.add_argument("--registers-dir", type=Path, default=PATHS["registers"], help="out/registers directory")
    parser.add_argument("--qa-dir", type=Path, default=PATHS["qa"], help="out/qa directory")
    parser.add_argument("--project-root", type=Path, default=None, help="root that ledger local_path values are relative to")
    parser.add_argument("--listing", type=Path, default=None, help="explicit consolidated listing JSON")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("build", help="build the registers (default)")
    sub.add_parser("summary", help="print counts from the existing manifest")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    command = args.command or "build"
    root_dir = args.project_root if args.project_root is not None else project_root()
    try:
        if command == "build":
            result = build(args.state_dir, args.registers_dir, args.qa_dir, root_dir, args.listing)
        else:
            result = summary(args.registers_dir)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
