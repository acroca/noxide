"""Attachment content extraction: local text first, vision fallback for scans/images.

Digital PDFs and text files are read locally (free, exact). Scanned PDFs are
rendered to page images and transcribed by the Copilot vision model; stored
images go through the same vision path. Results are capped to protect the
agent's context window.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import TYPE_CHECKING, Any

from . import copilot, usage

if TYPE_CHECKING:
    from .tools import VaultTools

logger = logging.getLogger(__name__)

_MAX_CHARS = 20_000
_MAX_TEXT_PAGES = 20
# Vision requests carry base64 page images — keep the payload well under API limits
_MAX_VISION_PAGES = 8
_RENDER_SCALE = 2.0  # 72 dpi * 2 ≈ 144 dpi, plenty for text
_JPEG_QUALITY = 80
# Below this average per examined page, the text layer is considered absent
# (scanned PDFs yield ~nothing; even sparse digital ones clear this easily)
_MIN_TEXT_CHARS_PER_PAGE = 5

_TEXT_EXTS = {"txt", "md", "csv", "json", "yaml", "yml", "toml", "log"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}

_TRANSCRIBE_SYSTEM_PROMPT = (
    "You transcribe documents. Output only the document's textual content, "
    "faithfully and in reading order, using markdown for structure (headings, "
    "lists, tables). Do not add commentary, explanations or preamble."
)
_IMAGE_SYSTEM_PROMPT = (
    "You describe images. Transcribe any text in the image verbatim, then "
    "briefly describe what the image shows. No preamble."
)


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_CHARS:
        return text
    return text[:_MAX_CHARS] + f"\n\n[truncated at {_MAX_CHARS} characters]"


class AttachmentExtractor:
    """Extracts readable content from files stored in the vault."""

    def __init__(self, vault: VaultTools) -> None:
        self._vault = vault

    async def extract(self, path: str) -> str:
        """Return the textual content of a vault attachment.

        Errors come back as bracketed strings (the agent loop's convention),
        except PermissionError which the loop already converts.
        """
        abs_path = self._vault.abs_path(path)  # raises PermissionError on escape
        if not abs_path.is_file():
            return f"[file not found: {path}]"

        ext = abs_path.suffix.lstrip(".").lower()
        if ext in _TEXT_EXTS:
            return _truncate(abs_path.read_text(encoding="utf-8", errors="replace"))
        if ext == "pdf":
            return await self._extract_pdf(abs_path.read_bytes())
        if ext in _IMAGE_EXTS:
            return await self._extract_image(abs_path.read_bytes(), ext)
        return f"[cannot extract .{ext} files — supported: pdf, images, plain text]"

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    async def _extract_pdf(self, data: bytes) -> str:
        try:
            n_pages, text = await asyncio.to_thread(_pdf_text_layer, data)
        except Exception as e:
            return f"[could not parse PDF: {e}]"

        pages_examined = max(1, min(n_pages, _MAX_TEXT_PAGES))
        if len(text) >= _MIN_TEXT_CHARS_PER_PAGE * pages_examined:
            header = f"[PDF, {n_pages} page(s), text layer"
            if n_pages > _MAX_TEXT_PAGES:
                header += f", first {_MAX_TEXT_PAGES} pages only"
            return f"{header}]\n\n{_truncate(text)}"

        # No usable text layer: scanned document — render pages and use vision
        try:
            images = await asyncio.to_thread(_render_pdf_pages, data)
        except Exception as e:
            return f"[could not render scanned PDF: {e}]"
        transcript = await self._vision(
            _TRANSCRIBE_SYSTEM_PROMPT,
            f"Transcribe these {len(images)} page(s) of a scanned document.",
            [("jpeg", img) for img in images],
        )
        header = f"[PDF, {n_pages} page(s), scanned — transcribed via vision"
        if n_pages > _MAX_VISION_PAGES:
            header += f", first {_MAX_VISION_PAGES} pages only"
        return f"{header}]\n\n{_truncate(transcript)}"

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    async def _extract_image(self, data: bytes, ext: str) -> str:
        mime_ext = "jpeg" if ext == "jpg" else ext
        described = await self._vision(
            _IMAGE_SYSTEM_PROMPT,
            "Transcribe any text in this image, then describe it briefly.",
            [(mime_ext, data)],
        )
        return f"[image, described via vision]\n\n{_truncate(described)}"

    # ------------------------------------------------------------------
    # Vision call
    # ------------------------------------------------------------------

    async def _vision(self, system: str, instruction: str, images: list[tuple[str, bytes]]) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for mime_ext, data in images:
            url = f"data:image/{mime_ext};base64," + base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        response = await copilot.get_client().chat([
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ], initiator="agent")
        usage.record("vision", response.get("model", ""), response.get("usage", {}))
        return response["choices"][0]["message"].get("content") or "[vision model returned no text]"


# ----------------------------------------------------------------------
# Sync helpers (run in a thread: CPU-bound, must not block the event loop)
# ----------------------------------------------------------------------


def _pdf_text_layer(data: bytes) -> tuple[int, str]:
    """Return (page count, text-layer content of the first _MAX_TEXT_PAGES pages)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages
    chunks = [page.extract_text() or "" for page in pages[:_MAX_TEXT_PAGES]]
    return len(pages), "\n\n".join(chunks).strip()


def _render_pdf_pages(data: bytes) -> list[bytes]:
    """Render the first _MAX_VISION_PAGES pages to JPEG bytes."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        images: list[bytes] = []
        for i in range(min(len(pdf), _MAX_VISION_PAGES)):
            bitmap = pdf[i].render(scale=_RENDER_SCALE)
            pil_image = bitmap.to_pil().convert("RGB")
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
            images.append(buf.getvalue())
        return images
    finally:
        pdf.close()
