"""Sidecar extraction for the "miscellaneous" file types: e-mail, images and the rest.

``python -m src.sidecar_misc run --path <file> [--out <dir>]`` looks at the
extension of one source file and writes, under the output directory:

* ``.msg`` / ``.eml`` (correspondence): ``message.json`` with the headers
  (from, to, cc, date, subject, message-id), the body text statistics and the
  attachment list (name, size, content type, zip flag), plus ``body.md`` with
  the headers as a table, the body text verbatim and the attachment table.
  Attachments are **listed only**: nothing is decoded into a file, nothing is
  opened and no archive is read.  EML files are parsed with the standard
  ``email`` package; MSG files with ``extract-msg`` when it is installed and
  otherwise with a small ``olefile`` property reader.  The MSG attachment list
  always comes from the OLE directory, so the attachment payload is never
  loaded.  The extension is corrected by the file's signature bytes (a ``.eml``
  with the OLE signature is parsed as an MSG), a single-part message that is
  nothing but an attachment is listed as one, an unknown body charset falls back
  to utf-8/cp1252 with a warning, and a file with no headers or binary content
  fails the "parses as a mail message" check.
* ``.jpg`` / ``.jpeg`` / ``.png`` / ``.heic`` (photos): ``image.json`` with the
  pixel size, EXIF date, camera and GPS fields when present (GPS is kept but
  flagged as personal data so the Markdown writer excludes it), the file
  SHA-256, a perceptual hash (``imagehash.phash``) for near-duplicate
  detection and ``preview.jpg`` with a 1568 px long edge (never upscaled).
  No caption is written: a model writes captions later.  HEIC opens only when
  Pillow has a HEIF plugin; otherwise the gap is recorded.
* anything else: ``no_converter.json`` recording the type and that no
  converter applies (PDF, PPTX and workbooks have their own modules).

Every run also writes ``sidecar_manifest.json`` (source hash, ledger fields,
files written, counts and checks).  The run is idempotent: when the manifest
records the same source SHA-256 and module version and every listed file
exists, nothing is rewritten unless ``--force`` is given.  The source file is
only read.  Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import email.policy
import email.utils
import hashlib
import io
import json
import re
import struct
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (python -m src.sidecar_misc, tests)
    from src.common import (
        PATHS,
        atomic_write_json,
        atomic_write_text,
        classify_document,
        ensure_dir,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )
    from src.state import Progress
except ModuleNotFoundError:  # run as a plain script from inside src/
    from common import (  # type: ignore[no-redef]
        PATHS,
        atomic_write_json,
        atomic_write_text,
        classify_document,
        ensure_dir,
        read_jsonl,
        sha256_file,
        utc_now_iso,
    )
    from state import Progress  # type: ignore[no-redef]

MODULE_NAME = "sidecar_misc"
MODULE_VERSION = "1.1.0"

MANIFEST_NAME = "sidecar_manifest.json"
MESSAGE_NAME = "message.json"
BODY_NAME = "body.md"
IMAGE_NAME = "image.json"
PREVIEW_NAME = "preview.jpg"
OTHER_NAME = "no_converter.json"
LEDGER_FILES: Tuple[str, ...] = ("pull_ledger.jsonl", "upload_ledger.jsonl")

PREVIEW_LONG_EDGE = 1568
PREVIEW_QUALITY = 85

EMAIL_EXTS = frozenset({"msg", "eml"})
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "heic"})
ZIP_CONTENT_TYPES = frozenset({"application/zip", "application/x-zip-compressed", "application/x-zip"})
OTHER_MODULES = {"pdf": "sidecar_pdf", "pptx": "sidecar_pptx", "xlsx": "sidecar_xlsx", "xlsm": "sidecar_xlsx",
                 "xls": "sidecar_xlsx"}
PERSONAL_DATA_FIELDS: Tuple[str, ...] = ("gps",)
"""Keys of ``image.json`` that the Markdown writer must leave out."""

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
"""Signature of an OLE compound file (an Outlook MSG, whatever its extension)."""
_CONTAINER_SIGNATURES: Tuple[Tuple[bytes, str], ...] = ((OLE_MAGIC, "ole"), (b"PK\x03\x04", "zip"), (b"%PDF", "pdf"),
                                                        (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"))
_SNIFF_BYTES = 4096
_BINARY_BODY_RATIO = 0.05
"""Share of replacement or control characters above which a body is reported as binary."""
HEADER_SANITY_KEYS: Tuple[str, ...] = ("from", "to", "subject", "date", "message_id")
"""At least one of these must be present for a file to count as an RFC 822 message."""
ATTACHMENT_ONLY_SOURCE = "none (attachment-only message)"

_NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![A-Za-z0-9_])[-+]?\.\d+")
_EXIF_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_ATTACH_DIR_RE = re.compile(r"^__attach_version1\.0_#([0-9A-Fa-f]{8})$")
_FILETIME_EPOCH_OFFSET_S = 11_644_473_600

# MAPI property tags (hex id) used by the olefile reader.
_PR_SUBJECT = "0037"
_PR_SENDER_NAME = "0C1A"
_PR_SENDER_EMAIL = "0C1F"
_PR_SENDER_SMTP = "5D01"
_PR_DISPLAY_TO = "0E04"
_PR_DISPLAY_CC = "0E03"
_PR_MESSAGE_ID = "1035"
_PR_IN_REPLY_TO = "1042"
_PR_BODY = "1000"
_PR_HTML = "1013"
_PR_TRANSPORT_HEADERS = "007D"
_PR_ATTACH_LONG_FILENAME = "3707"
_PR_ATTACH_FILENAME = "3704"
_PR_ATTACH_EXTENSION = "3703"
_PR_ATTACH_MIME_TAG = "370E"
_PR_ATTACH_DATA_STREAM = "__substg1.0_37010102"
_PR_ATTACH_DATA_STORAGE = "__substg1.0_3701000D"
_TAG_ATTACH_METHOD = 0x3705
_TAG_ATTACH_SIZE = 0x0E20
_TAG_MESSAGE_CODEPAGE = 0x3FFD
_TAG_INTERNET_CPID = 0x3FDE
_TAG_DELIVERY_TIME = 0x0E06
_TAG_CLIENT_SUBMIT_TIME = 0x0039
_PT_LONG = 0x0003
_PT_SYSTIME = 0x0040
_ATTACH_METHOD_NAMES = {0: "none", 1: "by value", 2: "by reference", 3: "by reference resolve",
                        4: "by reference only", 5: "embedded message", 6: "OLE object", 7: "web reference"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def numeric_tokens(text: str) -> List[str]:
    """Numeric tokens of ``text`` in order, thousands separators removed (same rule as the PPTX sidecar)."""
    return [m.group(0).replace(",", "") for m in _NUMERIC_RE.finditer(text or "")]


def sha256_bytes(blob: bytes) -> str:
    """SHA-256 hex digest of a byte string."""
    return hashlib.sha256(blob).hexdigest()


def file_kind(path: Path) -> Tuple[str, str]:
    """``(kind, extension)`` for a path: ``email``, ``image`` or ``other``."""
    ext = path.suffix.lower().lstrip(".")
    if ext in EMAIL_EXTS:
        return "email", ext
    if ext in IMAGE_EXTS:
        return "image", ext
    return "other", ext


def is_zip_attachment(name: Optional[str], content_type: Optional[str]) -> bool:
    """True when an attachment is a zip by file name or by declared content type."""
    if name and name.lower().strip().endswith(".zip"):
        return True
    return bool(content_type) and str(content_type).lower().split(";")[0].strip() in ZIP_CONTENT_TYPES


def text_stats(text: str) -> Dict[str, int]:
    """Character, word and numeric-token counts of a text."""
    return {"chars": len(text), "words": len(text.split()), "numeric_tokens": len(numeric_tokens(text))}


def _md_cell(value: Any) -> str:
    """Render a value for a Markdown table cell (pipes and newlines escaped)."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", " ")


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """A Markdown table from a header row and row sequences."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(v) for v in row) + " |")
    return "\n".join(lines)


class _HtmlText(HTMLParser):
    """Collect the visible text of an HTML document with block elements as line breaks."""

    _BLOCKS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote",
                         "pre", "hr", "section", "article", "ul", "ol"})
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
        elif tag == "td" or tag == "th":
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Visible text of an HTML fragment, blank lines collapsed."""
    parser = _HtmlText()
    parser.feed(markup or "")
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- #
# EML (standard library)
# --------------------------------------------------------------------------- #
def _header(msg: email.message.Message, name: str) -> str:
    """A decoded header value ('' when absent); malformed headers fall back to the raw text."""
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - defective header under the default policy
        value = None
    if value is None:
        return ""
    return str(value).strip()


def _iso_date(raw: str) -> Optional[str]:
    """ISO 8601 form of an RFC 2822 date header, or ``None`` when it cannot be parsed."""
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed.isoformat() if parsed else None


def _base64_decoded_size(payload: str) -> int:
    """Byte length of a base64 body without decoding it (whitespace ignored, padding honoured)."""
    compact = re.sub(r"\s+", "", payload or "")
    if not compact:
        return 0
    padding = len(compact) - len(compact.rstrip("="))
    return (len(compact) * 3) // 4 - padding


def _part_size(part: email.message.Message) -> Tuple[Optional[int], str]:
    """``(size in bytes, how it was measured)`` for an attachment part; the content is never parsed."""
    encoding = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if part.is_multipart():
        return len(part.as_bytes()), "serialised part (nested message)"
    if encoding == "base64":
        return _base64_decoded_size(part.get_payload(decode=False) or ""), "base64 length arithmetic"
    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        return None, "unknown"
    return len(raw), f"decoded {encoding or '7bit'} length"


def _is_top_level_attachment(msg: email.message.Message) -> bool:
    """True for a non-multipart message whose single part is itself an attachment (``iter_attachments`` skips it)."""
    if msg.is_multipart():
        return False
    try:
        if msg.is_attachment():
            return True
    except Exception:  # noqa: BLE001 - defective Content-Disposition header
        pass
    return msg.get_content_maintype() not in ("text", "message")


def iter_eml_attachments(msg: email.message.Message) -> Iterable[email.message.Message]:
    """Attachment parts of a message, including a single-part message that is nothing but an attachment."""
    if _is_top_level_attachment(msg):
        yield msg
        return
    yield from msg.iter_attachments()


def _eml_attachment(index: int, part: email.message.Message, whole_message: bool = False) -> Dict[str, Any]:
    """One attachment row from an EML part (listed, never opened)."""
    name = part.get_filename()
    content_type = part.get_content_type()
    size, method = _part_size(part)
    disposition = (part.get_content_disposition() or "").lower() or None
    note: Optional[str] = None
    if whole_message:
        note = "single-part message: the whole message is this attachment; no body text"
    elif content_type == "message/rfc822":
        note = "embedded message; not opened; its own attachments are not enumerated"
    return {
        "index": index, "name": name, "size_bytes": size, "size_method": method,
        "content_type": content_type, "disposition": disposition, "content_id": _header(part, "Content-ID") or None,
        "is_zip": is_zip_attachment(name, content_type), "opened": False, "note": note,
    }


def decode_body_bytes(raw: bytes, declared: Optional[str], warnings: List[str]) -> str:
    """Decode a body whose content manager failed: the declared charset when Python knows it, else utf-8, else cp1252.

    Never raises: an unknown charset (``iso-8859-8-i``, ``x-mac-roman``, ``unknown-8bit``
    all occur in real mail) ends in cp1252 with replacement characters and a warning.
    """
    attempts: List[Tuple[str, str]] = [("utf-8", "strict"), ("cp1252", "replace")]
    if declared:
        attempts.insert(0, (declared, "replace"))
    unknown = False
    for codec, errors in attempts:
        try:
            text = raw.decode(codec, errors=errors)
        except LookupError:
            unknown = True
            continue
        except UnicodeDecodeError:
            continue
        if declared and codec.lower() != declared.lower():
            cause = "is unknown to Python" if unknown else "did not decode cleanly"
            warnings.append(f"body charset {declared!r} {cause}; decoded as {codec} with replacement characters")
        elif not declared:
            warnings.append(f"body has no declared charset; decoded as {codec}")
        return text
    return raw.decode("latin-1")


def _eml_body(msg: email.message.Message, warnings: List[str]) -> Tuple[str, str]:
    """``(body text, source)`` preferring text/plain, then HTML with its tags stripped; decoding never raises."""
    for preference, label in (("plain", "text/plain"), ("html", "text/html (tags stripped)")):
        try:
            part = msg.get_body(preferencelist=(preference,))
        except Exception:  # noqa: BLE001
            part = None
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset or broken encoding
            try:
                raw = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001 - undecodable transfer encoding
                raw = str(part.get_payload(decode=False) or "").encode("utf-8", errors="replace")
            content = decode_body_bytes(raw, part.get_content_charset(), warnings)
        if not isinstance(content, str):
            continue
        return (content if preference == "plain" else html_to_text(content)), label
    return "", "none"


def binary_ratio(text: str) -> float:
    """Share of replacement (U+FFFD) and non-printing control characters in a text (0.0 for an empty text)."""
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ch == "�" or (ord(ch) < 32 and ch not in "\t\r\n") or ord(ch) == 127)
    return bad / len(text)


def sniff_container(path: Path) -> Optional[str]:
    """Container type suggested by the file's first bytes (``ole``, ``zip``, ``pdf``, ``png``, ``jpeg``) or ``None``."""
    with path.open("rb") as handle:
        head = handle.read(len(OLE_MAGIC))
    for magic, name in _CONTAINER_SIGNATURES:
        if head.startswith(magic):
            return name
    return None


def _has_nul_in_head(path: Path) -> bool:
    """True when the first few KiB hold a NUL byte (RFC 822 text never does)."""
    with path.open("rb") as handle:
        return b"\x00" in handle.read(_SNIFF_BYTES)


def parse_eml(path: Path) -> Dict[str, Any]:
    """Headers, body text and attachment list of an EML file via the standard library.

    Content that is not an RFC 822 message (no header at all, NUL bytes, a body
    of replacement characters) is recorded in ``warnings`` so the checks and the
    grade can see it; parsing itself never raises on odd charsets.
    """
    warnings: List[str] = []
    with path.open("rb") as handle:
        msg = email.message_from_binary_file(handle, policy=email.policy.default)
    body, body_source = _eml_body(msg, warnings)
    whole = _is_top_level_attachment(msg)
    attachments = [_eml_attachment(i, part, whole_message=whole)
                   for i, part in enumerate(iter_eml_attachments(msg), start=1)]
    if whole and not body:
        body_source = ATTACHMENT_ONLY_SOURCE
    raw_date = _header(msg, "Date")
    headers = {
        "from": _header(msg, "From"), "to": _header(msg, "To"), "cc": _header(msg, "Cc"),
        "date": raw_date, "date_iso": _iso_date(raw_date), "subject": _header(msg, "Subject"),
        "message_id": _header(msg, "Message-ID"), "in_reply_to": _header(msg, "In-Reply-To") or None,
    }
    sanity: List[str] = []
    if not any(headers[k] for k in HEADER_SANITY_KEYS):
        sanity.append("no From/To/Subject/Date/Message-ID header found; the file may not be an RFC 822 message")
    if _has_nul_in_head(path):
        sanity.append("NUL bytes in the first 4 KiB; the file is binary, not an RFC 822 message")
    ratio = binary_ratio(body)
    if ratio > _BINARY_BODY_RATIO:
        sanity.append(f"body text looks binary ({ratio:.0%} replacement or control characters)")
    return {
        "format": "eml", "parser": "email (standard library)", "headers": headers,
        "body_text": body, "body_source": body_source, "attachments": attachments,
        "warnings": warnings + sanity, "sanity": sanity,
    }


def parse_email(path: Path) -> Dict[str, Any]:
    """Parse a mail file by extension, corrected by its signature bytes.

    A ``.eml`` that starts with the OLE compound-file signature is an Outlook MSG
    saved under the wrong name and goes to the MSG parser (falling back to the
    RFC 822 parser, with the failure recorded, when it is not a readable MSG);
    a ``.msg`` without that signature that reads as RFC 822 text with headers is
    parsed as EML.  Every rerouting leaves a warning behind.
    """
    container = sniff_container(path)
    if path.suffix.lower() != ".eml":
        try:
            return parse_msg(path)
        except Exception as exc:  # noqa: BLE001 - maybe RFC 822 text named .msg
            if container is not None:
                raise
            result = parse_eml(path)
            if not any(result["headers"][k] for k in HEADER_SANITY_KEYS):
                raise exc
            result["warnings"].insert(0, f".msg file is not an OLE compound file ({type(exc).__name__}); "
                                         "parsed as RFC 822 text")
            return result
    prefix: List[str] = []
    if container == "ole":
        try:
            result = parse_msg(path)
        except Exception as exc:  # noqa: BLE001 - OLE signature but not a readable MSG
            prefix.append("OLE compound-file signature in a .eml, but MSG parsing failed "
                          f"({type(exc).__name__}: {str(exc)[:120]}); parsed as RFC 822 text")
        else:
            result["warnings"].insert(0, ".eml file carries the OLE compound-file signature; parsed as an Outlook MSG")
            return result
    elif container is not None:
        prefix.append(f"{container} signature at the start of a .eml; not an RFC 822 message")
    result = parse_eml(path)
    result["warnings"] = prefix + result["warnings"]
    result["sanity"] = prefix + result["sanity"]
    return result


def count_eml_attachment_parts(path: Path) -> int:
    """Independent recount of attachment parts (for the manifest check)."""
    with path.open("rb") as handle:
        msg = email.message_from_binary_file(handle, policy=email.policy.default)
    return sum(1 for _ in iter_eml_attachments(msg))


# --------------------------------------------------------------------------- #
# MSG: OLE property reading shared by both MSG paths
# --------------------------------------------------------------------------- #
def _filetime_to_iso(value: int) -> Optional[str]:
    """ISO 8601 UTC string for a Windows FILETIME (100 ns ticks since 1601), ``None`` when zero."""
    if not value:
        return None
    seconds = value / 10_000_000 - _FILETIME_EPOCH_OFFSET_S
    try:
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_properties_stream(data: bytes, header_len: int) -> Dict[int, Tuple[int, bytes]]:
    """``{tag id: (type, 8 raw value bytes)}`` from a ``__properties_version1.0`` stream."""
    out: Dict[int, Tuple[int, bytes]] = {}
    for offset in range(header_len, len(data) - 15, 16):
        prop_type, tag = struct.unpack_from("<HH", data, offset)
        out[tag] = (prop_type, data[offset + 8: offset + 16])
    return out


def _prop_long(props: Dict[int, Tuple[int, bytes]], tag: int) -> Optional[int]:
    """A PT_LONG property value."""
    entry = props.get(tag)
    if not entry or entry[0] != _PT_LONG:
        return None
    return struct.unpack("<i", entry[1][:4])[0]


def _prop_time(props: Dict[int, Tuple[int, bytes]], tag: int) -> Optional[str]:
    """A PT_SYSTIME property value as ISO 8601."""
    entry = props.get(tag)
    if not entry or entry[0] != _PT_SYSTIME:
        return None
    return _filetime_to_iso(struct.unpack("<Q", entry[1])[0])


class OleMessageReader:
    """Read MSG properties straight from the OLE compound file with ``olefile``.

    Only named streams are read; attachment payload streams are never opened
    (their size comes from the directory entry).
    """

    def __init__(self, path: Path) -> None:
        import olefile

        self.ole = olefile.OleFileIO(str(path))
        self.entries = {"/".join(parts) for parts in self.ole.listdir(streams=True, storages=True)}
        self.props = self._props("", 32)
        self.codepage = self._codepage()

    def close(self) -> None:
        """Release the file handle."""
        self.ole.close()

    def _props(self, prefix: str, header_len: int) -> Dict[int, Tuple[int, bytes]]:
        """Parsed properties stream under ``prefix`` ('' for the top level)."""
        name = f"{prefix}/__properties_version1.0" if prefix else "__properties_version1.0"
        if name not in self.entries:
            return {}
        return parse_properties_stream(self.ole.openstream(name).read(), header_len)

    def _codepage(self) -> str:
        """Python codec name for 8-bit strings, from the message code page (default cp1252)."""
        cpid = _prop_long(self.props, _TAG_MESSAGE_CODEPAGE) or _prop_long(self.props, _TAG_INTERNET_CPID)
        if cpid in (65001,):
            return "utf-8"
        if cpid and cpid > 0:
            try:
                "".encode(f"cp{cpid}")
                return f"cp{cpid}"
            except LookupError:
                pass
        return "cp1252"

    def string(self, tag: str, prefix: str = "") -> Optional[str]:
        """A string property (``001F`` UTF-16 or ``001E`` 8-bit), ``None`` when absent."""
        base = f"{prefix}/__substg1.0_{tag}" if prefix else f"__substg1.0_{tag}"
        if f"{base}001F" in self.entries:
            raw = self.ole.openstream(f"{base}001F").read()
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        if f"{base}001E" in self.entries:
            raw = self.ole.openstream(f"{base}001E").read()
            return raw.decode(self.codepage, errors="replace").rstrip("\x00")
        return None

    def binary(self, tag: str, prefix: str = "") -> Optional[bytes]:
        """A binary (``0102``) property, ``None`` when absent."""
        name = f"{prefix}/__substg1.0_{tag}0102" if prefix else f"__substg1.0_{tag}0102"
        if name not in self.entries:
            return None
        return self.ole.openstream(name).read()

    def attachment_dirs(self) -> List[str]:
        """Attachment storage names in index order."""
        dirs = [e for e in self.entries if _ATTACH_DIR_RE.match(e)]
        return sorted(dirs, key=lambda d: int(_ATTACH_DIR_RE.match(d).group(1), 16))  # type: ignore[union-attr]

    def attachment(self, index: int, prefix: str) -> Dict[str, Any]:
        """One attachment row from its storage: names, declared type, size from the directory entry."""
        props = self._props(prefix, 8)
        method = _prop_long(props, _TAG_ATTACH_METHOD)
        name = self.string(_PR_ATTACH_LONG_FILENAME, prefix) or self.string(_PR_ATTACH_FILENAME, prefix)
        content_type = self.string(_PR_ATTACH_MIME_TAG, prefix)
        data_stream = f"{prefix}/{_PR_ATTACH_DATA_STREAM}"
        size: Optional[int]
        if data_stream in self.entries:
            size, size_method = int(self.ole.get_size(data_stream)), "OLE directory entry size"
        elif _prop_long(props, _TAG_ATTACH_SIZE) is not None:
            size, size_method = _prop_long(props, _TAG_ATTACH_SIZE), "PR_ATTACH_SIZE (includes property overhead)"
        else:
            size, size_method = None, "unknown"
        note = None
        if method == 5 or f"{prefix}/{_PR_ATTACH_DATA_STORAGE}" in self.entries:
            note = "embedded message; not opened"
            content_type = content_type or "message/rfc822"
        return {
            "index": index, "name": name, "size_bytes": size, "size_method": size_method,
            "content_type": content_type, "extension": self.string(_PR_ATTACH_EXTENSION, prefix),
            "attach_method": _ATTACH_METHOD_NAMES.get(method if method is not None else -1, str(method)),
            "is_zip": is_zip_attachment(name, content_type), "opened": False, "note": note,
        }

    def attachments(self) -> List[Dict[str, Any]]:
        """Every attachment of the message, listed from the OLE directory."""
        return [self.attachment(i, d) for i, d in enumerate(self.attachment_dirs(), start=1)]

    def transport_headers(self) -> Dict[str, str]:
        """Internet headers from PR_TRANSPORT_MESSAGE_HEADERS, when the message carries them."""
        raw = self.string(_PR_TRANSPORT_HEADERS)
        if not raw:
            return {}
        parsed = email.message_from_string(raw, policy=email.policy.default)
        return {k: _header(parsed, k) for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To")}

    def body(self) -> Tuple[str, str]:
        """``(body text, source)``: PR_BODY, else PR_HTML with its tags stripped."""
        plain = self.string(_PR_BODY)
        if plain:
            return plain, "PR_BODY"
        markup = self.binary(_PR_HTML)
        if markup:
            return html_to_text(markup.decode(self.codepage, errors="replace")), "PR_HTML (tags stripped)"
        return "", "none"

    def headers(self) -> Dict[str, Any]:
        """The header block assembled from MAPI properties, transport headers filling the gaps."""
        transport = self.transport_headers()
        sender_name = self.string(_PR_SENDER_NAME) or ""
        sender_email = self.string(_PR_SENDER_SMTP) or self.string(_PR_SENDER_EMAIL) or ""
        if sender_name and sender_email and sender_email.lower() != sender_name.lower():
            sender = f"{sender_name} <{sender_email}>"
        else:
            sender = sender_name or sender_email
        date_iso = _prop_time(self.props, _TAG_CLIENT_SUBMIT_TIME) or _prop_time(self.props, _TAG_DELIVERY_TIME)
        return {
            "from": sender or transport.get("From", ""),
            "to": self.string(_PR_DISPLAY_TO) or transport.get("To", ""),
            "cc": self.string(_PR_DISPLAY_CC) or transport.get("Cc", ""),
            "date": transport.get("Date") or date_iso or "",
            "date_iso": date_iso or _iso_date(transport.get("Date", "")),
            "subject": self.string(_PR_SUBJECT) or transport.get("Subject", ""),
            "message_id": self.string(_PR_MESSAGE_ID) or transport.get("Message-ID", ""),
            "in_reply_to": self.string(_PR_IN_REPLY_TO) or transport.get("In-Reply-To") or None,
        }


def parse_msg_olefile(path: Path) -> Dict[str, Any]:
    """Headers, body and attachment list of an MSG file read with ``olefile`` only."""
    reader = OleMessageReader(path)
    try:
        body, body_source = reader.body()
        return {
            "format": "msg", "parser": f"olefile {_olefile_version()} property reader",
            "headers": reader.headers(), "body_text": body, "body_source": body_source,
            "attachments": reader.attachments(), "warnings": [],
        }
    finally:
        reader.close()


def _extract_msg_available() -> bool:
    """True when ``extract_msg`` imports cleanly."""
    try:
        import extract_msg  # noqa: F401
    except Exception:  # noqa: BLE001 - missing package or a broken dependency chain
        return False
    return True


def _dt_iso(value: Any) -> Optional[str]:
    """ISO form of a datetime (or ``None``)."""
    return value.isoformat() if isinstance(value, dt.datetime) else (str(value) if value else None)


def parse_msg_extract_msg(path: Path) -> Dict[str, Any]:
    """Headers and body via ``extract_msg`` (attachments delayed, so no payload is loaded); list via olefile."""
    import extract_msg

    msg = extract_msg.openMsg(str(path), delayAttachments=True)
    try:
        body = msg.body or ""
        body_source = "msg body"
        if not body:
            markup = msg.htmlBody
            if markup:
                body = html_to_text(markup.decode("utf-8", errors="replace") if isinstance(markup, bytes) else str(markup))
                body_source = "msg html body (tags stripped)"
            else:
                body_source = "none"
        header_date = ""
        try:
            header_date = str(msg.header.get("Date") or "") if msg.header else ""
        except Exception:  # noqa: BLE001
            header_date = ""
        headers = {
            "from": msg.sender or "", "to": msg.to or "", "cc": msg.cc or "",
            "date": header_date or _dt_iso(msg.date) or "", "date_iso": _dt_iso(msg.date) or _iso_date(header_date),
            "subject": msg.subject or "", "message_id": msg.messageId or "", "in_reply_to": msg.inReplyTo or None,
        }
    finally:
        msg.close()
    reader = OleMessageReader(path)
    try:
        attachments = reader.attachments()
    finally:
        reader.close()
    return {
        "format": "msg", "parser": f"extract-msg {extract_msg.__version__} (attachment list via olefile)",
        "headers": headers, "body_text": body, "body_source": body_source, "attachments": attachments,
        "warnings": [],
    }


def parse_msg(path: Path) -> Dict[str, Any]:
    """Parse an MSG with ``extract_msg`` when available, otherwise with the olefile reader."""
    if _extract_msg_available():
        try:
            return parse_msg_extract_msg(path)
        except Exception as exc:  # noqa: BLE001 - fall back and say so
            result = parse_msg_olefile(path)
            result["warnings"].append(f"extract_msg failed ({type(exc).__name__}: {str(exc)[:200]}); olefile reader used")
            return result
    result = parse_msg_olefile(path)
    result["warnings"].append("extract_msg not installed; olefile reader used")
    return result


def count_msg_attachment_dirs(path: Path) -> int:
    """Independent recount of attachment storages in the OLE directory."""
    import olefile

    ole = olefile.OleFileIO(str(path))
    try:
        return sum(1 for parts in ole.listdir(storages=True) if len(parts) == 1 and _ATTACH_DIR_RE.match(parts[0]))
    finally:
        ole.close()


# --------------------------------------------------------------------------- #
# E-mail outputs
# --------------------------------------------------------------------------- #
def message_record(parsed: Dict[str, Any], path: Path, sha256: str) -> Dict[str, Any]:
    """Contents of ``message.json`` (body text itself lives in ``body.md``)."""
    body = parsed["body_text"]
    attachments = parsed["attachments"]
    zips = [a for a in attachments if a["is_zip"]]
    return {
        "kind": "email", "format": parsed["format"], "parser": parsed["parser"],
        "source_name": path.name, "sha256": sha256, "bytes": path.stat().st_size,
        "headers": parsed["headers"],
        "body": {**text_stats(body), "source": parsed["body_source"], "sha256": sha256_bytes(body.encode("utf-8"))},
        "attachment_count": len(attachments), "zip_attachment_count": len(zips),
        "zip_attachments": [a["name"] for a in zips],
        "attachments_opened": False,
        "attachments": attachments, "warnings": parsed["warnings"],
        "parse_sanity": list(parsed.get("sanity") or []),
    }


def body_markdown(record: Dict[str, Any], body: str) -> str:
    """``body.md``: header table, body text verbatim, attachment table."""
    headers = record["headers"]
    head_rows = [(k, headers.get(k)) for k in ("from", "to", "cc", "date", "subject", "message_id", "in_reply_to")]
    lines = [f"# {record['source_name']}", "", "## Headers", "", markdown_table(("header", "value"), head_rows), "",
             f"<!-- method: {record['parser']} -->", "## Body", "", body.rstrip() or "(no body text)", "",
             "## Attachments", ""]
    if record["attachments"]:
        rows = [(a["index"], a["name"], a["size_bytes"], a["content_type"], "yes" if a["is_zip"] else "",
                 a.get("note") or "") for a in record["attachments"]]
        lines.append(markdown_table(("index", "name", "size (bytes)", "content type", "zip", "note"), rows))
    else:
        lines.append("(no attachments)")
    lines += ["", "Attachments are listed only; none was extracted or opened.", ""]
    return "\n".join(lines)


def _email_checks(path: Path, record: Dict[str, Any], out_dir: Path, files: Sequence[str]) -> List[Dict[str, Any]]:
    """Self-checks for an e-mail sidecar."""
    recount = count_eml_attachment_parts(path) if record["format"] == "eml" else count_msg_attachment_dirs(path)
    rows = record["attachments"]
    zip_names = [a for a in rows if (a["name"] or "").lower().endswith(".zip")]
    extra = sorted(p.name for p in out_dir.iterdir() if p.is_file() and p.name not in files and not p.name.startswith("."))
    sanity = record.get("parse_sanity") or []
    headers_present = [k for k in HEADER_SANITY_KEYS if record["headers"].get(k)]
    attachment_only = record["body"]["source"] == ATTACHMENT_ONLY_SOURCE and bool(rows)
    body_evidence = f"{record['body']['chars']} chars from {record['body']['source']}"
    if attachment_only:
        body_evidence = "attachment-only message (no body part)"
    return [
        {"check": "file parses as a mail message (headers present, no binary content)", "pass": not sanity,
         "evidence": (f"headers: {', '.join(headers_present)}" if not sanity else "; ".join(sanity))},
        {"check": "attachment rows equal attachment parts in the file", "pass": len(rows) == recount,
         "evidence": f"{len(rows)} rows, {recount} parts"},
        {"check": "every .zip attachment is flagged", "pass": all(a["is_zip"] for a in zip_names),
         "evidence": f"{sum(1 for a in zip_names if a['is_zip'])} of {len(zip_names)} flagged"},
        {"check": "no attachment extracted (only the sidecar files are in the output directory)", "pass": not extra,
         "evidence": "no extra files" if not extra else f"extra files: {', '.join(extra)}"},
        {"check": "body text present (or attachment-only message)", "pass": record["body"]["chars"] > 0 or attachment_only,
         "evidence": body_evidence},
    ]


def run_email(path: Path, out_dir: Path, sha256: str) -> Tuple[List[str], Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """Write ``message.json`` and ``body.md``; return ``(files, counts, checks, extra manifest fields)``."""
    parsed = parse_email(path)
    record = message_record(parsed, path, sha256)
    atomic_write_json(out_dir / MESSAGE_NAME, record)
    atomic_write_text(out_dir / BODY_NAME, body_markdown(record, parsed["body_text"]))
    files = [MESSAGE_NAME, BODY_NAME]
    counts = {"attachments": record["attachment_count"], "zip_attachments": record["zip_attachment_count"],
              "body_chars": record["body"]["chars"], "body_numeric_tokens": record["body"]["numeric_tokens"]}
    converters = [{"name": "email (standard library)", "version": sys.version.split()[0]}]
    methods = ["email-stdlib"]
    if record["format"] == "msg":
        converters = [{"name": "olefile", "version": _olefile_version()}]
        methods = ["olefile"]
        if record["parser"].startswith("extract-msg"):
            converters.insert(0, {"name": "extract-msg", "version": record["parser"].split()[1]})
            methods.insert(0, "extract-msg")
    extra = {"kind": "email", "converters": converters, "methods_used": methods, "warnings": record["warnings"]}
    return files, counts, _email_checks(path, record, out_dir, files + [MANIFEST_NAME]), extra


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def _register_heif() -> bool:
    """Register a HEIF opener with Pillow when ``pillow_heif`` is installed."""
    try:
        import pillow_heif  # type: ignore[import-not-found]

        pillow_heif.register_heif_opener()
        return True
    except Exception:  # noqa: BLE001
        return False


def exif_date_to_iso(value: Any) -> Optional[str]:
    """``YYYY:MM:DD HH:MM:SS`` (EXIF) to ``YYYY-MM-DDTHH:MM:SS``; ``None`` when not in that form."""
    match = _EXIF_DATE_RE.match(str(value or "").strip())
    if not match:
        return None
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _num(value: Any) -> Optional[float]:
    """A float from an EXIF rational/int, ``None`` when it is not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_decimal(dms: Any, ref: Any) -> Optional[float]:
    """Degrees/minutes/seconds (EXIF GPS) to signed decimal degrees."""
    try:
        parts = [_num(v) for v in dms]
    except TypeError:
        return None
    if len(parts) < 3 or any(p is None for p in parts):
        return None
    decimal = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0  # type: ignore[operator]
    if str(ref or "").upper() in ("S", "W"):
        decimal = -decimal
    return round(decimal, 7)


def _clean(value: Any) -> Any:
    """Strings stripped of NULs; bytes shown as their length; anything else unchanged."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    return value


def read_exif(img: Any) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """``(camera/date fields, GPS fields or None)`` from a Pillow image's EXIF."""
    from PIL import ExifTags

    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001
        return {}, None
    if not exif:
        return {}, None
    base = ExifTags.Base
    fields: Dict[str, Any] = {}
    for key, tag in (("make", base.Make), ("model", base.Model), ("software", base.Software),
                     ("orientation", base.Orientation), ("datetime", base.DateTime)):
        if tag in exif:
            fields[key] = _clean(exif.get(tag))
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:  # noqa: BLE001
        sub = {}
    for key, tag in (("datetime_original", base.DateTimeOriginal), ("datetime_digitized", base.DateTimeDigitized),
                     ("lens_model", base.LensModel), ("lens_make", base.LensMake)):
        if tag in sub:
            fields[key] = _clean(sub.get(tag))
    raw_date = fields.get("datetime_original") or fields.get("datetime_digitized") or fields.get("datetime")
    fields["date_iso"] = exif_date_to_iso(raw_date)
    try:
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001
        gps_ifd = {}
    return fields, _gps_fields(gps_ifd) if gps_ifd else None


def _gps_fields(gps: Dict[int, Any]) -> Optional[Dict[str, Any]]:
    """Decimal latitude/longitude/altitude and timestamps from a GPS IFD (``None`` when empty)."""
    from PIL import ExifTags

    g = ExifTags.GPS
    lat = _dms_to_decimal(gps.get(g.GPSLatitude), gps.get(g.GPSLatitudeRef)) if g.GPSLatitude in gps else None
    lon = _dms_to_decimal(gps.get(g.GPSLongitude), gps.get(g.GPSLongitudeRef)) if g.GPSLongitude in gps else None
    alt = _num(gps.get(g.GPSAltitude)) if g.GPSAltitude in gps else None
    if alt is not None and gps.get(g.GPSAltitudeRef) in (1, b"\x01"):
        alt = -alt
    out = {"personal_data": True, "latitude": lat, "longitude": lon, "altitude_m": alt,
           "date_stamp": _clean(gps.get(g.GPSDateStamp)) if g.GPSDateStamp in gps else None,
           "time_stamp": [_num(v) for v in gps.get(g.GPSTimeStamp)] if g.GPSTimeStamp in gps else None}
    return out if any(v is not None for k, v in out.items() if k != "personal_data") else None


def preview_size(width: int, height: int, long_edge: int = PREVIEW_LONG_EDGE) -> Tuple[int, int]:
    """Size of a preview whose long edge is at most ``long_edge`` px (never upscaled)."""
    longest = max(width, height)
    if longest <= long_edge or longest == 0:
        return width, height
    scale = long_edge / longest
    return (long_edge, max(1, round(height * scale))) if width >= height else (max(1, round(width * scale)), long_edge)


def write_preview(img: Any, target: Path) -> Dict[str, Any]:
    """Write the JPEG preview (orientation applied, long edge <= 1568 px) and describe it."""
    from PIL import Image, ImageOps

    try:
        orientation = int(img.getexif().get(0x0112) or 1)
    except Exception:  # noqa: BLE001
        orientation = 1
    oriented = ImageOps.exif_transpose(img) or img
    size = preview_size(oriented.width, oriented.height)
    if oriented.mode not in ("RGB", "L"):
        if oriented.mode in ("RGBA", "LA", "P", "PA"):
            oriented = Image.alpha_composite(Image.new("RGBA", oriented.size, (255, 255, 255, 255)),
                                             oriented.convert("RGBA")).convert("RGB")
        else:
            oriented = oriented.convert("RGB")
    resized = oriented.resize(size, Image.Resampling.LANCZOS) if size != oriented.size else oriented
    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(buffer.getvalue())
    tmp.replace(target)
    return {"path": target.name, "width": size[0], "height": size[1], "long_edge_px": max(size),
            "bytes": len(buffer.getvalue()), "orientation_applied": orientation != 1}


def image_record(path: Path, sha256: str, out_dir: Path) -> Dict[str, Any]:
    """Contents of ``image.json`` (and the preview as a side effect); gaps are recorded, not raised."""
    import imagehash
    from PIL import Image, UnidentifiedImageError

    ext = path.suffix.lower().lstrip(".")
    heif = _register_heif() if ext == "heic" else False
    record: Dict[str, Any] = {
        "kind": "image", "source_name": path.name, "extension": ext, "sha256": sha256, "bytes": path.stat().st_size,
        "opened": False, "format": None, "width": None, "height": None, "mode": None, "exif_present": False,
        "exif": {}, "gps_present": False, "gps": None, "phash": None, "preview": None, "caption": None,
        "caption_note": "no caption written here; a model writes captions later",
        "personal_data_fields": list(PERSONAL_DATA_FIELDS),
        "markdown_excludes": list(PERSONAL_DATA_FIELDS), "error": None,
    }
    try:
        with Image.open(path) as img:
            img.load()
            record.update({"opened": True, "format": img.format, "width": img.width, "height": img.height,
                           "mode": img.mode})
            fields, gps = read_exif(img)
            record.update({"exif_present": bool(fields), "exif": fields, "gps_present": gps is not None, "gps": gps})
            record["phash"] = str(imagehash.phash(img))
            record["preview"] = write_preview(img, out_dir / PREVIEW_NAME)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        if ext == "heic" and not heif:
            reason = "HEIC: Pillow has no HEIF plugin (pillow_heif not installed); " + reason
        record["error"] = reason
    return record


def markdown_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """The image record with every personal-data field removed (what the Markdown writer may print)."""
    safe = {k: v for k, v in record.items() if k not in PERSONAL_DATA_FIELDS}
    safe["gps"] = "present (excluded as personal data)" if record.get("gps_present") else None
    return safe


def _image_checks(record: Dict[str, Any], out_dir: Path) -> List[Dict[str, Any]]:
    """Self-checks for an image sidecar."""
    preview = record.get("preview") or {}
    preview_ok = bool(preview) and (out_dir / preview["path"]).is_file() and preview["long_edge_px"] <= PREVIEW_LONG_EDGE
    return [
        {"check": "image opened by Pillow", "pass": record["opened"],
         "evidence": record["error"] or f"{record['format']} {record['width']}x{record['height']}"},
        {"check": f"preview written with long edge <= {PREVIEW_LONG_EDGE} px", "pass": preview_ok,
         "evidence": f"{preview.get('width')}x{preview.get('height')}" if preview else "no preview"},
        {"check": "perceptual hash computed", "pass": bool(record["phash"]), "evidence": record["phash"] or "none"},
        {"check": "GPS flagged as personal data when present",
         "pass": (not record["gps_present"]) or bool((record["gps"] or {}).get("personal_data")),
         "evidence": "GPS present, flagged" if record["gps_present"] else "no GPS"},
    ]


def run_image(path: Path, out_dir: Path, sha256: str) -> Tuple[List[str], Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """Write ``image.json`` and ``preview.jpg``; return ``(files, counts, checks, extra manifest fields)``."""
    record = image_record(path, sha256, out_dir)
    atomic_write_json(out_dir / IMAGE_NAME, record)
    files = [IMAGE_NAME] + ([PREVIEW_NAME] if record["preview"] else [])
    counts = {"width": record["width"], "height": record["height"], "exif_fields": len(record["exif"]),
              "gps_present": record["gps_present"], "previews": 1 if record["preview"] else 0}
    extra = {"kind": "image",
             "converters": [{"name": "pillow", "version": _pillow_version()}, {"name": "imagehash", "version": _imagehash_version()}],
             "methods_used": ["pillow-exif", "imagehash-phash", "pillow-preview"], "warnings": [record["error"]] if record["error"] else []}
    return files, counts, _image_checks(record, out_dir), extra


# --------------------------------------------------------------------------- #
# Other types
# --------------------------------------------------------------------------- #
def other_record(path: Path, sha256: str, rel_path: Optional[str]) -> Dict[str, Any]:
    """Contents of ``no_converter.json``."""
    ext = path.suffix.lower().lstrip(".")
    handled_by = OTHER_MODULES.get(ext)
    return {
        "kind": "other", "source_name": path.name, "extension": ext or None, "sha256": sha256,
        "bytes": path.stat().st_size, "doc_class": classify_document(rel_path or path.name),
        "converter": None,
        "note": (f"no converter in {MODULE_NAME}; this type belongs to {handled_by}" if handled_by
                 else "no converter applies to this type; recorded only"),
        "handled_by": handled_by,
    }


def run_other(path: Path, out_dir: Path, sha256: str, rel_path: Optional[str]
              ) -> Tuple[List[str], Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """Write ``no_converter.json``; return ``(files, counts, checks, extra manifest fields)``."""
    record = other_record(path, sha256, rel_path)
    atomic_write_json(out_dir / OTHER_NAME, record)
    checks = [{"check": "type recorded with no converter", "pass": True,
               "evidence": f".{record['extension'] or 'none'} ({record['doc_class']})"}]
    extra = {"kind": "other", "converters": [], "methods_used": ["none"], "warnings": [record["note"]]}
    return [OTHER_NAME], {"converters": 0}, checks, extra


# --------------------------------------------------------------------------- #
# Ledger, manifest, idempotency
# --------------------------------------------------------------------------- #
def ledger_entry(sha256: str, path: Path, state_dir: Path) -> Optional[Dict[str, Any]]:
    """The pull or upload ledger row for this file, matched by sha256 (then by local path)."""
    rows: List[Dict[str, Any]] = []
    for name in LEDGER_FILES:
        rows.extend(r for r in read_jsonl(state_dir / name) if isinstance(r, dict))
    for row in rows:
        if row.get("sha256") == sha256:
            return row
    resolved = str(path.resolve())
    for row in rows:
        local = row.get("local_path")
        if local and str(Path(local).resolve()) == resolved:
            return row
    return None


def default_out_dir(entry: Optional[Dict[str, Any]]) -> Optional[Path]:
    """``out/sidecar/<rel_path>/`` for a ledger entry, or ``None`` when the file is not in a ledger."""
    if not entry or not entry.get("rel_path"):
        return None
    return PATHS["sidecar"] / str(entry["rel_path"])


def already_done(out_dir: Path, sha256: str) -> bool:
    """True when the manifest records this source hash and module version and every file exists."""
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("sha256") != sha256 or manifest.get("module_version") != MODULE_VERSION:
        return False
    return all((out_dir / f).is_file() for f in manifest.get("files", []))


def _is_under(path: Path, parent: Path) -> bool:
    """True when ``path`` is inside ``parent``."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def run_one(path: Path, out_dir: Path, state_dir: Path) -> Dict[str, Any]:
    """Extract the sidecar for ``path`` into ``out_dir`` and return the manifest."""
    if _is_under(out_dir, PATHS["source"]):
        raise ValueError(f"refusing to write under the locked source tree: {out_dir}")
    sha256 = sha256_file(path)
    entry = ledger_entry(sha256, path, state_dir)
    ensure_dir(out_dir)
    kind, _ext = file_kind(path)
    if kind == "email":
        files, counts, checks, extra = run_email(path, out_dir, sha256)
    elif kind == "image":
        files, counts, checks, extra = run_image(path, out_dir, sha256)
    else:
        files, counts, checks, extra = run_other(path, out_dir, sha256, (entry or {}).get("rel_path"))
    manifest = {
        "module": MODULE_NAME, "module_version": MODULE_VERSION, "generated_at": utc_now_iso(),
        "source_path": str(path), "sha256": sha256, "bytes": path.stat().st_size,
        "rel_path": (entry or {}).get("rel_path"), "drive_id": (entry or {}).get("drive_id"),
        "drive_modified": (entry or {}).get("drive_modified"), "ledger_transport": (entry or {}).get("transport"),
        **extra, "files": files, "counts": counts, "checks": checks,
    }
    atomic_write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


def _olefile_version() -> str:
    """Installed olefile version."""
    try:
        import olefile

        return getattr(olefile, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "missing"


def _pillow_version() -> str:
    """Installed Pillow version."""
    import PIL

    return getattr(PIL, "__version__", "unknown")


def _imagehash_version() -> str:
    """Installed imagehash version."""
    import imagehash

    return getattr(imagehash, "__version__", "unknown")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    """``run`` subcommand: the sidecar for one file; skips when already done for this hash."""
    path = Path(args.path)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1
    state_dir = Path(args.state_dir) if args.state_dir else PATHS["state"]
    sha256 = sha256_file(path)
    out_dir = Path(args.out) if args.out else default_out_dir(ledger_entry(sha256, path, state_dir))
    if out_dir is None:
        print("error: --out is required when the file is not in a ledger", file=sys.stderr)
        return 1
    progress = None if args.no_progress else Progress(args.progress)
    key = f"{MODULE_NAME}:{sha256}"
    if not args.force and already_done(out_dir, sha256):
        print(f"skip (done): {path.name} -> {out_dir}")
        if progress is not None and not progress.is_done(key, sha256):
            progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
        return 0
    if progress is not None:
        progress.set(key, "running", hash=sha256, out_dir=str(out_dir))
    try:
        manifest = run_one(path, out_dir, state_dir)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        if progress is not None:
            progress.set(key, "error", hash=sha256, error=str(exc)[:500])
        print(f"error: {path.name}: {exc}", file=sys.stderr)
        return 1
    if progress is not None:
        progress.set(key, "done", hash=sha256, out_dir=str(out_dir))
    failed = [c["check"] for c in manifest["checks"] if not c["pass"]]
    print(f"done: {path.name} ({manifest['kind']}) -> {out_dir} (files={len(manifest['files'])},"
          f" failed checks={len(failed)})")
    for check in failed:
        print(f"  check failed: {check}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser."""
    parser = argparse.ArgumentParser(description="Sidecar extraction for e-mail, images and other file types.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="write the sidecar for one .msg/.eml, image or other file")
    run_cmd.add_argument("--path", required=True, help="the source file to read")
    run_cmd.add_argument("--out", default=None, help="output directory (default: out/sidecar/<rel_path>/ from the ledger)")
    run_cmd.add_argument("--state-dir", default=None, help="directory holding the ledgers (default: state/)")
    run_cmd.add_argument("--progress", default=None, help="progress.json to update (default: state/progress.json)")
    run_cmd.add_argument("--no-progress", action="store_true", help="do not touch progress.json")
    run_cmd.add_argument("--force", action="store_true", help="rewrite even when already done for this hash")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
