"""Is content past the 512-token boundary reachable? Deterministic, no LLM.

The frozen eval set cannot answer this. Its eight retrieval questions all have
golden passages of 528 tokens or fewer -- barely over the limit, losing at most
18 tokens -- so truncation was never costing them anything and fixing it cannot
move their scores. Absence of an effect there is absence of a test, not absence
of an effect.

This probe tests the thing directly. For each long chunk it takes a phrase that
occurs ONLY past the truncation boundary, uses it as the query, and asks where
that chunk ranks. Under the truncated vectors the model never saw those tokens,
so a hit can only come from the rest of the chunk resembling the query. Under
pooled vectors the window containing them was embedded.

Dense only, deliberately. BM25 reads the full text either way, so including it
would mask exactly the effect under test.

Two honest caveats:
  - A verbatim phrase is an easier query than a real question, so the absolute
    numbers overstate what a user would see. The comparison between arms is still
    valid: both arms face the identical query set.
  - Both arms are forced to exact KNN, because `embedding` has an HNSW index and
    `embedding_v1` does not.

Run:
    $env:PYTHONUTF8="1"; uv run --active python -m copilot.eval.probe_truncation
"""

import json
import sys
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

from copilot.storage.db import get_conn

MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MIN_TOKENS = 700      # enough content past the boundary to sample a real phrase
PHRASE_TOKENS = 40
DEPTH = 20
KS = (1, 5, 10)
SAMPLE = 150


def rank_of(conn, chunk_id: int, qvec, column: str, depth: int) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute(
            f"SELECT id FROM text_chunks WHERE {column} IS NOT NULL "
            f"ORDER BY {column} <=> %s::vector LIMIT %s",
            (qvec, depth),
        )
        for i, row in enumerate(cur.fetchall(), start=1):
            if row["id"] == chunk_id:
                return i
    return None


def main() -> None:
    model = SentenceTransformer(MODEL)
    tok = model.tokenizer
    limit = model.max_seq_length - 2

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id, text FROM text_chunks WHERE embedding IS NOT NULL ORDER BY id")
        rows = cur.fetchall()

    rng = np.random.default_rng(0)
    long_rows = []
    for r in rows:
        ids = tok.encode(r["text"], add_special_tokens=False)
        if len(ids) >= MIN_TOKENS:
            long_rows.append((r["id"], ids))
    print(f"{len(long_rows)} chunks with >= {MIN_TOKENS} tokens; sampling {SAMPLE}")
    pick = rng.choice(len(long_rows), size=min(SAMPLE, len(long_rows)), replace=False)

    queries, targets = [], []
    for i in pick:
        cid, ids = long_rows[i]
        # A phrase starting comfortably past the boundary, so the truncated
        # vector provably never saw it.
        start = limit + 60
        phrase = tok.decode(ids[start:start + PHRASE_TOKENS], skip_special_tokens=True)
        if len(phrase.split()) < 8:
            continue
        queries.append(QUERY_PREFIX + phrase)
        targets.append(cid)

    print(f"{len(queries)} usable queries; encoding")
    qvecs = model.encode(queries, normalize_embeddings=True, show_progress_bar=False)

    arms = {"v1_truncated": "embedding_v1", "v2_pooled": "embedding"}
    ranks = {a: [] for a in arms}
    for qvec, cid in zip(qvecs, targets):
        v = qvec.tolist()
        for arm, column in arms.items():
            ranks[arm].append(rank_of(conn, cid, v, column, DEPTH))
    conn.close()

    n = len(queries)
    print(f"\nCan the chunk be found from a phrase that lives only past token {limit}?")
    print("  " + "arm".ljust(14) + "".join(f"hit@{k}".rjust(9) for k in KS) + "MRR".rjust(9))
    summary = {}
    for arm in arms:
        rs = ranks[arm]
        hits = {k: sum(1 for r in rs if r and r <= k) for k in KS}
        mrr = sum(1.0 / r for r in rs if r) / n
        summary[arm] = {"hit_at": hits, "mrr": round(mrr, 4), "found": sum(1 for r in rs if r)}
        print("  " + arm.ljust(14)
              + "".join(f"{hits[k]}/{n}".rjust(9) for k in KS) + f"{mrr:.3f}".rjust(9))

    gained = sum(1 for a, b in zip(ranks["v1_truncated"], ranks["v2_pooled"])
                 if (b is not None) and (a is None or b < a))
    lost = sum(1 for a, b in zip(ranks["v1_truncated"], ranks["v2_pooled"])
               if (a is not None) and (b is None or b > a))
    print(f"\n  improved rank: {gained}/{n}   worsened: {lost}/{n}   "
          f"unchanged: {n - gained - lost}/{n}")

    out = Path("data/results/_truncation_probe.json")
    out.write_text(json.dumps({"n": n, "min_tokens": MIN_TOKENS, "depth": DEPTH,
                               "summary": summary, "improved": gained, "worsened": lost},
                              indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    sys.exit(main())
