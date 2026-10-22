"""TST-04 (parser half): upload validation and text extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.ai.pdf_parser import (
    MAX_UPLOAD_BYTES,
    ContractParseError,
    extract_text_from_upload,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "contracts"

# Key facts each fixture's extracted text must contain.
EXPECTED = {
    "Acme_Standard": [
        "Acme Corp",
        "$1,000.00",
        "October 15, 2026",
        "5.00%",
        "sb-buyer@personal.example.com",
    ],
    "Globex_LateFee": [
        "Globex Industries Inc.",
        "$2,500.00",
        "November 1, 2026",
        "7.00%",
    ],
    "Initech_MultiMilestone": [
        "Initech LLC",
        "$1,200.00",
        "$1,800.00",
        "$900.00",
        "MS-03",
    ],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_pdfs_extract_key_facts(name: str) -> None:
    text = extract_text_from_upload(
        (CONTRACTS / f"{name}.pdf").read_bytes(), f"{name}.pdf"
    )
    for fact in EXPECTED[name]:
        assert fact in text, f"{fact!r} missing from {name}.pdf"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_markdown_extracts_key_facts(name: str) -> None:
    text = extract_text_from_upload(
        (CONTRACTS / f"{name}.md").read_bytes(), f"{name}.md"
    )
    for fact in EXPECTED[name]:
        assert fact in text


def test_txt_is_accepted_and_extension_is_case_insensitive() -> None:
    assert (
        extract_text_from_upload(b"Pay $10.00 by Jan 1.", "CONTRACT.TXT")
        == "Pay $10.00 by Jan 1."
    )


def test_text_is_normalised() -> None:
    raw = b"Line one\r\nLine\t\ttwo   spaced  \r\n\r\n\r\n\r\nPara\x00graph\x07"
    assert (
        extract_text_from_upload(raw, "c.md")
        == "Line one\nLine two spaced\n\nParagraph"
    )


def test_utf8_bom_is_stripped() -> None:
    assert extract_text_from_upload("\ufeffHello".encode(), "c.txt") == "Hello"


@pytest.mark.parametrize(
    ("data", "filename", "reason"),
    [
        (b"", "c.pdf", "empty"),
        (b"", "c.md", "empty"),
        (b"MZ\x90\x00binary", "malware.exe", "unsupported"),
        (b"MZ\x90\x00binary", "noextension", "unsupported"),
        (b"MZ\x90\x00binary", "renamed.pdf", "not_pdf"),
        (b"%PDF-1.7\nthis is not really a pdf", "broken.pdf", "unreadable"),
        (b"\xff\xfe\xfa invalid utf8", "c.md", "encoding"),
        (b"   \n\t\r\n  ", "c.txt", "no_text"),
    ],
)
def test_bad_uploads_raise_typed_error(data: bytes, filename: str, reason: str) -> None:
    with pytest.raises(ContractParseError) as exc:
        extract_text_from_upload(data, filename)
    assert exc.value.reason == reason


def test_oversized_upload_rejected_before_parsing() -> None:
    with pytest.raises(ContractParseError) as exc:
        extract_text_from_upload(b"%PDF-" + b"0" * MAX_UPLOAD_BYTES, "huge.pdf")
    assert exc.value.reason == "too_large"


def test_exactly_max_size_is_allowed() -> None:
    data = b"a" * MAX_UPLOAD_BYTES
    assert len(extract_text_from_upload(data, "big.txt")) == MAX_UPLOAD_BYTES


def test_error_message_is_user_safe() -> None:
    with pytest.raises(ContractParseError) as exc:
        extract_text_from_upload(b"", "c.pdf")
    assert "empty" in str(exc.value).lower()
