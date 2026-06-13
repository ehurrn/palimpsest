from palimpsest.eval.generators import gen_type_c_cases
from palimpsest.eval.oracle import grade


def _check_offsets(case):
    for page in case.pages:
        for e in page.entities:
            assert page.text[e["char_start"]:e["char_end"]] == e["text"]


def test_type_c_mix_and_structure():
    cases = gen_type_c_cases(n_per_kind=3, seed=1)
    assert len(cases) == 9
    assert sorted({c.case_kind for c in cases}) == ["hard_negative", "negative_control", "positive"]

    for c in cases:
        _check_offsets(c)
        assert c.type_key == "type_c"
        subj_pages = [p for p in c.pages for e in p.entities if e["kind"] == "subject_ref"]
        assert len(subj_pages) == 1
        subj_page = subj_pages[0]
        kinds = {e["kind"] for e in subj_page.entities}
        assert "org" in kinds and "date" in kinds  # subject qualifies for the scorer


def test_type_c_truth_per_kind():
    cases = gen_type_c_cases(n_per_kind=2, seed=2)
    for c in cases:
        person_norms = {e["norm"] for p in c.pages for e in p.entities if e["kind"] == "person"}
        if c.case_kind == "positive":
            assert c.truth["true_named_norm"] in person_norms
            assert len(person_norms) == 1                 # only the true person
        elif c.case_kind == "hard_negative":
            assert c.truth["true_named_norm"] in person_norms
            assert len(person_norms) == 2                 # true + decoy
        else:  # negative_control
            assert c.truth["true_named_norm"] is None
            assert len(person_norms) == 1                 # decoy only


def test_grade_reused_for_c():
    # the decoy case: true + decoy both linked → one TP, one FP
    out = grade("john smith", [(0.8, "john smith"), (0.8, "carl reed")])
    assert sorted(r.label for r in out) == ["FP", "TP"]
