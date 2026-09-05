"""Database schema definitions and table creation."""

CREATE_TABLES_SQL = """
-- pgvector extension (required for dense retrieval)
CREATE EXTENSION IF NOT EXISTS vector;

-- Companies in our cluster
CREATE TABLE IF NOT EXISTS companies (
    ticker      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cik         TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- XBRL financial facts (one row per metric/period)
CREATE TABLE IF NOT EXISTS financial_facts (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL REFERENCES companies(ticker),
    tag         TEXT NOT NULL,   -- XBRL tag e.g. RevenueFromContractWithCustomerExcludingAssessedTax
    label       TEXT NOT NULL,   -- human label e.g. Revenue
    value       NUMERIC NOT NULL,
    unit        TEXT NOT NULL,   -- USD, shares, etc.
    period_start DATE,           -- start date of the period (NULL for instant facts)
    period_end  DATE NOT NULL,   -- end date of the period
    fiscal_year INT,
    fiscal_period TEXT,          -- FY, Q1, Q2, Q3
    form        TEXT,            -- 10-K, 10-Q
    accn        TEXT NOT NULL,   -- accession number (source citation)
    UNIQUE (ticker, tag, period_end, accn)
);

CREATE INDEX IF NOT EXISTS idx_facts_ticker_tag ON financial_facts (ticker, tag);
CREATE INDEX IF NOT EXISTS idx_facts_ticker_fy  ON financial_facts (ticker, fiscal_year);

-- 10-K filing metadata
CREATE TABLE IF NOT EXISTS filings (
    accn        TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL REFERENCES companies(ticker),
    form        TEXT NOT NULL,
    filed_date  DATE NOT NULL,
    fiscal_year INT,
    doc_url     TEXT NOT NULL
);

-- Text chunks from 10-K body (MD&A, Risk Factors, Business)
CREATE TABLE IF NOT EXISTS text_chunks (
    id          BIGSERIAL PRIMARY KEY,
    accn        TEXT NOT NULL REFERENCES filings(accn),
    ticker      TEXT NOT NULL,
    section     TEXT,                -- e.g. "Risk Factors", "MD&A"
    chunk_index INT NOT NULL,
    text        TEXT NOT NULL,
    token_count INT,
    embedding   vector(384),        -- text-embedding-3-small; NULL until embed_chunks runs
    UNIQUE (accn, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_ticker ON text_chunks (ticker);
CREATE INDEX IF NOT EXISTS idx_chunks_accn   ON text_chunks (accn);
-- HNSW index for fast approximate nearest-neighbour search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON text_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Supply-chain edges extracted from 10-K customer concentration disclosures (Stage 2)
CREATE TABLE IF NOT EXISTS supply_edges (
    id                  SERIAL PRIMARY KEY,
    supplier_ticker     TEXT NOT NULL REFERENCES companies(ticker),
    customer_ticker     TEXT NOT NULL,
    revenue_pct         FLOAT,               -- % of supplier revenue from this customer
    fiscal_year         INT,
    disclosure_status   TEXT DEFAULT 'named', -- 'named' | 'inferred' | 'unnamed'
    accn                TEXT,                -- SEC filing accession (citation)
    chunk_id            INT REFERENCES text_chunks(id),
    source_text         TEXT,                -- verbatim disclosure sentence(s) from 10-K
    threshold_only      BOOLEAN DEFAULT FALSE, -- true = text said ">10%" only, exact % not stated
    extracted_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (supplier_ticker, customer_ticker, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_edges_supplier ON supply_edges (supplier_ticker);
CREATE INDEX IF NOT EXISTS idx_edges_customer ON supply_edges (customer_ticker);
CREATE INDEX IF NOT EXISTS idx_edges_fy       ON supply_edges (fiscal_year);
"""

# Idempotent migration for databases created before pgvector was added.
MIGRATE_ADD_EMBEDDING_SQL = """
ALTER TABLE text_chunks ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON text_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""

MIGRATE_ADD_THRESHOLD_ONLY_SQL = """
ALTER TABLE supply_edges ADD COLUMN IF NOT EXISTS threshold_only BOOLEAN DEFAULT FALSE;
"""

MIGRATE_ADD_CHUNK_TYPE_SQL = """
ALTER TABLE text_chunks ADD COLUMN IF NOT EXISTS chunk_type TEXT DEFAULT 'text';
"""

MIGRATE_ADD_PERIOD_START_SQL = """
ALTER TABLE financial_facts ADD COLUMN IF NOT EXISTS period_start DATE;
"""

# The pre-pooling embeddings, kept so the window-pooling change can be rolled
# back with one UPDATE and so probe_retrieval can run its arms against a frozen
# baseline. Declared here rather than created by hand in a psql session: a
# column that only exists on one machine is a result nobody else can reproduce.
MIGRATE_ADD_EMBEDDING_V1_SQL = """
ALTER TABLE text_chunks ADD COLUMN IF NOT EXISTS embedding_v1 vector(384);
"""


def create_tables(conn) -> None:
    """Build the schema, then bring it up to date. Safe to re-run.

    The migrations run here as well as from the pipelines that introduced them,
    because a column added only by a migration is a column a FRESH database does
    not have -- and two of these are read on paths that have nothing to do with
    the pipeline that added them:

        chunk_type    bm25.py and dense.py both filter on it by default, so a
                      database built from the DDL alone answers every
                      retrieve_text call with "column does not exist". It was
                      added by ingest_item8, which a reproduction following the
                      README never runs.
        period_start  read by the fact-selection fix.

    CREATE TABLE IF NOT EXISTS does not alter an existing table, so putting the
    columns in the DDL would fix new databases and silently skip every existing
    one. Calling the migrations covers both.
    """
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLES_SQL)
    conn.commit()
    migrate_add_embedding(conn)
    migrate_add_threshold_only(conn)
    migrate_add_chunk_type(conn)
    migrate_add_period_start(conn)
    migrate_add_embedding_v1(conn)


def migrate_add_embedding(conn) -> None:
    """Add embedding column + HNSW index to existing databases. Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(MIGRATE_ADD_EMBEDDING_SQL)
    conn.commit()


def migrate_add_threshold_only(conn) -> None:
    """Add threshold_only column to supply_edges. Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(MIGRATE_ADD_THRESHOLD_ONLY_SQL)
    conn.commit()


def migrate_add_chunk_type(conn) -> None:
    """Add chunk_type column to text_chunks. Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(MIGRATE_ADD_CHUNK_TYPE_SQL)
    conn.commit()


def migrate_add_embedding_v1(conn) -> None:
    """Add the pre-pooling embedding backup column. Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(MIGRATE_ADD_EMBEDDING_V1_SQL)
    conn.commit()


def migrate_add_period_start(conn) -> None:
    """Add period_start column to financial_facts (needed to validate duration
    facts are actually annual, not a quarterly slice under an annual-looking
    tag -- see ingest_financial_facts.py's module docstring for the full
    duration-mismatch defect this column exists to catch). Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(MIGRATE_ADD_PERIOD_START_SQL)
    conn.commit()
