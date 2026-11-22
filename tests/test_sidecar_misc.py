"""Tests for ``src.sidecar_misc`` on small synthetic fixtures.

Fixtures are generated here: an EML with a text/HTML alternative body and two
attachments (a PDF-like blob and a real zip), an MSG written with
``extract_msg.OleWriter`` (skipped when extract_msg is missing), a JPEG with
EXIF camera, date and GPS fields written by Pillow, a plain PNG and a text
file.  None of the real source files is used.
"""

from __future__ import annotations

import io
import json
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

import pytest
from PIL import ExifTags, Image

from src import sidecar_misc as sm

ZIP_MEMBER_TEXT = b"secret drawing register inside the zip"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _zip_bytes() -> bytes:
    """A real, valid zip with one member."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("register.csv", ZIP_MEMBER_TEXT)
    return buffer.getvalue()


def build_eml(path: Path) -> Dict[str, Any]:
    """An EML with plain and HTML bodies plus two attachments (one zip)."""
    msg = EmailMessage()
    msg["From"] = "Alice Reviewer <alice@example.com>"
    msg["To"] = "Bob Builder <bob@example.com>"
    msg["Cc"] = "carol@example.com"
    msg["Date"] = "Mon, 02 Mar 2026 10:15:00 +0800"
    msg["Subject"] = "RE: Pharmacy RDS batch 3B, 1,250 L/s"
    msg["Message-ID"] = "<msg-0001@example.com>"
    msg.set_content("Please see the attached.\nRoom pressure +15 Pa | confirm.\n")
    msg.add_alternative("<html><body><p>Please see the attached.</p><p>Room pressure +15 Pa</p></body></html>",
                        subtype="html")
    pdf_blob = b"%PDF-1.4 not really a pdf " + b"x" * 1001
    msg.add_attachment(pdf_blob, maintype="application", subtype="pdf", filename="RDS comments.pdf")
    zip_blob = _zip_bytes()
    msg.add_attachment(zip_blob, maintype="application", subtype="zip", filename="Drawings batch 3B.zip")
    path.write_bytes(msg.as_bytes())
    return {"pdf_size": len(pdf_blob), "zip_size": len(zip_blob), "zip_blob": zip_blob}


def build_msg(path: Path) -> None:
    """A minimal Outlook MSG written with extract_msg's OleWriter (one zip attachment, one recipient)."""
    import struct

    from extract_msg import OleWriter

    writer = OleWriter()

    def string_prop(tag: str, text: str, prefix: str = "") -> None:
        name = f"__substg1.0_{tag}001F"
        writer.addEntry(f"{prefix}/{name}" if prefix else name, text.encode("utf-16-le"))

    def prop(tag: int, typ: int, value: bytes) -> bytes:
        return struct.pack("<HHI", typ, tag, 6) + value

    string_prop("0037", "FW: DD Booklet sign off comments 45 Pa")
    string_prop("0C1A", "Alice Reviewer")
    string_prop("0C1F", "alice@example.com")
    string_prop("0E04", "Bob Builder")
    string_prop("0E03", "carol@example.com")
    string_prop("1000", "Body line one.\r\nBody line two with 1,250 L/s.")
    string_prop("1035", "<msg-0002@example.com>")
    string_prop("001A", "IPM.Note")
    filetime = (1772446500 + 11644473600) * 10_000_000  # 2026-03-02T10:15:00Z
    header = b"\x00" * 8 + struct.pack("<IIII", 1, 1, 1, 1) + b"\x00" * 8
    writer.addEntry("__properties_version1.0", header + prop(0x0039, 0x0040, struct.pack("<Q", filetime)))
    writer.addEntry("__nameid_version1.0", storage=True)
    for n in ("0002", "0003", "0004"):
        writer.addEntry(f"__nameid_version1.0/__substg1.0_{n}0102", b"")
    att = "__attach_version1.0_#00000000"
    writer.addEntry(att, storage=True)
    string_prop("3707", "Drawings batch 3B.zip", att)
    string_prop("3704", "DRAWIN~1.ZIP", att)
    string_prop("370E", "application/zip", att)
    string_prop("3703", ".zip", att)
    writer.addEntry(f"{att}/__substg1.0_37010102", _zip_bytes())
    writer.addEntry(f"{att}/__properties_version1.0", b"\x00" * 8 + prop(0x3705, 0x0003, struct.pack("<II", 1, 0)))
    rcp = "__recip_version1.0_#00000000"
    writer.addEntry(rcp, storage=True)
    string_prop("3001", "Bob Builder", rcp)
    string_prop("39FE", "bob@example.com", rcp)
    writer.addEntry(f"{rcp}/__properties_version1.0", b"\x00" * 8 + prop(0x0C15, 0x0003, struct.pack("<II", 1, 0)))
    writer.write(str(path))


def build_jpeg_with_exif(path: Path, size=(2000, 1200)) -> None:
    """A JPEG with camera, date and GPS EXIF written by Pillow."""
    img = Image.new("RGB", size, (40, 90, 160))
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "TestCam"
    exif[ExifTags.Base.Model] = "Model X"
    exif[ExifTags.Base.DateTime] = "2026:03:01 09:30:00"
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:03:01 09:30:00"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (1.0, 17.0, 30.0)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E"
    gps[ExifTags.GPS.GPSLongitude] = (103.0, 51.0, 0.0)
    gps[ExifTags.GPS.GPSAltitudeRef] = 0
    gps[ExifTags.GPS.GPSAltitude] = 12.5
    img.save(path, format="JPEG", quality=80, exif=exif.tobytes())


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """Build every fixture once and run the module on each."""
    base = tmp_path_factory.mktemp("sidecar_misc")
    state = base / "state"
    state.mkdir()
    eml = base / "RE_ batch 3B.eml"
    eml_info = build_eml(eml)
    jpg = base / "site photo.jpg"
    build_jpeg_with_exif(jpg)
    png = base / "small.png"
    Image.new("RGBA", (300, 200), (255, 0, 0, 128)).save(png, format="PNG")
    other = base / "notes.txt"
    other.write_text("plain notes 12 kW\n", encoding="utf-8")
    msg = base / "FW_ booklet.msg"
    try:
        build_msg(msg)
    except ImportError:
        msg = None  # type: ignore[assignment]
    rows = [{"rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/RE_ batch 3B.eml", "local_path": str(eml),
             "sha256": sm.sha256_file(eml), "drive_id": "drive-eml", "drive_modified": "2026-03-02T02:15:00Z",
             "transport": "connector"},
            {"rel_path": "DD Sign-OFF/30 - Pharmacy/Submitted/site photo.jpg", "local_path": str(jpg),
             "sha256": sm.sha256_file(jpg), "drive_id": "drive-jpg", "transport": "upload"}]
    (state / "pull_ledger.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    (state / "upload_ledger.jsonl").write_text(json.dumps(rows[1]) + "\n", encoding="utf-8")
    progress = base / "progress.json"
    outs: Dict[str, Path] = {}
    for name, src in (("eml", eml), ("jpg", jpg), ("png", png), ("other", other), ("msg", msg)):
        if src is None:
            continue
        out = base / "out" / name
        rc = sm.main(["run", "--path", str(src), "--out", str(out), "--state-dir", str(state),
                      "--progress", str(progress)])
        assert rc == 0, name
        outs[name] = out
    return {"base": base, "state": state, "progress": progress, "eml": eml, "jpg": jpg, "png": png,
            "other": other, "msg": msg, "outs": outs, **eml_info}


def _json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_helpers() -> None:
    assert sm.file_kind(Path("a/b.MSG")) == ("email", "msg")
    assert sm.file_kind(Path("x.JPEG")) == ("image", "jpeg")
    assert sm.file_kind(Path("x.heic")) == ("image", "heic")
    assert sm.file_kind(Path("x.docx")) == ("other", "docx")
    assert sm.is_zip_attachment("Plans.ZIP", None) and sm.is_zip_attachment("blob", "application/x-zip-compressed")
    assert not sm.is_zip_attachment("report.pdf", "application/pdf")
    assert sm.preview_size(2000, 1200) == (1568, 941)
    assert sm.preview_size(1200, 2000) == (941, 1568)
    assert sm.preview_size(300, 200) == (300, 200)
    assert sm.exif_date_to_iso("2026:03:01 09:30:00") == "2026-03-01T09:30:00"
    assert sm.exif_date_to_iso("unknown") is None
    assert sm.html_to_text("<p>One &amp; two</p><style>x{}</style><div>Three<br>Four</div>") == "One & two\n\nThree\nFour"
    assert sm._base64_decoded_size("aGVsbG8=\r\n") == 5
    assert sm.numeric_tokens("1,250 L/s +15 Pa") == ["1250", "+15"]


# --------------------------------------------------------------------------- #
# EML
# --------------------------------------------------------------------------- #
def test_eml_message_json(fixtures: Dict[str, Any]) -> None:
    record = _json(fixtures["outs"]["eml"] / "message.json")
    assert record["kind"] == "email" and record["format"] == "eml"
    headers = record["headers"]
    assert headers["from"] == "Alice Reviewer <alice@example.com>"
    assert headers["to"] == "Bob Builder <bob@example.com>" and headers["cc"] == "carol@example.com"
    assert headers["date"] == "Mon, 02 Mar 2026 10:15:00 +0800" and headers["date_iso"] == "2026-03-02T10:15:00+08:00"
    assert headers["subject"] == "RE: Pharmacy RDS batch 3B, 1,250 L/s" and headers["message_id"] == "<msg-0001@example.com>"
    assert record["body"]["source"] == "text/plain" and record["body"]["numeric_tokens"] == 1
    assert record["attachment_count"] == 2 and record["zip_attachment_count"] == 1
    assert record["zip_attachments"] == ["Drawings batch 3B.zip"]
    assert record["attachments_opened"] is False
    by_name = {a["name"]: a for a in record["attachments"]}
    pdf, zipped = by_name["RDS comments.pdf"], by_name["Drawings batch 3B.zip"]
    assert pdf["size_bytes"] == fixtures["pdf_size"] and pdf["content_type"] == "application/pdf" and not pdf["is_zip"]
    assert zipped["size_bytes"] == fixtures["zip_size"] and zipped["content_type"] == "application/zip"
    assert zipped["is_zip"] is True and zipped["opened"] is False
    assert pdf["size_method"] == "base64 length arithmetic"


def test_eml_body_markdown(fixtures: Dict[str, Any]) -> None:
    text = (fixtures["outs"]["eml"] / "body.md").read_text(encoding="utf-8")
    assert text.startswith("# RE_ batch 3B.eml\n")
    assert "## Headers" in text and "| subject | RE: Pharmacy RDS batch 3B, 1,250 L/s |" in text
    assert "## Body\n\nPlease see the attached.\nRoom pressure +15 Pa | confirm." in text  # body verbatim
    assert "| 2 | Drawings batch 3B.zip | " in text and "| yes |" in text
    assert "none was extracted or opened" in text
    assert ZIP_MEMBER_TEXT.decode() not in text


def test_zip_attachment_is_not_opened(fixtures: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zip handling is a flag only: no zip is opened and no attachment lands on disk."""
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("zipfile.ZipFile must not be called by sidecar_misc")

    monkeypatch.setattr(zipfile, "ZipFile", boom)
    out = tmp_path / "eml_again"
    manifest = sm.run_one(fixtures["eml"], out, fixtures["state"])
    written = sorted(p.name for p in out.iterdir())
    assert written == ["body.md", "message.json", "sidecar_manifest.json"]
    assert fixtures["zip_blob"] not in (out / "body.md").read_bytes()
    for name in written:
        assert ZIP_MEMBER_TEXT not in (out / name).read_bytes()
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    assert manifest["counts"] == {"attachments": 2, "zip_attachments": 1, "body_chars": manifest["counts"]["body_chars"],
                                  "body_numeric_tokens": 1}


def test_eml_manifest_and_progress(fixtures: Dict[str, Any]) -> None:
    manifest = _json(fixtures["outs"]["eml"] / "sidecar_manifest.json")
    assert manifest["module"] == "sidecar_misc" and manifest["kind"] == "email"
    assert manifest["sha256"] == sm.sha256_file(fixtures["eml"])
    assert manifest["rel_path"].endswith("RE_ batch 3B.eml") and manifest["drive_id"] == "drive-eml"
    assert manifest["drive_modified"] == "2026-03-02T02:15:00Z" and manifest["ledger_transport"] == "connector"
    assert manifest["methods_used"] == ["email-stdlib"] and manifest["converters"][0]["name"].startswith("email")
    assert manifest["files"] == ["message.json", "body.md"]
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    progress = _json(fixtures["progress"])
    item = progress["items"][f"sidecar_misc:{manifest['sha256']}"]
    assert item["status"] == "done" and item["hash"] == manifest["sha256"]


def test_eml_html_only_body(tmp_path: Path) -> None:
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["Subject"] = "html only"
    msg.set_content("<html><body><p>Only <b>HTML</b> here, 45 Pa.</p></body></html>", subtype="html")
    path = tmp_path / "h.eml"
    path.write_bytes(msg.as_bytes())
    parsed = sm.parse_eml(path)
    assert parsed["body_text"] == "Only HTML here, 45 Pa." and parsed["body_source"] == "text/html (tags stripped)"
    assert parsed["attachments"] == [] and parsed["headers"]["date_iso"] is None


# --------------------------------------------------------------------------- #
# MSG
# --------------------------------------------------------------------------- #
def _require_msg(fixtures: Dict[str, Any]) -> Path:
    if fixtures["msg"] is None:
        pytest.skip("extract_msg not installed: no OleWriter to build the MSG fixture")
    return fixtures["msg"]


def test_msg_via_extract_msg(fixtures: Dict[str, Any]) -> None:
    _require_msg(fixtures)
    record = _json(fixtures["outs"]["msg"] / "message.json")
    assert record["format"] == "msg" and record["parser"].startswith("extract-msg")
    headers = record["headers"]
    assert headers["subject"] == "FW: DD Booklet sign off comments 45 Pa"
    assert headers["from"] == "Alice Reviewer" and headers["to"].startswith("Bob Builder")
    assert headers["message_id"] == "<msg-0002@example.com>"
    assert headers["date_iso"].startswith("2026-03-02T10:15:00")
    assert record["body"]["numeric_tokens"] == 1
    assert record["attachment_count"] == 1 and record["zip_attachment_count"] == 1
    att = record["attachments"][0]
    assert att["name"] == "Drawings batch 3B.zip" and att["is_zip"] and att["opened"] is False
    assert att["size_bytes"] == len(_zip_bytes()) and att["size_method"] == "OLE directory entry size"
    assert att["content_type"] == "application/zip" and att["attach_method"] == "by value"
    manifest = _json(fixtures["outs"]["msg"] / "sidecar_manifest.json")
    assert manifest["methods_used"] == ["extract-msg", "olefile"]
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]
    body = (fixtures["outs"]["msg"] / "body.md").read_text(encoding="utf-8")
    assert "Body line two with 1,250 L/s." in body and ZIP_MEMBER_TEXT.decode() not in body


def test_msg_olefile_fallback_matches(fixtures: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The olefile reader gives the same headers and attachment list, and is used when extract_msg is missing."""
    path = _require_msg(fixtures)
    direct = sm.parse_msg_olefile(path)
    assert direct["headers"]["subject"] == "FW: DD Booklet sign off comments 45 Pa"
    assert direct["headers"]["from"] == "Alice Reviewer <alice@example.com>"
    assert direct["headers"]["to"] == "Bob Builder" and direct["headers"]["cc"] == "carol@example.com"
    assert direct["headers"]["date_iso"] == "2026-03-02T10:15:00+00:00"
    assert direct["body_text"].startswith("Body line one.") and direct["body_source"] == "PR_BODY"
    assert [a["name"] for a in direct["attachments"]] == ["Drawings batch 3B.zip"]
    assert direct["attachments"][0]["is_zip"] and direct["attachments"][0]["size_bytes"] == len(_zip_bytes())
    monkeypatch.setattr(sm, "_extract_msg_available", lambda: False)
    fallback = sm.parse_msg(path)
    assert fallback["parser"].startswith("olefile") and fallback["warnings"] == ["extract_msg not installed; olefile reader used"]
    manifest = sm.run_one(path, tmp_path / "msg_fallback", fixtures["state"])
    assert manifest["methods_used"] == ["olefile"] and all(c["pass"] for c in manifest["checks"])


def test_properties_stream_parsing() -> None:
    import struct

    data = b"\x00" * 8 + struct.pack("<HHI", 0x0003, 0x3705, 6) + struct.pack("<II", 5, 0)
    props = sm.parse_properties_stream(data, 8)
    assert sm._prop_long(props, 0x3705) == 5 and sm._prop_long(props, 0x0E20) is None
    assert sm._filetime_to_iso(0) is None
    assert sm._filetime_to_iso(116444736000000000) == "1970-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def test_jpeg_image_json(fixtures: Dict[str, Any]) -> None:
    out = fixtures["outs"]["jpg"]
    record = _json(out / "image.json")
    assert record["kind"] == "image" and record["opened"] and record["format"] == "JPEG"
    assert (record["width"], record["height"]) == (2000, 1200)
    assert record["sha256"] == sm.sha256_file(fixtures["jpg"])
    assert record["exif_present"] and record["exif"]["make"] == "TestCam" and record["exif"]["model"] == "Model X"
    assert record["exif"]["datetime_original"] == "2026:03:01 09:30:00" and record["exif"]["date_iso"] == "2026-03-01T09:30:00"
    assert record["gps_present"] is True and record["gps"]["personal_data"] is True
    assert record["gps"]["latitude"] == pytest.approx(1.2916667, abs=1e-6)
    assert record["gps"]["longitude"] == pytest.approx(103.85, abs=1e-6)
    assert record["gps"]["altitude_m"] == pytest.approx(12.5)
    assert len(record["phash"]) == 16 and int(record["phash"], 16) >= 0
    assert record["caption"] is None
    assert record["markdown_excludes"] == ["gps"]
    preview = record["preview"]
    assert preview["path"] == "preview.jpg" and (preview["width"], preview["height"]) == (1568, 941)
    with Image.open(out / "preview.jpg") as img:
        assert img.size == (1568, 941) and img.format == "JPEG"
    safe = sm.markdown_fields(record)
    assert "latitude" not in json.dumps(safe) and safe["gps"] == "present (excluded as personal data)"
    assert safe["exif"]["make"] == "TestCam"


def test_png_without_exif_not_upscaled(fixtures: Dict[str, Any]) -> None:
    out = fixtures["outs"]["png"]
    record = _json(out / "image.json")
    assert record["format"] == "PNG" and record["mode"] == "RGBA" and not record["exif_present"]
    assert record["gps_present"] is False and record["gps"] is None
    assert record["preview"]["width"] == 300 and record["preview"]["height"] == 200
    with Image.open(out / "preview.jpg") as img:
        assert img.size == (300, 200) and img.mode == "RGB"
    manifest = _json(out / "sidecar_manifest.json")
    assert manifest["kind"] == "image" and manifest["files"] == ["image.json", "preview.jpg"]
    assert manifest["rel_path"] is None
    assert {c["name"] for c in manifest["converters"]} == {"pillow", "imagehash"}
    assert all(c["pass"] for c in manifest["checks"]), manifest["checks"]


def test_near_duplicate_phash(fixtures: Dict[str, Any], tmp_path: Path) -> None:
    import imagehash

    with Image.open(fixtures["jpg"]) as img:
        resized = img.resize((1000, 600))
        resized.save(tmp_path / "copy.jpg", format="JPEG", quality=60)
    original = imagehash.hex_to_hash(_json(fixtures["outs"]["jpg"] / "image.json")["phash"])
    copy_record = sm.image_record(tmp_path / "copy.jpg", sm.sha256_file(tmp_path / "copy.jpg"), tmp_path)
    assert original - imagehash.hex_to_hash(copy_record["phash"]) <= 4


def test_unopenable_image_records_gap(tmp_path: Path) -> None:
    bad = tmp_path / "broken.heic"
    bad.write_bytes(b"not an image at all")
    manifest = sm.run_one(bad, tmp_path / "out", tmp_path)
    record = _json(tmp_path / "out" / "image.json")
    assert record["opened"] is False and record["preview"] is None and record["error"]
    assert manifest["files"] == ["image.json"]
    failed = [c["check"] for c in manifest["checks"] if not c["pass"]]
    assert "image opened by Pillow" in failed


# --------------------------------------------------------------------------- #
# Other types
# --------------------------------------------------------------------------- #
def test_other_type(fixtures: Dict[str, Any]) -> None:
    out = fixtures["outs"]["other"]
    record = _json(out / "no_converter.json")
    assert record["kind"] == "other" and record["extension"] == "txt" and record["converter"] is None
    assert "no converter applies" in record["note"] and record["doc_class"] == "other"
    manifest = _json(out / "sidecar_manifest.json")
    assert manifest["kind"] == "other" and manifest["files"] == ["no_converter.json"]
    assert manifest["methods_used"] == ["none"] and manifest["converters"] == []
    assert all(c["pass"] for c in manifest["checks"])


def test_other_type_names_owning_module(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    record = sm.other_record(pdf, sm.sha256_file(pdf), "DD Sign-OFF/30 - Pharmacy/Submitted/x.pdf")
    assert record["handled_by"] == "sidecar_pdf" and record["doc_class"] == "pdf document"


# --------------------------------------------------------------------------- #
# Idempotency and CLI
# --------------------------------------------------------------------------- #
def test_rerun_is_skipped_and_force_rewrites(fixtures: Dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    out = fixtures["outs"]["eml"]
    manifest_path = out / "sidecar_manifest.json"
    before = manifest_path.read_text(encoding="utf-8")
    args = ["run", "--path", str(fixtures["eml"]), "--out", str(out), "--state-dir", str(fixtures["state"]),
            "--progress", str(fixtures["progress"])]
    assert sm.main(args) == 0
    assert manifest_path.read_text(encoding="utf-8") == before
    assert "skip (done)" in capsys.readouterr().out
    (out / "body.md").unlink()
    assert sm.main(args) == 0  # a missing output invalidates the done state
    assert (out / "body.md").is_file()
    assert sm.main(args + ["--force"]) == 0
    assert "done:" in capsys.readouterr().out


def test_default_out_dir_from_ledger(fixtures: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sm.PATHS, "sidecar", tmp_path / "sidecar")
    assert sm.main(["run", "--path", str(fixtures["jpg"]), "--state-dir", str(fixtures["state"]), "--no-progress"]) == 0
    expected = tmp_path / "sidecar" / "DD Sign-OFF/30 - Pharmacy/Submitted/site photo.jpg"
    assert (expected / "image.json").is_file() and (expected / "preview.jpg").is_file()


def test_cli_failures(fixtures: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sm.main(["run", "--path", str(tmp_path / "missing.eml"), "--out", str(tmp_path), "--no-progress"]) == 1
    empty_state = tmp_path / "state"
    empty_state.mkdir()
    assert sm.main(["run", "--path", str(fixtures["png"]), "--state-dir", str(empty_state), "--no-progress"]) == 1
    corrupt = tmp_path / "corrupt.msg"
    corrupt.write_bytes(b"not an ole file")
    assert sm.main(["run", "--path", str(corrupt), "--out", str(tmp_path / "o"), "--no-progress"]) == 1
    monkeypatch.setitem(sm.PATHS, "source", tmp_path / "locked")
    (tmp_path / "locked").mkdir()
    assert sm.main(["run", "--path", str(fixtures["png"]), "--out", str(tmp_path / "locked" / "x"), "--no-progress"]) == 1
    assert not (tmp_path / "locked" / "x").exists()
