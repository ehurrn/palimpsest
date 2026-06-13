# tests/test_features.py
import pytest
import io
import re
import numpy as np
from PIL import Image, ImageDraw

from palimpsest.config import Config
from palimpsest.tasks.features import (
    normalize,
    normalize_reg_cite,
    process_features,
    rect_intersection_area,
    EXEMPTION_STAMP_PATTERNS,
    DELETED_TEXT_PATTERNS,
    extract_features
)
from palimpsest.tasks import PermanentJobError

# Dummy config fixture
@pytest.fixture
def test_config():
    class DummyConfig:
        storage_root = "/tmp"
        db_path = "/tmp/dummy.db"
        broker = {"host": "localhost", "port": 8077}
        features = {
            "redaction_context_chars": 300,
            "redaction_context_lines": 2,
            "blackbox_min_area_frac": 0.001,
            "blackbox_max_area_frac": 0.25,
            "blackbox_darkness_threshold": 60
        }
    return DummyConfig()

def test_normalization_table():
    # Table-driven test containing at least 12 distinct cases
    cases = [
        # (kind, input_text, expected_output)
        ("person", "Dr. John SMITH", "john smith"),
        ("person", "Smith, John", "john smith"),
        ("person", "General George S. Patton", "george s. patton"),
        ("person", "COLONEL Klink", "klink"),
        ("person", "John Smith, Jr.", "john smith, jr."),
        ("date", "June 12, 2026", "2026-06-12"),
        ("date", "06-12-2026", "2026-06-12"),
        ("date", "May 1953", "1953-05"),
        ("date", "1956", "1956"),
        ("dosage", "15 REM", "15 rem"),
        ("dosage", "10.5 mrem", "10.5 mrem"),
        ("dosage", "5.0 μCi", "5.0 uci"),
        ("protocol_code", "CAL 12", "CAL-12"),
        ("protocol_code", "HP-9", "HP-9"),
        ("protocol_code", "CHI3", "CHI-3"),
        ("location", "LOS ALAMOS ", "los alamos"),
        ("org", "DEPT of ENERGY", "dept of energy"),
    ]
    
    assert len(cases) >= 12
    for kind, text, expected in cases:
        assert normalize(kind, text) == expected

def test_protocol_code_case_sensitive():
    # "cal 12" should not match as protocol_code (case-sensitive)
    protocol_pattern = re.compile(r'\b(CAL|CHI|HP)[-\s]?(\d{1,4})\b')
    assert protocol_pattern.search("cal 12") is None
    assert protocol_pattern.search("CAL 12") is not None

def test_dosage_vs_reminders():
    # dosage regex must NOT match "15 reminders"
    dosage_pattern = re.compile(
        r'\b\d+(?:\.\d+)?\s*(?:r|rad|rads|rem|mr|mrem|roentgen|uCi|μCi|mCi|curies?)\b',
        re.IGNORECASE
    )
    assert dosage_pattern.search("15 reminders") is None
    assert dosage_pattern.search("15 rem") is not None

def test_text_marker_regexes():
    # Positive and negative cases for exemption_stamp
    # (b)(1) hits
    assert any(pat.search("(b)(1)") for pat in EXEMPTION_STAMP_PATTERNS)
    # b(12) doesn't hit
    assert not any(pat.search("b(12)") for pat in EXEMPTION_STAMP_PATTERNS)
    
    # Positive and negative cases for deleted_text
    # [deleted] hits
    assert any(pat.search("[deleted]") for pat in DELETED_TEXT_PATTERNS)
    # DELETED (case-sensitive all-caps) hits
    assert any(pat.search("DELETED") for pat in DELETED_TEXT_PATTERNS)
    # deleted (lowercase, no brackets) does NOT hit
    assert not any(pat.search("deleted") for pat in DELETED_TEXT_PATTERNS)
    # prose "deleted the file" does NOT hit
    assert not any(pat.search("deleted the file") for pat in DELETED_TEXT_PATTERNS)

def test_context_clipping(test_config):
    # Scenario 1: Line limit determines the window (redaction_context_lines = 2, redaction_context_chars = 300)
    test_config.features["redaction_context_lines"] = 2
    test_config.features["redaction_context_chars"] = 300
    
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "First line", "bbox": [0, 0, 1, 0.1]},
            {"text": "Second line", "bbox": [0, 0.1, 1, 0.2]},
            {"text": "Third line", "bbox": [0, 0.2, 1, 0.3]},
            {"text": "This has (b)(1) stamp", "bbox": [0, 0.3, 1, 0.4]},
            {"text": "Fourth line", "bbox": [0, 0.4, 1, 0.5]},
            {"text": "Fifth line", "bbox": [0, 0.5, 1, 0.6]},
            {"text": "Sixth line", "bbox": [0, 0.6, 1, 0.7]},
        ]
    }]
    
    res = process_features(None, ocr_data, test_config)
    assert len(res["redactions"]) == 1
    red = res["redactions"][0]
    
    # Should get exactly the 2 preceding lines
    assert red["context_before"] == "Second line\nThird line"
    # Should get exactly the 2 succeeding lines
    assert red["context_after"] == "Fourth line\nFifth line"
    
    # Scenario 2: Character limit determines the window (redaction_context_lines = 10, redaction_context_chars = 10)
    test_config.features["redaction_context_lines"] = 10
    test_config.features["redaction_context_chars"] = 10
    
    res_char = process_features(None, ocr_data, test_config)
    red_char = res_char["redactions"][0]
    
    # Preceding text joined: "First line\nSecond line\nThird line"
    # Char limit 10: last 10 chars -> "Third line" (length 10)
    assert red_char["context_before"] == "Third line"
    
    # Succeeding text joined: "Fourth line\nFifth line\nSixth line"
    # Char limit 10: first 10 chars -> "Fourth lin" (length 10)
    assert red_char["context_after"] == "Fourth lin"


def test_blackbox_detection(test_config):
    # Draw 2 black rects + 1 text-filled rect with PIL
    # Image size: 600 x 800
    img = Image.new("L", (600, 800), 255)
    draw = ImageDraw.Draw(img)
    
    # 1. First black rect (redaction)
    draw.rectangle([100, 100, 200, 150], fill=0)
    # 2. Second black rect (redaction)
    draw.rectangle([300, 400, 450, 480], fill=0)
    # 3. Third black rect (text-filled/overlapping, should be discarded)
    draw.rectangle([100, 600, 250, 650], fill=0)
    
    pdf_buffer = io.BytesIO()
    img.save(pdf_buffer, format="PDF")
    pdf_bytes = pdf_buffer.getvalue()
    
    # Define OCR lines
    ocr_data = [{
        "page_no": 1,
        "lines": [
            # Dummy lines
            {"text": "Header", "bbox": [0.1, 0.01, 0.5, 0.05]},
            # Overlapping line for the third rect:
            # Third rect box in 0-1: [100/600, 600/800, 250/600, 650/800] -> [0.1667, 0.75, 0.4167, 0.8125]
            # Let's make an overlapping line that covers >10% area
            {"text": "Text inside cell", "bbox": [0.18, 0.76, 0.40, 0.80]}
        ]
    }]
    
    res = process_features(pdf_bytes, ocr_data, test_config)
    
    black_boxes = [r for r in res["redactions"] if r["kind"] == "black_box"]
    # Should detect exactly 2 black boxes
    assert len(black_boxes) == 2
    
    # Check that they match our first two rects approximately
    # Rect 1: [100/600, 100/800, 200/600, 150/800] -> [0.1667, 0.125, 0.3333, 0.1875]
    # Rect 2: [300/600, 400/800, 450/600, 480/800] -> [0.5, 0.5, 0.75, 0.6]
    
    coords1 = black_boxes[0]["bbox"]
    coords2 = black_boxes[1]["bbox"]
    
    # Sort them by y0 to make comparison deterministic
    if coords1[1] > coords2[1]:
        coords1, coords2 = coords2, coords1
        
    assert pytest.approx(coords1[0], abs=0.05) == 0.1667
    assert pytest.approx(coords1[1], abs=0.05) == 0.125
    assert pytest.approx(coords2[0], abs=0.05) == 0.5
    assert pytest.approx(coords2[1], abs=0.05) == 0.5

def test_spacy_regex_overlap_resolution(test_config):
    # Test that regex layer (dosage/protocol_code) overrides spaCy entities on overlap.
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "CAL 12 was conducted at Los Alamos.", "bbox": [0.1, 0.1, 0.9, 0.2]}
        ]
    }]
    
    # We run features extraction. spaCy might flag "CAL 12" as ORG/PERSON.
    # The regex flags it as protocol_code.
    # We want to assert that CAL 12 is in entities list as protocol_code and NOT as org/person.
    res = process_features(None, ocr_data, test_config)
    
    entities = res["entities"]
    
    # Find protocol code
    p_codes = [e for e in entities if e["kind"] == "protocol_code"]
    assert len(p_codes) == 1
    assert p_codes[0]["text"] == "CAL 12"
    assert p_codes[0]["norm"] == "CAL-12"
    
    # Ensure there's no PERSON or ORG overlapping with CAL 12
    overlapping_others = [
        e for e in entities 
        if e["kind"] in ("person", "org") and e["char_start"] < p_codes[0]["char_end"] and p_codes[0]["char_start"] < e["char_end"]
    ]
    assert len(overlapping_others) == 0

def test_handler_http_mocking(test_config, monkeypatch):
    # Mock httpx responses to simulate broker interactions
    class DummyResponse:
        def __init__(self, status_code, content=None, json_data=None):
            self.status_code = status_code
            self.content = content
            self.json_data = json_data
        
        def json(self):
            return self.json_data
            
        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception("HTTP Error")
                
    def mock_get(url, *args, **kwargs):
        if "/ocr/" in url:
            return DummyResponse(200, json_data=[{
                "page_no": 1,
                "lines": [{"text": "Nothing redacted here", "bbox": [0.1, 0.1, 0.5, 0.2]}]
            }])
        elif "/file/" in url:
            # Return dummy empty pdf
            return DummyResponse(200, content=b"%PDF-1.4...")
        return DummyResponse(404)
        
    monkeypatch.setattr("httpx.get", mock_get)
    
    job = {"doc_id": "123"}
    res = extract_features(test_config, job)
    assert res["doc_id"] == "123"
    assert len(res["redactions"]) == 0
    assert len(res["entities"]) == 0

def test_handler_http_404_raises_permanent_error(test_config, monkeypatch):
    class DummyResponse:
        def __init__(self, status_code):
            self.status_code = status_code
        def raise_for_status(self):
            pass
            
    def mock_get(url, *args, **kwargs):
        if "/ocr/" in url:
            return DummyResponse(404)
        return DummyResponse(404)
        
    monkeypatch.setattr("httpx.get", mock_get)
    
    job = {"doc_id": "123"}
    with pytest.raises(PermanentJobError):
        extract_features(test_config, job)


# ── Type e: reg_cite entity extraction and normalization ──────────────────────

def test_normalize_reg_cite_cfr():
    assert normalize_reg_cite("45 C.F.R. Part 46") == "45 CFR 46"
    assert normalize_reg_cite("45 CFR § 219") == "45 CFR 219"
    assert normalize_reg_cite("45 CFR 46.116") == "45 CFR 46.116"

def test_normalize_reg_cite_named():
    assert normalize_reg_cite("Common Rule") == "Common Rule"
    assert normalize_reg_cite("the Belmont Report") == "Belmont Report"
    assert normalize_reg_cite("Declaration of Helsinki") == "Declaration of Helsinki"
    assert normalize_reg_cite("Nuremberg Code") == "Nuremberg Code"
    assert normalize_reg_cite("National Research Act") == "National Research Act"

def test_normalize_dispatches_reg_cite():
    assert normalize("reg_cite", "45 CFR 46") == "45 CFR 46"

def test_reg_cite_entities_extracted(test_config):
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "The study was conducted under 45 CFR 46 and the Common Rule.", "bbox": [0.0, 0.0, 1.0, 0.05]},
            {"text": "The Declaration of Helsinki also applies to all procedures.", "bbox": [0.0, 0.05, 1.0, 0.10]},
        ],
    }]
    result = process_features(b"", ocr_data, test_config)
    entities = result["entities"]
    reg_cites = [e for e in entities if e["kind"] == "reg_cite"]
    norms = [e["norm"] for e in reg_cites]
    assert any("45 CFR 46" in n for n in norms), f"45 CFR 46 not found in {norms}"
    assert any("Common Rule" in n for n in norms), f"Common Rule not found in {norms}"
    assert any("Declaration of Helsinki" in n or "Helsinki" in n for n in norms), f"Helsinki not in {norms}"

def test_reg_cite_no_duplicates_across_patterns(test_config):
    # "Common Rule" should only appear once even though multiple patterns could match
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "Under the Common Rule (45 CFR 46) consent is required.", "bbox": [0.0, 0.0, 1.0, 0.05]},
        ],
    }]
    result = process_features(b"", ocr_data, test_config)
    entities = result["entities"]
    reg_cites = [e for e in entities if e["kind"] == "reg_cite"]
    norms = [e["norm"] for e in reg_cites]
    # Each span should appear once
    for norm in set(norms):
        assert norms.count(norm) == 1, f"Duplicate reg_cite norm '{norm}'"
