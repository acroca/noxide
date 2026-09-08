"""Tests for attachment content extraction (local parse + mocked vision fallback)."""

from __future__ import annotations

import asyncio
import io
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.extract import AttachmentExtractor, _render_pdf_pages
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


async def test_mixed_pdf_preserves_text_and_warns_about_scanned_pages(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    from pypdf import PdfReader, PdfWriter

    pdf = PdfWriter()
    pdf.append(PdfReader(io.BytesIO(_digital_pdf("Important digital content"))))
    pdf.add_blank_page(width=612, height=792)
    pdf.append(PdfReader(io.BytesIO(_digital_pdf("Final digital content"))))
    pdf.write(tmp_path / "attachments/mixed.pdf")
    client = _vision_client("should not be called")

    with patch("assistant.copilot.get_client", return_value=client):
        result = await extractor.extract("attachments/mixed.pdf")

    assert "Important digital content" in result
    assert "Final digital content" in result
    assert "partial extraction: pages 2 have little or no text layer" in result
    assert "has not been transcribed" in result
    client.chat.assert_not_awaited()


async def test_mixed_pdf_warning_survives_character_and_page_caps(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    from pypdf import PdfReader, PdfWriter

    pdf = PdfWriter()
    pdf.append(PdfReader(io.BytesIO(_digital_pdf("x" * 25_000))))
    for _ in range(20):
        pdf.add_blank_page(width=612, height=792)
    pdf.write(tmp_path / "attachments/long.pdf")

    result = await extractor.extract("attachments/long.pdf")

    assert "21 page(s)" in result
    assert "first 20 pages only" in result
    assert "partial extraction: pages 2, 3" in result
    assert "19, 20 have little or no text layer" in result
    assert "[truncated at 20000 characters]" in result
    assert len(result) < 21_000


async def test_scanned_pdf_keeps_vision_page_cap(
    extractor: AttachmentExtractor, tmp_path: Path
) -> None:
    from pypdf import PdfWriter

    pdf = PdfWriter()
    for _ in range(9):
        pdf.add_blank_page(width=72, height=72)
    pdf.write(tmp_path / "attachments/scan.pdf")
    client = _vision_client("x" * 25_000)

    with patch("assistant.copilot.get_client", return_value=client):
        result = await extractor.extract("attachments/scan.pdf")

    assert "first 8 pages only" in result
    assert "[truncated at 20000 characters]" in result
    content = client.chat.call_args.args[0][-1]["content"]
    assert len([part for part in content if part["type"] == "image_url"]) == 8


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_pdfium_serialized_even_when_await_is_cancelled(cancel_first: bool) -> None:
    """A cancelled await must not let another worker enter PDFium early."""
    entered = threading.Event()
    release = threading.Event()
    second_waiting = threading.Event()
    native_closed = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                second_waiting.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    def open_document(data):
        if data == b"first":
            entered.set()
            assert release.wait(5), "test did not release first worker"
        else:
            assert native_closed.is_set(), "second document opened before first closed"
        pdf = MagicMock()
        pdf.__len__.return_value = 0
        pdf.close.side_effect = native_closed.set
        return pdf

    with patch("assistant.extract._PDFIUM_LOCK", ObservedLock()), \
         patch("pypdfium2.PdfDocument", side_effect=open_document) as constructor:
        first = asyncio.create_task(asyncio.to_thread(_render_pdf_pages, b"first"))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            second = asyncio.create_task(asyncio.to_thread(_render_pdf_pages, b"second"))
            assert await asyncio.to_thread(second_waiting.wait, 5)
            assert constructor.call_count == 1
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            assert await asyncio.to_thread(native_closed.wait, 5)
        assert second is not None
        assert second.result() == []
        assert constructor.call_count == 2


def test_pdfium_closes_native_objects_and_releases_lock_on_error() -> None:
    pdf = MagicMock()
    pdf.__len__.return_value = 1
    page = pdf.__getitem__.return_value
    bitmap = page.render.return_value
    bitmap.to_pil.side_effect = RuntimeError("conversion failed")
    lock = threading.Lock()

    with patch("assistant.extract._PDFIUM_LOCK", lock), \
         patch("pypdfium2.PdfDocument", return_value=pdf):
        with pytest.raises(RuntimeError, match="conversion failed"):
            _render_pdf_pages(b"pdf")

    bitmap.close.assert_called_once()
    page.close.assert_called_once()
    pdf.close.assert_called_once()
    assert not lock.locked()


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
