# palimpsest/db.py
import sqlite3
import sys
from palimpsest.config import load

def connect(cfg):
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn

def migrate(cfg):
    conn = connect(cfg)
    with conn:
        conn.execute("""
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
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
          doc_id     TEXT NOT NULL REFERENCES documents(doc_id),
          page_no    INTEGER NOT NULL,
          width      REAL, height REAL,
          ocr_source TEXT,
          text       TEXT,
          PRIMARY KEY (doc_id, page_no)
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS redactions (
          redaction_id INTEGER PRIMARY KEY,
          doc_id   TEXT NOT NULL, page_no INTEGER NOT NULL,
          kind     TEXT NOT NULL,
          label    TEXT,
          x0 REAL, y0 REAL, x1 REAL, y1 REAL,
          context_before TEXT, context_after TEXT,
          FOREIGN KEY (doc_id, page_no) REFERENCES pages(doc_id, page_no)
        );""")
        conn.execute("""
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
        );""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm, kind);")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id INTEGER PRIMARY KEY,
          doc_id TEXT NOT NULL, page_no INTEGER NOT NULL,
          char_start INTEGER, char_end INTEGER,
          text TEXT NOT NULL
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS gap_candidates (
          gap_id        INTEGER PRIMARY KEY,
          redaction_id  INTEGER NOT NULL REFERENCES redactions(redaction_id),
          clear_entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
          score REAL NOT NULL,
          score_cosine REAL, score_anchor REAL, score_kind REAL,
          method TEXT NOT NULL,
          status TEXT DEFAULT 'candidate',
          reviewed_by TEXT, reviewed_at TEXT, notes TEXT,
          UNIQUE(redaction_id, clear_entity_id)
        );""")
        conn.execute("""
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
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS review_queue (
          review_id INTEGER PRIMARY KEY,
          entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
          reason TEXT,
          status TEXT DEFAULT 'pending',
          decided_by TEXT, decided_at TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
          version INTEGER PRIMARY KEY
        );""")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1);")
        # Performance indexes for hot query paths
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_page ON chunks(doc_id, page_no);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_doc_page ON entities(doc_id, page_no);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS gapjoin_runs (
          redaction_id INTEGER PRIMARY KEY REFERENCES redactions(redaction_id),
          run_at TEXT NOT NULL
        );""")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2);")

        # Schema v3 — Type-e regulatory violation support
        conn.execute("""
        CREATE TABLE IF NOT EXISTS regulation_citations (
          reg_id         INTEGER PRIMARY KEY,
          citation       TEXT NOT NULL UNIQUE,
          effective_date TEXT,
          text_snippet   TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS violation_candidates (
          vc_id               INTEGER PRIMARY KEY,
          doc_id              TEXT NOT NULL,
          page_no             INTEGER NOT NULL,
          reg_id              INTEGER NOT NULL REFERENCES regulation_citations(reg_id),
          reg_cite_entity_id  INTEGER REFERENCES entities(entity_id),
          doc_year            INTEGER,
          violation_type      TEXT NOT NULL,
          score               REAL NOT NULL,
          status              TEXT DEFAULT 'candidate',
          reviewed_by TEXT, reviewed_at TEXT, notes TEXT,
          UNIQUE(doc_id, page_no, reg_id)
        );""")
        # Seed canonical regulations (INSERT OR IGNORE — idempotent)
        REGS = [
            ("45 CFR 46",
             "1991-06-18",
             "No investigator may involve a human being as a subject in research unless the investigator "
             "has obtained the legally effective informed consent of the subject or the subject's legally "
             "authorized representative. An investigator shall seek such consent only under circumstances "
             "that provide the prospective subject sufficient opportunity to consider whether to participate."),
            ("45 CFR 219",
             "1992-01-14",
             "This policy applies to all research involving human subjects conducted, supported, or "
             "otherwise subject to regulation by the Department of Energy. Each institution engaged in "
             "research which is covered by this policy shall provide written assurance satisfactory to "
             "the Department that it will comply with the requirements set forth in this policy."),
            ("Belmont Report",
             "1979-04-18",
             "Respect for persons incorporates at least two ethical convictions: first, that individuals "
             "should be treated as autonomous agents, and second, that persons with diminished autonomy "
             "are entitled to protection. The principle of respect for persons thus divides into two "
             "separate moral requirements: the requirement to acknowledge autonomy and the requirement "
             "to protect those with diminished autonomy."),
            ("Declaration of Helsinki",
             "1964-06-01",
             "In medical research involving human subjects, the well-being of the individual research "
             "subject must take precedence over all other interests. It is the duty of the physician "
             "to promote and safeguard the health of patients. The physician's knowledge and conscience "
             "are dedicated to the fulfillment of this duty."),
            ("Nuremberg Code",
             "1947-08-20",
             "The voluntary consent of the human subject is absolutely essential. This means that the "
             "person involved should have legal capacity to give consent; should be so situated as to "
             "be able to exercise free power of choice, without the intervention of any element of "
             "force, fraud, deceit, duress, over-reaching, or other ulterior form of constraint or "
             "coercion."),
            ("National Research Act",
             "1974-07-12",
             "The Secretary shall establish a commission to be known as the National Commission for the "
             "Protection of Human Subjects of Biomedical and Behavioral Research. The Commission shall "
             "carry out a comprehensive investigation and study to identify the basic ethical principles "
             "which should underlie the conduct of biomedical and behavioral research involving human "
             "subjects."),
        ]
        for citation, eff_date, snippet in REGS:
            conn.execute(
                "INSERT OR IGNORE INTO regulation_citations (citation, effective_date, text_snippet) VALUES (?, ?, ?)",
                (citation, eff_date, snippet)
            )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (3);")

        # Schema v4 — Series Suppression (Type f) support
        conn.execute("""
        CREATE TABLE IF NOT EXISTS series_gap_candidates (
          gap_id            INTEGER PRIMARY KEY,
          series_prefix     TEXT NOT NULL,
          missing_number    INTEGER NOT NULL,
          missing_accession TEXT NOT NULL UNIQUE,
          flanking_doc_id   TEXT REFERENCES documents(doc_id),
          ref_entity_id     INTEGER REFERENCES entities(entity_id),
          score             REAL NOT NULL,
          status            TEXT DEFAULT 'candidate',
          reviewed_by       TEXT,
          reviewed_at       TEXT,
          notes             TEXT
        );""")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4);")

        # Schema v5 — Outcome Suppression Gap (Type d)
        conn.execute("""
CREATE TABLE IF NOT EXISTS outcome_gap_candidates (
  ogc_id              INTEGER PRIMARY KEY,
  protocol_code       TEXT NOT NULL,
  initiation_doc_id   TEXT NOT NULL REFERENCES documents(doc_id),
  start_year          INTEGER,
  future_ref_entity_id INTEGER REFERENCES entities(entity_id),
  score               REAL NOT NULL,
  status              TEXT DEFAULT 'candidate',
  reviewed_by         TEXT,
  reviewed_at         TEXT,
  notes               TEXT,
  UNIQUE(protocol_code, initiation_doc_id)
);""")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (5);")

        # Schema v6 — Anonymous Identity Linkage (Type c)
        conn.execute("""
CREATE TABLE IF NOT EXISTS identity_link_candidates (
  ilc_id              INTEGER PRIMARY KEY,
  subject_doc_id      TEXT NOT NULL REFERENCES documents(doc_id),
  subject_page        INTEGER NOT NULL,
  subject_ref         TEXT NOT NULL,          -- e.g. "Subject 3"
  named_doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  named_page          INTEGER NOT NULL,
  named_entity_id     INTEGER REFERENCES entities(entity_id),
  org_match           REAL NOT NULL DEFAULT 0.0,
  date_proximity      REAL NOT NULL DEFAULT 0.0,
  dosage_bonus        REAL NOT NULL DEFAULT 0.0,
  score               REAL NOT NULL,
  status              TEXT DEFAULT 'candidate',  -- candidate | verified | rejected
  reviewed_by         TEXT,
  reviewed_at         TEXT,
  notes               TEXT,
  created_at          TEXT DEFAULT (datetime('now')),
  UNIQUE(subject_doc_id, subject_page, subject_ref, named_entity_id)
);""")
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6);")

        # Schema v7 — Evaluation harness + trust gate (specs/EVAL-TRUST-GATE.md §5)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
          run_id          INTEGER PRIMARY KEY,
          started_at      TEXT NOT NULL,
          finished_at     TEXT,
          scorer_git_sha  TEXT,
          corpus_hash     TEXT,
          seed            INTEGER,
          config_snapshot TEXT,
          notes           TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_cases (
          case_id   INTEGER PRIMARY KEY,
          run_id    INTEGER NOT NULL REFERENCES eval_runs(run_id),
          type_key  TEXT NOT NULL,
          case_kind TEXT NOT NULL,
          spec      TEXT NOT NULL,
          truth     TEXT NOT NULL
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
          result_id        INTEGER PRIMARY KEY,
          run_id           INTEGER NOT NULL REFERENCES eval_runs(run_id),
          case_id          INTEGER NOT NULL REFERENCES eval_cases(case_id),
          type_key         TEXT NOT NULL,
          raw_score        REAL,
          score_components TEXT,
          predicted        TEXT,
          label            TEXT NOT NULL,
          confidence       REAL
        );""")
        for _table in ("gap_candidates", "identity_link_candidates"):
            for _col, _decl in (
                ("confidence", "REAL"),
                ("confidence_method", "TEXT"),
                ("gate_tier", "TEXT"),
            ):
                try:
                    conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_decl}")
                except sqlite3.OperationalError:
                    pass  # column already exists — migration is idempotent
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7);")

        # Deterministic FAISS routing: record which shard each chunk was indexed into
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN shard_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists — idempotent


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        cfg = load()
        migrate(cfg)
        conn = connect(cfg)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]
        print(f"Migrated tables: {', '.join(tables)}")
