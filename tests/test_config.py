from palimpsest.config import load

def test_load_config(tmp_path):
    config_content = """
    [storage]
    root = "{tmp_dir}"
    [db]
    path = "{storage.root}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 900
    heartbeat_seconds = 120
    max_attempts = 3
    [mcp]
    port = 8078
    [harvest]
    base_url = "https://www.osti.gov/opennet"
    rate_limit_rps = 1.0
    backoff_initial_s = 5
    backoff_max_s = 300
    user_agent = "test-agent"
    accession_prefix = "NV"
    [ocr]
    engine_preference = ["vision"]
    min_confidence = 0.5
    rerun_if_osti_text_shorter_than = 200
    [features]
    redaction_context_chars = 300
    redaction_context_lines = 2
    blackbox_min_area_frac = 0.001
    blackbox_max_area_frac = 0.25
    blackbox_darkness_threshold = 60
    [embed]
    model = "nomic-embed"
    dim = 768
    chunk_chars = 800
    chunk_overlap = 150
    [gapjoin]
    score_threshold = 0.65
    w_cosine = 0.5
    w_anchor = 0.3
    w_kind = 0.2
    topk_embedding_candidates = 50
    [models]
    extract = "llama"
    classify = "qwen"
    keep_alive = "24h"
    [nodes]
    gonktop = []
    """.replace("{tmp_dir}", str(tmp_path))
    
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    
    cfg = load(cfg_file)
    assert cfg.storage_root == tmp_path
    assert cfg.db_path == tmp_path / "db" / "palimpsest.db"
