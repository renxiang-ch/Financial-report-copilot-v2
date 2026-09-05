"""
BM25 retrieval over text_chunks stored in Postgres.

BM25Okapi implementation ported from:
  notebooks/stage2_bm25_hybrid.ipynb  (tested on FinDER benchmark)
  Results: BM25 Recall@5=0.29, MRR=0.222 — strong on financial term matching.

Index is built once at startup from the DB and held in memory.
969 chunks is small enough that this is instant.
"""

import math
from collections import Counter

import numpy as np

from copilot.storage.db import get_conn


class BM25Okapi:
    """
    BM25Okapi scoring from scratch.

    score(d, q) = Σ IDF(t) × TF_norm(t, d)
    IDF(t)      = log((N - df + 0.5) / (df + 0.5) + 1)
    TF_norm(t)  = f*(k1+1) / (f + k1*(1 - b + b*|d|/avgdl))
    k1=1.5, b=0.75 (standard hyperparameters)
    """

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b  = b
        self.corpus_size = len(corpus)
        self.avgdl = sum(len(doc) for doc in corpus) / max(self.corpus_size, 1)
        self.doc_freqs = [Counter(doc) for doc in corpus]
        self.doc_len   = [len(doc) for doc in corpus]

        df: dict[str, int] = {}
        for freq in self.doc_freqs:
            for term in freq:
                df[term] = df.get(term, 0) + 1

        self.idf = {
            term: math.log((self.corpus_size - n + 0.5) / (n + 0.5) + 1)
            for term, n in df.items()
        }

    def get_scores(self, query: list[str]) -> np.ndarray:
        scores = np.zeros(self.corpus_size)
        for term in query:
            if term not in self.idf:
                continue
            idf = self.idf[term]
            for i, freq in enumerate(self.doc_freqs):
                f = freq.get(term, 0)
                if f == 0:
                    continue
                dl = self.doc_len[i]
                tf = f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                scores[i] += idf * tf
        return scores


class ChunkIndex:
    """
    In-memory BM25 index built from text_chunks in Postgres.
    Supports optional per-ticker filtering.
    """

    def __init__(self):
        self._chunks: list[dict] = []       # full chunk rows from DB
        self._bm25:   BM25Okapi | None = None

    def build(self, ticker: str | None = None, include_tables: bool = False,
              fiscal_year: int | None = None) -> None:
        """Load chunks from DB and build BM25 index.

        Tables are excluded at build time rather than filtered out of the
        results, so the pool the fusion sees is `pool` eligible chunks rather
        than `pool` minus however many happened to be tables. It also makes the
        in-memory index a quarter smaller.

        `fiscal_year` is applied here for the same reason. Filtering a year out
        of the results afterwards would leave the pool full of chunks that were
        never eligible, which is the pool-depth mistake this project has already
        measured once: a quota back-filled by searching deeper scores rank-30 of
        83 as though it were a real candidate.
        """
        cols = "SELECT id, ticker, accn, section, chunk_index, text FROM text_chunks"
        where, params = [], []
        if ticker:
            where.append("ticker = %s"); params.append(ticker.upper())
        if not include_tables:
            where.append("COALESCE(chunk_type, 'text') <> 'table'")
        if fiscal_year:
            where.append("accn IN (SELECT accn FROM filings WHERE fiscal_year = %s)")
            params.append(fiscal_year)
        sql = cols + (" WHERE " + " AND ".join(where) if where else "")
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                self._chunks = [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

        tokenized = [row["text"].lower().split() for row in self._chunks]
        self._bm25 = BM25Okapi(tokenized)

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Return top-k chunks with BM25 scores."""
        if self._bm25 is None or not self._chunks:
            raise RuntimeError("Index not built. Call build() first.")

        tokens = query.lower().split()
        scores = self._bm25.get_scores(tokens)
        top_idx = scores.argsort()[::-1][:k]

        results = []
        for idx in top_idx:
            chunk = dict(self._chunks[idx])
            chunk["score"] = float(scores[idx])
            results.append(chunk)
        return results


# Module-level index cache, keyed by what the index actually contains. Keying on
# ticker alone would have served a table-free index to a caller that asked for
# tables, or the reverse, depending only on who called first.
_indexes: dict[tuple[str | None, bool, int | None], ChunkIndex] = {}


def _get_index(ticker: str | None, include_tables: bool = False,
               fiscal_year: int | None = None) -> ChunkIndex:
    key = (ticker.upper() if ticker else None, include_tables, fiscal_year)
    if key not in _indexes:
        idx = ChunkIndex()
        idx.build(ticker=key[0], include_tables=include_tables,
                  fiscal_year=fiscal_year)
        _indexes[key] = idx
    return _indexes[key]


def retrieve_text(query: str, ticker: str | None = None, k: int = 5,
                  include_tables: bool = False,
                  fiscal_year: int | None = None) -> list[dict]:
    """
    Search text chunks using BM25.

    Args:
        query:  natural language query
        ticker: optional — restrict to one company
        k:      number of results to return
        fiscal_year: optional — restrict to filings of one fiscal year

    Returns list of dicts with keys: id, text, ticker, accn, section, score, citation

    `id` is the text_chunks primary key. It is what identifies a chunk when this
    list is fused with another one -- see hybrid._rrf_merge. `accn` is returned
    alongside the rendered citation because callers that verify an answer against
    its sources need the bare accession, not a sentence containing it.
    """
    idx = _get_index(ticker, include_tables=include_tables,
                     fiscal_year=fiscal_year)
    results = idx.search(query, k=k)
    return [
        {
            "id":      r["id"],
            "text":    r["text"],
            "ticker":  r["ticker"],
            "section": r["section"],
            "accn":    r["accn"],
            "score":   r["score"],
            "citation": f"SEC filing accession {r['accn']} — {r['section']}",
        }
        for r in results
    ]
