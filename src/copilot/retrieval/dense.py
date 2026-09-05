"""
Dense retrieval using pgvector cosine similarity.

Embeds the query with BAAI/bge-small-en-v1.5 (local, no API key needed),
then runs KNN search against the HNSW index on text_chunks.embedding.
"""

from functools import lru_cache

from copilot.storage.db import get_conn

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# bge models use this prefix for queries (not passages) to improve retrieval quality
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer  # lazy: avoids loading PyTorch at import time
    return SentenceTransformer(EMBED_MODEL)


def embed_query(text: str) -> list[float]:
    vec = _model().encode(BGE_QUERY_PREFIX + text, normalize_embeddings=True)
    return vec.tolist()


def retrieve_dense(query: str, ticker: str | None = None, k: int = 5,
                   include_tables: bool = False,
                   fiscal_year: int | None = None) -> list[dict]:
    """
    Return top-k text chunks by cosine similarity to the query.
    Returns empty list if no chunks have embeddings yet.

    Financial-statement tables are excluded by default -- see retrieve_hybrid for
    why. Filtering happens in the WHERE clause so the k that comes back is k
    eligible chunks, not k minus however many were tables.

    `fiscal_year` restricts to one filing year, for the same reason and in the
    same place. Every company here has around ten filings and a quarter of the
    corpus is boilerplate repeated across them, so an unscoped top-five fills up
    with the same paragraph from different years.
    """
    query_vec = embed_query(query)

    where = ["embedding IS NOT NULL"]
    params: list = []
    if ticker:
        where.append("ticker = %s")
        params.append(ticker.upper())
    if not include_tables:
        where.append("COALESCE(chunk_type, 'text') <> 'table'")
    if fiscal_year:
        where.append("accn IN (SELECT accn FROM filings WHERE fiscal_year = %s)")
        params.append(fiscal_year)
    params += [query_vec, k]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, ticker, accn, section, text,
                       1 - (embedding <=> %s::vector) AS score
                FROM text_chunks
                WHERE {' AND '.join(where)}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, *params),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "id":       row["id"],
            "text":     row["text"],
            "ticker":   row["ticker"],
            "section":  row["section"],
            "score":    float(row["score"]),
            "citation": f"SEC filing accession {row['accn']} — {row['section']}",
            "accn":     row["accn"],
        }
        for row in rows
    ]
