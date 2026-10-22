"""Upload validation and contract text extraction.

First stop for every uploaded contract. Accepts ``.pdf`` (``pypdf``) and ``.md`` /
``.txt`` (strict UTF-8). Anything else raises ``ContractParseError``, which the API
layer maps to HTTP 422. The returned text is untrusted data, never instructions.
"""

from __future__ import annotations

import io
import re
from pathlib import PurePath

from pypdf import PdfReader

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
TEXT_EXTENSIONS = frozenset({".md", ".txt"})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf"}

# Every C0/C1 control character except newline and tab.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_INLINE_WHITESPACE = re.compile(r"[ \t\xa0]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


class ContractParseError(ValueError):
    """An upload that cannot be turned into contract text.

    ``reason`` is a stable machine code; ``str(error)`` is safe to show the user.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def extract_text_from_upload(file_bytes: bytes, filename: str) -> str:
    if not file_bytes:
        raise ContractParseError("empty", "The uploaded file is empty.")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ContractParseError(
            "too_large", "The uploaded file exceeds the 5 MB limit."
        )

    extension = PurePath(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ContractParseError(
            "unsupported", "Unsupported file type. Upload a .pdf, .md or .txt contract."
        )

    if extension == ".pdf":
        if not file_bytes.startswith(PDF_MAGIC):
            raise ContractParseError(
                "not_pdf", "The file is named .pdf but is not a PDF."
            )
        raw = _read_pdf(file_bytes)
    else:
        try:
            raw = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ContractParseError(
                "encoding", "Text contracts must be UTF-8 encoded."
            ) from exc

    text = normalise_text(raw)
    if not text:
        raise ContractParseError(
            "no_text", "No readable text was found in the contract."
        )
    return text


def normalise_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = _CONTROL_CHARS.sub("", text)
    lines = (_INLINE_WHITESPACE.sub(" ", line).strip() for line in text.split("\n"))
    return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _read_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted:
            raise ContractParseError(
                "unreadable", "Password-protected PDFs are not supported."
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except ContractParseError:
        raise
    except Exception as exc:  # pypdf raises many unrelated types on corrupt input
        raise ContractParseError("unreadable", "The PDF could not be read.") from exc
    if not pages:
        raise ContractParseError("unreadable", "The PDF could not be read.")
    return "\n\n".join(pages)
