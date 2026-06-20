import sqlite3

from palimpsest.config import Config


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def migrate(cfg: Config) -> None:
    conn = connect(cfg)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (0)")

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
        """)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]

        if version < 2:
            try:
                conn.execute("ALTER TABLE redactions ADD COLUMN char_capacity INTEGER")
            except sqlite3.OperationalError:
                pass
            conn.execute("UPDATE schema_version SET version = 2")

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
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

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
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
    conn.close()

if __name__ == "__main__":
    from palimpsest.config import load
    cfg = load()
    migrate(cfg)
    conn = connect(cfg)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"Tables: {', '.join(sorted(tables))}")
    conn.close()
