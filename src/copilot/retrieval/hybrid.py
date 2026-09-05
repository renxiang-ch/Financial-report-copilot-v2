"""
Hybrid retrieval: BM25 + dense with Reciprocal Rank Fusion (RRF).

RRF formula: score(d) = Σ 1 / (k + rank_i)   where k=60 (standard constant)

Why RRF instead of score interpolation:
- Score distributions from BM25 and cosine similarity are not directly comparable.
- RRF is rank-based so it needs no normalisation and is robust to outliers.
- Falls back gracefully if embeddings are not yet generated (dense returns empty).
"""

from copilot.retrieval.bm25 import retrieve_text as bm25_retrieve
from copilot.retrieval.dense import retrieve_dense

RRF_K = 60


def _rrf_merge(
    bm25_results: list[dict],
    dense_results: list[dict],
    k: int,
) -> list[dict]:
    """
    Merge two ranked lists with RRF and return the top-k unique chunks.

    Chunks are identified by their text_chunks primary key. An earlier version
    keyed on ticker::section::text[:80], which is not an identity: 22.4% of the
    corpus shares such a key with at least one other chunk, and one key covered
    57 distinct chunks -- consecutive rows of the same Item 8 table, whose first
    80 characters are identical markdown pipes. Both directions of that were
    wrong. Two different chunks landing on one key had their reciprocal ranks
    SUMMED, scoring as though a single document had been found by both
    retrievers; and only one of them survived into the output, so the rest were
    dropped without a trace.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}

    def key(r: dict) -> str:
        # Fall back to the full text, never a prefix: a fallback that can collide
        # would silently reintroduce the bug it exists to avoid.
        cid = r.get("id")
        return f"id:{cid}" if cid is not None else f"txt:{r.get('ticker','')}::{r.get('text','')}"

    for rank, result in enumerate(bm25_results, start=1):
        k_ = key(result)
        scores[k_] = scores.get(k_, 0.0) + 1.0 / (RRF_K + rank)
        meta[k_] = result

    for rank, result in enumerate(dense_results, start=1):
        k_ = key(result)
        scores[k_] = scores.get(k_, 0.0) + 1.0 / (RRF_K + rank)
        if k_ not in meta:
            meta[k_] = result

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
    return [meta[k_] for k_, _ in ranked]


def retrieve_hybrid(query: str, ticker: str | None = None, k: int = 5,
                    include_tables: bool = False,
                    fiscal_year: int | None = None) -> list[dict]:
    """
    Search text chunks with BM25 + dense retrieval fused via RRF.

    Fetches 2*k candidates from each method so the merge pool is large enough.
    Gracefully falls back to BM25-only if dense retrieval has no embeddings yet.

    Financial-statement tables are excluded by default. Three measurements led to
    that, not taste:

    * Every chunk too long for the embedder is a table -- 990 of them, up to 1,500
      tokens against a 512-token limit, so two thirds of a large table was never
      embedded. No text chunk exceeds 502.
    * Excluding them costs nothing measurable: identical ranks on all eight
      retrieval eval questions, MRR 0.347 either way. Tables are 26.6% of the
      corpus but only 9% of retrieved slots, and no golden passage lives in one.
    * `get_text()` destroys column alignment, so a retrieved table reads as
      `| $ | 34.0 | — |` with nothing to say which column or year a figure belongs
      to. Reading a number out of that is exactly what query_financials exists to
      prevent.

    The cost is real and stated rather than glossed: metrics outside the ten XBRL
    labels -- stock compensation, lease commitments, tax reconciliation, debt
    maturities -- lose their only retrieval path and become "cannot determine".
    Under this project's honest-refusal principle that is an improvement over
    reading a mangled table, but it is a trade, not a free win.

    `include_tables=True` restores the old behaviour: a WHERE clause, never a
    DELETE, so the decision stays reversible and measurable.
    """
    pool = k * 2

    bm25_results = bm25_retrieve(query, ticker=ticker, k=pool,
                                 include_tables=include_tables,
                                 fiscal_year=fiscal_year)
    dense_results = retrieve_dense(query, ticker=ticker, k=pool,
                                   include_tables=include_tables,
                                   fiscal_year=fiscal_year)

    if not dense_results:
        # Embeddings not yet generated — BM25 only
        return bm25_results[:k]

    return _rrf_merge(bm25_results, dense_results, k=k)
