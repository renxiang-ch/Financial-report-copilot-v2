"""
Generate and store local embeddings for all text_chunks.

Uses BAAI/bge-small-en-v1.5 (384 dims) via sentence-transformers.
No API key required -- model is downloaded once and cached locally (~130 MB).

Long chunks
-----------
The model truncates at 512 tokens. An earlier version passed every chunk
straight to `model.encode()`, so 19.4% of the corpus was embedded from its first
512 tokens only and 276,165 tokens (4.0% of all corpus tokens) were never seen by
the model at all -- silently, because truncation is not an error. A chunk whose
only relevant sentence sat past that boundary was unreachable by dense retrieval
no matter what was asked.

The fix here is window mean-pooling: split an over-long chunk into overlapping
512-token windows, embed each, and average the unit vectors back to one. The
overlap exists so a phrase straddling a boundary survives whole in at least one
window.

This is a correction, not a free win, and the trade-off is worth stating: the
mean of several windows is a blend, so a chunk with one relevant window out of
four now matches that query more weakly than a well-sized chunk would. Compared
with truncation it is still strictly better -- a diluted signal beats no signal --
but the real fix for very long chunks is to chunk smaller at ingestion. That
would invalidate the frozen eval set's chunk-level golden citations, so it is out
of scope here.

Usage:
    python -m copilot.pipeline.embed_chunks
    python -m copilot.pipeline.embed_chunks --ticker AAPL
    python -m copilot.pipeline.embed_chunks --batch-size 64
    python -m copilot.pipeline.embed_chunks --reembed      # redo rows that already have one
"""

import argparse

import numpy as np
from sentence_transformers import SentenceTransformer

from copilot.storage.db import get_conn
from copilot.storage.schema import migrate_add_embedding

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_BATCH = 64
# Tokens shared between neighbouring windows. Enough to keep a sentence intact
# across a boundary without materially inflating the number of windows.
WINDOW_OVERLAP = 64


def _windows(token_ids: list[int], size: int, overlap: int) -> list[tuple[list[int], int]]:
    """Split a token sequence into overlapping windows.

    Returns (window, novel) pairs, where `novel` counts the tokens this window
    contributes that no earlier window already covered. That count is the pooling
    weight, and getting it wrong is not cosmetic. Weighting by window LENGTH
    instead gives a chunk of 528 tokens two windows of 510 and 82, so an 82-token
    tail -- 64 of whose tokens are a duplicate of the previous window and only 18
    of which are new -- would carry 14% of the pooled vector. Weighting by window
    COUNT, as a plain mean does, would give it 50%. Measured: under a plain mean
    the chunks that lost 50 tokens or fewer moved further from their original
    vector (median cosine 0.0599) than the chunks that lost more than 500 did
    (0.0466) -- the ones with almost nothing to gain were perturbed the most.
    Weighting by novel tokens moves a chunk in proportion to what truncation was
    actually costing it.
    """
    if len(token_ids) <= size:
        return [(token_ids, len(token_ids))]
    stride = max(size - overlap, 1)
    out: list[tuple[list[int], int]] = []
    covered = 0
    for start in range(0, len(token_ids), stride):
        win = token_ids[start:start + size]
        end = start + len(win)
        out.append((win, max(end - covered, 1)))
        covered = end
        if end >= len(token_ids):
            break
    return out


def encode_passages(model: SentenceTransformer, texts: list[str]) -> list[list[float]]:
    """Embed passages, mean-pooling over windows for any that exceed the limit.

    Every window across the whole batch is encoded in one call, so a batch of
    long chunks costs no more round-trips than a batch of short ones.
    """
    tok = model.tokenizer
    # -2 leaves room for the [CLS]/[SEP] the encoder adds back per window.
    size = model.max_seq_length - 2

    pieces: list[str] = []
    weights: list[int] = []
    spans: list[tuple[int, int]] = []   # (start, end) into `pieces`, one per text
    for text in texts:
        ids = tok.encode(text, add_special_tokens=False)
        start = len(pieces)
        for win, novel in _windows(ids, size, WINDOW_OVERLAP):
            pieces.append(tok.decode(win, skip_special_tokens=True))
            weights.append(novel)
        spans.append((start, len(pieces)))

    vecs = model.encode(pieces, normalize_embeddings=True, show_progress_bar=False)

    out: list[list[float]] = []
    for start, end in spans:
        if end - start == 1:
            out.append(np.asarray(vecs[start]).tolist())
            continue
        pooled = np.average(np.asarray(vecs[start:end]), axis=0,
                            weights=weights[start:end])
        norm = np.linalg.norm(pooled)
        # Re-normalise: a weighted mean of unit vectors is not itself a unit
        # vector, and cosine distance in pgvector assumes one.
        out.append((pooled / norm if norm else pooled).tolist())
    return out


def fetch_batch(conn, ticker: str | None, batch_size: int,
                reembed: bool, after_id: int) -> list[dict]:
    """Next batch to embed. `after_id` keeps --reembed moving forward: without it
    a re-embed would re-select the same rows forever, since they never stop
    matching `embedding IS NOT NULL`."""
    where = ["id > %s"]
    params: list = [after_id]
    if not reembed:
        where.append("embedding IS NULL")
    if ticker:
        where.append("ticker = %s")
        params.append(ticker.upper())
    params.append(batch_size)
    joined = " AND ".join(where)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, text FROM text_chunks WHERE {joined} ORDER BY id LIMIT %s",
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def store_embeddings(conn, rows: list[dict], embeddings) -> None:
    with conn.cursor() as cur:
        for row, vec in zip(rows, embeddings):
            cur.execute(
                "UPDATE text_chunks SET embedding = %s WHERE id = %s",
                (vec, row["id"]),
            )
    conn.commit()


def count_pending(conn, ticker: str | None, reembed: bool) -> int:
    where = [] if reembed else ["embedding IS NULL"]
    params: list = []
    if ticker:
        where.append("ticker = %s")
        params.append(ticker.upper())
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM text_chunks {clause}", tuple(params))
        return cur.fetchone()["count"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=None, help="Restrict to one ticker")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--reembed", action="store_true",
                        help="Recompute rows that already have an embedding")
    parser.add_argument("--only-pooled", action="store_true",
                        help="With --reembed, skip rows short enough to need no "
                             "pooling: their vector does not depend on the "
                             "pooling rule, so recomputing them only burns time")
    args = parser.parse_args()

    print(f"Loading model {EMBED_MODEL} (downloads ~130 MB on first run)...")
    model = SentenceTransformer(EMBED_MODEL)
    print(f"max_seq_length={model.max_seq_length}, window overlap={WINDOW_OVERLAP}")

    conn = get_conn()
    migrate_add_embedding(conn)

    total = count_pending(conn, args.ticker, args.reembed)
    print(f"Chunks to embed: {total}")
    if total == 0:
        print("Nothing to do.")
        return

    limit = model.max_seq_length - 2
    processed, after_id, pooled = 0, 0, 0
    while True:
        rows = fetch_batch(conn, args.ticker, args.batch_size, args.reembed, after_id)
        if not rows:
            break
        after_id = rows[-1]["id"]

        if args.only_pooled:
            rows = [r for r in rows
                    if len(model.tokenizer.encode(r["text"], add_special_tokens=False)) > limit]
            if not rows:
                continue
        texts = [r["text"] for r in rows]
        pooled += sum(
            1 for t in texts
            if len(model.tokenizer.encode(t, add_special_tokens=False)) > limit
        )
        store_embeddings(conn, rows, encode_passages(model, texts))

        processed += len(rows)
        print(f"  {processed}/{total} embedded ({pooled} window-pooled)...", flush=True)

    conn.close()
    print(f"Done. {processed} chunks embedded with {EMBED_MODEL}; "
          f"{pooled} needed window mean-pooling.")


if __name__ == "__main__":
    main()
