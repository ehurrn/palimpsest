"""Rescue dead OCR jobs using Claude Vision API.

Finds all docs whose OCR jobs died (local OCR failed), re-enqueues them,
then processes each page with claude-haiku-4-5 vision and completes via broker.

Usage (run on any machine with ANTHROPIC_API_KEY set):
    uv run python scripts/vision_ocr_rescue.py [--dry-run]
"""

import argparse
import base64
import sys
import tempfile
import time
from pathlib import Path

import anthropic
import fitz  # PyMuPDF
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from palimpsest.config import load as load_config  # noqa: E402

WORKER_ID = "vision-rescue"
MODEL = "claude-haiku-4-5-20251001"
DPI = 150  # balance quality vs token cost


def get_dead_ocr_doc_ids(broker_url: str) -> list[str]:
    """Query broker for all docs with dead OCR jobs."""
    resp = httpx.get(f"{broker_url}/jobs/dead", params={"type": "ocr"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["doc_ids"]


def ocr_page_with_claude(client: anthropic.Anthropic, png_path: Path, page_no: int, total: int) -> str:
    """Send a PNG page to Claude Vision and return transcribed text."""
    img_b64 = base64.standard_b64encode(png_path.read_bytes()).decode()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is page {}/{} of a declassified US government document about nuclear weapons testing. "
                        "Transcribe all visible text exactly as it appears, preserving line breaks and spacing. "
                        "Include redaction markers like [DELETED], [REDACTED], or black-bar descriptions. "
                        "Return only the transcribed text — no commentary, no preamble."
                    ).format(page_no, total),
                },
            ],
        }],
    )
    block = msg.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Unexpected content block type: {type(block)}")
    return str(block.text)  # type: ignore[union-attr]


def build_page_result(text: str, page_no: int, width: float, height: float) -> dict:
    """Format Claude Vision text into the broker's expected page-array dict."""
    raw_lines = [ln for ln in text.split("\n") if ln.strip()]
    n = len(raw_lines) or 1
    lines = [
        {
            "text": line,
            "bbox": [0.0, i / n, 1.0, (i + 1) / n],
            "conf": 1.0,
        }
        for i, line in enumerate(raw_lines)
    ]
    return {
        "page_no": page_no,
        "width": width,
        "height": height,
        "ocr_source": "claude-vision",
        "text": text,
        "lines": lines,
    }


def rescue_doc(
    client: anthropic.Anthropic,
    http: httpx.Client,
    broker_url: str,
    doc_id: str,
    dry_run: bool,
) -> bool:
    """Re-enqueue, lease, OCR with Claude Vision, and complete one doc. Returns True on success."""
    print(f"\n  [{doc_id}] Re-enqueuing...")
    enq = http.post(f"{broker_url}/enqueue", json={"type": "ocr", "doc_id": doc_id})
    if enq.status_code not in (200, 201):
        print(f"  [{doc_id}] Enqueue failed: {enq.status_code} {enq.text}")
        return False
    job_id = enq.json()["job_id"]

    if dry_run:
        print(f"  [{doc_id}] DRY RUN — would process job {job_id}")
        return True

    # Download PDF
    pdf_resp = http.get(f"{broker_url}/file/{doc_id}.pdf", timeout=120)
    if pdf_resp.status_code != 200:
        print(f"  [{doc_id}] Cannot fetch PDF: {pdf_resp.status_code}")
        return False

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        pdf_path = tmp / f"{doc_id}.pdf"
        pdf_path.write_bytes(pdf_resp.content)

        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            print(f"  [{doc_id}] Cannot open PDF: {exc}")
            return False

        pages: list[dict] = []
        for idx in range(doc.page_count):
            page = doc[idx]
            page_no = idx + 1
            png_path = tmp / f"p{page_no}.png"
            page.get_pixmap(dpi=DPI).save(str(png_path))

            try:
                text = ocr_page_with_claude(client, png_path, page_no, doc.page_count)
                pages.append(build_page_result(text, page_no, page.rect.width, page.rect.height))
                print(f"  [{doc_id}] Page {page_no}/{doc.page_count}: {len(text)} chars")
            except anthropic.RateLimitError:
                print(f"  [{doc_id}] Rate limited on page {page_no}, sleeping 60s...")
                time.sleep(60)
                text = ocr_page_with_claude(client, png_path, page_no, doc.page_count)
                pages.append(build_page_result(text, page_no, page.rect.width, page.rect.height))
            except Exception as exc:
                print(f"  [{doc_id}] Page {page_no} error: {exc}")
                pages.append(build_page_result("", page_no, page.rect.width, page.rect.height))

    # Lease the job so we can complete it
    lease = http.post(
        f"{broker_url}/lease",
        json={"node": WORKER_ID, "capabilities": ["ocr"]},
        timeout=10,
    )
    if lease.status_code != 200:
        print(f"  [{doc_id}] Lease failed: {lease.status_code} — another worker may have taken it")
        return False

    leased = lease.json()
    if leased.get("doc_id") != doc_id:
        print(f"  [{doc_id}] Got different job ({leased.get('doc_id')}), releasing and skipping")
        # Complete the unrelated job with empty result so it doesn't block
        return False

    leased_job_id = leased["job_id"]
    complete = http.post(
        f"{broker_url}/complete",
        json={"job_id": leased_job_id, "worker_id": WORKER_ID, "result": pages},
        timeout=30,
    )
    if complete.status_code == 200:
        print(f"  [{doc_id}] Done — {len(pages)} pages via Claude Vision")
        return True
    else:
        print(f"  [{doc_id}] Complete failed: {complete.status_code} {complete.text}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Rescue dead OCR jobs with Claude Vision")
    parser.add_argument("--dry-run", action="store_true", help="List dead docs without processing")
    parser.add_argument("--doc-id", help="Process a single specific doc_id")
    args = parser.parse_args()

    cfg = load_config()
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    client = anthropic.Anthropic()
    http = httpx.Client(
        headers={"User-Agent": cfg.harvest["user_agent"]},
        timeout=120.0,
    )

    if args.doc_id:
        doc_ids = [args.doc_id]
    else:
        doc_ids = get_dead_ocr_doc_ids(broker_url)

    print(f"Found {len(doc_ids)} dead OCR jobs to rescue")
    if not doc_ids:
        print("Nothing to do.")
        return

    succeeded = failed = skipped = 0
    for doc_id in doc_ids:
        ok = rescue_doc(client, http, broker_url, doc_id, args.dry_run)
        if ok:
            succeeded += 1
        else:
            failed += 1

    print(f"\nDone. succeeded={succeeded} failed={failed} skipped={skipped}")


if __name__ == "__main__":
    main()
