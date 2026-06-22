"""Synthetic case generators for the eval harness (types a, b, c).

A Case is a self-contained bundle of documents/pages/entities/redactions plus a
truth record. The runner (TASK-15) inserts these into the isolated eval DB,
embeds each page's chunk text, runs the real scorer, and grades the output.

Doc ids are namespaced by case_uid so many cases coexist in one eval DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Doc:
    doc_id: str
    year: int | None = None


@dataclass
class Page:
    doc_id: str
    page_no: int
    text: str
    entities: list[dict] = field(default_factory=list)  # kind,text,norm,char_start,char_end
    chunk_id: int | None = None
    redaction: dict | None = None  # kind,label,context_before,context_after


@dataclass
class Case:
    case_uid: str
    type_key: str
    case_kind: str  # positive | negative_control | hard_negative
    docs: list[Doc]
    pages: list[Page]
    truth: dict  # {"answer_norm": str | None} for a/b


def _segments_to_page(doc_id: str, page_no: int, segments: list) -> Page:
    """Assemble page text from segments, tracking entity offsets.

    A segment is either a plain str (literal text) or a dict
    {kind,text,norm} (an entity). Segments are joined with single spaces.
    """
    parts: list[str] = []
    entities: list[dict] = []
    cursor = 0
    for i, seg in enumerate(segments):
        if i > 0:
            parts.append(" ")
            cursor += 1
        if isinstance(seg, str):
            parts.append(seg)
            cursor += len(seg)
        else:
            text = seg["text"]
            start = cursor
            parts.append(text)
            cursor += len(text)
            entities.append(
                {
                    "kind": seg["kind"],
                    "text": text,
                    "norm": seg["norm"],
                    "char_start": start,
                    "char_end": cursor,
                }
            )
    return Page(doc_id=doc_id, page_no=page_no, text="".join(parts), entities=entities)


_ANSWERS_A = ["john smith", "robert hale", "edward grant", "alice brenner", "frank doyle"]
_DOSES = ["15 rem", "200 rad", "3.5 sv", "50 rem", "120 rad"]


def _ab_case(uid, kind, answer_text, answer_kind):
    """Build one a/b case. answer_kind ∈ {'person','dosage'}."""
    answer_norm = answer_text.lower()
    anchor1 = {"kind": "location", "text": f"Site-{uid}", "norm": f"site-{uid}"}
    anchor2 = {"kind": "org", "text": f"Ref-{uid}", "norm": f"ref-{uid}"}
    label = "(b)(6)" if answer_kind == "person" else "DELETED"
    ctx_before = f"Report from Site-{uid} dated Ref-{uid} concerning subject"
    ctx_after = f"filed under medical records at Site-{uid} again Ref-{uid}."

    pages: list[Page] = []
    docs: list[Doc] = []

    # Redacted document (doc_A) — always present, holds the redaction under test
    a_doc = f"{uid}_A"
    a_page = _segments_to_page(
        a_doc,
        1,
        [
            "Report from",
            anchor1,
            "dated",
            anchor2,
            "concerning subject [REDACTED] filed under medical records.",
        ],
    )
    a_page.redaction = {
        "kind": "deleted_text",
        "label": label,
        "context_before": ctx_before,
        "context_after": ctx_after,
    }
    pages.append(a_page)
    docs.append(Doc(a_doc, year=1957))

    def source_page(doc_id, ans_text, ans_kind):
        segs = [
            "Report from",
            anchor1,
            "dated",
            anchor2,
            "concerning subject",
            {"kind": ans_kind, "text": ans_text, "norm": ans_text.lower()},
        ]
        if ans_kind == "dosage":
            segs.append({"kind": "subject_ref", "text": "Subject 3", "norm": "subject 3"})
        segs.append("filed under medical records.")
        return _segments_to_page(doc_id, 1, segs)

    if kind == "positive":
        b_doc = f"{uid}_B"
        pages.append(source_page(b_doc, answer_text, answer_kind))
        docs.append(Doc(b_doc, year=1957))
        truth = {"answer_norm": answer_norm}
    elif kind == "negative_control":
        truth = {"answer_norm": None}  # no source anywhere
    else:  # hard_negative — a decoy of the same kind, true answer absent
        decoy = "robert decoy" if answer_kind == "person" else "999 rem"
        b_doc = f"{uid}_D"
        pages.append(source_page(b_doc, decoy, answer_kind))
        docs.append(Doc(b_doc, year=1957))
        truth = {"answer_norm": None}

    return Case(
        case_uid=uid,
        type_key=("type_b" if answer_kind == "dosage" else "type_a"),
        case_kind=kind,
        docs=docs,
        pages=pages,
        truth=truth,
    )


def gen_type_a_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            ans = _ANSWERS_A[i % len(_ANSWERS_A)]
            cases.append(_ab_case(f"a_{kind}_{i}", kind, ans, "person"))
    return cases


def gen_type_b_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            dose = _DOSES[i % len(_DOSES)]
            cases.append(_ab_case(f"b_{kind}_{i}", kind, dose, "dosage"))
    return cases


_ORGS = ["los alamos", "oak ridge", "hanford", "sandia", "argonne"]
_PERSONS_C = ["john smith", "robert hale", "edward grant", "alice brenner", "frank doyle"]


def _named_page(doc_id, person_norm, org_norm, year):
    page = _segments_to_page(
        doc_id,
        1,
        [
            {"kind": "person", "text": person_norm.title(), "norm": person_norm},
            "of",
            {"kind": "org", "text": org_norm.title(), "norm": org_norm},
            "examined; dose",
            {"kind": "dosage", "text": "15 rem", "norm": "15 rem"},
            "recorded.",
        ],
    )
    return page, Doc(doc_id, year=year)


def _c_case(uid, kind, org, year, person_true, person_decoy):
    s_doc = f"{uid}_S"
    s_page = _segments_to_page(
        s_doc,
        1,
        [
            "Examination of",
            {"kind": "subject_ref", "text": "Subject 3", "norm": "subject 3"},
            "at",
            {"kind": "org", "text": org.title(), "norm": org},
            "in",
            {"kind": "date", "text": str(year), "norm": str(year)},
            "received",
            {"kind": "dosage", "text": "15 rem", "norm": "15 rem"},
            "total.",
        ],
    )
    pages = [s_page]
    docs = [Doc(s_doc, year=year)]
    truth = {"true_named_norm": None}

    if kind in ("positive", "hard_negative"):
        pg, dc = _named_page(f"{uid}_P", person_true, org, year)
        pages.append(pg)
        docs.append(dc)
        truth = {"true_named_norm": person_true}
    if kind in ("hard_negative", "negative_control"):
        pg, dc = _named_page(f"{uid}_D", person_decoy, org, year)
        pages.append(pg)
        docs.append(dc)

    return Case(
        case_uid=uid, type_key="type_c", case_kind=kind, docs=docs, pages=pages, truth=truth
    )


def gen_type_c_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    gi = 0
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            org = _ORGS[i % len(_ORGS)]
            year = 1950 + 3 * gi  # unique per case, ≥3 apart → no cross-case linking
            person_true = _PERSONS_C[i % len(_PERSONS_C)]
            person_decoy = _PERSONS_C[(i + 1) % len(_PERSONS_C)]
            cases.append(_c_case(f"c_{kind}_{i}", kind, org, year, person_true, person_decoy))
            gi += 1
    return cases


def _d_case(uid: str, kind: str) -> Case:
    """Build one type_d (outcome-suppression gap) case.

    positive: an initiation doc (protocol_code + date) with no matching outcome
    doc — TypeDScorer must flag a gap. negative_control / hard_negative: add a
    second doc that records an outcome for the same protocol, so the detector
    must NOT flag a gap. The protocol_code norm is unique per case so the
    detector never links across cases.
    """
    pcode = f"CAL-{uid}"
    pnorm = pcode.lower()
    init_doc = f"{uid}_I"
    init_page = _segments_to_page(
        init_doc,
        1,
        [
            "Protocol",
            {"kind": "protocol_code", "text": pcode, "norm": pnorm},
            "initiated on",
            {"kind": "date", "text": "1960-01-01", "norm": "1960-01-01"},
            "at the facility.",
        ],
    )
    pages = [init_page]
    docs = [Doc(init_doc, year=1960)]
    truth = {"answer_norm": "gap"}  # a gap should be detected

    if kind in ("negative_control", "hard_negative"):
        out_doc = f"{uid}_O"
        out_page = _segments_to_page(
            out_doc,
            1,
            [
                "Protocol",
                {"kind": "protocol_code", "text": pcode, "norm": pnorm},
                "final",
                {
                    "kind": "outcome_ref",
                    "text": "mortality results",
                    "norm": "outcome_ind:mortality",
                },
                "reported.",
            ],
        )
        pages.append(out_page)
        docs.append(Doc(out_doc, year=1965))
        truth = {"answer_norm": None}  # outcome present → no gap

    return Case(
        case_uid=uid, type_key="type_d", case_kind=kind, docs=docs, pages=pages, truth=truth
    )


def gen_type_d_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            cases.append(_d_case(f"d_{kind}_{i}", kind))
    return cases
