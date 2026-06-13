"""Tests for OCR task handler (TASK-05)."""
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest
from PIL import Image

from palimpsest.config import Config
from palimpsest.tasks import PermanentJobError
from palimpsest.tasks.ocr import (
    _filter_and_sort_lines,
    _flip_vision_bbox,
    handle_ocr,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(engine_preference: list[str] | None = None, min_confidence: float = 0.5) -> Config:
    if engine_preference is None:
        engine_preference = ["vision", "tesseract"]
    return Config(
        raw={},
        storage_root=Path("/tmp/pal"),
        db_path=Path("/tmp/pal/db.db"),
        broker={"host": "localhost", "port": 8077, "lease_ttl_seconds": 900,
                "heartbeat_seconds": 120, "max_attempts": 3},
        mcp={"port": 8078},
        harvest={},
        ocr={
            "engine_preference": engine_preference,
            "min_confidence": min_confidence,
            "rerun_if_osti_text_shorter_than": 200,
        },
        features={},
        embed={},
        gapjoin={},
        models={"classify": "q", "extract": "q", "keep_alive": "24h"},
        nodes={},
        orchestrator={},
        eval={},
    )


def _make_text_pdf() -> bytes:
    """Create a tiny PDF with > 200 chars of embedded text (wrapped via textbox)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # insert_textbox wraps text so all chars are preserved in the text layer
    page.insert_textbox(fitz.Rect(72, 72, 540, 720), "Testing " * 40, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _make_image_pdf() -> bytes:
    """Create a tiny PDF whose page contains only a raster image (no text layer)."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=50)
    img = Image.new("RGB", (200, 50), color=(255, 255, 255))
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)
    page.insert_image(fitz.Rect(0, 0, 200, 50), stream=img_buf.read())
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _make_blank_pdf() -> bytes:
    """Create a single-page PDF with no content at all."""
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _mock_http_client(pdf_bytes: bytes) -> MagicMock:
    """Return a mock context-manager httpx.Client that returns pdf_bytes on GET."""
    resp = MagicMock()
    resp.content = pdf_bytes
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# Vision coordinate flip
# ---------------------------------------------------------------------------

def test_flip_vision_bbox_spec_example():
    """Vision (x=0.1, y=0.2, w=0.3, h=0.05) → top-left [0.1, 0.75, 0.4, 0.8]."""
    assert _flip_vision_bbox(0.1, 0.2, 0.3, 0.05) == pytest.approx([0.1, 0.75, 0.4, 0.8])


def test_flip_vision_bbox_top_strip():
    """Vision bbox at page top (y near 1.0) → top-left y0 near 0."""
    # Vision bottom-left (0, 0.9, 0.1, 0.1) → top-left [0, 0, 0.1, 0.1]
    assert _flip_vision_bbox(0.0, 0.9, 0.1, 0.1) == pytest.approx([0.0, 0.0, 0.1, 0.1])


def test_flip_vision_bbox_bottom_strip():
    """Vision bbox at page bottom (y=0) → top-left y0 near 1."""
    # Vision (0, 0, 0.5, 0.2) → top-left [0, 0.8, 0.5, 1.0]
    assert _flip_vision_bbox(0.0, 0.0, 0.5, 0.2) == pytest.approx([0.0, 0.8, 0.5, 1.0])


# ---------------------------------------------------------------------------
# Confidence filter and sort
# ---------------------------------------------------------------------------

def test_filter_removes_low_confidence_lines():
    lines = [
        {"text": "keep", "bbox": [0.1, 0.1, 0.5, 0.2], "conf": 0.9},
        {"text": "drop", "bbox": [0.1, 0.3, 0.5, 0.4], "conf": 0.3},
    ]
    result = _filter_and_sort_lines(lines, min_confidence=0.5)
    assert [ln["text"] for ln in result] == ["keep"]


def test_filter_keeps_lines_at_threshold():
    lines = [{"text": "exact", "bbox": [0.0, 0.0, 1.0, 0.1], "conf": 0.5}]
    result = _filter_and_sort_lines(lines, min_confidence=0.5)
    assert len(result) == 1


def test_sort_top_to_bottom_then_left_to_right():
    # top-left and top-right both have y0 that rounds to 0.10, so same row → sorted by x0.
    # bottom-left has y0=0.80, a clearly different row.
    lines = [
        {"text": "bottom-left", "bbox": [0.1, 0.80,  0.4, 0.90], "conf": 0.9},
        {"text": "top-right",   "bbox": [0.6, 0.100, 0.9, 0.20], "conf": 0.9},
        {"text": "top-left",    "bbox": [0.1, 0.104, 0.4, 0.21], "conf": 0.9},
    ]
    result = _filter_and_sort_lines(lines, min_confidence=0.5)
    assert [ln["text"] for ln in result] == ["top-left", "top-right", "bottom-left"]


def test_sort_same_row_by_x0():
    """Lines whose y0 rounds to the same 2-decimal value are ordered by x0."""
    # Both y0 values round to 0.10 → same row, so x0 breaks the tie.
    lines = [
        {"text": "right", "bbox": [0.7, 0.100, 0.9, 0.12], "conf": 0.9},
        {"text": "left",  "bbox": [0.1, 0.104, 0.3, 0.13], "conf": 0.9},
    ]
    result = _filter_and_sort_lines(lines, min_confidence=0.5)
    assert [ln["text"] for ln in result] == ["left", "right"]


# ---------------------------------------------------------------------------
# handle_ocr — embedded text path
# ---------------------------------------------------------------------------

@patch("palimpsest.tasks.ocr.httpx.Client")
def test_embedded_text_chosen_when_long_enough(mock_client_cls: MagicMock) -> None:
    """When embedded text >= 200 chars, ocr_source must be 'osti'."""
    mock_client_cls.return_value = _mock_http_client(_make_text_pdf())
    pages = handle_ocr(_make_config(), {"doc_id": "11111", "job_id": 1, "type": "ocr"})
    assert len(pages) == 1
    assert pages[0]["ocr_source"] == "osti"
    assert len(pages[0]["text"]) >= 200


# ---------------------------------------------------------------------------
# handle_ocr — OCR fallback path
# ---------------------------------------------------------------------------

@patch("palimpsest.tasks.ocr.httpx.Client")
def test_image_only_falls_back_to_ocr(mock_client_cls: MagicMock) -> None:
    """Image-only PDF (< 200 chars embedded) must invoke an OCR engine."""
    mock_client_cls.return_value = _mock_http_client(_make_image_pdf())
    fake_lines = [{"text": "hello", "bbox": [0.1, 0.1, 0.8, 0.3], "conf": 0.95}]

    with patch("palimpsest.tasks.ocr._ocr_page", return_value=(fake_lines, "vision")) as mock_ocr:
        pages = handle_ocr(
            _make_config(engine_preference=["vision"]),
            {"doc_id": "22222", "job_id": 2, "type": "ocr"},
        )
    assert mock_ocr.called
    assert pages[0]["ocr_source"] == "vision"
    assert pages[0]["lines"][0]["text"] == "hello"


# ---------------------------------------------------------------------------
# handle_ocr — blank page
# ---------------------------------------------------------------------------

@patch("palimpsest.tasks.ocr.httpx.Client")
def test_blank_page_produces_valid_empty_page(mock_client_cls: MagicMock) -> None:
    """A page with no text is valid: empty lines list and empty text string."""
    mock_client_cls.return_value = _mock_http_client(_make_blank_pdf())
    with patch("palimpsest.tasks.ocr._ocr_page", return_value=([], "vision")):
        pages = handle_ocr(_make_config(), {"doc_id": "33333", "job_id": 3, "type": "ocr"})
    assert len(pages) == 1
    assert pages[0]["lines"] == []
    assert pages[0]["text"] == ""


# ---------------------------------------------------------------------------
# handle_ocr — confidence filter applied end-to-end
# ---------------------------------------------------------------------------

@patch("palimpsest.tasks.ocr.httpx.Client")
def test_confidence_filter_applied_in_ocr_path(mock_client_cls: MagicMock) -> None:
    """Lines below min_confidence must not appear in the result."""
    mock_client_cls.return_value = _mock_http_client(_make_image_pdf())
    mock_lines = [
        {"text": "good", "bbox": [0.1, 0.1, 0.8, 0.2], "conf": 0.9},
        {"text": "bad",  "bbox": [0.1, 0.3, 0.8, 0.4], "conf": 0.2},
    ]
    with patch("palimpsest.tasks.ocr._ocr_page", return_value=(mock_lines, "vision")):
        pages = handle_ocr(
            _make_config(engine_preference=["vision"], min_confidence=0.5),
            {"doc_id": "44444", "job_id": 4, "type": "ocr"},
        )
    texts = [ln["text"] for ln in pages[0]["lines"]]
    assert "good" in texts
    assert "bad" not in texts


# ---------------------------------------------------------------------------
# handle_ocr — error cases
# ---------------------------------------------------------------------------

@patch("palimpsest.tasks.ocr.httpx.Client")
def test_corrupt_pdf_raises_permanent_error(mock_client_cls: MagicMock) -> None:
    """Corrupt PDF bytes must raise PermanentJobError matching 'unreadable pdf'."""
    mock_client_cls.return_value = _mock_http_client(b"this is not a pdf")
    with pytest.raises(PermanentJobError, match="unreadable pdf"):
        handle_ocr(_make_config(), {"doc_id": "55555", "job_id": 5, "type": "ocr"})


@patch("palimpsest.tasks.ocr.httpx.Client")
@patch("palimpsest.tasks.ocr.fitz.open")
def test_zero_page_pdf_raises_permanent_error(
    mock_fitz_open: MagicMock, mock_client_cls: MagicMock
) -> None:
    """A document with page_count=0 must raise PermanentJobError."""
    mock_client_cls.return_value = _mock_http_client(b"stub")
    mock_doc = MagicMock()
    mock_doc.is_encrypted = False
    mock_doc.page_count = 0
    mock_fitz_open.return_value = mock_doc
    with pytest.raises(PermanentJobError, match="0 pages"):
        handle_ocr(_make_config(), {"doc_id": "66666", "job_id": 6, "type": "ocr"})


@patch("palimpsest.tasks.ocr.httpx.Client")
def test_page_object_has_required_schema_fields(mock_client_cls: MagicMock) -> None:
    """Every returned page object must have the §6 schema fields."""
    mock_client_cls.return_value = _mock_http_client(_make_text_pdf())
    pages = handle_ocr(_make_config(), {"doc_id": "77777", "job_id": 7, "type": "ocr"})
    for page in pages:
        assert "page_no" in page
        assert "width" in page
        assert "height" in page
        assert "ocr_source" in page
        assert "lines" in page
        assert "text" in page
