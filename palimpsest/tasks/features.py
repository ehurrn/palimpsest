# palimpsest/tasks/features.py
import re
import logging
from typing import List, Dict, Any
import httpx
import numpy as np
import cv2
import fitz  # PyMuPDF

from palimpsest.config import Config
from palimpsest.tasks import handler, PermanentJobError

logger = logging.getLogger(__name__)

# Person entity quality filters
PERSON_STOPLIST = frozenset({
    "common rule", "subpart", "discussion", "purchase orders", "contractor",
    "public law", "department", "speaker", "order", "sec", "serve", "refer",
    "funding", "title", "section", "chapter", "article", "paragraph",
    "government", "agency", "committee", "board", "office", "bureau",
    "administration", "institute", "center", "laboratory", "division",
    "program", "project", "study", "report", "analysis", "review",
    "act", "code", "rule", "law", "regulation", "policy", "procedure",
})

# Tokens that indicate the entity is an org, not a person
_ORG_INDICATOR_TOKENS = frozenset({
    "hospital", "university", "college", "board", "bureau", "register",
    "branch", "office", "center", "institute", "committee", "laboratory",
    "school", "agency", "foundation", "department", "division", "council",
    "association", "society", "corporation", "company", "inc", "corp",
})

_MONTH_RE = re.compile(
    r'\b(january|february|march|april|may|june|july|august|september|'
    r'october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b',
    re.IGNORECASE,
)
_DIGIT_BRACKET_RE = re.compile(r'[\d\(\)\[\]@]')

def _is_valid_person(text: str) -> bool:
    """Return False if this PERSON entity is likely a NER false positive."""
    stripped = text.strip()
    alpha_chars = sum(c.isalpha() for c in stripped)
    if alpha_chars < 3:
        return False
    if _DIGIT_BRACKET_RE.search(stripped):
        return False
    # Reject OCR garbage with too much punctuation / whitespace
    if alpha_chars / max(len(stripped), 1) < 0.6:
        return False
    # Reject if it looks like a date (contains a month name)
    if _MONTH_RE.search(stripped):
        return False
    tokens = stripped.split()
    # Reject isolated-single-char artifacts ("U l T", "p o l")
    if len(tokens) > 1 and sum(1 for t in tokens if len(t) == 1) / len(tokens) > 0.5:
        return False
    # Reject all-caps short abbreviations (CFR, IRB) — real shouted names are
    # longer and usually contain lowercase letters after the first character
    if len(tokens) == 1 and stripped.isupper() and len(stripped) <= 5:
        return False
    norm = " ".join(stripped.lower().split())
    if norm in PERSON_STOPLIST:
        return False
    for term in PERSON_STOPLIST:
        if norm.startswith(term):
            return False
    # Reject if any token is an org-indicator word
    token_set = {t.lower().rstrip("s") for t in tokens}  # crude deplural
    if token_set & _ORG_INDICATOR_TOKENS:
        return False
    # Single all-lowercase short token is not a name
    if len(tokens) == 1 and stripped.islower() and len(stripped) < 8:
        return False
    return True

_VOWEL_RE = re.compile(r'[aeiouAEIOU]')

def _page_ocr_quality(page_lines: list) -> float:
    """Fraction of whitespace-delimited tokens that contain a vowel.

    Pages below ~0.5 are likely garbled tesseract output and should skip NER.
    """
    tokens = [t for line in page_lines for t in line["text"].split() if t]
    if not tokens:
        return 1.0
    return sum(1 for t in tokens if _VOWEL_RE.search(t)) / len(tokens)

# Compile regexes once at module level
EXEMPTION_STAMP_PATTERNS = [
    re.compile(r'\(\s*b\s*\)\s*\(\s*([1-9])\s*\)', re.IGNORECASE),
    re.compile(r'\bb\s*\(\s*([1-9])\s*\)', re.IGNORECASE)
]

DELETED_TEXT_PATTERNS = [
    re.compile(r'\[\s*deleted\s*\]', re.IGNORECASE),
    re.compile(r'\[\s*redacted\s*\]', re.IGNORECASE),
    re.compile(r'\bDELETED\b'),      # Case-sensitive
    re.compile(r'\bSANITIZED\b')     # Case-sensitive
]

MONTHS = {
    "january": "01", "jan": "01",
    "february": "02", "feb": "02",
    "march": "03", "mar": "03",
    "april": "04", "apr": "04",
    "may": "05",
    "june": "06", "jun": "06",
    "july": "07", "jul": "07",
    "august": "08", "aug": "08",
    "september": "09", "sep": "09",
    "october": "10", "oct": "10",
    "november": "11", "nov": "11",
    "december": "12", "dec": "12"
}

_nlp = None

def get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp

def rect_intersection_area(r1: List[float], r2: List[float]) -> float:
    """Compute the intersection area of two normalized bboxes [x0, y0, x1, y1]."""
    ix0 = max(r1[0], r2[0])
    iy0 = max(r1[1], r2[1])
    ix1 = min(r1[2], r2[2])
    iy1 = min(r1[3], r2[3])
    if ix1 > ix0 and iy1 > iy0:
        return (ix1 - ix0) * (iy1 - iy0)
    return 0.0

def normalize_person(text: str) -> str:
    text = " ".join(text.split())
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        suffix_candidates = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "jr.", "sr."}
        # Separate trailing suffixes from name parts
        suffixes = []
        name_parts = []
        for i, p in enumerate(parts):
            if i >= 1 and p.lower().rstrip(".") in {s.rstrip(".") for s in suffix_candidates}:
                suffixes.append(p)
            else:
                name_parts.append(p)
        if len(name_parts) == 2:
            # "Last, First" -> "First Last"
            text = f"{name_parts[1]} {name_parts[0]}"
        elif len(name_parts) == 1:
            text = name_parts[0]
        else:
            # 3+ non-suffix parts: keep as-is but rejoin
            text = " ".join(name_parts)
        if suffixes:
            text = f"{text}, {', '.join(suffixes)}"

    title_pattern = re.compile(
        r'^(?:dr|mr|mrs|ms|lt|col|gen|capt|prof|major|colonel|general|captain|lieutenant)\.?\s+',
        re.IGNORECASE
    )
    while True:
        m = title_pattern.match(text)
        if m:
            text = text[m.end():]
        else:
            break
    text = " ".join(text.split())
    return text.lower()

def normalize_date(text: str) -> str:
    text = " ".join(text.split()).strip().lower()
    
    # 1. YYYY-MM-DD
    m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', text)
    if m:
        year, month, day = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
        
    # 2. MM-DD-YYYY or DD-MM-YYYY (default to MM-DD-YYYY)
    m = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$', text)
    if m:
        g1, g2, year = m.groups()
        val1, val2 = int(g1), int(g2)
        if val1 > 12:
            month, day = val2, val1
        else:
            month, day = val1, val2
        return f"{year}-{month:02d}-{day:02d}"
        
    # 3. Month Day Year (e.g., "june 12 2026" or "june 12, 2026")
    m = re.match(r'^([a-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})$', text)
    if m:
        month_str, day_str, year_str = m.groups()
        if month_str in MONTHS:
            return f"{year_str}-{MONTHS[month_str]}-{int(day_str):02d}"
            
    # 4. Day Month Year (e.g., "12 june 2026" or "12 june, 2026")
    m = re.match(r'^(\d{1,2})\s+([a-z]+)\s*,?\s*(\d{4})$', text)
    if m:
        day_str, month_str, year_str = m.groups()
        if month_str in MONTHS:
            return f"{year_str}-{MONTHS[month_str]}-{int(day_str):02d}"
            
    # 5. Month Year (e.g., "june 2026")
    m = re.match(r'^([a-z]+)\s+(\d{4})$', text)
    if m:
        month_str, year_str = m.groups()
        if month_str in MONTHS:
            return f"{year_str}-{MONTHS[month_str]}"
            
    # 6. Year Month (e.g., "2026 june")
    m = re.match(r'^(\d{4})\s+([a-z]+)$', text)
    if m:
        year_str, month_str = m.groups()
        if month_str in MONTHS:
            return f"{year_str}-{MONTHS[month_str]}"
            
    # 7. Year (e.g. "2026")
    m = re.match(r'^(\d{4})$', text)
    if m:
        return m.group(1)
        
    return text

def normalize_dosage(text: str) -> str:
    text = " ".join(text.split()).strip().lower()
    m = re.match(r'^(\d+(?:\.\d+)?)\s*(.*)$', text)
    if m:
        num, unit = m.groups()
        unit = unit.lower().replace("μ", "u")
        return f"{num} {unit}"
    return text

def normalize_protocol_code(text: str) -> str:
    m = re.search(r'\b(CAL|CHI|HP)[-\s]?(\d{1,4})\b', text)
    if m:
        prefix, num = m.groups()
        return f"{prefix.upper()}-{num}"
    return text.upper()

def normalize_location_org(text: str) -> str:
    return " ".join(text.split()).strip().lower()

def normalize_reg_cite(text: str) -> str:
    t = " ".join(text.split()).strip()
    # Canonicalise CFR: "45 CFR Part 46" / "45 C.F.R. §46" → "45 CFR 46"
    m = re.match(r'^(\d+)\s*C\.?F\.?R\.?\s*(?:Part\s*|§\s*)?(\d+(?:\.\d+)*).*$', t, re.IGNORECASE)
    if m:
        return f"{m.group(1)} CFR {m.group(2)}"
    m = re.match(r'^(\d+)\s*U\.S\.C\.?\s*[§]?\s*(\d+(?:\.\d+)*).*$', t, re.IGNORECASE)
    if m:
        return f"{m.group(1)} USC {m.group(2)}"
    named = {
        r'common\s+rule': 'Common Rule',
        r'belmont\s+report': 'Belmont Report',
        r'declaration\s+of\s+helsinki': 'Declaration of Helsinki',
        r'nuremberg\s+code': 'Nuremberg Code',
        r'national\s+research\s+act': 'National Research Act',
    }
    tl = t.lower()
    for pat, canon in named.items():
        if re.search(pat, tl):
            return canon
    return t

def normalize_outcome_ref(text: str) -> str:
    """Normalize outcome_ref entities: prefix with future_ref: or outcome_ind:.

    Args:
        text: Raw matched text from outcome_ref patterns.

    Returns:
        Normalized string with 'future_ref:' or 'outcome_ind:' prefix.
    """
    t = " ".join(text.split()).strip().lower()
    future_signals = ["to be submitted", "annual report due", "follow-up study planned",
                      "final report to follow", "final report forthcoming", "pending final report",
                      "pending report"]
    if any(sig in t for sig in future_signals):
        return "future_ref:" + t
    return "outcome_ind:" + t

def normalize_seq_ref(text: str) -> str:
    t = " ".join(text.split()).strip().upper()
    t = re.sub(r'^REPORT\s+(?:NO\.?|NUMBER)\s*(\d+)', r'REPORT-NO-\1', t)
    t = re.sub(r'^NV\s*-\s*(\d+)', r'NV-\1', t)
    t = re.sub(r'^NV\s*(\d+)', r'NV\1', t)
    return t

def normalize_subject_ref(text: str) -> str:
    return " ".join(text.split()).strip().lower()

def normalize(kind: str, text: str) -> str:
    """Normalize entities according to the rules in §7.3."""
    if kind == "person":
        return normalize_person(text)
    elif kind == "date":
        return normalize_date(text)
    elif kind == "dosage":
        return normalize_dosage(text)
    elif kind == "protocol_code":
        return normalize_protocol_code(text)
    elif kind in ("location", "org"):
        return normalize_location_org(text)
    elif kind == "reg_cite":
        return normalize_reg_cite(text)
    elif kind == "outcome_ref":
        return normalize_outcome_ref(text)
    elif kind == "seq_ref":
        return normalize_seq_ref(text)
    elif kind == "subject_ref":
        return normalize_subject_ref(text)
    else:
        return " ".join(text.split()).strip().lower()

def process_features(pdf_bytes: bytes, ocr_data: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    """Pure feature extraction function for easy unit testing."""
    redaction_context_chars = cfg.features.get("redaction_context_chars", 300)
    redaction_context_lines = cfg.features.get("redaction_context_lines", 2)
    
    redactions = []
    entities = []
    
    # Load PDF using PyMuPDF if bytes are provided
    pdf_doc = None
    if pdf_bytes:
        try:
            pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            logger.error(f"Failed to open PDF with PyMuPDF: {e}")
            
    # Compile the dosage, protocol, and reg_cite patterns for the page text
    reg_cite_patterns = [
        re.compile(r'\b\d+\s*C\.?F\.?R\.?\s*(?:Part\s*|§\s*)?\d+(?:\.\d+)*\b', re.IGNORECASE),
        re.compile(r'\b\d+\s*U\.S\.C\.?\s*[§]?\s*\d+(?:\.\d+)*\b'),
        re.compile(r'\bCommon\s+Rule\b', re.IGNORECASE),
        re.compile(r'\bBelmont\s+Report\b', re.IGNORECASE),
        re.compile(r'\bDeclaration\s+of\s+Helsinki\b', re.IGNORECASE),
        re.compile(r'\bNuremberg\s+Code\b', re.IGNORECASE),
        re.compile(r'\bNational\s+Research\s+Act\b', re.IGNORECASE),
    ]
    dosage_pattern = re.compile(
        r'\b\d+(?:\.\d+)?\s*(?:r|rad|rads|rem|mr|mrem|roentgen|uCi|μCi|mCi|curies?)\b',
        re.IGNORECASE
    )
    protocol_pattern = re.compile(r'\b(CAL|CHI|HP)[-\s]?(\d{1,4})\b')
    outcome_indicator_pattern = re.compile(
        r'\b(?:results?\s+(?:of|show|indicate)|outcome\s+of|mortality|survival\s+rates?|'
        r'post[-\s]?exposure\s+(?:survey|study|results?)|follow[-\s]?up\s+results?)\b',
        re.IGNORECASE
    )
    future_ref_pattern = re.compile(
        r'\b(?:to\s+be\s+submitted|annual\s+report\s+due|follow[-\s]?up\s+study\s+planned|'
        r'final\s+report\s+(?:to\s+follow|forthcoming)|pending\s+(?:final\s+)?report)\b',
        re.IGNORECASE
    )
    seq_ref_pattern = re.compile(r'\b(NV\d{7}|NV-\d+|Report\s+No\.\s+\d+)\b', re.IGNORECASE)
    subject_ref_pattern = re.compile(r'\b(Subject|Patient|Case|Individual)\s+[A-Z\d]+\b', re.IGNORECASE)
    
    for page_idx, page in enumerate(ocr_data):
        page_no = page["page_no"]
        page_lines = page.get("lines", [])
        if not page_lines:
            continue
            
        page_text = "\n".join(line["text"] for line in page_lines)
        
        # Keep track of line character offsets in page_text.
        # NOTE: Multi-line entities (e.g. a long regulatory citation that
        # wraps across OCR lines) will receive the bounding box of the
        # line where the regex match *starts*.  This is expected for
        # Phase 1 — downstream UI rendering should anticipate visually
        # truncated highlighting for such entities.
        line_offsets = []
        current_offset = 0
        for line in page_lines:
            l_len = len(line["text"])
            line_offsets.append((current_offset, current_offset + l_len, line["bbox"]))
            current_offset += l_len + 1  # +1 for newline
            
        # A. Redaction markers - text kinds
        for i, line in enumerate(page_lines):
            line_text = line["text"]
            line_bbox = line["bbox"]
            
            matched = False
            # Check exemption_stamp
            for pat in EXEMPTION_STAMP_PATTERNS:
                m = pat.search(line_text)
                if m:
                    digit = next((g for g in m.groups() if g is not None), "1")
                    label = f"(b)({digit})"
                    
                    before_lines = page_lines[max(0, i - redaction_context_lines):i]
                    after_lines = page_lines[i + 1:i + 1 + redaction_context_lines]
                    before_text = "\n".join(bl["text"] for bl in before_lines)
                    after_text = "\n".join(al["text"] for al in after_lines)
                    context_before = before_text[-redaction_context_chars:] if before_text else ""
                    context_after = after_text[:redaction_context_chars] if after_text else ""
                    
                    redactions.append({
                        "page_no": page_no,
                        "kind": "exemption_stamp",
                        "label": label,
                        "bbox": line_bbox,
                        "context_before": context_before,
                        "context_after": context_after
                    })
                    matched = True
                    break
                    
            if not matched:
                # Check deleted_text
                for pat in DELETED_TEXT_PATTERNS:
                    m = pat.search(line_text)
                    if m:
                        label = m.group(0).strip()
                        
                        before_lines = page_lines[max(0, i - redaction_context_lines):i]
                        after_lines = page_lines[i + 1:i + 1 + redaction_context_lines]
                        before_text = "\n".join(bl["text"] for bl in before_lines)
                        after_text = "\n".join(al["text"] for al in after_lines)
                        context_before = before_text[-redaction_context_chars:] if before_text else ""
                        context_after = after_text[:redaction_context_chars] if after_text else ""
                        
                        redactions.append({
                            "page_no": page_no,
                            "kind": "deleted_text",
                            "label": label,
                            "bbox": line_bbox,
                            "context_before": context_before,
                            "context_after": context_after
                        })
                        break
                        
        # B. Redaction markers - black_box (image analysis)
        if pdf_doc and page_no - 1 < pdf_doc.page_count:
            try:
                pdf_page = pdf_doc.load_page(page_no - 1)
                pix = pdf_page.get_pixmap(dpi=150, colorspace=fitz.csGRAY)
                pw, ph = pix.width, pix.height
                
                if pw > 0 and ph > 0:
                    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(ph, pw).copy()
                    
                    threshold_val = cfg.features.get("blackbox_darkness_threshold", 60) - 1
                    _, mask = cv2.threshold(img, threshold_val, 255, cv2.THRESH_BINARY_INV)
                    
                    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    
                    # Calculate median line height
                    line_heights = [line["bbox"][3] - line["bbox"][1] for line in page_lines]
                    median_height = np.median(line_heights) if line_heights else 0.015
                    
                    for contour in contours:
                        rx, ry, rw, rh = cv2.boundingRect(contour)
                        bbox_area = rw * rh
                        if bbox_area == 0:
                            continue
                            
                        contour_area = cv2.contourArea(contour)
                        area_frac = bbox_area / (pw * ph)
                        
                        min_area_frac = cfg.features.get("blackbox_min_area_frac", 0.001)
                        max_area_frac = cfg.features.get("blackbox_max_area_frac", 0.25)
                        
                        if not (min_area_frac <= area_frac <= max_area_frac):
                            continue
                            
                        # Rectangularity
                        if contour_area / bbox_area <= 0.85:
                            continue
                            
                        # Aspect Ratio
                        aspect = rw / rh
                        if not (0.2 <= aspect <= 50.0):
                            continue
                            
                        # Exclude thin lines / noise: h or w < 0.5% of page dimension
                        if rw < 0.005 * pw or rh < 0.005 * ph:
                            continue
                            
                        # Normalized coordinates
                        bx0 = rx / pw
                        by0 = ry / ph
                        bx1 = (rx + rw) / pw
                        by1 = (ry + rh) / ph
                        
                        # Disambiguator: discard if > 10% area intersects any OCR line bbox
                        discard = False
                        black_box_area = (bx1 - bx0) * (by1 - by0)
                        for line in page_lines:
                            line_bbox = line["bbox"]
                            inter = rect_intersection_area([bx0, by0, bx1, by1], line_bbox)
                            if inter / black_box_area > 0.10:
                                discard = True
                                break
                                
                        if discard:
                            continue
                            
                        # Find nearest OCR lines above and below
                        line_above = None
                        min_dist_above = float('inf')
                        line_below = None
                        min_dist_below = float('inf')
                        
                        limit_dist = 1.5 * median_height
                        for line in page_lines:
                            ly0, ly1 = line["bbox"][1], line["bbox"][3]
                            if ly1 <= by0:
                                dist = by0 - ly1
                                if dist <= limit_dist and dist < min_dist_above:
                                    min_dist_above = dist
                                    line_above = line
                            elif ly0 >= by1:
                                dist = ly0 - by1
                                if dist <= limit_dist and dist < min_dist_below:
                                    min_dist_below = dist
                                    line_below = line
                                    
                        context_before = line_above["text"] if line_above else ""
                        context_after = line_below["text"] if line_below else ""
                        
                        redactions.append({
                            "page_no": page_no,
                            "kind": "black_box",
                            "label": "black_box",
                            "bbox": [bx0, by0, bx1, by1],
                            "context_before": context_before,
                            "context_after": context_after
                        })
            except Exception as e:
                logger.error(f"Error processing black boxes on page {page_no}: {e}")
                
        # C. Entities
        regex_entities = []
        
        # Dosage extraction
        for m in dosage_pattern.finditer(page_text):
            char_start = m.start()
            char_end = m.end()
            text = m.group(0)
            norm = normalize("dosage", text)
            
            bbox = [0.0, 0.0, 1.0, 1.0]
            for start, end, b in line_offsets:
                if start <= char_start <= end:
                    bbox = b
                    break
                    
            regex_entities.append({
                "page_no": page_no,
                "kind": "dosage",
                "text": text,
                "norm": norm,
                "char_start": char_start,
                "char_end": char_end,
                "bbox": bbox
            })
            
        # Protocol Code extraction
        for m in protocol_pattern.finditer(page_text):
            char_start = m.start()
            char_end = m.end()
            text = m.group(0)
            norm = normalize("protocol_code", text)
            
            bbox = [0.0, 0.0, 1.0, 1.0]
            for start, end, b in line_offsets:
                if start <= char_start <= end:
                    bbox = b
                    break
                    
            regex_entities.append({
                "page_no": page_no,
                "kind": "protocol_code",
                "text": text,
                "norm": norm,
                "char_start": char_start,
                "char_end": char_end,
                "bbox": bbox
            })
            
        # Regulatory-citation extraction (Type e)
        seen_reg_spans: set[tuple[int, int]] = set()
        for pat in reg_cite_patterns:
            for m in pat.finditer(page_text):
                char_start = m.start()
                char_end = m.end()
                # Skip if this span overlaps any already-found reg_cite
                if any(s < char_end and char_start < e for s, e in seen_reg_spans):
                    continue
                seen_reg_spans.add((char_start, char_end))
                text = m.group(0)
                norm = normalize("reg_cite", text)
                bbox = [0.0, 0.0, 1.0, 1.0]
                for start, end, b in line_offsets:
                    if start <= char_start <= end:
                        bbox = b
                        break
                regex_entities.append({
                    "page_no": page_no,
                    "kind": "reg_cite",
                    "text": text,
                    "norm": norm,
                    "char_start": char_start,
                    "char_end": char_end,
                    "bbox": bbox,
                })

        # outcome_ref extraction (both future-ref signals and outcome-indicator terms)
        seen_outcome_spans: set[tuple[int, int]] = set()
        for pat in (outcome_indicator_pattern, future_ref_pattern):
            for m in pat.finditer(page_text):
                char_start, char_end = m.start(), m.end()
                if any(s < char_end and char_start < e for s, e in seen_outcome_spans):
                    continue
                seen_outcome_spans.add((char_start, char_end))
                text = m.group(0)
                norm = normalize("outcome_ref", text)
                bbox = [0.0, 0.0, 1.0, 1.0]
                for start, end, b in line_offsets:
                    if start <= char_start <= end:
                        bbox = b
                        break
                regex_entities.append({
                    "page_no": page_no,
                    "kind": "outcome_ref",
                    "text": text,
                    "norm": norm,
                    "char_start": char_start,
                    "char_end": char_end,
                    "bbox": bbox,
                })

        # seq_ref extraction
        for m in seq_ref_pattern.finditer(page_text):
            char_start = m.start()
            char_end = m.end()
            text = m.group(0)
            norm = normalize("seq_ref", text)
            bbox = [0.0, 0.0, 1.0, 1.0]
            for start, end, b in line_offsets:
                if start <= char_start <= end:
                    bbox = b
                    break
            regex_entities.append({
                "page_no": page_no,
                "kind": "seq_ref",
                "text": text,
                "norm": norm,
                "char_start": char_start,
                "char_end": char_end,
                "bbox": bbox,
            })

        # subject_ref extraction
        for m in subject_ref_pattern.finditer(page_text):
            char_start = m.start()
            char_end = m.end()
            text = m.group(0)
            norm = normalize("subject_ref", text)
            bbox = [0.0, 0.0, 1.0, 1.0]
            for start, end, b in line_offsets:
                if start <= char_start <= end:
                    bbox = b
                    break
            regex_entities.append({
                "page_no": page_no,
                "kind": "subject_ref",
                "text": text,
                "norm": norm,
                "char_start": char_start,
                "char_end": char_end,
                "bbox": bbox,
            })

        # Add regex entities to final page entities list
        page_entities = list(regex_entities)
        
        # spaCy NER extraction — skip on garbled OCR pages
        ocr_quality = _page_ocr_quality(page_lines)
        if ocr_quality < 0.5:
            logger.debug(f"Page {page_no}: skipping NER (OCR quality {ocr_quality:.2f})")
        else:
            try:
                nlp = get_nlp()
                doc = nlp(page_text)

                for ent in doc.ents:
                    spacy_kind = None
                    if ent.label_ == "PERSON":
                        spacy_kind = "person"
                    elif ent.label_ == "DATE":
                        spacy_kind = "date"
                    elif ent.label_ in ("GPE", "LOC"):
                        spacy_kind = "location"
                    elif ent.label_ == "ORG":
                        spacy_kind = "org"

                    if spacy_kind is None:
                        continue

                    char_start = ent.start_char
                    char_end = ent.end_char
                    text = ent.text

                    if spacy_kind == "person" and not _is_valid_person(text):
                        continue

                    # Check overlap with regex entities
                    overlap = False
                    for r_ent in regex_entities:
                        if char_start < r_ent["char_end"] and r_ent["char_start"] < char_end:
                            overlap = True
                            break

                    if not overlap:
                        norm = normalize(spacy_kind, text)
                        bbox = [0.0, 0.0, 1.0, 1.0]
                        for start, end, b in line_offsets:
                            if start <= char_start <= end:
                                bbox = b
                                break

                        entity_dict = {
                            "page_no": page_no,
                            "kind": spacy_kind,
                            "text": text,
                            "norm": norm,
                            "char_start": char_start,
                            "char_end": char_end,
                            "bbox": bbox
                        }
                        if spacy_kind == "person":
                            entity_dict["living_status"] = "unknown"

                        page_entities.append(entity_dict)
            except Exception as e:
                logger.error(f"spaCy NER failed on page {page_no}: {e}")
            
        entities.extend(page_entities)
        
    return {
        "doc_id": ocr_data[0].get("doc_id", "") if ocr_data else "",
        "redactions": redactions,
        "entities": entities
    }

@handler("features")
def extract_features(cfg: Config, job: dict) -> dict:
    """Worker task handler for feature extraction."""
    doc_id = job["doc_id"]
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    
    # 1. Fetch OCR JSON from broker
    try:
        ocr_resp = httpx.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30.0)
        if ocr_resp.status_code == 404:
            raise PermanentJobError(f"OCR file not found for doc_id {doc_id}")
        ocr_resp.raise_for_status()
        ocr_data = ocr_resp.json()
    except httpx.HTTPError as e:
        # Transient connection error
        raise Exception(f"Failed to fetch OCR JSON from broker: {e}")
        
    # 2. Fetch PDF file from broker (needed for image/black_box analysis)
    pdf_bytes = b""
    try:
        pdf_resp = httpx.get(f"{broker_url}/file/{doc_id}.pdf", timeout=30.0)
        if pdf_resp.status_code == 200:
            pdf_bytes = pdf_resp.content
        else:
            logger.warning(f"Could not fetch PDF for doc_id {doc_id} (status {pdf_resp.status_code}); running text-only feature extraction.")
    except Exception as e:
        logger.warning(f"Could not fetch PDF for doc_id {doc_id} due to error ({e}); running text-only feature extraction.")
        
    # 3. Process features
    result = process_features(pdf_bytes, ocr_data, cfg)
    
    # Make sure doc_id matches
    result["doc_id"] = doc_id
    
    return result
