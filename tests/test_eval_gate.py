from palimpsest.eval.gate import confidence_and_tier, apply_gate

ARTIFACT = {
    "types": {
        "type_a": {"threshold": 0.80, "isotonic": [[0.5, 0.2], [0.8, 0.9], [1.0, 0.98]]},
        "type_c": {"threshold": None, "isotonic": [[0.6, 0.3], [0.8, 0.5]]},
    }
}


def test_confidence_and_tier_above_threshold():
    conf, tier = confidence_and_tier("type_a", 0.9, ARTIFACT)
    assert tier == "surfaceable"
    assert 0.0 <= conf <= 1.0


def test_below_threshold_is_tentative():
    _, tier = confidence_and_tier("type_a", 0.7, ARTIFACT)
    assert tier == "tentative"


def test_disabled_gate_is_tentative():
    _, tier = confidence_and_tier("type_c", 0.99, ARTIFACT)
    assert tier == "tentative"          # threshold is null → never surfaceable


def test_unknown_type_is_tentative():
    conf, tier = confidence_and_tier("type_x", 0.99, ARTIFACT)
    assert (conf, tier) == (None, "tentative")


def test_apply_gate_enforce_drops_tentative():
    rows = [
        {"score": 0.9, "type_key": "type_a", "text": "PERSON-0042"},
        {"score": 0.7, "type_key": "type_a", "text": "PERSON-0043"},
    ]
    out = apply_gate(rows, ARTIFACT, min_tier="surfaceable", enforcement="enforce")
    assert len(out) == 1
    assert out[0]["gate_tier"] == "surfaceable"
    assert out[0]["text"] == "PERSON-0042"          # masking untouched


def test_apply_gate_annotate_keeps_all():
    rows = [{"score": 0.7, "type_key": "type_a"}]
    out = apply_gate(rows, ARTIFACT, enforcement="annotate")
    assert len(out) == 1
    assert out[0]["gate_tier"] == "tentative"
    assert "confidence" in out[0]
