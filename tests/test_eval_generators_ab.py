from palimpsest.eval.generators import gen_type_a_cases, gen_type_b_cases


def _check_offsets(case):
    for page in case.pages:
        for e in page.entities:
            assert page.text[e["char_start"]:e["char_end"]] == e["text"]


def test_type_a_case_mix_and_truth():
    cases = gen_type_a_cases(n_per_kind=3, seed=1)
    kinds = sorted({c.case_kind for c in cases})
    assert kinds == ["hard_negative", "negative_control", "positive"]
    assert len(cases) == 9

    for c in cases:
        _check_offsets(c)
        assert c.type_key == "type_a"
        red_pages = [p for p in c.pages if p.redaction]
        assert len(red_pages) == 1            # exactly one redaction under test


    for c in cases:
        answer = c.truth["answer_norm"]
        norms = {e["norm"] for p in c.pages for e in p.entities}
        if c.case_kind == "positive":
            assert answer is not None and answer in norms   # source present
        else:
            assert answer is None                            # no correct answer exists


def test_type_a_positive_anchor_in_context():
    cases = gen_type_a_cases(n_per_kind=1, seed=2)
    pos = next(c for c in cases if c.case_kind == "positive")
    red = next(p for p in pos.pages if p.redaction)
    ctx = (red.redaction["context_before"] + " " + red.redaction["context_after"]).lower()
    # the case's two unique anchor norms appear in the redaction context (drives anchor route)
    assert f"site-{pos.case_uid}".lower() in ctx
    assert f"ref-{pos.case_uid}".lower() in ctx


def test_type_b_answer_is_dosage():
    cases = gen_type_b_cases(n_per_kind=2, seed=3)
    assert len(cases) == 6
    pos = [c for c in cases if c.case_kind == "positive"]
    for c in pos:
        ans = c.truth["answer_norm"]
        dosage_norms = {e["norm"] for p in c.pages for e in p.entities if e["kind"] == "dosage"}
        assert ans in dosage_norms
    for c in cases:
        _check_offsets(c)
