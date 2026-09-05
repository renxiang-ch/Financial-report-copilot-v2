"""Deterministic A/B on the retrieval layer. No LLM, no cost.

The eval harness's retrieval metric runs through the agent and an LLM judge, and
its historical range on unchanged code is 25%-62.5%. A single run cannot resolve
a change smaller than that band. This probe measures the same underlying thing --
where the passage holding the golden key_phrase lands -- with the agent and the
judge removed, so the only thing that varies is the retrieval layer.

It reports rank, not a binary hit. At n=8 a hit@5 count moves only in steps of
12.5 percentage points, so it is blind to any change that reorders the list
without crossing the k boundary, which is most changes. Reciprocal rank moves
whenever the retrieval layer moves.

Three arms:
    v1_oldkey : truncated embeddings + the colliding RRF key   (state before)
    v1_newkey : truncated embeddings + identity RRF key        (isolates the key)
    v2_newkey : pooled embeddings + identity RRF key           (state after)

Requires the embedding_v1 backup column written before re-embedding.

Run:
    $env:PYTHONUTF8="1"; uv run --active python -m copilot.eval.probe_retrieval
"""

import json
import sys
from pathlib import Path

from copilot.retrieval.bm25 import retrieve_text as bm25_retrieve
from copilot.retrieval.dense import embed_query
from copilot.storage.db import get_conn

RRF_K = 60
POOL = 30      # candidates per retriever, deep enough that a rank usually exists
DEPTH = 20     # how far down the fused list a rank is still recorded
KS = (1, 3, 5, 10)

OLD_KEY = lambda r: f"{r.get('ticker','')}::{r.get('section','')}::{r['text'][:80]}"
NEW_KEY = lambda r: f"id:{r['id']}"


def dense(query: str, ticker: str | None, k: int, column: str) -> list[dict]:
    vec = embed_query(query)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # `embedding` carries an HNSW index and `embedding_v1` does not, so
            # left alone the arms would differ by approximate-vs-exact KNN as
            # well as by the change under test. Both are forced to a sequential
            # scan: at 16k rows it costs little, and it makes the comparison
            # about the vectors rather than about which column got an index.
            cur.execute("SET LOCAL enable_indexscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
            where = f"{column} IS NOT NULL" + (" AND ticker = %s" if ticker else "")
            params = [vec] + ([ticker.upper()] if ticker else []) + [vec, k]
            cur.execute(
                f"SELECT id, ticker, accn, section, text, "
                f"1 - ({column} <=> %s::vector) AS score "
                f"FROM text_chunks WHERE {where} "
                f"ORDER BY {column} <=> %s::vector LIMIT %s",
                tuple(params),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def merge(bm25_results, dense_results, k, keyfn):
    scores, meta = {}, {}
    for rank, r in enumerate(bm25_results, start=1):
        kk = keyfn(r)
        scores[kk] = scores.get(kk, 0.0) + 1.0 / (RRF_K + rank)
        meta[kk] = r
    for rank, r in enumerate(dense_results, start=1):
        kk = keyfn(r)
        scores[kk] = scores.get(kk, 0.0) + 1.0 / (RRF_K + rank)
        meta.setdefault(kk, r)
    return [meta[kk] for kk, _ in sorted(scores.items(), key=lambda x: -x[1])[:k]]


def golden_rank(results, phrase: str) -> int | None:
    """1-based rank of the first passage containing the phrase, or None."""
    needle = " ".join(phrase.lower().split())
    for i, r in enumerate(results, start=1):
        if needle in " ".join((r.get("text") or "").lower().split()):
            return i
    return None


def main() -> None:
    items = json.loads(Path("data/datasets/eval_set.json").read_text(encoding="utf-8"))["items"]
    probes = []
    for it in items:
        if it.get("type") != "retrieval" or it.get("retired"):
            continue
        for cite in it.get("golden_citations", []):
            if cite.get("key_phrase"):
                probes.append((it["id"], it["question"], it.get("ticker"), cite["key_phrase"]))
                break

    arms = {"v1_oldkey": ("embedding_v1", OLD_KEY),
            "v1_newkey": ("embedding_v1", NEW_KEY),
            "v2_newkey": ("embedding",    NEW_KEY)}
    ranks = {a: [] for a in arms}
    rows = []

    for qid, question, ticker, phrase in probes:
        bm = bm25_retrieve(question, ticker=ticker, k=POOL)
        row = {"id": qid}
        for arm, (column, keyfn) in arms.items():
            dn = dense(question, ticker, POOL, column)
            r = golden_rank(merge(bm, dn, DEPTH, keyfn), phrase)
            row[arm] = r
            ranks[arm].append(r)
        rows.append(row)
        print(f"{qid:40} " + "  ".join(
            f"{a}={('#' + str(row[a])) if row[a] else '--':>4}" for a in arms))

    n = len(probes)
    print(f"\n{n} retrieval questions, deterministic (no agent, no judge). "
          f"'#r' = rank of the golden passage, fused list depth {DEPTH}.")
    print("  " + "arm".ljust(11) + "".join(f"hit@{k}".rjust(8) for k in KS) + "MRR".rjust(8))
    summary = {}
    for arm in arms:
        rs = ranks[arm]
        hits = {k: sum(1 for r in rs if r and r <= k) for k in KS}
        mrr = sum(1.0 / r for r in rs if r) / n
        summary[arm] = {"hit_at": hits, "mrr": round(mrr, 4),
                        "ranks": rs, "found": sum(1 for r in rs if r)}
        print("  " + arm.ljust(11)
              + "".join(f"{hits[k]}/{n}".rjust(8) for k in KS)
              + f"{mrr:.3f}".rjust(8))

    out = Path("data/results/_retrieval_probe.json")
    out.write_text(json.dumps(
        {"n": n, "depth": DEPTH, "pool": POOL, "summary": summary, "per_question": rows},
        indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    sys.exit(main())
