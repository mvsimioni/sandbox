"""Adversarial tests for ``src.sidecar_misc`` (independent review).

Every fixture is synthetic (``email`` standard library, Pillow, extract_msg's
``OleWriter``); no file under ``data/source`` is read.  Tests that document a
confirmed defect are marked ``xfail(strict=True)`` with the issue id from
``out/qa/verify/code_sidecar_misc_review.md``; once the code is fixed the
marker must be removed.
"""

from __future__ import annotations

import io
import json
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List

import pytest
from PIL import ExifTags, Image

from src import recall_check
from src import sidecar_misc as sm
from tests.test_sidecar_misc import _zip_bytes, build_jpeg_with_exif

UNICODE_NAME = "RE_ IGH [Batch 3B] (Rev A) éè 日本 – AH comments.eml"


def _json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _plain_eml(path: Path, body: str = "Plain body, 1,250 L/s and +15 Pa.\n", **headers: str) -> Path:
    """A single-part text/plain EML with the given headers."""
    msg = EmailMessage()
    for key, value in {"From": "Alice <alice@example.com>", "To": "bob@example.com", "Subject": "no attachments",
                       "Date": "Tue, 03 Mar 2026 09:00:00 +0800", "Message-ID": "<plain-1@example.com>",
                       **headers}.items():
        msg[key] = value
    msg.set_content(body)
    path.write_bytes(msg.as_bytes())
    return path


def _ledger(state: Path, rows: List[Dict[str, Any]], name: str = "pull_ledger.jsonl") -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #
def test_eml_without_attachments_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An EML with no attachments: zero rows, '(no attachments)' in body.md, every check passes, exit 0."""
    eml = _plain_eml(tmp_path / "plain.eml")
    out = tmp_path / "out"
    assert sm.main(["run", "--path", str(eml), "--out", str(out), "--state-dir", str(tmp_path), "--no-progress"]) == 0
    assert "done:" in capsys.readouterr().out
    record = _json(out / "message.json")
    assert record["attachment_count"] == 0 and record["zip_attachment_count"] == 0 and record["attachments"] == []
    assert record["headers"]["date_iso"] == "2026-03-03T09:00:00+08:00"
    assert record["body"]["numeric_tokens"] == 2
    body = (out / "body.md").read_text(encoding="utf-8")
    assert "(no attachments)" in body and "Plain body, 1,250 L/s and +15 Pa." in body
    manifest = _json(out / "sidecar_manifest.json")
    assert manifest["counts"] == {"attachments": 0, "zip_attachments": 0, "body_chars": record["body"]["chars"],
                                  "body_numeric_tokens": 2}
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    assert sorted(p.name for p in out.iterdir()) == ["body.md", "message.json", "sidecar_manifest.json"]


def test_unicode_name_with_spaces_and_brackets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    """A unicode file name with spaces, brackets and a dash resolves through the ledger to the default out dir."""
    src = _plain_eml(tmp_path / UNICODE_NAME, Subject="Élévation [AHU-07] (Rev A)")
    rel = f"DD Sign-OFF/30 - Pharmacy/AH sign off with comments/{UNICODE_NAME}"
    state = tmp_path / "state"
    _ledger(state, [{"rel_path": rel, "local_path": str(src), "sha256": sm.sha256_file(src), "drive_id": "d-uni",
                     "drive_modified": "2026-03-03T01:00:00Z", "transport": "connector"}])
    monkeypatch.setitem(sm.PATHS, "sidecar", tmp_path / "sidecar")
    progress = tmp_path / "progress.json"
    args = ["run", "--path", str(src), "--state-dir", str(state), "--progress", str(progress)]
    assert sm.main(args) == 0
    expected = tmp_path / "sidecar" / rel
    assert (expected / "message.json").is_file() and (expected / "body.md").is_file()
    manifest = _json(expected / "sidecar_manifest.json")
    assert manifest["rel_path"] == rel and manifest["drive_id"] == "d-uni"
    assert _json(expected / "message.json")["headers"]["subject"] == "Élévation [AHU-07] (Rev A)"
    assert (expected / "body.md").read_text(encoding="utf-8").startswith(f"# {UNICODE_NAME}\n")
    capsys.readouterr()
    assert sm.main(args) == 0 and "skip (done)" in capsys.readouterr().out
    item = _json(progress)["items"][f"sidecar_misc:{manifest['sha256']}"]
    assert item["status"] == "done" and item["out_dir"] == str(expected)


def test_zip_flag_by_type_by_name_and_unnamed_part(tmp_path: Path) -> None:
    """Zips are flagged by extension alone (octet-stream) and by content type alone; a nameless part does not crash."""
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["Subject"] = "three attachments"
    msg.set_content("body 45 Pa\n")
    msg.add_attachment(_zip_bytes(), maintype="application", subtype="octet-stream", filename="PLANS.ZIP")
    msg.add_attachment(_zip_bytes(), maintype="application", subtype="zip", filename="data.bin")
    msg.add_attachment(b"abc", maintype="application", subtype="octet-stream")
    path = tmp_path / "three.eml"
    path.write_bytes(msg.as_bytes())
    manifest = sm.run_one(path, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "message.json")
    flags = [(a["name"], a["is_zip"]) for a in record["attachments"]]
    assert flags == [("PLANS.ZIP", True), ("data.bin", True), (None, False)]
    assert record["zip_attachments"] == ["PLANS.ZIP", "data.bin"] and record["zip_attachment_count"] == 2
    assert manifest["counts"]["attachments"] == 3 and all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    body = (tmp_path / "out" / "body.md").read_text(encoding="utf-8")
    assert "| 1 | PLANS.ZIP |" in body and "| 3 |  | 3 |" in body
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["body.md", "message.json", "sidecar_manifest.json"]


def test_unknown_charset_body_does_not_abort_run(tmp_path: Path) -> None:
    """A body declared in a charset Python lacks (common: iso-8859-8-i) must be recorded, not abort the run."""
    raw = (b"From: a@example.com\r\nSubject: odd charset\r\nContent-Type: text/plain; charset=\"iso-8859-8-i\"\r\n"
           b"Content-Transfer-Encoding: 8bit\r\n\r\nbody 45 Pa \xe9\r\n")
    path = tmp_path / "charset.eml"
    path.write_bytes(raw)
    manifest = sm.run_one(path, tmp_path / "out", tmp_path)
    assert manifest["counts"]["body_numeric_tokens"] == 1
    assert "45" in (tmp_path / "out" / "body.md").read_text(encoding="utf-8")


def test_single_part_zip_attachment_is_listed_and_flagged(tmp_path: Path) -> None:
    """A non-multipart message whose only part is a zip with Content-Disposition: attachment."""
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["Subject"] = "bare zip"
    msg.set_content(_zip_bytes(), maintype="application", subtype="zip", filename="bare.zip")
    path = tmp_path / "bare.eml"
    path.write_bytes(msg.as_bytes())
    assert "Content-Disposition: attachment" in path.read_text(encoding="utf-8")
    sm.run_one(path, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "message.json")
    assert record["attachment_count"] == 1 and record["zip_attachments"] == ["bare.zip"]


def test_ole_bytes_in_eml_are_reported(tmp_path: Path) -> None:
    """An Outlook MSG mis-named .eml (OLE magic) must leave a warning or a failed check behind."""
    path = tmp_path / "really_a_msg.eml"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(256)) * 8)
    manifest = sm.run_one(path, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "message.json")
    assert not any(record["headers"][k] for k in ("from", "to", "subject", "date", "message_id"))
    assert manifest["warnings"] or not all(c["pass"] for c in manifest["checks"])


def test_forwarded_message_with_nested_zip(tmp_path: Path) -> None:
    """Documents I-08: a message/rfc822 attachment is one row; the zip inside it is neither listed nor flagged."""
    inner = EmailMessage()
    inner["From"] = "x@example.com"
    inner["Subject"] = "inner"
    inner.set_content("inner body")
    inner.add_attachment(_zip_bytes(), maintype="application", subtype="zip", filename="hidden.zip")
    outer = EmailMessage()
    outer["From"] = "y@example.com"
    outer["Subject"] = "FW: inner"
    outer.set_content("see forwarded 12 kW")
    outer.add_attachment(inner, filename="forwarded.eml")
    path = tmp_path / "fwd.eml"
    path.write_bytes(outer.as_bytes())
    manifest = sm.run_one(path, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "message.json")
    assert [(a["content_type"], a["is_zip"], a["size_method"]) for a in record["attachments"]] == \
        [("message/rfc822", False, "serialised part (nested message)")]
    assert manifest["counts"]["zip_attachments"] == 0 and all(c["pass"] for c in manifest["checks"])
    assert "hidden.zip" not in json.dumps(record)  # the nested zip is invisible to the register


@pytest.mark.xfail(strict=True, reason="I-06: recall_check.email_body splits body.md on '## Attachments', so a body quoting that line is truncated")
def test_body_marker_collision_keeps_whole_body(tmp_path: Path) -> None:
    body = "line one\n## Attachments\nafter marker 77 Pa\n"
    path = _plain_eml(tmp_path / "marker.eml", body=body)
    sm.run_one(path, tmp_path / "out", tmp_path)
    assert "after marker 77 Pa" in recall_check.email_body(tmp_path / "out")


def test_extract_msg_path_never_opens_attachment_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy on olefile: neither MSG path reads the attachment data stream (3701) or the embedded-message storage."""
    pytest.importorskip("extract_msg")
    import olefile

    from tests.test_sidecar_misc import build_msg

    opened: List[str] = []
    original = olefile.OleFileIO.openstream

    def spy(self: Any, name: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(name if isinstance(name, str) else "/".join(name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(olefile.OleFileIO, "openstream", spy)
    msg = tmp_path / "fx.msg"
    build_msg(msg)
    via_extract = sm.parse_msg_extract_msg(msg)
    assert via_extract["attachments"][0]["is_zip"] and via_extract["attachments"][0]["opened"] is False
    assert opened and not any("3701" in n for n in opened), opened
    opened.clear()
    sm.parse_msg_olefile(msg)
    assert opened and not any("3701" in n for n in opened), opened
    manifest = sm.run_one(msg, tmp_path / "out", tmp_path)
    assert not any("3701" in n for n in opened), opened
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["body.md", "message.json", "sidecar_manifest.json"]
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def _jpeg_with_orientation(path: Path, orientation: int, size=(2000, 1200), gps: bool = True) -> None:
    img = Image.new("RGB", size, (30, 60, 90))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    exif[ExifTags.Base.Make] = "RotCam"
    if gps:
        ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        ifd[ExifTags.GPS.GPSLatitudeRef] = "S"
        ifd[ExifTags.GPS.GPSLatitude] = (33.0, 51.0, 54.0)
        ifd[ExifTags.GPS.GPSLongitudeRef] = "W"
        ifd[ExifTags.GPS.GPSLongitude] = (151.0, 12.0, 36.0)
    img.save(path, format="JPEG", exif=exif.tobytes())


def test_rotated_jpeg_preview_transposed_and_no_exif_leak(tmp_path: Path) -> None:
    """Orientation 6 (rotated page): preview is portrait 941x1568, GPS flagged, and the preview carries no EXIF."""
    jpg = tmp_path / "rotated (site) [AHU-07].jpg"
    _jpeg_with_orientation(jpg, 6)
    manifest = sm.run_one(jpg, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "image.json")
    assert (record["width"], record["height"]) == (2000, 1200) and record["exif"]["orientation"] == 6
    assert record["preview"]["orientation_applied"] is True
    assert (record["preview"]["width"], record["preview"]["height"]) == (941, 1568)
    assert record["gps"]["personal_data"] is True
    assert record["gps"]["latitude"] == pytest.approx(-33.865, abs=1e-6)
    assert record["gps"]["longitude"] == pytest.approx(-151.21, abs=1e-6)
    with Image.open(tmp_path / "out" / "preview.jpg") as preview:
        assert preview.size == (941, 1568) and preview.format == "JPEG"
        assert dict(preview.getexif()) == {} and dict(preview.getexif().get_ifd(ExifTags.IFD.GPSInfo)) == {}
    raw = (tmp_path / "out" / "preview.jpg").read_bytes()
    assert b"RotCam" not in raw and b"Exif" not in raw
    safe = sm.markdown_fields(record)
    assert "latitude" not in json.dumps(safe) and "longitude" not in json.dumps(safe)
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]


def test_deep_and_cmyk_images_get_jpeg_previews(tmp_path: Path) -> None:
    """16-bit greyscale PNG and CMYK JPEG previews open as JPEG without upscaling; phash present."""
    deep = tmp_path / "deep.png"
    Image.new("I;16", (400, 300)).save(deep)
    cmyk = tmp_path / "print.jpg"
    Image.new("CMYK", (1800, 2400), (0, 128, 128, 0)).save(cmyk, format="JPEG")
    for src, expected in ((deep, (400, 300)), (cmyk, (1176, 1568))):
        out = tmp_path / ("out_" + src.stem)
        manifest = sm.run_one(src, out, tmp_path)
        record = _json(out / "image.json")
        assert record["opened"] and record["error"] is None and len(record["phash"]) == 16
        with Image.open(out / "preview.jpg") as preview:
            assert preview.size == expected and preview.format == "JPEG" and preview.mode in ("RGB", "L")
        assert all(c["pass"] for c in manifest["checks"]), (src.name, manifest["checks"])


def test_heic_without_plugin_records_gap(tmp_path: Path) -> None:
    """A .heic that Pillow cannot open: gap recorded, exit code 0, the 'opened' check fails, no preview."""
    try:
        import pillow_heif  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("pillow_heif installed: HEIC would open")
    heic = tmp_path / "IMG_0001.HEIC"
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buffer, format="PNG")
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + buffer.getvalue())
    out = tmp_path / "out"
    assert sm.main(["run", "--path", str(heic), "--out", str(out), "--state-dir", str(tmp_path), "--no-progress"]) == 0
    record = _json(out / "image.json")
    assert record["opened"] is False and record["preview"] is None and "HEIF plugin" in record["error"]
    manifest = _json(out / "sidecar_manifest.json")
    assert manifest["files"] == ["image.json"] and manifest["warnings"] == [record["error"]]
    assert not (out / "preview.jpg").exists()


@pytest.mark.xfail(strict=True, reason="I-04: PIL.Image.DecompressionBombError is not caught in image_record, so a huge image aborts the run")
def test_decompression_bomb_is_recorded_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jpg = tmp_path / "huge.jpg"
    build_jpeg_with_exif(jpg)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    manifest = sm.run_one(jpg, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "image.json")
    assert record["opened"] is False and record["error"] and manifest["files"] == ["image.json"]


@pytest.mark.xfail(strict=True, reason="I-05: a source changed in place reuses the out dir and leaves the previous version's preview.jpg behind")
def test_source_changed_in_place_removes_stale_files(tmp_path: Path) -> None:
    jpg = tmp_path / "photo.jpg"
    build_jpeg_with_exif(jpg)
    out = tmp_path / "out"
    sm.run_one(jpg, out, tmp_path)
    assert (out / "preview.jpg").is_file()
    jpg.write_bytes(b"not an image any more")
    manifest = sm.run_one(jpg, out, tmp_path)
    assert manifest["files"] == ["image.json"]
    assert sorted(p.name for p in out.iterdir()) == ["image.json", "sidecar_manifest.json"]


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason="I-07: the local_path fallback resolves a project-relative ledger path against the CWD, not project_root()")
def test_ledger_path_fallback_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _plain_eml(tmp_path / "rel" / "note.eml") if (tmp_path / "rel").mkdir() is None else None
    assert src is not None
    state = tmp_path / "state"
    _ledger(state, [{"rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/note.eml", "local_path": "rel/note.eml",
                     "sha256": "0" * 64, "drive_id": "d-stale"}])
    monkeypatch.chdir(tmp_path)
    assert sm.ledger_entry(sm.sha256_file(src), src, state) is not None  # matches from the ledger's own base dir
    monkeypatch.chdir(tmp_path / "rel")
    assert sm.ledger_entry(sm.sha256_file(src), src, state) is not None  # must not depend on the caller's CWD
