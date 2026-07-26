"""Tests for attachment content extraction (local parse + mocked vision fallback)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.extract import AttachmentExtractor
from assistant.tools import VaultTools


def _digital_pdf(text: str = "Hello Vault") -> bytes:
    """Build a minimal one-page PDF with a real text layer (valid xref)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def _scanned_pdf() -> bytes:
    """A one-page PDF with no text layer (as a scanner would produce)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(612, 792)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _vision_client(reply: str) -> MagicMock:
    client = MagicMock()
    client.chat = AsyncMock(
        return_value={"choices": [{"message": {"role": "assistant", "content": reply}}]}
    )
    return client


@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    (tmp_path / "attachments").mkdir()
    return VaultTools(tmp_path)


@pytest.fixture
def extractor(vault: VaultTools) -> AttachmentExtractor:
    return AttachmentExtractor(vault)


async def test_digital_pdf_uses_text_layer_without_vision(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/doc.pdf").write_bytes(_digital_pdf("Hello Vault"))
    client = _vision_client("should not be called")

    with patch("assistant.copilot.get_client", return_value=client):
        result = await extractor.extract("attachments/doc.pdf")

    assert "Hello Vault" in result
    assert "text layer" in result
    client.chat.assert_not_awaited()


async def test_scanned_pdf_falls_back_to_vision(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/scan.pdf").write_bytes(_scanned_pdf())
    client = _vision_client("Invoice #42 — total 99 EUR")

    with patch("assistant.copilot.get_client", return_value=client):
        result = await extractor.extract("attachments/scan.pdf")

    assert "Invoice #42" in result
    assert "vision" in result
    # The vision request must carry the rendered page as an image data URL
    messages = client.chat.call_args.args[0]
    user_content = messages[-1]["content"]
    images = [p for p in user_content if p["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_vision_requests_are_agent_initiated(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    """Vision extraction serves an ongoing user turn — its calls must not be
    billed as user-initiated premium requests."""
    (tmp_path / "attachments/scan.pdf").write_bytes(_scanned_pdf())
    client = _vision_client("Invoice #42")

    with patch("assistant.copilot.get_client", return_value=client):
        await extractor.extract("attachments/scan.pdf")

    assert client.chat.call_args.kwargs["initiator"] == "agent"


async def test_image_attachment_is_described_via_vision(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/photo.jpg").write_bytes(_jpeg_bytes())
    client = _vision_client("A white rectangle.")

    with patch("assistant.copilot.get_client", return_value=client):
        result = await extractor.extract("attachments/photo.jpg")

    assert "A white rectangle." in result
    messages = client.chat.call_args.args[0]
    images = [p for p in messages[-1]["content"] if p["type"] == "image_url"]
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_text_file_is_read_directly(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/notes.txt").write_text("plain contents")

    result = await extractor.extract("attachments/notes.txt")

    assert result == "plain contents"


async def test_long_content_is_truncated(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/big.txt").write_text("x" * 50_000)

    result = await extractor.extract("attachments/big.txt")

    assert len(result) < 25_000
    assert "[truncated at 20000 characters]" in result


async def test_unsupported_extension_returns_error_string(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/archive.zip").write_bytes(b"PK\x03\x04")

    result = await extractor.extract("attachments/archive.zip")

    assert result.startswith("[cannot extract .zip")


async def test_missing_file_returns_sentinel(extractor: AttachmentExtractor) -> None:
    result = await extractor.extract("attachments/nope.pdf")

    assert result.startswith("[file not found")


async def test_path_escape_raises_permission_error(extractor: AttachmentExtractor) -> None:
    with pytest.raises(PermissionError):
        await extractor.extract("../../etc/passwd")


async def test_corrupt_pdf_returns_error_string(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/broken.pdf").write_bytes(b"%PDF-1.4 garbage")

    result = await extractor.extract("attachments/broken.pdf")

    assert result.startswith("[could not")


async def test_vision_records_usage_event(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    (tmp_path / "attachments/photo.jpg").write_bytes(_jpeg_bytes())
    client = _vision_client("A white rectangle.")
    client.chat.return_value["model"] = "gpt-4o"
    client.chat.return_value["usage"] = {"prompt_tokens": 700, "completion_tokens": 30}

    with patch("assistant.copilot.get_client", return_value=client), \
         patch("assistant.extract.usage") as mock_usage:
        await extractor.extract("attachments/photo.jpg")

    mock_usage.record.assert_called_once_with(
        "vision", "gpt-4o", {"prompt_tokens": 700, "completion_tokens": 30}
    )
