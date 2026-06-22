import sqlite3

from palimpsest.config import Config


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # Phase 1.5 throughput tuning. Under WAL, synchronous=NORMAL is crash-safe
    # (at worst loses the last transaction on OS crash, never corrupts); temp
    # tables/indexes are built in RAM; mmap maps up to ~30GB for read-heavy access.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=30000000000")
    conn.row_factory = sqlite3.Row
    return conn


def migrate(cfg: Config) -> None:
    conn = connect(cfg)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        # schema_version holds a single row. Guard the seed insert so repeated
        # migrate() calls cannot accumulate duplicate version rows; all reads use
        # MAX(version) to stay correct even on DBs that already have duplicates.
        if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")

        # DDL from 00-ARCHITECTURE.md §5
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
              doc_id        TEXT PRIMARY KEY,
              accession     TEXT,
              title         TEXT,
              year          INTEGER,
              has_fulltext  INTEGER DEFAULT 0,
              source_url    TEXT,
              local_path    TEXT,
              sha256        TEXT,
              page_count    INTEGER,
              status        TEXT DEFAULT 'cataloged',
              fetched_at    TEXT, ocr_at TEXT, features_at TEXT, indexed_at TEXT,
              error         TEXT
            );

            CREATE TABLE IF NOT EXISTS pages (
              doc_id     TEXT NOT NULL REFERENCES documents(doc_id),
              page_no    INTEGER NOT NULL,
              width      REAL, height REAL,
              ocr_source TEXT,
              text       TEXT,
              PRIMARY KEY (doc_id, page_no)
            );

            CREATE TABLE IF NOT EXISTS redactions (
              redaction_id INTEGER PRIMARY KEY,
              doc_id   TEXT NOT NULL, page_no INTEGER NOT NULL,
              kind     TEXT NOT NULL,
              label    TEXT,
              x0 REAL, y0 REAL, x1 REAL, y1 REAL,
              context_before TEXT, context_after TEXT,
              char_capacity INTEGER,
              FOREIGN KEY (doc_id, page_no) REFERENCES pages(doc_id, page_no)
            );

            CREATE TABLE IF NOT EXISTS entities (
              entity_id INTEGER PRIMARY KEY,
              doc_id   TEXT NOT NULL, page_no INTEGER NOT NULL,
              kind     TEXT NOT NULL,
              text     TEXT NOT NULL,
              norm     TEXT NOT NULL,
              char_start INTEGER, char_end INTEGER,
              x0 REAL, y0 REAL, x1 REAL, y1 REAL,
              living_status TEXT DEFAULT 'unknown',
              FOREIGN KEY (doc_id, page_no) REFERENCES pages(doc_id, page_no)
            );
            CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm, kind);

            CREATE TABLE IF NOT EXISTS chunks (
              chunk_id INTEGER PRIMARY KEY,
              doc_id TEXT NOT NULL, page_no INTEGER NOT NULL,
              char_start INTEGER, char_end INTEGER,
              text TEXT NOT NULL,
              shard_id TEXT
            );

            CREATE TABLE IF NOT EXISTS gapjoin_runs (
              id INTEGER PRIMARY KEY,
              redaction_id INTEGER NOT NULL REFERENCES redactions(redaction_id),
              run_at TEXT
            );

            CREATE TABLE IF NOT EXISTS regulation_citations (
              reg_id INTEGER PRIMARY KEY,
              citation TEXT,
              effective_date TEXT
            );

            CREATE TABLE IF NOT EXISTS gap_candidates (
              gap_id        INTEGER PRIMARY KEY,
              redaction_id  INTEGER NOT NULL REFERENCES redactions(redaction_id),
              clear_entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
              score REAL NOT NULL,
              score_cosine REAL, score_anchor REAL, score_kind REAL,
              method TEXT NOT NULL,
              status TEXT DEFAULT 'candidate',
              reviewed_by TEXT, reviewed_at TEXT, notes TEXT,
              UNIQUE (redaction_id, clear_entity_id)
            );

            CREATE TABLE IF NOT EXISTS jobs (
              job_id    INTEGER PRIMARY KEY,
              type      TEXT NOT NULL,
              doc_id    TEXT NOT NULL,
              payload   TEXT DEFAULT '{}',
              state     TEXT DEFAULT 'pending',
              attempts  INTEGER DEFAULT 0,
              priority  INTEGER DEFAULT 5,
              lease_owner TEXT, lease_expires_at TEXT,
              created_at TEXT, updated_at TEXT, error TEXT,
              UNIQUE (type, doc_id)
            );

            CREATE TABLE IF NOT EXISTS review_queue (
              review_id INTEGER PRIMARY KEY,
              entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
              reason TEXT,
              status TEXT DEFAULT 'pending',
              decided_by TEXT, decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS series_gap_candidates (
              id INTEGER PRIMARY KEY,
              series_prefix TEXT,
              missing_number INTEGER,
              missing_accession TEXT UNIQUE,
              flanking_doc_id TEXT,
              ref_entity_id INTEGER,
              score REAL,
              status TEXT DEFAULT 'candidate'
            );

            CREATE TABLE IF NOT EXISTS identity_link_candidates (
              ilc_id INTEGER PRIMARY KEY,
              subject_doc_id TEXT,
              subject_page INTEGER,
              subject_ref TEXT,
              named_doc_id TEXT,
              named_page INTEGER,
              named_entity_id INTEGER,
              org_match REAL,
              date_proximity REAL,
              dosage_bonus REAL,
              score REAL,
              status TEXT DEFAULT 'candidate',
              reviewed_by TEXT, reviewed_at TEXT, notes TEXT
            );

            CREATE TABLE IF NOT EXISTS outcome_gap_candidates (
              id INTEGER PRIMARY KEY,
              protocol_code TEXT,
              initiation_doc_id TEXT,
              start_year INTEGER,
              future_ref_entity_id INTEGER,
              score REAL
            );

            CREATE TABLE IF NOT EXISTS violation_candidates (
              id INTEGER PRIMARY KEY,
              doc_id TEXT,
              page_no INTEGER,
              reg_id INTEGER,
              reg_cite_entity_id INTEGER,
              doc_year INTEGER,
              violation_type TEXT,
              score REAL,
              status TEXT DEFAULT 'candidate'
            );

            -- Hot-path indexes. The broker polls jobs on (state, type, priority)
            -- every /lease, and the orchestrator/harvest_stats scan documents on
            -- (status, has_fulltext); without these both full-scan every tick.
            CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(state, type, priority);
            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status, has_fulltext);

            -- Phase 1.5: speed up redaction/chunk joins keyed by doc_id and the
            -- reaper's sweep over expired leases (state, lease_expires_at).
            CREATE INDEX IF NOT EXISTS idx_redactions_doc ON redactions(doc_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_reaper ON jobs(state, lease_expires_at);
        """)
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]

        if version < 2:
            try:
                conn.execute("ALTER TABLE redactions ADD COLUMN char_capacity INTEGER")
            except sqlite3.OperationalError:
                pass
            conn.execute("UPDATE schema_version SET version = 2")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version < 3:
            _add_missing_tables = [
                """CREATE TABLE IF NOT EXISTS gapjoin_runs (
                  id INTEGER PRIMARY KEY,
                  redaction_id INTEGER NOT NULL REFERENCES redactions(redaction_id),
                  run_at TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS regulation_citations (
                  reg_id INTEGER PRIMARY KEY,
                  citation TEXT,
                  effective_date TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS series_gap_candidates (
                  id INTEGER PRIMARY KEY,
                  series_prefix TEXT,
                  missing_number INTEGER,
                  missing_accession TEXT UNIQUE,
                  flanking_doc_id TEXT,
                  ref_entity_id INTEGER,
                  score REAL,
                  status TEXT DEFAULT 'candidate'
                )""",
                """CREATE TABLE IF NOT EXISTS identity_link_candidates (
                  ilc_id INTEGER PRIMARY KEY,
                  subject_doc_id TEXT,
                  subject_page INTEGER,
                  subject_ref TEXT,
                  named_doc_id TEXT,
                  named_page INTEGER,
                  named_entity_id INTEGER,
                  org_match REAL,
                  date_proximity REAL,
                  dosage_bonus REAL,
                  score REAL,
                  status TEXT DEFAULT 'candidate',
                  reviewed_by TEXT, reviewed_at TEXT, notes TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS outcome_gap_candidates (
                  id INTEGER PRIMARY KEY,
                  protocol_code TEXT,
                  initiation_doc_id TEXT,
                  start_year INTEGER,
                  future_ref_entity_id INTEGER,
                  score REAL
                )""",
                """CREATE TABLE IF NOT EXISTS violation_candidates (
                  id INTEGER PRIMARY KEY,
                  doc_id TEXT,
                  page_no INTEGER,
                  reg_id INTEGER,
                  reg_cite_entity_id INTEGER,
                  doc_year INTEGER,
                  violation_type TEXT,
                  score REAL,
                  status TEXT DEFAULT 'candidate'
                )""",
            ]
            for stmt in _add_missing_tables:
                conn.execute(stmt)
            try:
                conn.execute("ALTER TABLE chunks ADD COLUMN shard_id TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("UPDATE schema_version SET version = 3")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version < 4:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS briefs (
                  doc_id          TEXT PRIMARY KEY REFERENCES documents(doc_id),
                  model           TEXT,
                  doc_type        TEXT,
                  summary         TEXT,
                  claims_json     TEXT,
                  events_json     TEXT,
                  redactions_json TEXT,
                  flags_json      TEXT,
                  interest_score  REAL,
                  novelty_score   REAL,
                  created_at      TEXT
                )
            """)
            conn.execute("UPDATE schema_version SET version = 4")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version < 5:
            # Eval-harness tables (specs/EVAL-*). The schema is shared via
            # migrate(); disposable run data lives in an isolated eval DB.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eval_runs (
                  run_id          INTEGER PRIMARY KEY,
                  started_at      TEXT,
                  finished_at     TEXT,
                  scorer_git_sha  TEXT,
                  corpus_hash     TEXT,
                  seed            INTEGER,
                  config_snapshot TEXT,
                  notes           TEXT
                );
                CREATE TABLE IF NOT EXISTS eval_cases (
                  case_id   INTEGER PRIMARY KEY,
                  run_id    INTEGER,
                  type_key  TEXT,
                  case_kind TEXT,
                  spec      TEXT,
                  truth     TEXT
                );
                CREATE TABLE IF NOT EXISTS eval_results (
                  result_id        INTEGER PRIMARY KEY,
                  run_id           INTEGER,
                  case_id          INTEGER,
                  type_key         TEXT,
                  raw_score        REAL,
                  score_components TEXT,
                  predicted        TEXT,
                  label            TEXT,
                  confidence       REAL
                );
            """)
            conn.execute("UPDATE schema_version SET version = 5")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version < 6:
            # Trust-gate annotations (specs/EVAL-TRUST-GATE.md §4.2) on the two
            # surfaced candidate tables. Wrapped in try/except for re-run safety.
            for _tbl in ("gap_candidates", "identity_link_candidates"):
                for _col in ("confidence REAL", "confidence_method TEXT", "gate_tier TEXT"):
                    try:
                        conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col}")
                    except sqlite3.OperationalError:
                        pass
            conn.execute("UPDATE schema_version SET version = 6")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version < 7:
            # Type-e needs the regulation snippet; type-d needs a uniqueness key so
            # re-running the outcome-gap scorer (INSERT OR IGNORE) is idempotent.
            try:
                conn.execute("ALTER TABLE regulation_citations ADD COLUMN text_snippet TEXT")
            except sqlite3.OperationalError:
                pass
            # Drop any pre-existing duplicates so the unique index can be built.
            conn.execute(
                "DELETE FROM outcome_gap_candidates WHERE id NOT IN "
                "(SELECT MIN(id) FROM outcome_gap_candidates "
                "GROUP BY protocol_code, initiation_doc_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_gap_unique "
                "ON outcome_gap_candidates(protocol_code, initiation_doc_id)"
            )
            # Idempotency key for TypeEScorer (one violation per reg_cite entity).
            conn.execute(
                "DELETE FROM violation_candidates WHERE id NOT IN "
                "(SELECT MIN(id) FROM violation_candidates GROUP BY reg_cite_entity_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_violation_unique "
                "ON violation_candidates(reg_cite_entity_id)"
            )
            # Seed canonical research-ethics regulations. TypeEScorer matches
            # reg_cite entities against these; 45 CFR 46 is pinned to reg_id 1.
            conn.executemany(
                "INSERT OR IGNORE INTO regulation_citations "
                "(reg_id, citation, effective_date, text_snippet) VALUES (?, ?, ?, ?)",
                [
                    (
                        1,
                        "45 CFR 46",
                        "1991-06-18",
                        "Federal Policy for the Protection of Human Subjects (Common Rule)",
                    ),
                    (
                        2,
                        "Belmont Report",
                        "1979-04-18",
                        "Ethical principles for the protection of human subjects of research",
                    ),
                    (
                        3,
                        "Common Rule",
                        "1991-06-18",
                        "45 CFR 46 Subpart A — basic human-subject protections",
                    ),
                    (
                        4,
                        "Declaration of Helsinki",
                        "1964-06-01",
                        "WMA ethical principles for medical research involving human subjects",
                    ),
                    (
                        5,
                        "Nuremberg Code",
                        "1947-08-19",
                        "Permissible medical experiments — voluntary informed consent",
                    ),
                    (
                        6,
                        "National Research Act",
                        "1974-07-12",
                        "Pub. L. 93-348 — established the IRB system",
                    ),
                ],
            )
            conn.execute("UPDATE schema_version SET version = 7")
    conn.close()


if __name__ == "__main__":
    from palimpsest.config import load

    cfg = load()
    migrate(cfg)
    conn = connect(cfg)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"Tables: {', '.join(sorted(tables))}")
    conn.close()
