"""OCR task handler — extracts text from PDF pages (TASK-05)."""
import logging
import tempfile
import threading
import time
from pathlib import Path

import fitz
import httpx

from palimpsest.config import Config
from palimpsest.tasks import PermanentJobError, handler

log = logging.getLogger(__name__)


def _flip_vision_bbox(x: float, y: float, w: float, h: float) -> list[float]:
    """Convert Apple Vision bottom-left normalized bbox to top-left normalized coords.

    Vision convention: (x, y, w, h) with y=0 at page bottom.
    Output convention: [x0, y0, x1, y1] with y=0 at page top.
    """
    return [x, 1.0 - y - h, x + w, 1.0 - y]


def _extract_embedded_text(page: fitz.Page) -> list[dict]:
    """Return text lines from the PDF embedded text layer.

    Each line dict has keys: text (str), bbox ([x0,y0,x1,y1] normalized, top-left),
    conf (float, always 1.0 for embedded text).
    """
    page_width = page.rect.width
    page_height = page.rect.height
    lines: list[dict] = []

    for block in page.get_text("dict").get("blocks", []):  # type: ignore[union-attr]
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            parts: list[str] = []
            x0 = float("inf")
            y0 = float("inf")
            x1 = float("-inf")
            y1 = float("-inf")
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                parts.append(text)
                b = span["bbox"]
                x0 = min(x0, b[0])
                y0 = min(y0, b[1])
                x1 = max(x1, b[2])
                y1 = max(y1, b[3])
            if not parts:
                continue
            lines.append({
                "text": " ".join(parts),
                "bbox": [
                    x0 / page_width,
                    y0 / page_height,
                    x1 / page_width,
                    y1 / page_height,
                ],
                "conf": 1.0,
            })

    return lines


def _ocr_with_vision(png_path: Path) -> list[dict]:
    """OCR via Apple Vision (ocrmac). Flips bbox to top-left origin."""
    from ocrmac.ocrmac import OCR  # macOS-only; callers catch ImportError

    results = OCR(str(png_path), recognition_level="accurate").recognize()
    lines: list[dict] = []
    for text, conf, bbox in results:
        x, y, w, h = bbox
        lines.append({
            "text": text,
            "bbox": _flip_vision_bbox(float(x), float(y), float(w), float(h)),
            "conf": float(conf),
        })
    return lines


def _ocr_with_tesseract(png_path: Path) -> list[dict]:
    """OCR via Tesseract (pytesseract). Normalizes pixel bboxes to 0-1."""
    import pytesseract
    from PIL import Image

    img = Image.open(png_path)
    img_w, img_h = img.size
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    lines: list[dict] = []
    for i, raw_text in enumerate(data["text"]):
        text = raw_text.strip()
        if not text:
            continue
        raw_conf = float(data["conf"][i])
        if raw_conf < 0:
            continue
        px, py, pw, ph = (
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
        )
        lines.append({
            "text": text,
            "bbox": [
                px / img_w,
                py / img_h,
                (px + pw) / img_w,
                (py + ph) / img_h,
            ],
            "conf": raw_conf / 100.0,
        })
    return lines


def _ocr_page(
    png_path: Path,
    engine_preference: list[str],
) -> tuple[list[dict], str]:
    """Try each OCR engine in preference order.

    Returns (lines, engine_name) for the first engine that succeeds.
    Raises RuntimeError if all engines fail — broker will retry on another node.
    """
    errors: list[str] = []
    for engine in engine_preference:
        try:
            if engine == "vision":
                return _ocr_with_vision(png_path), "vision"
            if engine == "tesseract":
                return _ocr_with_tesseract(png_path), "tesseract"
        except ImportError as exc:
            log.warning("OCR engine %r unavailable: %s", engine, exc)
            errors.append("%s: %s" % (engine, exc))
        except Exception as exc:
            log.warning("OCR engine %r failed: %s", engine, exc)
            errors.append("%s: %s" % (engine, exc))
    raise RuntimeError("All OCR engines failed: %s" % "; ".join(errors))


def _filter_and_sort_lines(lines: list[dict], min_confidence: float) -> list[dict]:
    """Drop lines below min_confidence; sort top-to-bottom then left-to-right."""
    filtered = [line for line in lines if line["conf"] >= min_confidence]
    filtered.sort(key=lambda line: (round(line["bbox"][1], 2), line["bbox"][0]))
    return filtered


@handler("ocr")
def handle_ocr(
    cfg: Config,
    job: dict,
    *,
    lost_evt: threading.Event | None = None,
    shutdown_event: threading.Event | None = None,
) -> list[dict]:
    """Produce the page-array JSON (§6) for a document.

    Fetches the PDF from the broker, uses embedded text where available,
    falls back to OCR (Vision → Tesseract) for image-only pages.

    lost_evt / shutdown_event are checked between pages so the worker can
    abort a long OCR run as soon as the broker revokes the lease or a
    SIGTERM is received, rather than grinding through all remaining pages
    only to discard the result.
    """
    doc_id: str = job["doc_id"]
    broker_url = "http://%s:%s" % (cfg.broker["host"], cfg.broker["port"])
    min_confidence: float = float(cfg.ocr["min_confidence"])
    engine_preference: list[str] = list(cfg.ocr["engine_preference"])
    min_text_len: int = int(cfg.ocr["rerun_if_osti_text_shorter_than"])

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        pdf_path = tmp / ("%s.pdf" % doc_id)

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.get("%s/file/%s.pdf" % (broker_url, doc_id))
                resp.raise_for_status()
                pdf_path.write_bytes(resp.content)
        except httpx.HTTPStatusError as exc:
            raise PermanentJobError("unreadable pdf: HTTP %s fetching %s" % (exc.response.status_code, doc_id)) from exc

        try:
            doc = fitz.open(str(pdf_path))
            encrypted = doc.is_encrypted
            page_count = doc.page_count
        except Exception as exc:
            raise PermanentJobError("unreadable pdf: %s" % exc) from exc

        if encrypted:
            raise PermanentJobError("unreadable pdf: encrypted")

        if page_count == 0:
            raise PermanentJobError("unreadable pdf: 0 pages")

        pages: list[dict] = []

        for idx in range(page_count):
            # Check for lease loss or shutdown before each page so we free
            # unified memory as soon as possible instead of finishing a dead job.
            if lost_evt is not None and lost_evt.is_set():
                log.warning(
                    "doc %s: lease lost, aborting OCR after %d/%d pages",
                    doc_id, idx, page_count,
                )
                break
            if shutdown_event is not None and shutdown_event.is_set():
                log.info(
                    "doc %s: shutdown requested, aborting OCR after %d/%d pages",
                    doc_id, idx, page_count,
                )
                break

            page_no = idx + 1
            page = doc[idx]
            width: float = page.rect.width
            height: float = page.rect.height

            t_start = time.monotonic()

            embedded = _extract_embedded_text(page)
            total_len = sum(len(line["text"]) for line in embedded)

            if total_len >= min_text_len:
                filtered = _filter_and_sort_lines(embedded, min_confidence)
                ocr_source = "osti"
            else:
                png_path = tmp / ("%s_p%d.png" % (doc_id, page_no))
                page.get_pixmap(dpi=300).save(str(png_path))
                ocr_lines, ocr_source = _ocr_page(png_path, engine_preference)
                filtered = _filter_and_sort_lines(ocr_lines, min_confidence)

            elapsed = time.monotonic() - t_start
            if elapsed > 5.0:
                log.warning("doc %s page %d took %.1fs (> 5s threshold on Metal)", doc_id, page_no, elapsed)

            pages.append({
                "page_no": page_no,
                "width": width,
                "height": height,
                "ocr_source": ocr_source,
                "lines": filtered,
                "text": "\n".join(line["text"] for line in filtered),
            })

        return pages
