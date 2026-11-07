"""Response bodies: JSON, empty, and the multipart QR code Invoicing v2 returns."""

from __future__ import annotations

import httpx

from backend.app.services.paypal._client import _parse_body
from backend.app.services.paypal.invoices import payer_view_url

BOUNDARY = "7ab705ab-a6cc-4540-b250-51fc14bfe14f"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAJYAAACWAQAAAAAUekxPAAAA"


def multipart(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        content=body.encode(),
    )


def test_qr_code_multipart_is_read_as_fields() -> None:
    # The exact shape of a live sandbox generate-qr-code answer (Nov 7).
    body = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="image"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        f"{PNG_B64}\r\n"
        f"--{BOUNDARY}--\r\n"
    )
    assert _parse_body(multipart(body)) == {"image": PNG_B64}


def test_multipart_without_named_parts_is_empty() -> None:
    assert _parse_body(multipart(f"--{BOUNDARY}--\r\n")) == {}


def test_json_and_empty_bodies() -> None:
    assert _parse_body(httpx.Response(200, json={"id": "X"})) == {"id": "X"}
    assert _parse_body(httpx.Response(204)) == {}


def test_payer_view_comes_from_recipient_view_url() -> None:
    # Invoicing v2 has no payer-view link; the payer URL is in detail.metadata.
    url = "https://www.sandbox.paypal.com/invoice/p/#245ZMVYMCL2EDVMK"
    document = {
        "links": [{"rel": "self", "href": "x"}, {"rel": "qr-code", "href": "y"}],
        "detail": {"metadata": {"recipient_view_url": url}},
    }
    assert payer_view_url(document) == url
    assert payer_view_url({"links": [{"rel": "payer-view", "href": "z"}]}) == "z"
    assert payer_view_url({}) is None
